#!/usr/bin/env python3
"""Exercise protected mounts and submounts in a disposable full Ubuntu VM."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

from auto_run import wait_for
import data_guard_probe as data
import host_boot_probe as boot
import unified_guard_probe as unified
from efi_mount_probe import validate_inputs, wait_for_boot, wait_for_deleted, stop_vm
from run import Channel, WORK

BASE = Path(__file__).resolve().parent
GUEST = BASE / 'guest/mount_probe.py'


def source_hashes():
    return {**unified.source_hashes(), **{
        str(path.relative_to(BASE.parent)): boot.sha256(path)
        for path in (Path(__file__).resolve(), GUEST)}}


def create_initrd(folder, original, kernel_release):
    combined = folder / 'mount-guest.py'
    combined.write_text(unified.GUEST.read_text() + '\n' + GUEST.read_text() +
                        '\nUnifiedProbe = MountProbe\n')
    previous, previous_hook = unified.GUEST, boot.HOOK
    try:
        unified.GUEST = combined
        # init-bottom runs before PID 1 starts the protected root controller.
        # Its real boot-time map activation is retained, while all three live
        # controllers execute the same currently staged recovery runtime.
        boot.HOOK += ('\ncp /opt/data-guard/*.py "$TOOLS/opt/guard/"\n'
                      'insmod /opt/vm-autofs.ko\n')
        image = unified.create_initrd(folder, original)
    finally:
        unified.GUEST, boot.HOOK = previous, previous_hook
    # The intentionally small seed omitted autofs. Load the matching kernel
    # module in this disposable initrd before PID 1 assesses automount support.
    module = Path(subprocess.check_output(
        ['modinfo', '-k', kernel_release, '-F', 'filename', 'autofs4'], text=True).strip())
    staging = folder / 'autofs-overlay'
    (staging / 'opt').mkdir(parents=True)
    content = subprocess.check_output(['zstd', '-dc', str(module)]) if module.suffix == '.zst' else module.read_bytes()
    (staging / 'opt/vm-autofs.ko').write_bytes(content)
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'],
        cwd=staging, input=b'.\0opt\0opt/vm-autofs.ko\0', check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    with image.open('ab') as target:
        target.write(gzip.compress(archive, mtime=0))
    return image


def progressing(folder, counts, previous=None):
    def check():
        snapshot = data.ram_call(folder, 'snapshot')
        for name, device in snapshot['devices'].items():
            state = device['state'] or {}
            if state.get('state') in ('expired', 'failed', 'blocked', 'interrupted'):
                raise RuntimeError('Mount backing controller reached terminal state: ' + json.dumps(state))
            if (state.get('state') != 'ready' or state.get('recoveries', 0) < counts[name]
                    or device['ack_count'] < (previous['devices'][name]['ack_count'] + 3 if previous else 5)):
                return None
            if device['errors']:
                raise RuntimeError('Primary workload saw an I/O error')
        for name, child in snapshot['children'].items():
            if child['errors']:
                raise RuntimeError('Child workload saw an I/O error')
            if child['acks'] < (previous['children'][name]['acks'] + 3 if previous else 5):
                return None
        return snapshot
    return wait_for(check, 30, 'same mount graph and child workloads progressing')


def reconnect_root(qmp):
    removed = time.monotonic()
    qmp.call('device_del', id='stick')
    wait_for_deleted(qmp, 'stick', removed)
    time.sleep(.2)
    qmp.call('device_add', driver='usb-uas', bus='xhci.0', port='1', id='stick',
             serial='RAMRESCUE-LAB-001', attached=False)
    qmp.call('device_add', driver='scsi-hd', bus='stick.0', id='lun', drive='usbdisk')
    qmp.call('qom-set', path='/machine/peripheral/stick', property='attached', value=True)
    return {'removed': removed, 'attached': time.monotonic()}


def run_scenarios(folder, qmp, vm, report):
    wait_for_boot(folder, vm)
    def userspace_ready():
        value = boot.ram_call(folder, 'snapshot')
        return value if (value['guard_ready'] and value['multi_user'] == 'active'
                         and value['services']['dbus']['ActiveState'] == 'active') else None
    report['root_before'] = wait_for(userspace_ready, 60, 'Ubuntu userspace and D-Bus ready')
    report['configure'] = data.ram_call(folder, 'configure', timeout=90)
    report['activate'] = data.ram_call(folder, 'activate', timeout=90)
    names = [spec['name'] for spec in data.SPECS]
    report['ready'] = unified.guarded_ready(folder, names)
    report['plan'] = data.ram_call(folder, 'install_mount_plan')
    report['mounted'] = data.ram_call(folder, 'mount_registered')
    report['root_worker'] = boot.ram_call(folder, 'start_workload')
    report['workers'] = data.ram_call(folder, 'start')
    report['before'] = progressing(folder, dict.fromkeys(names, 0))
    ext4, vfat = data.SPECS
    data.remove(qmp, ext4)
    time.sleep(.2)
    data.attach(qmp, ext4)
    report['ext4_recovered'] = progressing(folder, {names[0]: 1, names[1]: 0}, report['before'])
    data.remove(qmp, vfat)
    time.sleep(.2)
    data.attach(qmp, vfat, wrong=True)
    def rejected():
        value = data.ram_call(folder, 'snapshot')
        reason = (value['devices'][names[1]]['state'] or {}).get('reason', '')
        return value if 'UUID does not match' in reason else None
    report['wrong_identity'] = wait_for(rejected, 5, 'wrong UUID admission refusal')
    data.remove(qmp, vfat)
    data.attach(qmp, vfat)
    report['vfat_recovered'] = progressing(folder, dict.fromkeys(names, 1), report['ext4_recovered'])
    report['root_gap'] = reconnect_root(qmp)
    def root_ready():
        value = boot.ram_call(folder, 'snapshot')
        state = value['state'] or {}
        if state.get('state') in ('expired', 'failed', 'blocked', 'interrupted'):
            raise RuntimeError('Root controller reached terminal state: ' + json.dumps(state))
        return value if value['guard_ready'] and state.get('recoveries', 0) >= 1 else None
    report['root_after'] = wait_for(root_ready, 30, 'original root controller recovered')
    report['after'] = progressing(folder, dict.fromkeys(names, 1), report['vfat_recovered'])
    report['audit'] = data.ram_call(folder, 'audit')
    report['root_audit'] = boot.ram_call(folder, 'stop_and_audit')
    report['readonly'] = data.ram_call(folder, 'readonly_policy')
    data.remove(qmp, vfat)
    time.sleep(.2)
    data.attach(qmp, vfat)
    report['readonly_recovered'] = unified.guarded_ready(folder, [names[1]], minimum_recoveries=2)
    report['manager'] = data.ram_call(folder, 'manager_status')
    data.remove(qmp, vfat)
    def expired():
        value = data.ram_call(folder, 'snapshot')
        return value if (value['devices'][names[1]]['state'] or {}).get('state') == 'expired' else None
    report['terminal'] = wait_for(expired, 20, 'terminal no-path outcome with closed workers')
    # A later mount request may not erase the old terminal recovery transaction.
    data.attach(qmp, vfat)
    report['terminal_mount_start'] = data.ram_call(folder, 'terminal_mount_start', timeout=50)
    report['logs'] = data.ram_call(folder, 'logs')
    report['unmounted'] = data.ram_call(folder, 'prepare_shutdown')
    before, after = report['before'], report['after']
    readonly_path = '/mnt/rr-data-vm-ext4/nested-vfat'
    readonly_mount = report['readonly_recovered']['mount_graph'][readonly_path]
    checks = {
        'unit_syntax_verified_by_guest_systemd': report['plan']['verify']['returncode'] == 0,
        'initial_mount_absent_until_automount_access': not report['plan']['snapshot']['mount_graph']['/mnt/rr-data-vm-ext4'],
        'initial_mount_and_nested_mounts_created': all(report['mounted']['mount_graph'].values()),
        'all_original_mount_ids_and_options_preserved': before['mount_graph'] == after['mount_graph'],
        'wrong_identity_kept_original_mounts': report['wrong_identity']['mount_graph'] == before['mount_graph'],
        'wrong_identity_was_not_committed': report['wrong_identity']['devices'][names[1]]['state']['recoveries'] == 0,
        'all_three_controllers_maintained': len(report['manager']['controllers']) == 3,
        'same_boot_and_pid1': before['boot_id'] == after['boot_id'] and before['pid1']['start_ticks'] == after['pid1']['start_ticks'],
        'same_root_guard_process': all(
            report['root_before']['services']['ram-rescue-guard']['process'][key] ==
            report['root_after']['services']['ram-rescue-guard']['process'][key]
            for key in ('pid', 'start_ticks')),
        'root_recovered_and_writable': report['root_after']['guard_ready'] and report['root_audit']['root_write_fsync'],
        'root_durable_data_matches': report['root_audit']['prefix_matches'],
        'readonly_mount_not_changed_by_start': report['readonly']['before']['mount_graph'] == report['readonly']['after']['mount_graph'],
        'readonly_mount_still_readonly_after_recovery': 'ro' in readonly_mount.split()[5].split(','),
        'terminal_guard_prevents_new_mount': report['terminal_mount_start']['start']['returncode'] != 0 and
            not report['terminal_mount_start']['snapshot']['mount_graph'][readonly_path],
        'orderly_unmounts_completed': not any(report['unmounted']['mount_graph'].values()),
    }
    for name in names:
        old, new = before['devices'][name], after['devices'][name]
        checks[name + '_original_open_fd_process'] = old['process']['pid'] == new['process']['pid'] and old['process']['start_ticks'] == new['process']['start_ticks']
        checks[name + '_no_io_errors'] = not new['errors']
        checks[name + '_durable_data_matches'] = report['audit']['audit'][name]['hash_matches']
    for name in before['children']:
        old, new = before['children'][name], after['children'][name]
        checks[name + '_original_open_fd_process'] = old['process']['pid'] == new['process']['pid'] and old['process']['start_ticks'] == new['process']['start_ticks']
        checks[name + '_no_io_errors'] = not new['errors']
        checks[name + '_durable_data_matches'] = report['audit']['children'][name]['hash_matches']
    report['checks'] = checks
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    vm.wait(timeout=150)
    checks['shutdown_clean'] = vm.returncode == 0
    report['passed'] = all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    try:
        build_dir, seed_path, build, seed, before_hash = validate_inputs(args)
    except ValueError as error:
        parser.error(str(error))
    folder = WORK / ('mounts-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = source_hashes()
    report = {'schema': 1, 'scope': 'native mount and child-bind retention across registered USB reconnects',
              'build': build, 'seed_report': str(seed_path), 'source_sha256': sources,
              'source_image_sha256_before': before_hash, 'passed': False}
    print('Mount acceptance VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            kernel_release = json.loads(args.enrollment.read_text())['guard']['kernel_release']
            image = create_initrd(folder, build_dir / 'initrd.img', kernel_release)
            if source_hashes() != sources:
                raise RuntimeError('Sources changed during VM preparation')
            report['command'] = data.vm_command(folder, build_dir, image, overlay, seed)
            vm = subprocess.Popen(report['command'], stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            run_scenarios(folder, qmp, vm, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                for action in ('snapshot', 'logs'):
                    try:
                        report['failure_' + action] = data.ram_call(folder, action)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            stop_vm(vm)
            if qmp:
                qmp.close()
            report['source_image_unchanged'] = boot.sha256(seed) == before_hash
            report['sources_unchanged'] = source_hashes() == sources
            try:
                report['readonly_filesystem_audit'] = data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged']
                and all(value['returncode'] == 0 for value in report.get('readonly_filesystem_audit', {}).values())
                and not report.get('filesystem_audit_error'))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Mount acceptance report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Mount acceptance failed')


if __name__ == '__main__':
    # The Python runtime was retired; replay this historical experiment intact.
    from historical import run_legacy
    raise SystemExit(run_legacy(__file__))
