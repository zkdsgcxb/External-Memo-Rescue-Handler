#!/usr/bin/python3
"""Restore only the enrolled EFI mount on device arrival, using systemd/udev."""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from install import atomic, sha256

RULE = Path('/etc/udev/rules.d/90-ram-rescue-efi.rules')
PATH_UNIT = Path('/etc/systemd/system/ram-rescue-efi.path')
ENABLED = Path('/etc/systemd/system/local-fs.target.wants/ram-rescue-efi.path')
STATE = Path('/var/lib/ram-rescue-efi')
FSTAB = Path('/etc/fstab')
MOUNT = '/boot/efi'
KEYS = ('ID_FS_UUID', 'ID_PART_ENTRY_UUID', 'ID_USB_SERIAL_SHORT')


def command(args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30)


def properties(device):
    return dict(line.split('=', 1) for line in
                command(['udevadm', 'info', '--query=property', '--name', device]).splitlines()
                if '=' in line)


def render_rule(identity):
    for key in KEYS:
        value = identity[key]
        # Exact values only; reject udev glob patterns, quoting and substitutions.
        if not re.fullmatch(r'[A-Za-z0-9_.:+-]+', value):
            raise ValueError('Unsafe or non-exact EFI identity: ' + key)
    match = ['ACTION=="add|change"', 'SUBSYSTEM=="block"',
             'ENV{DEVTYPE}=="partition"', 'ENV{ID_FS_TYPE}=="vfat"']
    match += ['ENV{' + key + '}=="' + identity[key] + '"' for key in KEYS]
    return ('# Expose only the enrolled EFI to the native systemd path unit.\n'
            '# No RUN helper, polling daemon, filesystem repair or Guard action.\n'
            + ', '.join(match + ['SYMLINK+="ram-rescue-efi"']) + '\n')


def render_path():
    # Default path dependencies would order us after sysinit, while the ordinary
    # mount precedes local-fs. Avoid that cycle and retain shutdown ordering.
    return '''[Unit]
Description=Restore the enrolled USB EFI mount when available
DefaultDependencies=no
After=local-fs-pre.target
Before=umount.target
Conflicts=umount.target

[Path]
PathExists=/dev/ram-rescue-efi
Unit=boot-efi.mount
TriggerLimitIntervalSec=30s
TriggerLimitBurst=5

[Install]
WantedBy=local-fs.target
'''


def render_fsck():
    # Do not cache an earlier successful check across a missed/merged removal.
    # The stock oneshot remains ordered before mount and skips mounted devices.
    return '[Service]\nRemainAfterExit=no\n'


def fsck_unit(identity):
    return command(['systemd-escape', '--path', '--template=systemd-fsck@.service',
                    '/dev/disk/by-uuid/' + identity['ID_FS_UUID']]).strip()


def fsck_dropin(identity):
    return PATH_UNIT.parent / (fsck_unit(identity) + '.d') / '50-ram-rescue-efi.conf'


@contextmanager
def maintenance_lock():
    # Prevent an apt/dpkg EFI update while resetting the old fsck cache through
    # one normal unmount/check/mount cycle. Never force a busy mount to detach.
    with ExitStack() as stack:
        for name in ('lock-frontend', 'lock'):
            stream = stack.enter_context(open('/var/lib/dpkg/' + name, 'r+'))
            fcntl.lockf(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def trigger(identity):
    device = Path('/dev/disk/by-uuid') / identity['ID_FS_UUID']
    if device.exists():
        command(['udevadm', 'trigger', '--action=change', '--settle',
                 str(Path('/sys/class/block') / device.resolve(strict=True).name)])


def withdraw(record):
    # Check all ownership before stopping/removing anything. Missing files are
    # permitted for retry after an interrupted or partially completed removal.
    dropin = fsck_dropin(record['identity'])
    for path, key in ((RULE, 'rule_sha256'), (PATH_UNIT, 'path_sha256'),
                      (dropin, 'fsck_sha256')):
        if os.path.lexists(path) and (path.is_symlink() or sha256(path) != record[key]):
            raise RuntimeError('Integration changed; review before removing: ' + str(path))
    if os.path.lexists(ENABLED) and (
            not ENABLED.is_symlink() or ENABLED.readlink() != PATH_UNIT):
        raise RuntimeError('Enabled path unit changed; review before removing')
    # Reload first so an installation failure before its first daemon-reload
    # can still stop the newly written unit by its actual name.
    if PATH_UNIT.exists():
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'stop', PATH_UNIT.name])
    ENABLED.unlink(missing_ok=True)
    RULE.unlink(missing_ok=True)
    PATH_UNIT.unlink(missing_ok=True)
    dropin.unlink(missing_ok=True)
    if dropin.parent.is_dir() and not any(dropin.parent.iterdir()):
        dropin.parent.rmdir()
    command(['systemctl', 'daemon-reload'])
    command(['udevadm', 'control', '--reload-rules'])
    trigger(record['identity'])


