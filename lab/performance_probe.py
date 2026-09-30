#!/usr/bin/env python3
"""Compare the original/current controller in fresh, otherwise identical VMs.

All three controllers run the selected payload, including the root controller.
The production enrollment and initramfs boot sequence are unchanged; a VM-only
init-bottom hook replaces the runtime before systemd starts any controller.
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

import data_guard_probe as data
import host_boot_probe as boot
import unified_guard_probe as unified
from auto_run import wait_for
from efi_mount_probe import validate_inputs, wait_for_boot, wait_for_deleted, stop_vm
from measure_guard import MEMORY_HELPERS
from run import Channel, WORK

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
BASELINE = '779869cf50842e6e8f5c8fae265be8629410ea49'
BASELINE_FILES = ('dm_monitor.py', 'path_guard.py', 'owned_operation.py')


def create_initrd(folder, original, variant):
    combined = folder / 'performance-guest.py'
    combined.write_text(unified.GUEST.read_text() + '\n' +
                        (BASE / 'guest/performance_probe.py').read_text() +
                        '\nUnifiedProbe = PerformanceProbe\n')
    previous_guest, previous_hook = unified.GUEST, boot.HOOK
    try:
        unified.GUEST = combined
        boot.HOOK += ('\ncp /opt/data-guard/*.py "$TOOLS/opt/guard/"\n'
                      'cp /opt/performance-memory.py "$TOOLS/opt/vmprobe/memory_helpers.py"\n')
        image = unified.create_initrd(folder, original)
    finally:
        unified.GUEST, boot.HOOK = previous_guest, previous_hook
    staging = folder / 'performance-overlay'
    staging.mkdir()
    (staging / 'opt').mkdir()
    (staging / 'opt/performance-memory.py').write_text(MEMORY_HELPERS)
    if variant == 'baseline':
        for name in BASELINE_FILES:
            content = subprocess.check_output(['git', 'show', f'{BASELINE}:guard/runtime/{name}'], cwd=REPO)
            for relative in ('opt/data-guard', 'opt/data-launcher-src/guard/runtime'):
                target = staging / relative / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    paths = [Path('.'), *sorted(path.relative_to(staging) for path in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=staging,
                             input=b'\0'.join(str(path).encode() for path in paths) + b'\0',
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    with image.open('ab') as output:
        output.write(gzip.compress(archive, mtime=0))
    return image


def reconnect_all(qmp):
    started = time.monotonic()
    requested = {'root': started}
    qmp.call('device_del', id='stick')
    for spec in data.SPECS:
        requested[spec['name']] = time.monotonic()
        data.remove(qmp, spec)
    wait_for_deleted(qmp, 'stick', started)
    devices = {'root': 'stick', **{spec['name']: spec['name'] for spec in data.SPECS}}
    removed = {name: next(event['host_time'] for event in qmp.events
                         if event['host_time'] >= requested[name]
                         and event['message'].get('event') == 'DEVICE_DELETED'
                         and event['message'].get('data', {}).get('device') == device)
               for name, device in devices.items()}
    time.sleep(.2)
    # A different USB port exercises discovery after the healthy event scope is cleared.
    qmp.call('device_add', driver='usb-uas', bus='xhci.0', port='4',
             id='stick', serial='RAMRESCUE-LAB-001', attached=False)
    qmp.call('device_add', driver='scsi-hd', bus='stick.0', id='lun', drive='usbdisk')
    qmp.call('qom-set', path='/machine/peripheral/stick', property='attached', value=True)
    attached = {'root': time.monotonic()}
    for spec in data.SPECS:
        attached[spec['name']] = data.attach(qmp, spec)
    return {name: {'removed': removed[name], 'attached': attached[name],
                   'delete_requested': requested[name],
                   'gap_seconds': attached[name] - removed[name],
                   'clock': 'host monotonic: QMP deletion event received to attach reply received'}
            for name in removed}


def quiet_recovery_result(folder, report):
    """Keep serial status requests outside the recovery sampler's 16s window."""
    observation = {'clock': 'host monotonic', 'quiet_start': time.monotonic()}
    report['recovery_rpc_quiet_window'] = observation
    # The sampler was launched before the fault. Waiting its full duration
    # again after reattachment leaves a margin for startup and guest scheduling.
    time.sleep(report['quiet_wait_seconds'])
    observation['quiet_end'] = time.monotonic()
    result = data.ram_call(folder, 'sampler_result')
    observation['first_fetch_completed'] = bool(result)
    if not result:
        raise RuntimeError('Quiet recovery sampler was incomplete at the first fetch')
    return result


