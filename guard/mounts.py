#!/usr/bin/env python3
"""Render explicit protected mount consumers using native systemd units.

The Guard owns block identity and recovery; systemd owns mount dependencies.
This module neither probes media nor mounts, repairs or remounts a filesystem.
Existing root mounts remain owned by the boot configuration.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
sys.path.insert(0, str(BASE / 'runtime'))
from registry import validate_record


OPTIONS = frozenset({'rw', 'ro', 'nosuid', 'nodev', 'noexec', 'noatime',
                     'relatime', 'strictatime', 'nodiratime'})
RESERVED = ('/dev', '/proc', '/sys', '/run', '/etc', '/usr', '/boot')


def clean_path(value):
    """Restrict configuration paths instead of interpreting unit-file syntax."""
    if (not isinstance(value, str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
            or str(PurePosixPath(value)) != value
            or any(part in ('.', '..') for part in value.split('/'))):
        raise ValueError('Expected a normalized absolute path without unit syntax')
    return value


def unit_name(path, suffix='mount'):
    """Escape the intentionally restricted ASCII paths as systemd does."""
    clean_path(path)
    value = path.strip('/').replace('-', r'\x2d').replace('/', '-')
    if value.startswith('.'):
        value = r'\x2e' + value[1:]
    result = value + '.' + suffix
    if len(result) > 255:
        raise ValueError('Mount path exceeds the systemd unit name limit')
    return result


def beneath(path, parent):
    return path == parent or path.startswith(parent + '/')


def controller(record):
    config = record['guard']
    return ('ram-rescue-guard.service' if config['profile'] == 'host' else
            'ram-rescue-maintain@' + config['map_name'] + '.service')


def source(entry, records):
    """Derive the device path from enrollment, never from a Linux disk name."""
    if entry.get('map') not in records:
        raise ValueError('Mount source is not an enrolled stable mapping')
    record = records[entry['map']]
    identity, config = record['identity'], record['guard']
    if identity.get('kind', 'lvm') == 'filesystem':
        if 'lv' in entry or 'type' in entry:
            raise ValueError('Filesystem type and UUID come from the enrolled identity')
        uuid, fs_type = config['map_uuid'], identity['fs_type']
    else:
        lv = identity['lvs'].get(entry.get('lv'))
        if not lv or entry.get('type') != 'ext4':
            raise ValueError('LVM mount needs an enrolled LV and explicit ext4 type')
        if entry['lv'] == config['root_lv']:
            raise ValueError('Root filesystem remains mounted by the boot configuration')
        uuid, fs_type = lv['dm_uuid'], 'ext4'
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,127}', uuid):
        raise ValueError('Unsupported enrolled DM UUID for a stable device path')
    return '/dev/disk/by-id/dm-uuid-' + uuid, fs_type, controller(record)


def validate_plan(plan, records):
    if (not isinstance(plan, dict) or set(plan) != {'schema', 'mounts'}
            or type(plan['schema']) is not int or plan['schema'] != 1
            or not isinstance(plan['mounts'], list) or not 1 <= len(plan['mounts']) <= 64):
        raise ValueError('Expected a version 1 mount plan with 1 to 64 consumers')
    for name, record in records.items():
        validate_record(record)
        if name != record['guard']['map_name']:
            raise ValueError('Enrollment key differs from its stable map name')
    destinations = set()
    for entry in plan['mounts']:
        if not isinstance(entry, dict):
            raise ValueError('Mount consumer must be an object')
        bind = 'bind' in entry
        allowed = {'where', 'bind'} if bind else {'where', 'map', 'lv', 'type', 'options', 'automount'}
        if set(entry) - allowed:
            raise ValueError('Unknown mount consumer fields')
        where = clean_path(entry.get('where'))
        if where == '/' or any(beneath(where, path) for path in RESERVED):
            raise ValueError('Cannot replace a boot or runtime system mount')
        unit_name(where)
        if where in destinations:
            raise ValueError('Duplicate mount destination')
        destinations.add(where)
        if bind:
            clean_path(entry['bind'])
        else:
            source(entry, records)
            if type(entry.get('automount', False)) is not bool:
                raise ValueError('automount must be a boolean')
            options = entry.get('options', ['rw', 'nosuid', 'nodev'])
            if (not isinstance(options, list) or not options
                    or any(not isinstance(option, str) or option not in OPTIONS for option in options)
                    or len(options) != len(set(options))
                    or {'rw', 'ro'} <= set(options)):
                raise ValueError('Invalid mount options; remount and filesystem repair are not supported')
    entries = {entry['where']: entry for entry in plan['mounts']}
    edges = {}
    for where, entry in entries.items():
        # Both nesting and bind-source dependencies participate in cycle checks.
        dependencies = {parent for parent in entries if parent != where and beneath(where, parent)}
        if 'bind' in entry:
            owners = [parent for parent, value in entries.items()
                      if 'bind' not in value and beneath(entry['bind'], parent)]
            if not owners:
                raise ValueError('Bind source must belong to a declared protected filesystem')
            owner = max(owners, key=len)
            if beneath(entry['bind'], where):
                raise ValueError('Bind source cannot be underneath its destination')
            dependencies.add(owner)
        if entry.get('automount') and any(entries[parent].get('automount') for parent in dependencies):
            raise ValueError('Nested automounts are not supported')
        edges[where] = dependencies
    def visit(where, active, complete):
        if where in active:
            raise ValueError('Mount dependency cycle')
        if where not in complete:
            for dependency in edges[where]:
                visit(dependency, active | {where}, complete)
            complete.add(where)
    complete = set()
    for where in entries:
        visit(where, set(), complete)
    return plan


def render_units(plan, records):
    validate_plan(plan, records)
    units = {}
    for entry in plan['mounts']:
        where = entry['where']
        common = ('[Unit]\nDescription=Protected mount consumer ' + where + '\n'
                  'DefaultDependencies=no\nBefore=umount.target\nConflicts=umount.target\n')
        if 'bind' in entry:
            what, fs_type, options = entry['bind'], 'none', 'bind'
            dependencies = 'RequiresMountsFor=' + what + '\n'
        else:
            what, fs_type, owner = source(entry, records)
            device = unit_name(what, 'device')
            dependencies = f'Requires={owner}\nAfter={owner} {device}\nBindsTo={device}\n'
            if records[entry['map']]['guard']['profile'] == 'host':
                # A skipped Condition on the boot owner is not a failed
                # Requires dependency. Assert the protected boot explicitly.
                dependencies += 'AssertKernelCommandLine=ram_rescue_guard=1\n'
            options = ','.join(entry.get('options', ['rw', 'nosuid', 'nodev']))
        unit = (common + dependencies + '\n[Mount]\n' +
                f'What={what}\nWhere={where}\nType={fs_type}\nOptions={options}\n'
                'TimeoutSec=30\nDirectoryMode=0700\n')
        if entry.get('automount'):
            # An idle timeout would discard mount identities/open-fd continuity.
            units[unit_name(where, 'automount')] = (
                common + '\n[Automount]\n' + f'Where={where}\n' +
                'TimeoutIdleSec=0\nDirectoryMode=0700\n\n[Install]\nWantedBy=multi-user.target\n')
        else:
            unit += '\n[Install]\nWantedBy=multi-user.target\n'
        units[unit_name(where)] = unit
    return units


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--record', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = {}
    for path in args.record:
        record = json.loads(path.read_text())
        name = record['guard']['map_name']
        if name in records:
            parser.error('Duplicate enrollment for ' + name)
        records[name] = record
    units = render_units(json.loads(args.plan.read_text()), records)
    # A fresh output directory makes this a reviewable build artifact. Installing
    # it is a separate native systemd operation; no active mount is changed here.
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name, text in units.items():
        (args.output / name).write_text(text)
    print(json.dumps({'units': sorted(units), 'output': str(args.output)}, indent=2))


if __name__ == '__main__':
    main()
