#!/usr/bin/env python3
"""Check persistent, event-driven aftercare in a disposable Ubuntu VM.

The VM fixture owns creation of its disposable multipath maps. Production
aftercare owns their registered recovery controllers. No host device enters
QEMU, and the previously validated root image remains a read-only backing.
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import traceback

from auto_run import wait_for
import data_guard_probe as data
import host_boot_probe as boot
from efi_mount_probe import validate_inputs, wait_for_boot, stop_vm
from run import Channel, WORK


BASE = Path(__file__).resolve().parent
REPO = BASE.parent
GUEST = BASE / 'guest/unified_probe.py'
SPECS = data.SPECS


def source_hashes():
    paths = [Path(__file__).resolve(), GUEST, data.GUEST,
             Path(data.__file__).resolve(), Path(boot.__file__).resolve(),
             REPO / 'ram-rescue-demo/src/rescue.py',
             *sorted((REPO / 'guard').glob('*.py')),
             *sorted((REPO / 'guard/runtime').glob('*.py')),
             *sorted(p for p in (REPO / 'guard/integration').rglob('*') if p.is_file())]
    return {str(path.relative_to(REPO)): boot.sha256(path) for path in paths}


def create_initrd(folder, original):
    """Reuse the data fixture, replacing only its VM orchestration subclass."""
    combined = folder / 'unified-guest.py'
    combined.write_text(data.GUEST.read_text() + '\n' + GUEST.read_text() +
                        '\nDataProbe = UnifiedProbe\n')
    previous = data.GUEST
    try:
        data.GUEST = combined
        image = data.create_initrd(folder, original)
    finally:
        data.GUEST = previous
    staging = folder / 'unified-overlay'
    launcher = staging / 'opt/data-launcher-src/guard'
    launcher.mkdir(parents=True)
    for source in (REPO / 'guard').glob('*.py'):
        shutil.copyfile(source, launcher / source.name)
    if (REPO / 'guard/admin').is_dir():
        shutil.copytree(REPO / 'guard/admin', launcher / 'admin',
                        ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(REPO / 'guard/integration', launcher / 'integration')
    paths = [Path('.'), *sorted(path.relative_to(staging) for path in staging.rglob('*'))]
    archive = subprocess.run(
        ['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=staging,
        input=b'\0'.join(str(path).encode() for path in paths) + b'\0',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    with image.open('ab') as target:
        target.write(gzip.compress(archive, mtime=0))
    return image


def guarded_ready(folder, names, minimum_recoveries=0):
    def check():
        snapshot = data.ram_call(folder, 'snapshot')
        for name in names:
            state = snapshot['devices'][name]['state'] or {}
            if state.get('state') in ('expired', 'failed', 'blocked', 'interrupted'):
                raise RuntimeError('Managed map reached terminal outcome: ' + json.dumps(state))
            if state.get('state') != 'ready' or state.get('recoveries', 0) < minimum_recoveries:
                return None
        return snapshot
    return wait_for(check, 30, 'registered map controllers ready')


def root_unchanged(before, after):
    old = before['services']['ram-rescue-guard']['process']
    new = after['services']['ram-rescue-guard']['process']
    return (before['guard_ready'] and after['guard_ready'] and
            old['pid'] == new['pid'] and old['start_ticks'] == new['start_ticks'] and
            before['state']['recoveries'] == after['state']['recoveries'])


def run_scenarios(folder, qmp, vm, report):
    wait_for_boot(folder, vm)
    report['root_before'] = boot.ram_call(folder, 'snapshot')
    report['configure'] = data.ram_call(folder, 'configure', timeout=90)
    ext4, vfat = SPECS
    data.remove(qmp, vfat)
    report['activate'] = data.ram_call(folder, 'activate', timeout=90)
    report['initial'] = guarded_ready(folder, [ext4['name']])
    report['late_absent'] = data.ram_call(folder, 'manager_status')
    data.attach(qmp, vfat)
    report['late_map_created'] = data.ram_call(folder, 'create_late_map')
    report['late_ready'] = guarded_ready(folder, [spec['name'] for spec in SPECS])
    report['mounted'] = data.ram_call(folder, 'mount_registered')
    report['root_worker'] = boot.ram_call(folder, 'start_workload')
    report['workers_started'] = data.ram_call(folder, 'start')
    names = [spec['name'] for spec in SPECS]
    report['before'] = data.ready(folder, dict.fromkeys(names, 0),
                                  minimum_acks=dict.fromkeys(names, 5))
    removed = data.remove(qmp, ext4)
    qmp.call('device_add', driver='usb-storage', bus='xhci.0', port='4',
             id='unregistered-usb', drive='decoydisk', serial='UNREGISTERED-USB')
    time.sleep(max(0, .2 - (time.monotonic() - removed)))
    attached = data.attach(qmp, ext4)
    report['gap'] = {'removed': removed, 'attached': attached, 'seconds': attached - removed}
    report['recovered'] = data.ready(folder, {ext4['name']: 1, vfat['name']: 0},
        minimum_acks={name: report['before']['devices'][name]['ack_count'] + 3 for name in names})
    report['manager_after_recovery'] = data.ram_call(folder, 'manager_status')
    report['root_after'] = boot.ram_call(folder, 'snapshot')
    report['audit'] = data.ram_call(folder, 'audit')
    report['root_audit'] = boot.ram_call(folder, 'stop_and_audit')
    report['logs'] = data.ram_call(folder, 'logs')
    report['boot_fixture'] = data.ram_call(folder, 'install_boot_fixture')
    report['prepare_shutdown'] = data.ram_call(folder, 'prepare_shutdown')
    before, after = report['before'], report['recovered']
    checks = {
        'root_controller_not_replaced': root_unchanged(report['root_before'], report['root_after']),
        'root_registration_recognizes_existing_owner': report['configure']['root_adopted']['value']['state'] == 'already_managed',
        'original_boot_and_pid1': before['boot_id'] == after['boot_id'] and before['pid1']['start_ticks'] == after['pid1']['start_ticks'],
        'usb_path_reenumerated': before['devices'][ext4['name']]['slaves'] != after['devices'][ext4['name']]['slaves'],
        'root_durable_data_matches': report['root_audit']['prefix_matches'],
        'root_still_writable': report['root_audit']['root_write_fsync'],
        'unknown_usb_present_and_no_new_map_created': (
            'UNREGISTERED-USB' in report['manager_after_recovery']['usb_serials'] and
            set(report['root_after']['mappings']) == set(report['root_before']['mappings']) | set(names)),
        'exactly_one_controller_per_map': len(report['manager_after_recovery']['controllers']) == 3,
        'unified_status_includes_all_three_maps': (
            len(report['manager_after_recovery']['status']['value']['devices']) == 3 and
            all(entry['state'] == 'ready' for entry in report['manager_after_recovery']['status']['value']['devices'])),
        'raw_and_stable_maps_excluded_from_automount': len(report['manager_after_recovery']['exclusions']) == 2 and all(
            'E:UDISKS_IGNORE=1' in values['raw'].splitlines() and
            'E:UDISKS_IGNORE=1' in values['map'].splitlines() and
            'E:DM_NOSCAN=1' in values['map'].splitlines()
            for values in report['manager_after_recovery']['exclusions'].values()),
        'absent_registered_map_did_not_start': not report['initial']['devices'][vfat['name']]['state'],
        'late_registered_map_guard_started': report['late_ready']['devices'][vfat['name']]['state']['state'] == 'ready',
    }
    for name in names:
        old, new = before['devices'][name], after['devices'][name]
        checks[name + '_original_open_fd_process'] = old['process']['pid'] == new['process']['pid'] and old['process']['start_ticks'] == new['process']['start_ticks']
        checks[name + '_mount_survived'] = old['mount'] == new['mount']
        checks[name + '_no_io_errors'] = not new['errors']
        checks[name + '_durable_data_matches'] = report['audit']['audit'][name]['hash_matches']
    report['checks'] = checks
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    vm.wait(timeout=150)
    checks['first_shutdown_clean'] = vm.returncode == 0
    report['passed'] = all(checks.values())


def reboot_acceptance(folder, command, log, qlog, report):
    """Cold-boot the same writable guest overlay with both test disks present."""
    console = folder / 'console.log'
    if console.exists():
        console.rename(folder / 'first-boot-console.log')
    for name in ('qmp.sock', 'rescue.sock', 'agent.sock'):
        (folder / name).unlink(missing_ok=True)
    vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    qmp = None
    try:
        wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'second-boot QMP socket')
        qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
        wait_for_boot(folder, vm)
        report['second_boot_root'] = boot.ram_call(folder, 'snapshot')
        report['second_boot_ready'] = guarded_ready(folder, [spec['name'] for spec in SPECS])
        report['second_boot_manager'] = data.ram_call(folder, 'manager_status')
        checks = report['checks']
        checks['new_boot_id'] = report['second_boot_root']['boot_id'] != report['root_before']['boot_id']
        checks['root_protected_after_reboot'] = report['second_boot_root']['guard_ready']
        checks['registered_controllers_auto_start_after_reboot'] = all(
            (entry['state'] or {}).get('state') == 'ready'
            for entry in report['second_boot_ready']['devices'].values())
        report['second_shutdown'] = boot.ram_call(folder, 'shutdown')
        vm.wait(timeout=150)
        checks['second_shutdown_clean'] = vm.returncode == 0
        report['passed'] = all(checks.values())
    except BaseException:
        report['passed'] = False
        for action in ('snapshot', 'logs', 'manager_status'):
            try:
                report['second_failure_' + action] = data.ram_call(folder, action)
            except Exception:
                report.setdefault('diagnostic_errors', {})['second_' + action] = traceback.format_exc()
        raise
    finally:
        stop_vm(vm)
        if qmp:
            qmp.close()


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
    folder = WORK / ('unified-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = source_hashes()
    report = {'schema': 1, 'scope': 'persistent registered-map aftercare with native systemd and udev',
              'build': build, 'seed_report': str(seed_path), 'source_sha256': sources,
              'source_image_sha256_before': before_hash, 'specs': SPECS, 'passed': False}
    print('Unified aftercare VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            image = create_initrd(folder, build_dir / 'initrd.img')
            if source_hashes() != sources:
                raise RuntimeError('Implementation changed during VM preparation')
            command = data.vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            run_scenarios(folder, qmp, vm, report)
            qmp.close()
            qmp = None
            reboot_acceptance(folder, command, log, qlog, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                for action in ('snapshot', 'logs', 'manager_status'):
                    try:
                        report['failure_' + action] = data.ram_call(folder, action)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            stop_vm(vm)
            if qmp:
                qmp.close()
            report['source_image_sha256_after'] = boot.sha256(seed)
            report['source_image_unchanged'] = report['source_image_sha256_after'] == before_hash
            report['sources_unchanged'] = source_hashes() == sources
            try:
                report['readonly_filesystem_audit'] = data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged']
                and all(audit['returncode'] == 0 for audit in report.get('readonly_filesystem_audit', {}).values())
                and not report.get('filesystem_audit_error'))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Unified aftercare report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Unified aftercare acceptance failed')


if __name__ == '__main__':
    # The Python runtime was retired; replay this historical experiment intact.
    from historical import run_legacy
    raise SystemExit(run_legacy(__file__))
