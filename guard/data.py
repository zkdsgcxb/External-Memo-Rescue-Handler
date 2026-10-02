#!/usr/bin/python3
"""Temporary aftercare for explicitly enrolled, existing USB multipath maps.

This launcher never creates, mounts, reformats or removes a block device. Its
runtime units and automount exclusions last for this boot only. Stopping a
controller ends admission and leaves both the map and exclusions in place.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))

from host_files import atomic
from native_payload import verify_runtime

RAM = Path('/run/ram-rescue-demo')
STATE = Path('/run/ram-rescue-data')
UNITS = Path('/run/systemd/system')
RULES = Path('/run/udev/rules.d')
SLICE = 'ramrescuedata.slice'


def run(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT,
                                   timeout=45).strip()


def map_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}', value):
        raise ValueError('Data map names must be rr-data- followed by 1..64 letters, digits, _ or -')
    return value


def service(name):
    return 'ram-rescue-data-' + map_name(name) + '.service'


def read_enrollment(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError('Oversized enrollment')
    result = json.loads(path.read_text())
    if result.get('schema') != 1 or not isinstance(result.get('identity'), dict):
        raise ValueError('Unsupported data enrollment')
    from admin.data import validate_config
    validate_config(result['guard'])
    map_name(result['guard']['map_name'])
    return result


def require_root():
    if os.geteuid() != 0:
        raise RuntimeError('Use local privileged authentication to run this command as root')


def require_ram():
    """Use the existing protected boot runtime, including its shared /run."""
    command_line = Path('/proc/cmdline').read_text().split()
    if 'ram_rescue_guard=1' not in command_line or 'nompath' not in command_line:
        raise RuntimeError('Data aftercare currently requires this protected boot with nompath')
    mounts = json.loads(run(['findmnt', '-J', '-M', str(RAM), '-o', 'TARGET,FSTYPE,OPTIONS']))
    entries = mounts.get('filesystems', [])
    if (len(entries) != 1 or entries[0]['target'] != str(RAM)
            or entries[0]['fstype'] != 'tmpfs'
            or 'noswap' not in entries[0]['options'].split(',')):
        raise RuntimeError('Protected rescue tmpfs,noswap is unavailable')
    for name in ('run', 'dev', 'sys', 'proc'):
        if not os.path.samefile(RAM / name, '/' + name):
            raise RuntimeError('Rescue runtime does not share host /' + name)
    for path in ('usr/bin/python3', 'sbin/dmsetup', 'sbin/blkid'):
        if not os.access(RAM / path, os.X_OK):
            raise RuntimeError('Required RAM tool is unavailable: ' + path)
    if run(['systemctl', 'show', '--property=ActiveState', '--value',
            'ram-rescue-guard.service']) != 'active':
        raise RuntimeError('The existing root Guard must be active')
    for unit in ('multipathd.service', 'multipathd.socket'):
        state = run(['systemctl', 'show', '--property=ActiveState', '--value', unit])
        if state not in ('inactive', 'failed'):
            raise RuntimeError('A stock multipath controller may be active: ' + unit)


def udev_rules(profile, *, wanted_service=None):
    """Exclude only this raw partition and suppress scans of this stable map."""
    identity, config = profile['identity'], profile['guard']
    name = map_name(config['map_name'])
    uuid = config['map_uuid']
    values = [identity['vid'], identity['pid'], identity['usb_serial'], uuid]
    # udev matches are glob patterns. Reject metacharacters instead of silently
    # broadening an exact disk identity into a rule affecting other devices.
    if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:+-]+', value)
           for value in values):
        raise ValueError('USB identity or map UUID cannot be expressed as an exact udev match')
    partition = identity['partition_number']
    if not isinstance(partition, int) or isinstance(partition, bool) or partition <= 0:
        raise ValueError('Invalid enrolled partition number')
    activation = ''
    if wanted_service is not None:
        if not re.fullmatch(r'[A-Za-z0-9@_.-]+\.service', wanted_service):
            raise ValueError('Unsafe systemd activation unit')
        activation = f', TAG+="systemd", ENV{{SYSTEMD_WANTS}}+="{wanted_service}"'
    return ('# Temporary data Guard exclusions; retained until reboot even after stop.\n'
            'SUBSYSTEM=="block", ACTION=="add|change", '
            f'ATTR{{partition}}=="{partition}", ATTRS{{idVendor}}=="{identity["vid"]}", '
            f'ATTRS{{idProduct}}=="{identity["pid"]}", ATTRS{{serial}}=="{identity["usb_serial"]}", '
            'ENV{UDISKS_IGNORE}="1"\n'
            'SUBSYSTEM=="block", ACTION=="add|change", '
            f'ENV{{DM_NAME}}=="{name}", ENV{{DM_UUID}}=="{uuid}", '
            'ENV{DM_NOSCAN}="1", ENV{DM_UDEV_DISABLE_OTHER_RULES_FLAG}="1", '
            'ENV{UDISKS_IGNORE}="1", OPTIONS:="nowatch"' + activation + '\n')


def slice_unit():
    return ('[Unit]\nDescription=Aggregate budget for temporary USB data aftercare\n\n'
            '[Slice]\nCPUAccounting=yes\nCPUQuota=20%\nCPUQuotaPeriodSec=20ms\n'
            'MemoryAccounting=yes\nMemoryMax=256M\nMemorySwapMax=0\n')


def service_unit(name, runtime):
    config = STATE / map_name(name) / 'config.json'
    directory = Path('/') / runtime.relative_to(RAM)
    start = f'{directory}/guard-runtime run --config {config}'
    stop = f'{directory}/guard-runtime takeover --config {config}'
    return (f'[Unit]\nDescription=Temporary aftercare for {name}\n'
            'After=ram-rescue-guard.service systemd-udevd.service\n'
            'Before=shutdown.target\nConflicts=shutdown.target\n'
            'ConditionKernelCommandLine=ram_rescue_guard=1\n\n'
            f'[Service]\nType=notify\nNotifyAccess=main\nSlice={SLICE}\n'
            f'RootDirectory={RAM}\nWorkingDirectory=/\n'
            f'ExecStart={start}\n'
            f'ExecStopPost={stop}\n'
            'Restart=no\nTimeoutStartSec=30\nTimeoutStopSec=15\n'
            'MemoryAccounting=yes\nMemoryMax=128M\nMemorySwapMax=0\n'
            'StandardOutput=journal\nStandardError=journal\n')


def stage_runtime():
    """Reuse the verified native package already supplied by protected boot."""
    return verify_runtime(RAM).parent


def enroll(name, partition, output):
    require_root()
    from admin.data import collect
    profile = collect(map_name(name), partition)
    output = Path(output)
    if output.exists():
        raise RuntimeError('Refusing to replace an existing enrollment')
    atomic(output, (json.dumps(profile, indent=2) + '\n').encode())
    return {'enrollment': str(output), 'map': name, 'operation': 'read_only_enrollment'}


def start(profile):
    require_root()
    require_ram()
    from admin.data import collect, validate_config
    config = profile['guard']
    validate_config(config)
    name = map_name(config['map_name'])
    rule_text = udev_rules(profile)
    current = collect(name, config['initial_node'])
    if current != profile:
        raise RuntimeError('Map or disk changed since enrollment; enroll the healthy map again')
    directory = STATE / name
    unit = UNITS / service(name)
    rule = RULES / ('58-ram-rescue-data-' + name + '.rules')
    if directory.exists() or unit.exists() or rule.exists():
        raise RuntimeError('This map already has temporary aftercare state; do not erase its transaction')
    runtime = stage_runtime()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    (directory / 'state').mkdir(mode=0o700)
    atomic(directory / 'identity.json', (json.dumps(profile['identity']) + '\n').encode())
    atomic(directory / 'config.json', (json.dumps(config) + '\n').encode())
    atomic(directory / 'enrollment.json', (json.dumps(profile) + '\n').encode())
    UNITS.mkdir(parents=True, exist_ok=True)
    RULES.mkdir(parents=True, exist_ok=True)
    slice_path = UNITS / SLICE
    desired_slice = slice_unit().encode()
    if slice_path.exists() and slice_path.read_bytes() != desired_slice:
        raise RuntimeError('Existing aggregate data slice differs; refusing to replace its policy')
    if not slice_path.exists():
        atomic(slice_path, desired_slice, mode=0o644)
    atomic(unit, service_unit(name, runtime).encode(), mode=0o644)
    atomic(rule, rule_text.encode(), mode=0o644)
    run(['udevadm', 'control', '--reload-rules'])
    run(['udevadm', 'trigger', '--action=change', '--settle', config['initial_sys_path']])
    device = os.stat('/dev/mapper/' + name).st_rdev
    run(['udevadm', 'trigger', '--action=change', '--settle',
         f'/sys/dev/block/{os.major(device)}:{os.minor(device)}'])
    # A newly added exclusion must actually reach the current raw partition.
    properties = run(['udevadm', 'info', '--query=property', '--name', config['initial_node']])
    if 'UDISKS_IGNORE=1' not in properties.splitlines():
        raise RuntimeError('Raw partition automount exclusion was not applied')
    if collect(name, config['initial_node']) != profile:
        raise RuntimeError('Map or disk changed while installing its exclusions')
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'start', service(name)])
    if run(['systemctl', 'show', '--property=ActiveState', '--value', service(name)]) != 'active':
        raise RuntimeError('Data Guard did not become active')
    return {'map': name, 'service': service(name), 'state': 'active',
            'runtime': str(runtime), 'persistent_installation': False}


def stop(name):
    require_root()
    name = map_name(name)
    directory = STATE / name
    if not (directory / 'config.json').is_file():
        raise RuntimeError('No temporary data aftercare exists for this map')
    config = json.loads((directory / 'config.json').read_text())
    if config.get('map_name') != name or config.get('run_dir') != str(directory / 'state'):
        raise RuntimeError('Temporary state does not belong to this map')
    run(['systemctl', 'stop', service(name)])
    return {'map': name, 'state': 'stopped', 'map_retained': True,
            'automount_exclusions': 'retained_until_reboot',
            'note': 'No new recovery is admitted; this does not unmount or remove the map.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    registration = commands.add_parser('enroll', help='Read one healthy existing map and USB partition')
    registration.add_argument('--map', required=True, type=map_name)
    registration.add_argument('--partition', required=True)
    registration.add_argument('--output', required=True, type=Path)
    activation = commands.add_parser('start', help='Run its controller in the existing protected RAM runtime')
    activation.add_argument('--enrollment', required=True, type=Path)
    deactivation = commands.add_parser('stop', help='End recovery; leave the map and exclusions intact')
    deactivation.add_argument('--map', required=True, type=map_name)
    args = parser.parse_args()
    if args.command == 'enroll':
        result = enroll(args.map, args.partition, args.output)
    elif args.command == 'start':
        result = start(read_enrollment(args.enrollment))
    else:
        result = stop(args.map)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
