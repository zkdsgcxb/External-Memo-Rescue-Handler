"""Explicit data-device lifecycle for the public beta; no package-install actions.

The native controller owns recovery and safe stop. This cold management module
owns configuration and initial maps. Incomplete actions retain their receipts.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
sys.path[:0] = [str(BASE), str(BASE.parent / 'ram-rescue-demo/src')]

import data
import manage
import release_support
from admin.admission import Admission, digest, identity_layout
from admin.data import usb_identity, check_environment, check_isolation
from admin.dm import DeviceMapper, expected_table, table_digest
from admin.identity import FilesystemIdentity
from admin.registry import record_from_profile, validate_record
from host_files import atomic
from trusted_paths import open_directory, read_trusted_json
from version import VERSION

CONFIG = Path('/etc/ram-rescue-handler/devices')
STATE = Path('/var/lib/ram-rescue-handler/devices')
BOOT = Path('/etc/systemd/system/ram-rescue-devices.service')
BOOT_TEXT = ('[Unit]\nDescription=Start explicitly enabled USB data protection\n'
             'After=systemd-udev-settle.service\nWants=systemd-udev-settle.service\n'
             '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
             'ExecStart=/usr/bin/rescue-guard-admin device boot\nTimeoutStartSec=120\n'
             '[Install]\nWantedBy=multi-user.target\n')


def directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(open_directory(path))


def write(path, value):
    directory(path.parent)
    atomic(path, (json.dumps(value, sort_keys=True, indent=2) + '\n').encode())


def config_path(name):
    return CONFIG / (data.map_name(name) + '.json')


def state_path(name):
    return STATE / (data.map_name(name) + '.json')


def load(name):
    value = read_trusted_json(config_path(name))
    if (value.get('schema') != 1 or type(value.get('enabled')) is not bool
            or value.get('record', {}).get('guard', {}).get('map_name') != name):
        raise RuntimeError('Invalid device configuration')
    validate_record(value['record'])
    if value['record']['identity']['fs_type'] != 'ext4':
        raise RuntimeError('The public lifecycle supports ext4 only')
    return value


def journal(name, phase, **values):
    write(state_path(name), {'schema': 1, 'phase': phase,
        'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), **values})


def map_entry(name):
    found = [item for item in manage.maps().values() if item['name'] == name]
    if len(found) > 1:
        raise RuntimeError('Ambiguous mapping')
    return found[0] if found else None


def preview(device, name):
    data.map_name(name)
    accepted = release_support.require_supported()
    node, path, identity = usb_identity(device)
    if identity['fs_type'] != 'ext4':
        raise RuntimeError('The public beta accepts healthy ext4 data partitions only')
    recovery = FilesystemIdentity(identity)
    if DeviceMapper().target_version('multipath') < (1, 15, 0):
        raise RuntimeError('The kernel lacks DM_MPATH_PROBE_PATHS (multipath >= 1.15.0)')
    existing = map_entry(name)
    if existing:
        profile = data.collect(name, node)
        record = record_from_profile(profile)
        if identity != record['identity']:
            raise RuntimeError('Selected partition differs from the existing map')
    else:
        cfg = {'schema': 1, 'profile': 'host-data', 'map_name': name,
               'map_uuid': 'RAMRESCUE-DATA-' + name.removeprefix('rr-data-'),
               'kernel_release': os.uname().release, 'queue_seconds': 8,
               'run_dir': '/run/ram-rescue-data/' + name + '/state',
               'identity_path': '/run/ram-rescue-data/' + name + '/identity.json',
               'partition_sectors': int((path / 'size').read_text()),
               'partition_start': int((path / 'start').read_text()),
               'logical_block_size': int((path.parent / 'queue/logical_block_size').read_text()),
               'layout': identity_layout(recovery, node)}
        record = record_from_profile({'schema': 1, 'identity': identity, 'guard': cfg})
        check_environment(cfg)
        check_isolation(node, path, None, identity)
        with Admission(cfg, recovery).verify(time.monotonic() + 15) as held:
            held.revalidate()
    # Plans bind this boot and instance; they are not replayable after replug.
    instance = {'node': node, 'sys_path': str(path), 'diskseq': (path.parent / 'diskseq').read_text().strip()}
    result = {'schema': 1, 'record': record, 'instance': instance,
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'release_subject': accepted, 'existing_map': bool(existing),
              'effects': ['save_disabled_configuration'], 'enabled': False}
    result['plan_sha256'] = digest(result)
    return result


def confirmation(expected, actual, action):
    if expected is None:
        if not sys.stdin.isatty():
            raise RuntimeError('Use --expect-plan with the displayed plan SHA256')
        print(action + '；输入完整摘要确认：' + actual, file=sys.stderr)
        expected = input().strip()
    if expected != actual:
        raise RuntimeError('Plan changed or confirmation does not match; review it again')


@manage.exclusive_control
def enroll(device, name, expected):
    plan = preview(device, name)
    confirmation(expected, plan['plan_sha256'], '保存停用配置，不启用保护')
    if os.path.lexists(config_path(name)):
        raise RuntimeError('Device name already has configuration')
    if os.path.lexists(state_path(name)):
        old = read_trusted_json(state_path(name))
        if old.get('phase') != 'removed' or old.get('boot_id') == plan['boot_id']:
            raise RuntimeError('An incomplete receipt or same-boot removed name must not be reused')
    if (manage.REGISTRY / (name + '.json')).exists() or (data.STATE / name).exists():
        raise RuntimeError('An existing owner must be retired before using the new lifecycle')
    journal(name, 'enrolling', record_sha256=digest(plan['record']))
    write(config_path(name), {'schema': 1, 'enabled': False, 'record': plan['record']})
    journal(name, 'disabled', record_sha256=digest(plan['record']))
    return {'map': name, 'state': 'disabled', 'reboot_activates': False}


def current_node(record):
    recovery = FilesystemIdentity(record['identity'])
    node = recovery.verify()
    with Admission(record['guard'], recovery).verify(time.monotonic() + 15) as held:
        held.revalidate()
        if held.node != node:
            raise RuntimeError('Device changed during admission')
    return node


def require_idle(record, node, item):
    path = (Path('/sys/class/block') / Path(node).name).resolve(strict=True)
    check_isolation(node, path, item['sys'].resolve() if item else None, record['identity'])
    if item:
        snapshot = DeviceMapper().snapshot(record['guard']['map_name'])
        info = snapshot['info']
        dev = (item['sys'] / 'dev').read_text().strip()
        from admin.data import mounts
        if (snapshot['uuid'] != record['guard']['map_uuid'] or info['open_count'] != 0
                or snapshot['inactive'] or any(info[k] for k in ('suspended', 'internal_suspend', 'deferred_remove', 'read_only'))
                or any(row[2] == dev for row in mounts())):
            raise RuntimeError('Mapping is busy, changed, or not idle')
        raw_dev = os.stat(node).st_rdev
        if table_digest(snapshot['active']) != table_digest(expected_table(
                record['guard']['partition_sectors'], f'{os.major(raw_dev)}:{os.minor(raw_dev)}')):
            raise RuntimeError('Mapping table changed independently')


def install_boot():
    if os.path.lexists(BOOT):
        if BOOT.is_symlink() or BOOT.read_text() != BOOT_TEXT:
            raise RuntimeError('Boot integration changed independently')
    else:
        atomic(BOOT, BOOT_TEXT.encode(), mode=0o644)
    data.run(['systemctl', 'daemon-reload'])
    data.run(['systemctl', 'enable', BOOT.name])


def start(name, value):
    record = value['record']
    cfg = record['guard']
    release_support.require_supported()
    check_environment(cfg)
    node = current_node(record)
    item = map_entry(name)
    unit = data.service(name)
    active = data.run(['systemctl', 'show', '--property=ActiveState', '--value', unit])
    runtime_record = data.STATE / name / 'record.json'
    if active == 'active':
        if read_trusted_json(runtime_record) != record:
            raise RuntimeError('Running owner differs from saved configuration')
        return {'map': name, 'state': 'active', 'already_active': True}
    if active not in ('inactive', 'failed'):
        raise RuntimeError('Controller transition is still in progress')
    require_idle(record, node, item)
    native_journal = Path(cfg['run_dir']) / 'path-transaction.json'
    rearm = native_journal.exists()
    if rearm and (not item or read_trusted_json(native_journal).get('phase') != 'safe_stopped'):
        raise RuntimeError('Previous recovery is incomplete; inspect device status and diagnostics')
    data.require_ram()
    data.stage_runtime()
    journal(name, 'enabling', record_sha256=digest(record))
    rule = data.RULES / ('58-ram-rescue-data-' + name + '.rules')
    rule_text = data.udev_rules(record)
    directory(rule.parent)
    if os.path.lexists(rule) and (rule.is_symlink() or rule.read_text() != rule_text):
        raise RuntimeError('Automount exclusions changed independently')
    atomic(rule, rule_text.encode(), mode=0o644)
    data.run(['udevadm', 'control', '--reload-rules'])
    sys_path = str((Path('/sys/class/block') / Path(node).name).resolve(strict=True))
    data.run(['udevadm', 'trigger', '--action=change', '--settle', sys_path])
    props = data.run(['udevadm', 'info', '--query=property', '--name', node])
    if 'UDISKS_IGNORE=1' not in props.splitlines():
        raise RuntimeError('Raw-device automount exclusion is not effective')
    require_idle(record, current_node(record), item)
    if not item:
        # Start with queuing off. A failed creation cannot leave an unattended
        # no-path queue. The controller's fenced startup verifies again.
        table = f"0 {cfg['partition_sectors']} multipath 2 queue_mode bio 0 1 1 round-robin 0 1 1 {node} 1"
        data.run(['dmsetup', 'create', name, '--uuid', cfg['map_uuid'], '--table', table])
        data.run(['udevadm', 'settle'])
        item = map_entry(name)
        if not item:
            raise RuntimeError('Created mapping did not appear')
        require_idle(record, node, item)
    folder = data.STATE / name
    directory(folder)
    if runtime_record.exists() and read_trusted_json(runtime_record) != record:
        raise RuntimeError('Runtime registration changed; refusing replacement')
    write(runtime_record, record)
    directory(Path(cfg['run_dir']))
    write(Path(cfg['identity_path']), record['identity'])
    text = data.service_unit(name, data.stage_runtime())
    lines = []
    for line in text.splitlines():
        if line.startswith('ExecStart='):
            line = f'ExecStart=/opt/guard-runtime/guard-runtime maintain --record {runtime_record}' + (' --rearm' if rearm else ' --first-enable')
        elif line.startswith('ExecStopPost='):
            line = f'ExecStopPost=/opt/guard-runtime/guard-runtime maintain --record {runtime_record} --takeover'
        lines.append(line)
    atomic(data.UNITS / unit, ('\n'.join(lines) + '\n').encode(), mode=0o644)
    slice_path = data.UNITS / data.SLICE
    if slice_path.exists() and slice_path.read_text() != data.slice_unit():
        raise RuntimeError('Data resource slice changed independently')
    atomic(slice_path, data.slice_unit().encode(), mode=0o644)
    data.run(['systemctl', 'daemon-reload'])
    try:
        data.run(['systemctl', 'start', unit])
    except BaseException:
        # ExecStopPost performs fenced takeover. Never issue an unfenced
        # fail_if_no_path against a possibly running owner here.
        journal(name, 'enable_incomplete', record_sha256=digest(record))
        raise
    observed = read_trusted_json(Path(cfg['run_dir']) / 'path-state.json')
    if observed.get('state') != 'ready':
        raise RuntimeError('Controller has not confirmed ready; inspect retained state')
    journal(name, 'active', record_sha256=digest(record))
    return {'map': name, 'state': 'active', 'device': '/dev/mapper/' + name}


@manage.exclusive_control
def enable(name, expected):
    value = load(name)
    confirmation(expected, digest(value), '启用所选设备，并允许后续开机启用')
    release_support.require_supported()
    # Persist consent only after the integration exists. A failed start remains
    # visibly enabled/incomplete and can be disabled; it is never called ready.
    install_boot()
    value['enabled'] = True
    write(config_path(name), value)
    return start(name, value)


def stop_owner(name, value):
    record = value['record']
    runtime_record = data.STATE / name / 'record.json'
    if not map_entry(name) and not Path(record['guard']['run_dir']).exists():
        return {'map': name, 'state': 'stopped', 'map_present': False}
    if not runtime_record.exists():
        raise RuntimeError('No native owner receipt; inspect incomplete activation before removal')
    if read_trusted_json(runtime_record) != record:
        raise RuntimeError('Native owner record differs')
    result = subprocess.run(['/usr/sbin/chroot', str(data.ram_environment.PRIVATE),
        '/opt/guard-runtime/guard-runtime', 'safe-stop', '--record', str(runtime_record)],
        text=True, capture_output=True, timeout=40)
    outcome = json.loads(result.stdout or result.stderr)
    if result.returncode or outcome.get('state') != 'stopped':
        raise RuntimeError('Safe stop refused; resources retained: ' + json.dumps(outcome))
    return {'map': name, **outcome}


@manage.exclusive_control
def stop(name, persistent=False):
    value = load(name)
    if persistent:
        # Revoke future boot activation even if this boot's owner is busy.
        value['enabled'] = False
        write(config_path(name), value)
        journal(name, 'disable_pending', record_sha256=digest(value['record']))
    result = stop_owner(name, value)
    journal(name, 'disabled' if persistent else 'stopped', record_sha256=digest(value['record']))
    return {**result, 'enabled_at_boot': value['enabled'], 'map_preserved': True}


@manage.exclusive_control
def remove(name, expected):
    value = load(name)
    confirmation(expected, digest(value), '移除停用设备的映射和配置，保留文件系统内容')
    if value['enabled']:
        raise RuntimeError('Disable this device before removing it')
    record = value['record']
    item = map_entry(name)
    if item:
        stop_owner(name, value)
        node = current_node(record)
        require_idle(record, node, item)
        journal(name, 'removing', record_sha256=digest(record))
        # No --force, --deferred, unmount, filesystem write or process kill.
        data.run(['dmsetup', 'remove', name])
    config_path(name).unlink()
    journal(name, 'removed', record_sha256=digest(record))
    # Keep this boot's native journal/lock/RAM and exclusions; never re-use its
    # name or restore raw automount while old observations could still exist.
    return {'map': name, 'state': 'removed', 'filesystem_changed': False,
            'runtime_evidence': 'retained_until_reboot'}


def status(name=None):
    names = [data.map_name(name)] if name else [p.stem for p in sorted(CONFIG.glob('*.json'))]
    rows = []
    for selected in names:
        value = load(selected)
        record = value['record']
        path = Path(record['guard']['run_dir']) / 'path-state.json'
        native = read_trusted_json(path) if path.exists() else None
        rows.append({'map': selected, 'enabled_at_boot': value['enabled'],
                     'config_sha256': digest(value), 'configuration': value,
                     'map_present': map_entry(selected) is not None, 'native': native,
                     'service': data.run(['systemctl', 'show', '--property=ActiveState', '--value', data.service(selected)]),
                     'operation': read_trusted_json(state_path(selected)) if state_path(selected).exists() else None})
    return {'version': VERSION, 'devices': rows}


@manage.exclusive_control
def boot():
    outcomes = []
    for path in sorted(CONFIG.glob('*.json')):
        value = load(path.stem)
        if value['enabled']:
            outcomes.append(start(path.stem, value))
    return {'devices': outcomes}



@manage.exclusive_control
def uninstall(check_only=False):
    if list(CONFIG.glob('*.json')):
        raise RuntimeError('Disable and remove every configured data device before uninstalling')
    if any(item['uuid'].startswith(('RAMRESCUE-DATA-', 'RAMRESCUE-HOST-')) for item in manage.maps().values()):
        raise RuntimeError('Protected mappings still exist; package removal is refused')
    if any(path.exists() for path in (manage.INSTALL / 'install.json', Path('/etc/ram-rescue-guard.json'))):
        raise RuntimeError('Legacy/root integration still references the package; retire it explicitly first')
    if data.ram_environment.RECEIPT.exists():
        raise RuntimeError('RAM runtime remains from this boot; reboot after orderly device removal before uninstalling')
    for path in STATE.glob('*.json'):
        if read_trusted_json(path).get('phase') != 'removed':
            raise RuntimeError('Incomplete device operation remains: ' + path.name)
    if BOOT.exists():
        if BOOT.is_symlink() or BOOT.read_text() != BOOT_TEXT:
            raise RuntimeError('Boot integration changed independently')
        if check_only:
            raise RuntimeError('Run rescue-guard-admin device uninstall before removing the package')
        data.run(['systemctl', 'disable', '--now', BOOT.name])
        BOOT.unlink()
        data.run(['systemctl', 'daemon-reload'])
    return {'state': 'removable' if check_only else 'uninstalled', 'receipts_retained': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    for verb in ('plan', 'enroll'):
        command = commands.add_parser(verb)
        command.add_argument('--device', required=True)
        command.add_argument('--name', required=True, type=data.map_name)
        if verb == 'enroll':
            command.add_argument('--expect-plan')
    for verb in ('enable', 'stop', 'disable', 'remove', 'status'):
        command = commands.add_parser(verb)
        command.add_argument('--name', required=verb != 'status', type=data.map_name)
        if verb in ('enable', 'remove'):
            command.add_argument('--expect-plan')
    commands.add_parser('boot', help=argparse.SUPPRESS)
    commands.add_parser('uninstall')
    commands.add_parser('support')
    commands.add_parser('package-remove', help=argparse.SUPPRESS)
    args = parser.parse_args()
    data.require_root()
    if args.command == 'plan':
        result = preview(args.device, args.name)
    elif args.command == 'enroll':
        result = enroll(args.device, args.name, args.expect_plan)
    elif args.command == 'enable':
        result = enable(args.name, args.expect_plan)
    elif args.command in ('stop', 'disable'):
        result = stop(args.name, persistent=args.command == 'disable')
    elif args.command == 'remove':
        result = remove(args.name, args.expect_plan)
    elif args.command == 'status':
        result = status(args.name)
    elif args.command == 'support':
        result = {'subject': release_support.subject()}
    elif args.command in ('uninstall', 'package-remove'):
        result = uninstall(check_only=args.command == 'package-remove')
    else:
        result = boot()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'state': 'blocked', 'reason': str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
