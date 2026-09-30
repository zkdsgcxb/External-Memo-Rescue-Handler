#!/usr/bin/env python3
"""Trace CPU accounting during repeated USB loss in a disposable Ubuntu VM.

The sampler and tracing deliberately add overhead. Results explain scheduling;
they are not measurements of production Guard resource consumption.
"""
import argparse
import base64
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
from efi_mount_probe import validate_inputs, wait_for_boot, stop_vm
from performance_probe import reconnect_all
from run import Channel, WORK

BASE = Path(__file__).resolve().parent
REPO = BASE.parent


def create_initrd(folder, original):
    combined = folder / 'diagnostic-guest.py'
    combined.write_text(unified.GUEST.read_text() + '\n' +
                        (BASE / 'guest/cpu_diagnostic.py').read_text() +
                        '\nUnifiedProbe = CpuDiagnostic\n')
    previous_guest, previous_hook = unified.GUEST, boot.HOOK
    try:
        unified.GUEST = combined
        boot.HOOK += '\ncp /opt/data-guard/*.py "$TOOLS/opt/guard/"\n'
        return unified.create_initrd(folder, original)
    finally:
        unified.GUEST, boot.HOOK = previous_guest, previous_hook


def scenarios(folder, qmp, vm, report, rounds):
    wait_for_boot(folder, vm)
    def userspace_ready():
        snapshot = boot.ram_call(folder, 'snapshot')
        return snapshot if snapshot['services']['dbus']['ActiveState'] == 'active' else None
    report['root_before'] = wait_for(userspace_ready, 40, 'Ubuntu userspace ready')
    report['configure'] = data.ram_call(folder, 'configure', timeout=90)
    report['activate'] = data.ram_call(folder, 'activate', timeout=90)
    report['late'] = data.ram_call(folder, 'create_late_map')
    names = [spec['name'] for spec in data.SPECS]
    unified.guarded_ready(folder, names)
    data.ram_call(folder, 'mount_registered')
    report['root_worker'] = boot.ram_call(folder, 'start_workload')
    data.ram_call(folder, 'start')
    report['before'] = data.ready(folder, dict.fromkeys(names, 0),
                                  minimum_acks=dict.fromkeys(names, 5))
    report['diagnostic'] = data.ram_call(folder, 'diagnostic_start', timeout=40)
    report['rounds'] = []
    previous = report['before']
    time.sleep(1)
    for index in range(1, rounds + 1):
        print('Tracing recovery', index, 'of', rounds, flush=True)
        fault = reconnect_all(qmp)
        after = data.ready(folder, dict.fromkeys(names, index),
                           minimum_acks={name: previous['devices'][name]['ack_count'] + 3 for name in names})
        def root_ready():
            state = boot.ram_call(folder, 'snapshot')
            return state if state['guard_ready'] and state['state']['recoveries'] >= index else None
        root_after = wait_for(root_ready, 25, 'root recovery')
        report['rounds'].append({'fault': fault, 'after': after, 'root_after': root_after})
        previous = after
        time.sleep(.8)
    result = data.ram_call(folder, 'diagnostic_stop', timeout=40)
    for name, encoded in result['files'].items():
        if name not in ('accounting.json.gz', 'trace.txt.gz'):
            raise ValueError('Unexpected diagnostic artifact')
        (folder / name).write_bytes(base64.b64decode(encoded, validate=True))
    report['audit'] = data.ram_call(folder, 'audit')
    report['root_audit'] = boot.ram_call(folder, 'stop_and_audit')
    report['logs'] = data.ram_call(folder, 'logs')
    final = report['rounds'][-1]
    checks = {
        'all_requested_faults_recovered': len(report['rounds']) == rounds,
        'same_boot': report['before']['boot_id'] == previous['boot_id'],
        'root_recoveries': final['root_after']['state']['recoveries'] == rounds,
        'root_original_process': report['root_worker']['start_ticks'] == final['root_after']['worker_process']['start_ticks'],
        'root_original_mount': report['root_before']['root_mount'] == final['root_after']['root_mount'],
        'root_durable_data': report['root_audit']['prefix_matches'],
        'root_writable': report['root_audit']['root_write_fsync'],
    }
    for name in names:
        old, new = report['before']['devices'][name], previous['devices'][name]
        checks[name + '_original_process'] = all(old['process'][field] == new['process'][field]
                                                 for field in ('pid', 'start_ticks'))
        checks[name + '_original_mount'] = old['mount'] == new['mount']
        checks[name + '_zero_io_errors'] = not new['errors']
        checks[name + '_durable_data'] = report['audit']['audit'][name]['hash_matches']
        checks[name + '_recoveries'] = new['state']['recoveries'] == rounds
    data.ram_call(folder, 'prepare_shutdown')
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    vm.wait(timeout=150)
    checks['clean_shutdown'] = vm.returncode == 0
    report['checks'] = checks
    report['passed'] = all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, choices=range(1, 11), default=6)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    build_dir, _, build, seed, before_hash = validate_inputs(args)
    folder = WORK / ('cpudiag-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = unified.source_hashes()
    sources.update({str(path.relative_to(REPO)): boot.sha256(path) for path in
                    (Path(__file__), BASE / 'guest/cpu_diagnostic.py', BASE / 'performance_probe.py')})
    report = {'schema': 1, 'source_sha256': sources, 'build': build, 'rounds_requested': args.rounds,
              'source_image_sha256_before': before_hash, 'passed': False,
              'scope': 'Diagnostic tracing of repeated USB loss; not production performance measurement'}
    print('CPU diagnostic VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            image = create_initrd(folder, build_dir / 'initrd.img')
            report['initrd_sha256'] = boot.sha256(image)
            command = data.vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            scenarios(folder, qmp, vm, report, args.rounds)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm and vm.poll() is None:
                for action in ('snapshot', 'diagnostic_stop', 'logs'):
                    try:
                        result = data.ram_call(folder, action, timeout=30)
                        if action == 'diagnostic_stop':
                            for name, encoded in result['files'].items():
                                if name in ('accounting.json.gz', 'trace.txt.gz'):
                                    (folder / name).write_bytes(base64.b64decode(encoded, validate=True))
                        else:
                            report['failure_' + action] = result
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
            print('CPU diagnostic report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('CPU diagnostic acceptance failed')


if __name__ == '__main__':
    main()
