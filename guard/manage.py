#!/usr/bin/python3
"""One entry point for quiet maintenance of enrolled USB multipath maps.

systemd starts controllers when registered stable maps appear. Root protection
is adopted from its existing boot owner, never started a second time here.
"""
import argparse
from functools import wraps
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))

import data as services
from admin.data import collect
from host_files import atomic, sha256
from admin.registry import record_from_profile, validate_record

REGISTRY = Path('/etc/ram-rescue-manager/devices')
RUNTIME = Path('/run/ram-rescue-manager')
INSTALL = Path('/var/lib/ram-rescue-manager')
VERSIONS = Path('/usr/local/lib/ram-rescue-manager')
ENTRY = Path('/usr/local/sbin/rescue-guard')
UNIT = Path('/etc/systemd/system/ram-rescue-manager.service')
CONTROLLER = UNIT.with_name('ram-rescue-maintain@.service')
SLICE = UNIT.with_name(services.SLICE)
RULE = Path('/etc/udev/rules.d/58-ram-rescue-manager.rules')
ROOT_CONFIG = Path('/run/ram-rescue-guard/config.json')
CONTROL_LOCK = Path('/run/lock/ram-rescue-manager.lock')


def exclusive_control(function):
    """Serialize persistent edits; preparation must stay callable by PID 1."""
    @wraps(function)
    def locked(*args, **kwargs):
        services.require_root()
        fd = os.open(CONTROL_LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return function(*args, **kwargs)
        finally:
            os.close(fd)
    return locked


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError('Oversized manager record: ' + str(path))
    return json.loads(path.read_text())


def write_json(path, value):
    atomic(path, (json.dumps(value, indent=2, sort_keys=True) + '\n').encode())


def records():
    result = []
    for path in sorted(REGISTRY.glob('*.json')):
        record = read_json(path)
        validate_record(record)
        if record['guard']['profile'] != 'host-data' or path.stem != record['guard']['map_name']:
            raise RuntimeError('Registry contains a foreign entry: ' + str(path))
        result.append(record)
    return result


def maps():
    return {entry.name: {'name': (entry / 'dm/name').read_text().strip(),
                         'uuid': (entry / 'dm/uuid').read_text().strip(), 'sys': entry}
            for entry in Path('/sys/class/block').glob('dm-*')}


def root_profile():
    if not ROOT_CONFIG.exists():
        return None
    config = read_json(ROOT_CONFIG)
    identity = services.RAM / config['identity_path'].lstrip('/')
    return {'schema': 1, 'identity': read_json(identity), 'guard': config}


def resolve_map(device):
    """Accept a stable map, its mounted LV, or its sole USB partition."""
    path = Path(device).resolve(strict=True)
    if path.is_dir():
        dev = services.run(['findmnt', '-nro', 'MAJ:MIN', '-T', str(path)])
    else:
        info = path.stat()
        if not stat.S_ISBLK(info.st_mode):
            raise RuntimeError('Select a protected mount directory or block device')
        dev = f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'
    selected = (Path('/sys/dev/block') / dev).resolve(strict=True)
    candidates = []
    root = root_profile()
    for item in maps().values():
        if item['uuid'].startswith('RAMRESCUE-DATA-'):
            peers = {item['sys'].resolve(), *[p.resolve() for p in (item['sys'] / 'slaves').iterdir()]}
            if selected in peers:
                candidates.append(item)
        elif root and item['uuid'] == root['guard']['map_uuid']:
            peers = {item['sys'].resolve(), *[p.resolve() for p in (item['sys'] / 'holders').iterdir()],
                     *[p.resolve() for p in (item['sys'] / 'slaves').iterdir()]}
            if selected in peers:
                candidates.append(item)
    if len(candidates) != 1:
        raise RuntimeError('No unique supported stable mapping; establish DM protection before enrollment')
    return candidates[0], root


def controller_name(name):
    return 'ram-rescue-maintain@' + services.map_name(name) + '.service'


def render_rules(entries):
    return ''.join(services.udev_rules(record, wanted_service=controller_name(record['guard']['map_name']))
                   for record in entries)


def render_manager(program):
    return ('[Unit]\nDescription=Prepare quiet USB mapping maintenance\n'
            'Requires=ram-rescue-guard.service\nAfter=ram-rescue-guard.service systemd-udevd.service\n'
            'ConditionKernelCommandLine=ram_rescue_guard=1\n\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            f'ExecStart=/usr/bin/python3 {program} prepare\nTimeoutStartSec=45\n\n'
            '[Install]\nWantedBy=multi-user.target\n')


def render_controller():
    return ('[Unit]\nDescription=Maintain enrolled USB mapping %i\n'
            'Requires=ram-rescue-manager.service\nAfter=ram-rescue-manager.service\n'
            'Before=shutdown.target\nConflicts=shutdown.target\n\n'
            '[Service]\nType=notify\nNotifyAccess=main\nSlice=ramrescuedata.slice\n'
            f'RootDirectory={services.RAM}\nWorkingDirectory=/\n'
            'ExecStart=/opt/manager/maintain --record /run/ram-rescue-manager/entries/%i.json\n'
            'ExecStopPost=/opt/manager/maintain --record /run/ram-rescue-manager/entries/%i.json --takeover\n'
            'Restart=no\nTimeoutStartSec=30\nTimeoutStopSec=15\n'
            'MemoryAccounting=yes\nMemoryMax=128M\nMemorySwapMax=0\n'
            'StandardOutput=journal\nStandardError=journal\n')


def prepare():
    services.require_root()
    services.require_ram()
    entries = records()
    runtime = services.stage_runtime()
    alias = services.RAM / 'opt/manager'
    try:
        alias.symlink_to(runtime.relative_to(alias.parent))
    except FileExistsError:
        if not alias.is_symlink() or alias.resolve() != runtime:
            raise RuntimeError('This boot already uses another manager runtime; do not replace live code')
    folder = RUNTIME / 'entries'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for record in entries:
        path = folder / (record['guard']['map_name'] + '.json')
        if path.exists():
            if read_json(path) != record:
                raise RuntimeError('RAM enrollment differs; never replace a live identity')
        else:
            write_json(path, record)
    return {'prepared': len(entries), 'runtime': str(runtime), 'root_owner': 'existing_boot_service'}


def activate_present(entries):
    live = maps()
    requested = []
    for record in entries:
        config = record['guard']
        found = [item for item in live.values() if item['name'] == config['map_name'] and item['uuid'] == config['map_uuid']]
        if len(found) == 1:
            # SYSTEMD_WANTS is acted on when a device becomes active. Explicit
            # start covers maps already active before this rule was installed.
            name = controller_name(config['map_name'])
            services.run(['systemctl', 'start', name])
            requested.append(name)
    return requested


def receipt():
    value = read_json(INSTALL / 'install.json')
    if value.get('state') != 'installed':
        raise RuntimeError('Manager installation is incomplete; inspect its receipt')
    return value


def save_rules(entries):
    record = receipt()
    if sha256(RULE) != record['rule_sha256']:
        raise RuntimeError('Manager udev rules were edited independently')
    text = render_rules(entries).encode()
    atomic(RULE, text, mode=0o644)
    record['rule_sha256'] = hashlib.sha256(text).hexdigest()
    write_json(INSTALL / 'install.json', record)
    services.run(['udevadm', 'control', '--reload-rules'])


@exclusive_control
def register(device):
    services.require_root()
    item, root = resolve_map(device)
    if root and item['uuid'] == root['guard']['map_uuid']:
        # Root activation belongs to initramfs. A second owner would be unsafe.
        record_from_profile(root)
        return {'map': item['name'], 'state': 'already_managed', 'owner': 'ram-rescue-guard.service'}
    slaves = list((item['sys'] / 'slaves').iterdir())
    if len(slaves) != 1:
        raise RuntimeError('Expected one enrolled USB partition behind the mapping')
    profile = collect(item['name'], '/dev/' + slaves[0].name)
    record = record_from_profile(profile)
    REGISTRY.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = REGISTRY / (item['name'] + '.json')
    if target.exists():
        if read_json(target) != record:
            raise RuntimeError('Existing registration differs; identity is never automatically relearned')
    else:
        write_json(target, record)
    result = {'map': item['name'], 'state': 'registered', 'registration': str(target)}
    if (INSTALL / 'install.json').exists():
        save_rules(records())
        prepare()
        # Apply exclusions to the current objects as well as future arrivals.
        services.run(['udevadm', 'trigger', '--action=change', '--settle', str(slaves[0].resolve())])
        services.run(['udevadm', 'trigger', '--action=change', '--settle', str(item['sys'].resolve())])
        result['services'] = activate_present([record])
    return result


def install_sources():
    relative = [Path('guard/manage.py'), Path('guard/data.py'), Path('guard/host_files.py'),
                Path('guard/native_payload.py'),
                Path('ram-rescue-demo/src/rescue.py'),
                *[p.relative_to(BASE.parent) for p in sorted((BASE / 'admin').glob('*.py'))]]
    content = {path: (BASE.parent / path).read_bytes() for path in relative}
    hashed = hashlib.sha256()
    for path, data in sorted(content.items()):
        hashed.update(str(path).encode() + b'\0' + data + b'\0')
    destination = VERSIONS / hashed.hexdigest()
    if destination.exists():
        raise RuntimeError('Installation version already exists; refusing an incomplete retry')
    destination.mkdir(mode=0o755, parents=True)
    for path, data in content.items():
        target = destination / path
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        atomic(target, data, mode=0o755 if path == Path('guard/manage.py') else 0o644)
    return destination / 'guard/manage.py'


@exclusive_control
def install():
    services.require_root()
    services.require_ram()
    entries = records()
    if any(os.path.lexists(path) for path in (INSTALL, ENTRY, UNIT, CONTROLLER, SLICE, RULE)):
        raise RuntimeError('Manager integration already exists; refusing to overwrite it')
    program = install_sources()
    files = {UNIT: render_manager(program).encode(), CONTROLLER: render_controller().encode(),
             SLICE: services.slice_unit().encode(), RULE: render_rules(entries).encode()}
    INSTALL.mkdir(mode=0o700, parents=True)
    record = {'state': 'installing', 'program': str(program),
              'files': {str(path): hashlib.sha256(content).hexdigest() for path, content in files.items()},
              'rule_sha256': hashlib.sha256(files[RULE]).hexdigest()}
    write_json(INSTALL / 'install.json', record)
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic(path, content, mode=0o644)
    ENTRY.parent.mkdir(parents=True, exist_ok=True)
    ENTRY.symlink_to(program)
    services.run(['systemctl', 'daemon-reload'])
    services.run(['udevadm', 'control', '--reload-rules'])
    services.run(['systemctl', 'enable', '--now', UNIT.name])
    # Coldplug already-active maps and raw partitions receive the exclusions.
    for entry in entries:
        current = [item for item in maps().values() if item['name'] == entry['guard']['map_name']]
        for item in current:
            for slave in (item['sys'] / 'slaves').iterdir():
                services.run(['udevadm', 'trigger', '--action=change', '--settle', str(slave.resolve())])
            services.run(['udevadm', 'trigger', '--action=change', '--settle', str(item['sys'].resolve())])
    record['state'] = 'installed'
    write_json(INSTALL / 'install.json', record)
    started = activate_present(entries)
    return {'state': 'installed', 'command': str(ENTRY), 'services': started,
            'root_owner': 'existing_boot_service', 'new_polling_daemon': False}


def require_empty_data_scope():
    """This upgrade only changes next-boot administration of an existing root."""
    if os.path.lexists(REGISTRY) and (REGISTRY.is_symlink() or not REGISTRY.is_dir()):
        raise RuntimeError('Expected the original data registry directory')
    if REGISTRY.exists() and any(REGISTRY.iterdir()):
        raise RuntimeError('Manager upgrade requires an empty data registry')
    output = services.run(['systemctl', 'list-units', '--all', '--type=service',
                           '--no-legend', '--plain', '--no-pager',
                           'ram-rescue-maintain@*.service', 'ram-rescue-data-*.service'])
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[0].startswith(('ram-rescue-maintain@', 'ram-rescue-data-')):
            raise RuntimeError('Cannot establish data controller state: ' + line)
        # A failed unit can still retain an uninterruptible helper in its cgroup.
        if fields[2] != 'inactive':
            raise RuntimeError('Data controller may still be active: ' + fields[0])


def replace_entry(target):
    """Atomically replace the command symlink and sync its directory."""
    with tempfile.TemporaryDirectory(prefix='.' + ENTRY.name + '-', dir=ENTRY.parent) as folder:
        link = Path(folder) / 'entry'
        link.symlink_to(target)
        os.replace(link, ENTRY)
        directory = os.open(ENTRY.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


@exclusive_control
def upgrade():
    """Stage new persistent integration; only a protected reboot activates it."""
    services.require_ram()
    receipt_path = INSTALL / 'install.json'
    if INSTALL.is_symlink() or receipt_path.is_symlink() or not receipt_path.is_file():
        raise RuntimeError('Expected the original manager installation receipt')
    record = receipt()
    paths = {UNIT, CONTROLLER, SLICE, RULE}
    if not isinstance(record.get('files'), dict) or set(record['files']) != {str(path) for path in paths}:
        raise RuntimeError('Unexpected manager receipt file set')
    expected = {**record['files'], str(RULE): record.get('rule_sha256')}
    previous = {}
    for path in sorted(paths | {receipt_path}):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Integration is not an original regular file: ' + str(path))
        content = path.read_bytes()
        if path != receipt_path and hashlib.sha256(content).hexdigest() != expected[str(path)]:
            raise RuntimeError('Integration changed independently: ' + str(path))
        previous[path] = (content, stat.S_IMODE(path.stat().st_mode))
    old_program = Path(record['program'])
    if (old_program.parent.name != 'guard' or old_program.name != 'manage.py'
            or old_program.parent.parent.parent != VERSIONS
            or old_program.parent.is_symlink() or old_program.parent.parent.is_symlink()
            or old_program.is_symlink() or not old_program.is_file()):
        raise RuntimeError('Installed manager program is outside its version directory')
    if not ENTRY.is_symlink() or str(ENTRY.readlink()) != record['program']:
        raise RuntimeError('Manager command changed independently')
    require_empty_data_scope()
    if previous[RULE][0] != render_rules([]).encode():
        raise RuntimeError('Empty-registry upgrade requires empty manager rules')

    backup = Path(tempfile.mkdtemp(prefix='upgrade-', dir=INSTALL))
    journal = {'state': 'staging', 'previous_program': record['program'], 'program': None,
               'entry_target': str(ENTRY.readlink()), 'files': {}}
    for index, (path, (content, mode)) in enumerate(sorted(previous.items())):
        name = f'{index}.bin'
        atomic(backup / name, content)
        journal['files'][str(path)] = {'backup': name, 'mode': mode,
                                     'sha256': hashlib.sha256(content).hexdigest()}
    write_json(backup / 'upgrade.json', journal)
    changed = False
    files = {}
    next_record = pending = None
    try:
        program = install_sources()
        files = {UNIT: render_manager(program).encode(), CONTROLLER: render_controller().encode(),
                 SLICE: services.slice_unit().encode(), RULE: render_rules([]).encode()}
        journal.update(state='applying', program=str(program))
        write_json(backup / 'upgrade.json', journal)
        require_empty_data_scope()
        pending = {**record, 'state': 'upgrading', 'upgrade_backup': str(backup)}
        changed = True  # atomic() may replace its target before reporting a sync error.
        write_json(receipt_path, pending)
        for path, content in files.items():
            if content != previous[path][0]:
                atomic(path, content, mode=0o644)
        replace_entry(program)
        services.run(['systemctl', 'daemon-reload'])
        next_record = {**record, 'state': 'installed', 'program': str(program),
                       'files': {str(path): hashlib.sha256(content).hexdigest()
                                 for path, content in files.items()},
                       'rule_sha256': hashlib.sha256(files[RULE]).hexdigest(),
                       'upgrade_backup': str(backup), 'activation': 'protected_reboot'}
        write_json(receipt_path, next_record)
        journal['state'] = 'complete'
        write_json(backup / 'upgrade.json', journal)
    except BaseException as error:
        failures = []
        if changed:
            for path in sorted(paths):
                try:
                    if path.is_symlink() or path.read_bytes() not in (previous[path][0], files[path]):
                        raise RuntimeError('Integration changed during upgrade: ' + str(path))
                    atomic(path, previous[path][0], mode=previous[path][1])
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            try:
                if not ENTRY.is_symlink() or str(ENTRY.readlink()) not in (record['program'], journal['program']):
                    raise RuntimeError('Manager command changed during upgrade')
                replace_entry(journal['entry_target'])
            except Exception as rollback_error:
                failures.append(str(rollback_error))
            try:
                services.run(['systemctl', 'daemon-reload'])
            except Exception as rollback_error:
                failures.append(str(rollback_error))
            try:
                if receipt_path.is_symlink() or read_json(receipt_path) not in (record, pending, next_record):
                    raise RuntimeError('Manager receipt changed during upgrade')
                if failures:
                    write_json(receipt_path, {**pending, 'state': 'upgrade_failed'})
                else:
                    atomic(receipt_path, previous[receipt_path][0], mode=previous[receipt_path][1])
            except Exception as rollback_error:
                failures.append(str(rollback_error))
        journal.update(state='rollback_failed' if failures else 'rolled_back',
                       error=str(error), rollback_errors=failures)
        write_json(backup / 'upgrade.json', journal)
        raise RuntimeError('Manager upgrade failed; ' + journal['state'] + '; inspect ' + str(backup)) from error
    return {'state': 'upgraded', 'command': str(ENTRY), 'backup': str(backup),
            'requires_protected_reboot': True, 'running_root_changed': False,
            'runtime_prepared': False, 'services_started': []}


def status():
    services.require_root()
    result = []
    root = root_profile()
    entries = ([root] if root else []) + records()
    live = maps()
    for entry in entries:
        config = entry['guard']
        evidence = Path(config['run_dir']) / 'path-state.json'
        event = read_json(evidence) if evidence.exists() else {}
        name = config['map_name']
        unit = 'ram-rescue-guard.service' if entry is root else controller_name(name)
        active = services.run(['systemctl', 'show', '--property=ActiveState', '--value', unit])
        present = any(item['name'] == name and item['uuid'] == config['map_uuid'] for item in live.values())
        previous = event.get('state')
        if not present:
            state = 'waiting_for_map'
        elif previous in {'expired', 'failed', 'interrupted', 'blocked'}:
            state = previous
        elif active != 'active':
            state = 'inactive' if previous else 'not_started'
        else:
            state = previous or 'starting'
        result.append({'map': name, 'service': unit, 'service_state': active,
                       'state': state, 'last_state': previous,
                       'recoveries': event.get('recoveries', 0), 'map_present': present,
                       'reason': event.get('reason')})
    return {'devices': result, 'root_boot_prepared': root is not None,
            'manager_installed': (INSTALL / 'install.json').exists()}


@exclusive_control
def uninstall():
    services.require_root()
    record = receipt()
    known = {entry['guard']['map_uuid'] for entry in records()}
    if any(item['uuid'] in known for item in maps().values()):
        raise RuntimeError('Registered data maps still exist; unmount and remove them normally before uninstalling')
    expected = {**record['files'], str(RULE): record['rule_sha256']}
    for path, wanted in expected.items():
        if sha256(Path(path)) != wanted:
            raise RuntimeError('Integration changed independently: ' + path)
    if not ENTRY.is_symlink() or str(ENTRY.readlink()) != record['program']:
        raise RuntimeError('Manager command changed independently')
    services.run(['systemctl', 'disable', '--now', UNIT.name])
    for path in expected:
        Path(path).unlink()
    ENTRY.unlink()
    services.run(['systemctl', 'daemon-reload'])
    services.run(['udevadm', 'control', '--reload-rules'])
    record['state'] = 'uninstalled'
    write_json(INSTALL / 'install.json', record)
    return {'state': 'uninstalled', 'registration_retained': True, 'root_protection_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    registration = commands.add_parser('register', help='Enroll a protected device or recognize its existing owner')
    registration.add_argument('--device', required=True)
    for command in ('install', 'upgrade', 'status', 'uninstall'):
        commands.add_parser(command)
    commands.add_parser('prepare', help=argparse.SUPPRESS)
    args = parser.parse_args()
    actions = {'install': install, 'upgrade': upgrade, 'status': status,
               'uninstall': uninstall, 'prepare': prepare}
    result = register(args.device) if args.command == 'register' else actions[args.command]()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
