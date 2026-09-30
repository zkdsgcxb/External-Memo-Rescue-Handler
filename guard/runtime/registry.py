"""Stable enrollment policy shared by the silent background entry point.

Registrations describe a disk, never a Linux device instance. Root protection
belongs to the initramfs owner; only filesystem records can become new runtime
profiles here. This module performs no persistent or block-device writes.
"""
from copy import deepcopy
import re
import time

from admission import Admission, digest, readonly
from data_guard import check_environment, check_isolation, check_map, validate_config
from data_recovery import FilesystemRecovery
from dm_monitor import DeviceMapper


INSTANCE_FIELDS = frozenset({'initial_node', 'initial_sys_path', 'initial_diskseq'})
COMMON_FIELDS = frozenset({
    'schema', 'profile', 'map_name', 'map_uuid', 'run_dir', 'identity_path',
    'kernel_release', 'queue_seconds', 'partition_sectors', 'partition_start',
    'logical_block_size', 'layout', 'layout_version',
})


def _positive_integer(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(label + ' must be a positive integer')


def _root_identity(identity, config):
    """Root records identify the existing boot owner, not a start request."""
    if (config.get('profile') != 'host' or config.get('map_name') != 'ram-rescue-path'
            or not re.fullmatch(r'RAMRESCUE-HOST-[A-Za-z0-9_-]{1,64}',
                                str(config.get('map_uuid', '')))
            or config.get('run_dir') != '/run/ram-rescue-guard/state'
            or config.get('identity_path') != '/etc/rescue/identity.json'):
        raise ValueError('Root registration must identify the existing boot owner')
    for key in ('pv_uuid', 'vg_uuid', 'vg_name', 'partuuid', 'usb_serial'):
        if not isinstance(identity.get(key), str) or not identity[key].strip():
            raise ValueError('Root identity needs ' + key)
    lvs = identity.get('lvs')
    if not isinstance(lvs, dict) or config.get('root_lv') not in lvs:
        raise ValueError('Root LV must belong to the registered LVM identity')
    if (not isinstance(config.get('layout'), list) or not config['layout']
            or any(not isinstance(row, dict) or row.get('segtype') != 'linear'
                   for row in config['layout'])):
        raise ValueError('Root registration requires its enrolled linear LVM layout')
    if not isinstance(config.get('root_fs_uuid'), str) or not config['root_fs_uuid']:
        raise ValueError('Root registration requires its filesystem UUID')
    if type(config.get('queue_seconds')) is not int or not 2 <= config['queue_seconds'] <= 60:
        raise ValueError('Invalid root admission budget')


def validate_record(record):
    """Validate a persistent record without probing a disk or changing state."""
    if (not isinstance(record, dict) or type(record.get('schema')) is not int
            or record['schema'] != 1 or set(record) != {'schema', 'identity', 'guard'}):
        raise ValueError('Unsupported registry record')
    identity, config = record['identity'], record['guard']
    if not isinstance(identity, dict) or not isinstance(config, dict):
        raise ValueError('Registry record requires identity and guard objects')
    if INSTANCE_FIELDS.intersection(config):
        raise ValueError('Persistent registration cannot replay a device instance')
    if type(config.get('schema')) is not int or config['schema'] != 1:
        raise ValueError('Unsupported guard configuration schema')
    kind = identity.get('kind', 'lvm')
    allowed = COMMON_FIELDS | ({'root_lv', 'root_fs_uuid'} if kind == 'lvm' else set())
    if set(config) - allowed:
        raise ValueError('Unexpected runtime fields in persistent registration')
    if not isinstance(config.get('kernel_release'), str) or not re.fullmatch(
            r'[A-Za-z0-9._+-]{1,128}', config['kernel_release']):
        raise ValueError('Invalid enrolled kernel release')
    for key in ('partition_sectors', 'logical_block_size'):
        _positive_integer(config.get(key), key)
    for key in ('sectors', 'partition_number'):
        _positive_integer(identity.get(key), key)
    size = config['logical_block_size']
    if size < 512 or size & (size - 1):
        raise ValueError('Logical block size must be a power of two at least 512')
    start = config.get('partition_start', 0)
    if type(start) is not int or start < 0 or start + config['partition_sectors'] > identity['sectors']:
        raise ValueError('Enrolled partition geometry exceeds the disk')
    if kind == 'filesystem':
        if config.get('profile') != 'host-data':
            raise ValueError('Filesystem identity requires a filesystem guard backend')
        FilesystemRecovery(identity)
        validate_config(config)
        expected = {key: identity[key] for key in ('kind', 'fs_type', 'fs_uuid', 'partuuid')}
        if config.get('layout') != expected:
            raise ValueError('Filesystem layout differs from registered identity')
    elif kind == 'lvm':
        _root_identity(identity, config)
    else:
        raise ValueError('Unsupported registered identity kind')
    if 'layout_version' in config and config['layout_version'] != digest(config['layout']):
        raise ValueError('Registered layout version differs from its content')
    return record


def record_from_profile(profile):
    """Retain enrollment policy while discarding boot-local observations."""
    if (not isinstance(profile, dict) or type(profile.get('schema')) is not int
            or profile['schema'] != 1
            or not isinstance(profile.get('identity'), dict)
            or not isinstance(profile.get('guard'), dict)):
        raise ValueError('Unsupported enrollment profile')
    record = {key: deepcopy(profile[key]) for key in ('schema', 'identity', 'guard')}
    for key in INSTANCE_FIELDS:
        record['guard'].pop(key, None)
    return validate_record(record)


def current_profile(record, runner=readonly):
    """Reattest an existing data map; never create it or start a root owner.

    Admission holds a fresh fd and checks the registered partition geometry and
    content. Existing map and mount isolation checks remain shared with manual
    enrollment. The caller owns the process fence and supplies its fenced runner
    so child probes cannot outlive that ownership unnoticed.
    """
    validate_record(record)
    if record['identity'].get('kind', 'lvm') != 'filesystem':
        raise ValueError('Root protection is already owned by the protected boot')
    profile = deepcopy(record)
    identity, config = profile['identity'], profile['guard']
    recovery = FilesystemRecovery(identity, runner=runner)
    check_environment(config)
    mapper = DeviceMapper()
    if mapper.target_version('multipath') < (1, 15, 0):
        raise RuntimeError('DM_MPATH_PROBE_PATHS requires multipath target >= 1.15.0')
    node = recovery.candidate_node()
    sys_path, map_sys = check_map(config, node, mapper)
    check_isolation(node, sys_path, map_sys, identity, runner=runner)
    with Admission(config, recovery).verify(time.monotonic() + 15, 'registered-start') as candidate:
        candidate.revalidate('registered-start')
        check_map(config, candidate.node, mapper)
        config.update(initial_node=candidate.node, initial_sys_path=candidate.sys_path,
                      initial_diskseq=candidate.diskseq)
    return profile