def fstab_entry(text):
    entries = [line.split() for line in text.splitlines()
               if line.strip() and not line.lstrip().startswith('#')]
    matches = [entry for entry in entries if len(entry) >= 2 and entry[1] == MOUNT]
    if len(matches) != 1 or len(matches[0]) != 6 or matches[0][2] != 'vfat':
        raise RuntimeError('Expected exactly one ordinary vfat EFI fstab entry')
    entry = matches[0]
    if any(option in entry[3].split(',') for option in ('x-systemd.automount', 'noauto')):
        raise RuntimeError('This integration requires the existing ordinary EFI mount')
    if not entry[4].isdigit() or int(entry[4]) not in (0, 1):
        raise RuntimeError('Invalid EFI fstab dump field')
    if not entry[5].isdigit() or int(entry[5]) <= 0:
        raise RuntimeError('Keep the existing native filesystem check dependency')
    return entry


def validate(preparation):
    saved = json.loads(preparation.read_text())
    if not saved.get('clean') or not saved.get('backup_verified'):
        raise RuntimeError('Requires a verified backup and clean unmounted fsck result')
    if saved['fsck_readonly']['returncode'] != 0 or saved['mount_restored']['returncode'] != 0:
        raise RuntimeError('EFI preparation or mount restoration was unsuccessful')
    backup = preparation.parent / 'efi-partition.img.zst'
    if sha256(backup) != saved['backup_compressed_sha256']:
        raise RuntimeError('EFI backup checksum differs')
    entry = fstab_entry(FSTAB.read_text())
    old = saved['identity']
    if entry[0] != '/dev/disk/by-uuid/' + old['UUID']:
        raise RuntimeError('EFI source changed since preparation')
    device = Path(entry[0]).resolve(strict=True)
    props = properties(str(device))
    if (props.get('ID_FS_TYPE') != 'vfat' or props.get('ID_FS_UUID') != old['UUID']
            or props.get('ID_PART_ENTRY_UUID') != old['PART_ENTRY_UUID']
            or props.get('ID_PART_ENTRY_NUMBER') != '1'):
        raise RuntimeError('Live EFI identity differs from the checked partition')
    sys_path = (Path('/sys/class/block') / device.name).resolve(strict=True)
    if (sys_path.parent / 'diskseq').read_text().strip() != saved['diskseq']:
        raise RuntimeError('The checked disk instance changed')
    # Keep the existing root protection controller as the authority for this disk.
    maps = [p for p in Path('/sys/class/block').glob('dm-*')
            if (p / 'dm/name').read_text().strip() == 'ram-rescue-path']
    if len(maps) != 1:
        raise RuntimeError('Expected the current protected root map')
    slaves = list((maps[0] / 'slaves').iterdir())
    if len(slaves) != 1 or slaves[0].resolve().parent != sys_path.parent:
        raise RuntimeError('EFI is not on the current protected root disk')
    identity = {key: props[key] for key in KEYS}
    return render_rule(identity), identity


def install(preparation, vm_report):
    if os.geteuid() != 0:
        raise RuntimeError('Use local administrator authentication')
    with maintenance_lock():
        install_locked(preparation, vm_report)


