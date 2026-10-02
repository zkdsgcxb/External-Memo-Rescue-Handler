#!/usr/bin/env python3
"""Exercise native systemd FAT mount on a disposable Ubuntu USB disk.

The protected Ubuntu root uses a fresh qcow2 overlay. A separate disposable
GPT USB image contains the 64 MiB FAT partition. The final case removes both
virtual devices to exercise root recovery during EFI re-enumeration. No host
block device is accepted or opened.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import traceback

from auto_run import wait_for
import host_boot_probe as boot
from run import Channel, WORK, qemu_command

BASE = Path(__file__).resolve().parent
RENDERER = BASE.parent / 'guard/efi_mount.py'
GUEST_SOURCE = BASE / 'guest/efi_probe.py'
sys.path.insert(0, str(RENDERER.parent))
RENDERER_SHA256 = boot.sha256(RENDERER)
from efi_mount import fsck_unit, render_fsck, render_path, render_rule, source_hashes
if boot.sha256(RENDERER) != RENDERER_SHA256:
    raise RuntimeError('Production EFI renderer changed while loading')
SOURCE_SHA256 = source_hashes()

UUID = '1BAD-C0DE'
OPTIONS = 'umask=0077'
PARTUUID = '57f23024-de51-4ab3-a15a-71e443fc2d6f'
MOUNT = 'boot-efi.mount'
PATH_UNIT = 'ram-rescue-efi.path'
PARTITION_OFFSET = 2048 * 512
PARTITION_SIZE = 131072 * 512


def runner_hashes():
    return {str(path.relative_to(BASE.parent)): boot.sha256(path)
            for path in (Path(__file__).resolve(), GUEST_SOURCE)}


RUNNER_SOURCES = runner_hashes()


def ram_call(folder, action, timeout=35):
    """Call the RAM observer with a total deadline, recording failed requests too."""
    source = (
        "import sys,json,traceback;sys.path.insert(0,'/opt/vmprobe');import probe\n"
        "try:\n"
        f" value=probe.efi_action({action!r})\n"
        " response={'ok':True,'value':value}\n"
        "except BaseException:\n"
        " response={'ok':False,'error':traceback.format_exc()}\n"
        "print('EFI_REPLY='+json.dumps(response),flush=True)\n"
    )
    encoded = base64.b64encode(source.encode()).decode()
    command = f'python3 -c "import base64;exec(base64.b64decode(\'{encoded}\'))"\n'
    if len(command) >= 4096:
        raise ValueError('VM serial command exceeds the terminal line limit')
    deadline = time.monotonic() + timeout
    record = {'action': action}
    output = b''
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(folder / 'rescue.sock'))
            sock.sendall(b'stty -echo\n')
            time.sleep(.05)
            sock.sendall(command.encode())
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'EFI serial request timed out: {action}')
                sock.settimeout(remaining)
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeError('VM serial shell closed')
                output += chunk
                if len(output) > 4 * 1024 * 1024:
                    raise RuntimeError('EFI serial request exceeded the output limit')
                _, found, reply = output.partition(b'EFI_REPLY=')
                if found and b'\n' in reply:
                    result = json.loads(reply.split(b'\n', 1)[0])
                    record['response'] = result
                    if not result['ok']:
                        raise RuntimeError(result['error'])
                    return result['value']
    except BaseException:
        record['error'] = traceback.format_exc()
        record['output_tail'] = output[-4096:].decode(errors='replace')
        raise
    finally:
        request_failed = sys.exc_info()[0] is not None
        record['host_time'] = time.monotonic()
        try:
            with (folder / 'actions.jsonl').open('a') as stream:
                stream.write(json.dumps(record) + '\n')
        except OSError:
            if not request_failed:
                raise
            print('Could not log failed EFI request:\n' + traceback.format_exc(),
                  file=sys.stderr)


def fsck_start(snapshot):
    match = re.search(r'^ExecMainStartTimestampMonotonic=(\d+)$',
                      snapshot['fsck']['stdout'], re.MULTILINE)
    return int(match[1]) if match else 0


def arrival_during_unmount(snapshot):
    markers = (snapshot.get('delayed_umount') or '').splitlines()
    if len(markers) < 2:
        return False
    start = float(markers[0].split()[1])
    end = float(markers[1].split()[1])
    for event in (snapshot.get('udev_events') or '').split('\n\n'):
        if '\nID_FS_UUID=' + UUID + '\n' not in event:
            continue
        match = re.search(r'^UDEV\s+\[([\d.]+)\]\s+add\s', event, re.MULTILINE)
        if match and start < float(match[1]) < end:
            return True
    return False


def mounted(snapshot):
    return snapshot['units'][MOUNT]['ActiveState'] == 'active'


def validate_inputs(args):
    """Accept only matching lab artifacts before creating any writable images."""
    if os.geteuid() == 0:
        raise ValueError('Run as an ordinary user; host root is never required')
    build_dir = args.build_dir.resolve()
    enrollment = args.enrollment.resolve()
    seed_path = args.seed_report.resolve()
    for path in (build_dir, enrollment, seed_path):
        if not path.is_relative_to(WORK.resolve()):
            raise ValueError('Inputs must resolve below lab/work')
    build = json.loads((build_dir / 'build.json').read_text())
    for name, key in (('initrd.img', 'initramfs_sha256'), ('vmlinuz', 'kernel_sha256')):
        artifact = build_dir / name
        if not artifact.is_file() or not artifact.resolve().is_relative_to(WORK.resolve()):
            raise ValueError('Build images must be regular files below lab/work')
        if boot.sha256(artifact) != build[key]:
            raise ValueError('Build digest differs')
    profile = json.loads(enrollment.read_text())
    seed = json.loads(seed_path.read_text())
    if (boot.sha256(enrollment) != build['enrollment_sha256'] or not seed['passed'] or
            profile['identity'] != seed['cases']['seed']['observation']['gate']['identity'] or
            profile['identity']['usb_serial'] != 'RAMRESCUE-LAB-001' or
            profile['guard']['map_uuid'] != 'RAMRESCUE-HOST-VMTEST'):
        raise ValueError('Only the matching enrolled disposable VM seed is accepted')
    disk = seed_path.parent / 's0/usb.raw'
    if (disk.is_symlink() or not disk.is_file() or
            not disk.resolve().is_relative_to(WORK.resolve())):
        raise ValueError('Seed must be a regular raw file below lab/work')
    before_hash = boot.sha256(disk)
    if before_hash != seed['source_image_sha256_after']:
        raise ValueError('Seed digest differs')
    return build_dir, seed_path, build, disk, before_hash


def create_images(folder, disk):
    overlay = folder / 'usb.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw',
                    '-b', str(disk), str(overlay)], check=True)
    for filename, size in (('decoy.raw', 16 * 1024**2), ('efi.raw', 80 * 1024**2)):
        with (folder / filename).open('xb') as stream:
            stream.truncate(size)
    subprocess.run(
        ['sfdisk', str(folder / 'efi.raw')],
        input=f'label: gpt\nstart=2048,size=131072,type=U,uuid={PARTUUID}\n',
        text=True, stdout=subprocess.DEVNULL, check=True,
    )
    subprocess.run(['mkfs.fat', '--offset=2048', '-F', '32', '-i', UUID.replace('-', ''),
                    '-n', 'EFI-VM-ONLY', str(folder / 'efi.raw'), '65536'], check=True)
    return overlay


def guest_configuration():
    identity = {'ID_FS_UUID': UUID, 'ID_PART_ENTRY_UUID': PARTUUID,
                'ID_USB_SERIAL_SHORT': 'EFI-VM-ONLY'}
    return {'uuid': UUID, 'options': OPTIONS, 'rule': render_rule(identity),
            'path_unit': render_path(), 'fsck_unit': fsck_unit(identity),
            'fsck_dropin': render_fsck()}


def create_initrd(folder, original, config):
    # Reuse the existing RAM observer, without leaving mutated module globals
    # behind for another probe imported in the same Python process.
    original_guest, original_hook = boot.GUEST, boot.HOOK
    try:
        boot.GUEST += '\n' + GUEST_SOURCE.read_text()
        boot.GUEST += f'\nefi_probe = EfiProbe(gate, systemctl, **{config!r})\n'
        boot.GUEST += 'efi_action = efi_probe.action\n'
        # The seed root lacks this newer kernel's optional NLS module. Load the
        # packaged initramfs copy before switch_root, as the real host can.
        boot.HOOK += '\nmodprobe nls_iso8859-1\n'
        return boot.overlay_initrd(folder, original)
    finally:
        boot.GUEST, boot.HOOK = original_guest, original_hook


def vm_command(folder, build_dir, image, overlay, disk):
    masks = ' '.join('systemd.mask=' + name + '.service' for name in
                     ('lab-agent', 'lab-guard', 'lab-shell', 'lab-ready', 'lab-workload'))
    command = qemu_command(
        folder, same_port=True, kernel=build_dir / 'vmlinuz', initramfs=image,
        extra_kernel_args='root=/dev/mapper/labrescue-ubuntu ro nompath ram_rescue_guard=1 ' + masks,
    )
    command[command.index('-m') + 1] = '3072'
    append = command.index('-append') + 1
    command[append] = command[append].replace('rdinit=/init ', '').replace('panic=-1 ', '')
    for index, word in enumerate(command):
        if word == '-blockdev':
            value = json.loads(command[index + 1])
            if value['node-name'] == 'usbdisk':
                value.update(
                    driver='qcow2', file={'driver': 'file', 'filename': str(overlay)},
                    backing={'driver': 'raw', 'read-only': True,
                             'file': {'driver': 'file', 'filename': str(disk)}},
                )
                command[index + 1] = json.dumps(value)
    command += [
        '-blockdev', json.dumps({'driver': 'raw', 'node-name': 'efidisk',
                                'file': {'driver': 'file', 'filename': str(folder / 'efi.raw')}}),
        '-device', 'usb-storage,bus=xhci.0,port=2,id=efistick,drive=efidisk,serial=EFI-VM-ONLY',
    ]
    return command


def wait_for_boot(folder, vm):
    def booted():
        console = folder / 'console.log'
        text = console.read_text(errors='replace') if console.exists() else ''
        if (vm.poll() is not None or 'Kernel panic' in text or
                'RAM Guard protected boot stopped:' in text):
            raise RuntimeError('Protected Ubuntu boot failed')
        return 'VM_HOST_BOOT_SHELL_READY' in text
    wait_for(booted, 180, 'Ubuntu serial observer')


def wait_for_deleted(qmp, device, since):
    wait_for(lambda: any(
        event['host_time'] >= since and event['message'].get('event') == 'DEVICE_DELETED' and
        event['message'].get('data', {}).get('device') == device for event in qmp.events
    ), 10, device + ' USB deletion')


def remove_efi(folder, qmp, *, wait_unmounted=True):
    started = time.monotonic()
    qmp.call('device_del', id='efistick')
    wait_for_deleted(qmp, 'efistick', started)
    if not wait_unmounted:
        return {'deleted_host_time': time.monotonic()}

    def absent():
        snapshot = ram_call(folder, 'snapshot')
        return snapshot if not snapshot['device'] and not mounted(snapshot) else None
    return wait_for(absent, 15, 'systemd device removal')


def add_efi(qmp):
    qmp.call('device_add', driver='usb-storage', bus='xhci.0', port='2',
             id='efistick', drive='efidisk', serial='EFI-VM-ONLY')


def wait_for_mounted(folder):
    def ready():
        snapshot = ram_call(folder, 'snapshot')
        return snapshot if snapshot['device'] and mounted(snapshot) else None
    return wait_for(ready, 25, 'udev mount after EFI USB re-enumeration')


def reconnect_efi(folder, qmp, report):
    report['before_mounted_removal'] = ram_call(folder, 'snapshot')
    report['absent_mounted'] = remove_efi(folder, qmp)
    report['absent_read'] = ram_call(folder, 'read')
    qmp.call('device_add', driver='usb-storage', bus='xhci.0', port='3',
             id='efi-decoy', drive='decoydisk', serial='EFI-DECOY')
    time.sleep(.3)
    add_efi(qmp)
    report['reattached_mounted'] = wait_for_mounted(folder)
    report['recovered_mounted'] = ram_call(folder, 'read')
    report['second_absent'] = remove_efi(folder, qmp, wait_unmounted=False)
    time.sleep(.2)
    add_efi(qmp)
    report['second_reattached'] = wait_for_mounted(folder)
    report['second_recovered'] = ram_call(folder, 'read')


def reconnect_root_and_efi(folder, qmp, report):
    report['delay_umount'] = ram_call(folder, 'delay_umount')
    report['joint_before'] = boot.ram_call(folder, 'snapshot')
    joint_start = time.monotonic()
    qmp.call('device_del', id='stick')
    report['joint_deleted'] = remove_efi(folder, qmp, wait_unmounted=False)
    wait_for_deleted(qmp, 'stick', joint_start)
    time.sleep(.2)
    add_efi(qmp)
    report['joint_efi_added_host_time'] = time.monotonic()
    time.sleep(1)
    qmp.call('device_add', driver='usb-uas', bus='xhci.0', port='1',
             id='stick', serial='RAMRESCUE-LAB-001', attached=False)
    qmp.call('device_add', driver='scsi-hd', bus='stick.0', id='lun', drive='usbdisk')
    qmp.call('qom-set', path='/machine/peripheral/stick', property='attached', value=True)
    report['joint_root_added_host_time'] = time.monotonic()

    def recovered():
        snapshot = boot.ram_call(folder, 'snapshot')
        ready = snapshot['guard_ready'] and snapshot['state'].get('recoveries', 0) >= 1
        return snapshot if ready else None
    report['joint_root_recovered'] = wait_for(recovered, 25, 'protected root recovery')
    report['joint_efi_recovered'] = wait_for_mounted(folder)
    report['joint_read'] = ram_call(folder, 'read')


def acceptance_checks(report):
    expected = report['configure']['expected_sha256']

    def correct(result):
        return result['returncode'] == 0 and result['stdout'].strip() == expected

    repeat = report['repeat_start']
    limit = report['failure_limit']
    fsck_starts = [fsck_start(report['configure']['snapshot'])]
    fsck_starts += [fsck_start(report[key]) for key in
                    ('reattached_mounted', 'second_reattached', 'joint_efi_recovered')]
    joint = report['joint_efi_recovered']
    return {
        'production_guard_ready': report['boot']['guard_ready'],
        'initial_plain_mount': mounted(report['configure']['snapshot']),
        'initial_read_hash': correct(report['initial_read']),
        'absent_access_bounded': report['absent_read']['returncode'] != 0 and report['absent_read']['elapsed'] < 2,
        'mounted_disconnect_unmounted': not mounted(report['absent_mounted']),
        'new_device_name': report['before_mounted_removal']['device'] != report['reattached_mounted']['device'],
        'mounted_disconnect_reconnect_hash': correct(report['recovered_mounted']),
        'second_reconnect_hash': correct(report['second_recovered']),
        'joint_reconnect_hash': correct(report['joint_read']),
        'mounted_repeat_start_keeps_mount': (
            repeat['before']['mounts'] == repeat['after']['mounts'] and correct(repeat['read'])
        ),
        'failure_limit_stops_retries': (
            limit['failed']['units'][PATH_UNIT]['ActiveState'] == 'failed' and
            'limit' in limit['failed']['units'][PATH_UNIT]['Result'] and
            fsck_start(limit['failed']) == fsck_start(limit['quiet'])
        ),
        'failure_limit_manual_mount_and_reset_recovers': (
            correct(limit['read']) and limit['restored']['units'][PATH_UNIT]['ActiveState'] == 'active'
        ),
        'joint_root_guard_ready': report['joint_root_recovered']['guard_ready'],
        'enrolled_link_matches_new_device': joint['enrolled_link'] == joint['device'],
        'path_watch_active': joint['units'][PATH_UNIT]['ActiveState'] == 'active',
        'efi_arrival_overlapped_old_unmount': arrival_during_unmount(joint),
        'fsck_reran_every_reconnect': all(a < b for a, b in zip(fsck_starts, fsck_starts[1:])),
        'joint_same_boot_pid1': (
            report['joint_before']['boot_id'] == report['joint_root_recovered']['boot_id'] and
            report['joint_before']['pid1'] == report['joint_root_recovered']['pid1']
        ),
        'no_autofs_mount': bool(report['second_recovered']['snapshot']['mounts']) and all(
            ' - vfat ' in line for line in report['second_recovered']['snapshot']['mounts']
        ),
        'clean_final_unmount': not mounted(report['stop']['snapshot']),
    }


def run_scenarios(folder, qmp, vm, report):
    wait_for_boot(folder, vm)
    report['boot'] = boot.ram_call(folder, 'snapshot')
    report['configure'] = ram_call(folder, 'configure')
    report['initial_read'] = ram_call(folder, 'read')
    reconnect_efi(folder, qmp, report)
    reconnect_root_and_efi(folder, qmp, report)
    report['repeat_start'] = ram_call(folder, 'repeat_start')
    report['failure_limit'] = ram_call(folder, 'failure_limit')
    report['stop'] = ram_call(folder, 'stop')
    report['checks'] = acceptance_checks(report)
    # Maintenance stop was checked above. Re-enable the watcher before poweroff
    # to exercise native shutdown ordering, too.
    report['pre_shutdown'] = ram_call(folder, 'restart_path')
    report['checks']['watcher_and_mount_active_before_shutdown'] = (
        mounted(report['pre_shutdown']) and
        report['pre_shutdown']['units'][PATH_UNIT]['ActiveState'] == 'active'
    )
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    try:
        vm.wait(timeout=150)
        report['checks']['graceful_shutdown'] = vm.returncode == 0
    except subprocess.TimeoutExpired:
        report['checks']['graceful_shutdown'] = False
    report['passed'] = all(report['checks'].values())


def stop_vm(vm):
    if vm is None or vm.poll() is not None:
        return
    vm.terminate()
    try:
        vm.wait(timeout=10)
    except subprocess.TimeoutExpired:
        vm.kill()
        vm.wait(timeout=10)


def audit_fat(folder):
    with (folder / 'efi.raw').open('rb') as source, (folder / 'efi-partition.raw').open('xb') as target:
        source.seek(PARTITION_OFFSET)
        partition = source.read(PARTITION_SIZE)
        if len(partition) != PARTITION_SIZE:
            raise RuntimeError('Disposable EFI image is shorter than its partition')
        target.write(partition)
    result = subprocess.run(
        ['fsck.fat', '-n', str(folder / 'efi-partition.raw')], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )
    return {'returncode': result.returncode, 'output': result.stdout}


def finalize(folder, disk, before_hash, report, vm=None, qmp=None):
    """Keep every cleanup/audit failure without masking the scenario exception."""
    errors = report.setdefault('cleanup_errors', [])
    for name, cleanup in (('stop_vm', lambda: stop_vm(vm)),
                          ('close_qmp', lambda: qmp.close() if qmp else None)):
        try:
            cleanup()
        except Exception:
            errors.append({'stage': name, 'error': traceback.format_exc()})
    try:
        report['source_image_sha256_after'] = boot.sha256(disk)
        report['source_image_unchanged'] = report['source_image_sha256_after'] == before_hash
    except Exception:
        errors.append({'stage': 'source_digest', 'error': traceback.format_exc()})
    try:
        report['renderer_unchanged'] = boot.sha256(RENDERER) == RENDERER_SHA256
        report['production_sources_unchanged'] = source_hashes() == SOURCE_SHA256
        report['runner_sources_unchanged'] = runner_hashes() == RUNNER_SOURCES
    except Exception:
        errors.append({'stage': 'implementation_digest', 'error': traceback.format_exc()})
    try:
        if vm is not None and vm.poll() is None:
            raise RuntimeError('Refusing a filesystem audit while QEMU is still running')
        report['fat_readonly_audit'] = audit_fat(folder)
    except Exception:
        errors.append({'stage': 'fat_readonly_audit', 'error': traceback.format_exc()})
    report['passed'] = bool(
        report['passed'] and not errors and report.get('source_image_unchanged') and
        report.get('renderer_unchanged') and report.get('production_sources_unchanged') and
        report.get('runner_sources_unchanged') and
        report.get('fat_readonly_audit', {}).get('returncode') == 0
    )
    try:
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('EFI native mount report:', folder / 'report.json', flush=True)
    except Exception:
        report['passed'] = False
        print('Could not write EFI VM report:\n' + traceback.format_exc(), file=sys.stderr)


def capture_failure(folder, report):
    for action in ('snapshot', 'journal'):
        try:
            report['failure_' + action] = ram_call(folder, action)
        except Exception:
            report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    try:
        build_dir, seed_path, build, disk, before_hash = validate_inputs(args)
    except ValueError as error:
        parser.error(str(error))
    folder = WORK / ('efi-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    if len(str(folder / 'rescue.sock').encode()) >= 108:
        parser.error('Socket pathname too long')
    config = guest_configuration()
    report = {
        'schema': 1, 'scope': 'native EFI path-triggered mount',
        'rule_renderer_sha256': RENDERER_SHA256, 'source_sha256': SOURCE_SHA256,
        'build': build, 'runner_sha256': boot.sha256(Path(__file__)),
        'runner_sources': RUNNER_SOURCES, 'options': OPTIONS,
        'fsck_dropin': config['fsck_dropin'], 'udev_rule': config['rule'],
        'path_unit': config['path_unit'], 'seed_report': str(seed_path),
        'source_image_sha256_before': before_hash, 'passed': False,
    }
    print('EFI native mount VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            if source_hashes() != SOURCE_SHA256 or runner_hashes() != RUNNER_SOURCES:
                raise RuntimeError('EFI implementation changed before VM preparation')
            overlay = create_images(folder, disk)
            image = create_initrd(folder, build_dir / 'initrd.img', config)
            command = vm_command(folder, build_dir, image, overlay, disk)
            report['command'] = command
            (folder / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            run_scenarios(folder, qmp, vm, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                capture_failure(folder, report)
            raise
        finally:
            finalize(folder, disk, before_hash, report, vm, qmp)
    if not report['passed']:
        raise SystemExit('EFI native mount acceptance failed')


if __name__ == '__main__':
    # The Python runtime was retired; replay this historical experiment intact.
    from historical import run_legacy
    raise SystemExit(run_legacy(__file__))