def scenarios(folder, qmp, vm, report):
    wait_for_boot(folder, vm)
    def userspace_ready():
        snapshot = boot.ram_call(folder, 'snapshot')
        return snapshot if snapshot['services']['dbus']['ActiveState'] == 'active' else None
    report['root_before'] = wait_for(userspace_ready, 40, 'Ubuntu D-Bus ready for systemd-run')
    report['configure'] = data.ram_call(folder, 'configure', timeout=90)
    report['activate'] = data.ram_call(folder, 'activate', timeout=90)
    report['late'] = data.ram_call(folder, 'create_late_map')
    unified.guarded_ready(folder, [spec['name'] for spec in data.SPECS])
    data.ram_call(folder, 'mount_registered')
    report['budget'] = data.ram_call(folder, 'quota' + str(report['quota_percent']))
    report['runtime_before'] = data.ram_call(folder, 'runtime_hashes')
    expected = report['runtime_payload_sha256']
    if any(any(hashes.get(name) != digest for name, digest in expected.items())
           for hashes in report['runtime_before'].values()):
        raise RuntimeError('Running root/data runtime differs from the selected experiment payload')
    report['healthy_warmup_seconds'] = 3
    time.sleep(report['healthy_warmup_seconds'])
    report['phases'] = []
    for phase in ('idle', 'unrelated', 'relevant'):
        print('Measuring:', phase, flush=True)
        report['phases'].append(data.ram_call(folder, phase, timeout=40))
    report['root_worker'] = boot.ram_call(folder, 'start_workload')
    data.ram_call(folder, 'start')
    names = [spec['name'] for spec in data.SPECS]
    report['before'] = data.ready(folder, dict.fromkeys(names, 0), minimum_acks=dict.fromkeys(names, 5))
    report['sampler'] = data.ram_call(folder, 'start_sampler')
    time.sleep(1)
    print('Injecting simultaneous root/data USB loss', flush=True)
    report['fault'] = reconnect_all(qmp)
    recovery_phase = quiet_recovery_result(folder, report) if report.get('recovery_rpc_quiet', False) else None
    report['after'] = data.ready(folder, dict.fromkeys(names, 1),
        minimum_acks={name: report['before']['devices'][name]['ack_count'] + 3 for name in names})
    def root_ready():
        snapshot = boot.ram_call(folder, 'snapshot')
        return snapshot if snapshot['guard_ready'] and snapshot['state']['recoveries'] >= 1 else None
    report['root_after'] = wait_for(root_ready, 25, 'root recovered alongside both data maps')
    report['phases'].append(recovery_phase if recovery_phase is not None else
                           wait_for(lambda: data.ram_call(folder, 'sampler_result'), 25, 'recovery accounting'))
    report['audit'] = data.ram_call(folder, 'audit')
    report['root_audit'] = boot.ram_call(folder, 'stop_and_audit')
    report['logs'] = data.ram_call(folder, 'logs')
    report['runtime_after'] = data.ram_call(folder, 'runtime_hashes')
    checks = {
        'original_boot': report['before']['boot_id'] == report['after']['boot_id'],
        'root_recovered': report['root_after']['guard_ready'],
        'runtime_payload_unchanged': report['runtime_before'] == report['runtime_after'],
        'same_root_guard_process': all(report['root_before']['services']['ram-rescue-guard']['process'][key] ==
                                       report['root_after']['services']['ram-rescue-guard']['process'][key]
                                       for key in ('pid', 'start_ticks')),
        'exactly_one_root_recovery': report['root_after']['state']['recoveries'] == 1,
        'root_original_process': report['root_worker']['start_ticks'] == report['root_after']['worker_process']['start_ticks'],
        'root_original_mount': report['root_before']['root_mount'] == report['root_after']['root_mount'],
        'root_durable_data': report['root_audit']['prefix_matches'],
        'root_writable': report['root_audit']['root_write_fsync'],
    }
    for name in names:
        before, after = report['before']['devices'][name], report['after']['devices'][name]
        checks[name + '_original_process'] = before['process']['pid'] == after['process']['pid'] and before['process']['start_ticks'] == after['process']['start_ticks']
        checks[name + '_original_mount'] = before['mount'] == after['mount']
        checks[name + '_zero_io_errors'] = not after['errors']
        checks[name + '_durable_data'] = report['audit']['audit'][name]['hash_matches']
        checks[name + '_same_owner'] = before['state']['owner_epoch'] == after['state']['owner_epoch']
        checks[name + '_one_recovery'] = before['state']['recoveries'] == 0 and after['state']['recoveries'] == 1
    checks['all_recovery_windows_observed'] = len(report['phases'][-1].get('recovery_windows', {})) == 4
    report['checks'] = checks
    data.ram_call(folder, 'prepare_shutdown')
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    vm.wait(timeout=150)
    checks['clean_shutdown'] = vm.returncode == 0
    report['passed'] = all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('baseline', 'current'), required=True)
    parser.add_argument('--quota-percent', type=int, choices=(5, 10, 20), default=20,
                        help='VM-only quota for root and the aggregate data slice')
    parser.add_argument('--quiet-recovery', action='store_true',
                        help='Wait 17s after reattachment before serial recovery status requests')
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    build_dir, seed_path, build, seed, before_hash = validate_inputs(args)
    # Leave room for rescue.sock within sockaddr_un's 108-byte pathname limit.
    folder = WORK / ('perf-' + args.variant[0] + str(args.quota_percent) + '-' +
                     time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = unified.source_hashes()
    sources.update({str(path.relative_to(REPO)): boot.sha256(path) for path in
                    (Path(__file__), BASE / 'guest/performance_probe.py', BASE / 'measure_guard.py')})
    report = {'schema': 1, 'variant': args.variant, 'baseline_ref': BASELINE,
              'baseline_override_files': list(BASELINE_FILES),
              'quota_percent': args.quota_percent,
              'recovery_rpc_quiet': args.quiet_recovery,
              'quiet_wait_seconds': 17 if args.quiet_recovery else 0,
              'source_sha256': sources, 'source_image_sha256_before': before_hash,
              'build': build, 'scope': 'all three Guard cgroups, descendants included; sampler and kernel workers excluded; one CPU = 100%',
              'passed': False}
    print('Performance VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            image = create_initrd(folder, build_dir / 'initrd.img', args.variant)
            report['initrd_sha256'] = boot.sha256(image)
            payload = folder / 'data-overlay/opt/data-guard'
            overrides = folder / 'performance-overlay/opt/data-guard'
            report['runtime_payload_sha256'] = {
                path.name: boot.sha256(overrides / path.name if (overrides / path.name).exists() else path)
                for path in sorted(payload.glob('*.py'))}
            command = data.vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            scenarios(folder, qmp, vm, report)
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
            report['sources_unchanged'] = all(boot.sha256(REPO / path) == digest for path, digest in sources.items())
            try:
                report['filesystem_audits'] = data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged']
                                    and not report.get('filesystem_audit_error')
                                    and all(audit['returncode'] == 0 for audit in report.get('filesystem_audits', {}).values()))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Performance report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Performance acceptance failed')


if __name__ == '__main__':
    main()