def install_locked(preparation, vm_report):
    rule, identity = validate(preparation)
    vm = json.loads(vm_report.read_text())
    if vm.get('passed') is not True or vm.get('scope') != 'native EFI path-triggered mount':
        raise RuntimeError('Requires the passed native path-triggered VM report')
    if vm.get('rule_renderer_sha256') != sha256(Path(__file__)):
        raise RuntimeError('VM did not test this rule renderer and installer version')
    dropin = fsck_dropin(identity)
    if any(os.path.lexists(p) for p in (RULE, PATH_UNIT, ENABLED, dropin, STATE)):
        raise RuntimeError('EFI event integration already exists; refusing to overwrite it')
    original_fstab = FSTAB.read_bytes()
    STATE.mkdir(mode=0o700)
    atomic(STATE / 'fstab.before', original_fstab)
    record = {'state': 'preparing', 'identity': identity,
              'rule_sha256': hashlib.sha256(rule.encode()).hexdigest(),
              'path_sha256': hashlib.sha256(render_path().encode()).hexdigest(),
              'fsck_sha256': hashlib.sha256(render_fsck().encode()).hexdigest(),
              'fstab_sha256': hashlib.sha256(original_fstab).hexdigest(),
              'preparation_sha256': sha256(preparation), 'vm_report_sha256': sha256(vm_report)}
    atomic(STATE / 'install.json', (json.dumps(record, indent=2) + '\n').encode())
    try:
        atomic(RULE, rule.encode(), 0o644)
        atomic(PATH_UNIT, render_path().encode(), 0o644)
        dropin.parent.mkdir(exist_ok=True)
        atomic(dropin, render_fsck().encode(), 0o644)
        command(['udevadm', 'verify', str(RULE)])
        command(['systemd-analyze', 'verify', str(PATH_UNIT)])
        command(['systemctl', 'daemon-reload'])
        command(['udevadm', 'control', '--reload-rules'])
        # Clear the already-active stock fsck cache while EFI is unmounted.
        # Reloading RemainAfterExit alone cannot undo its previous exited state.
        try:
            command(['systemctl', 'stop', 'boot-efi.mount'])
            command(['systemctl', 'stop', fsck_unit(identity)])
        finally:
            command(['systemctl', 'start', 'boot-efi.mount'])
        if command(['systemctl', 'show', fsck_unit(identity), '--value',
                    '-p', 'RemainAfterExit']).strip() != 'no':
            raise RuntimeError('EFI filesystem check still caches completion')
        result = command(['findmnt', '-rn', '-M', MOUNT, '-o', 'UUID,FSTYPE,OPTIONS']).split()
        if len(result) != 3 or result[:2] != [identity['ID_FS_UUID'], 'vfat'] or 'rw' not in result[2].split(','):
            raise RuntimeError('EFI is not mounted read-write from the enrolled partition')
        command(['systemctl', 'enable', '--now', PATH_UNIT.name])
        trigger(identity)
        if (not ENABLED.is_symlink() or ENABLED.readlink() != PATH_UNIT or
                command(['systemctl', 'is-active', PATH_UNIT.name]).strip() != 'active'):
            raise RuntimeError('Native EFI path monitor is not enabled and active')
        if Path('/dev/ram-rescue-efi').resolve(strict=True) != (
                Path('/dev/disk/by-uuid') / identity['ID_FS_UUID']).resolve(strict=True):
            raise RuntimeError('Enrolled EFI symlink was not created correctly')
        if FSTAB.read_bytes() != original_fstab:
            raise RuntimeError('fstab changed during installation')
        record['state'] = 'installed'
        atomic(STATE / 'install.json', (json.dumps(record, indent=2) + '\n').encode())
    except BaseException:
        record['state'] = 'failed_pending_reload'
        atomic(STATE / 'install.json', (json.dumps(record, indent=2) + '\n').encode())
        withdraw(record)
        record['state'] = 'failed_removed'
        atomic(STATE / 'install.json', (json.dumps(record, indent=2) + '\n').encode())
        raise
    print(json.dumps({'installed': True, 'efi_mounted_rw': True,
                      'fstab_changed': False, 'guard_changed': False,
                      'new_daemon': False, 'reboot_required': False}))


def remove():
    if os.geteuid() != 0:
        raise RuntimeError('Use local administrator authentication')
    record = json.loads((STATE / 'install.json').read_text())
    pending = record['state'] in ('removal_pending_reload', 'failed_pending_reload')
    if (record['state'] != 'installed' and not pending) or (not pending and
            (not RULE.exists() or not PATH_UNIT.exists() or not ENABLED.is_symlink()
             or not fsck_dropin(record['identity']).exists())):
        raise RuntimeError('Integration changed; review before removing')
    record['state'] = 'removal_pending_reload'
    atomic(STATE / 'install.json', (json.dumps(record, indent=2) + '\n').encode())
    withdraw(record)
    record['state'] = 'removed'
    atomic(STATE / 'install.json', (json.dumps(record, indent=2) + '\n').encode())
    print('EFI event rule and path unit removed; the ordinary mount and fstab are unchanged.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preparation', type=Path)
    parser.add_argument('--vm-report', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--install', action='store_true')
    action.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    if args.remove:
        remove()
    elif args.install:
        if not args.preparation or not args.vm_report:
            parser.error('--preparation and --vm-report are required')
        install(args.preparation, args.vm_report)
    elif args.preparation:
        print(validate(args.preparation)[0], end='')
    else:
        parser.error('--preparation is required for a read-only preview')


if __name__ == '__main__':
    main()
