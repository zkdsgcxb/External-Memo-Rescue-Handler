#!/usr/bin/env python3
"""Measure settled Guard memory across repeated simultaneous USB recoveries."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

from auto_run import wait_for
import data_guard_probe as data
import host_boot_probe as boot
import performance_probe as performance
import unified_guard_probe as unified
from efi_mount_probe import validate_inputs, wait_for_boot, stop_vm
from run import Channel, WORK

BASE = Path(__file__).resolve().parent
REPO = BASE.parent


def source_hashes():
    sources = unified.source_hashes()
    sources.update({str(path.relative_to(REPO)): boot.sha256(path) for path in
                    (Path(__file__), BASE / 'guest/soak_probe.py',
                     BASE / 'performance_probe.py', BASE / 'guest/performance_probe.py',
                     BASE / 'measure_guard.py')})
    return sources


def create_initrd(folder, original):
    """Compose the existing performance fixture without altering its sources."""
    composition = folder / 'soak-composition'
    (composition / 'guest').mkdir(parents=True)
    (composition / 'guest/performance_probe.py').write_text(
        (BASE / 'guest/performance_probe.py').read_text() + '\n' +
        (BASE / 'guest/soak_probe.py').read_text() + '\nPerformanceProbe = SoakProbe\n')
    previous = performance.BASE
    try:
        performance.BASE = composition
        return performance.create_initrd(folder, original, 'current')
    finally:
        performance.BASE = previous


def same_process(before, after):
    return bool(before and after and all(before[key] == after[key] for key in ('pid', 'start_ticks')))


def memory_trend(checkpoints):
    """Expose first-fault growth separately from later settled checkpoints."""
    result = {}
    for field in ('memory_current_bytes', 'Pss_bytes', 'Rss_bytes', 'memory_stat_anon_bytes',
                  'memory_stat_file_bytes', 'memory_stat_kernel_bytes'):
        values = [point['accounting']['aggregate'][field] for point in checkpoints]
        later = values[1:]
        tail = later[-min(5, len(later)):]
        count = len(later)
        center = (count - 1) / 2
        denominator = sum((index - center) ** 2 for index in range(count))
        slope = (sum((index - center) * value for index, value in enumerate(later)) /
                 denominator) if denominator else 0
        result[field] = {'initial': values[0], 'after_first_recovery': later[0],
                         'final': later[-1], 'first_recovery_delta': later[0] - values[0],
                         'after_first_to_final_delta': later[-1] - later[0],
                         'post_recovery_min': min(later), 'post_recovery_max': max(later),
                         'last_five_range': max(tail) - min(tail),
                         'post_recovery_linear_slope_bytes_per_cycle': slope}
    return result


def scenarios(folder, qmp, vm, report):
    wait_for_boot(folder, vm)
    def userspace_ready():
        value = boot.ram_call(folder, 'snapshot')
        return value if value['guard_ready'] and value['services']['dbus']['ActiveState'] == 'active' else None
    report['root_initial'] = wait_for(userspace_ready, 40, 'root and Ubuntu D-Bus ready')
    report['configure'] = data.ram_call(folder, 'configure', timeout=90)
    report['activate'] = data.ram_call(folder, 'activate', timeout=90)
    report['late'] = data.ram_call(folder, 'create_late_map')
    names = [spec['name'] for spec in data.SPECS]
    unified.guarded_ready(folder, names)
    data.ram_call(folder, 'mount_registered')
    report['runtime_before'] = data.ram_call(folder, 'runtime_hashes')
    if any(hashes != report['runtime_payload_sha256'] for hashes in report['runtime_before'].values()):
        raise RuntimeError('Live root/data runtime differs from the staged current runtime')
    report['root_worker'] = boot.ram_call(folder, 'start_workload')
    data.ram_call(folder, 'start')
    data.ready(folder, dict.fromkeys(names, 0), minimum_acks=dict.fromkeys(names, 5))
    time.sleep(3)
    initial = data.ram_call(folder, 'checkpoint')
    report['initial'] = initial
    report['cycles'] = []
    previous = initial
    for index in range(1, report['requested_cycles'] + 1):
        cycle = {'index': index}
        report['cycles'].append(cycle)
        cycle['fault'] = performance.reconnect_all(qmp)
        cycle['data_ready'] = data.ready(folder, dict.fromkeys(names, index),
            minimum_acks={name: previous['snapshot']['devices'][name]['ack_count'] + 3 for name in names})
        def root_ready():
            value = boot.ram_call(folder, 'snapshot')
            state = value['state'] or {}
            if state.get('state') in ('expired', 'failed', 'blocked', 'interrupted'):
                raise RuntimeError('Root reached terminal state during repeated recovery: ' + json.dumps(state))
            return value if value['guard_ready'] and state.get('recoveries', 0) >= index else None
        cycle['root_ready'] = wait_for(root_ready, 25, 'root recovery ' + str(index))
        cycle['all_ready_host_time'] = time.monotonic()
        cycle['request_to_observed_ready_seconds'] = cycle['all_ready_host_time'] - min(
            value['delete_requested'] for value in cycle['fault'].values())
        # Settling is outside the recovery journal window, included explicitly
        # in cycle accounting; this does not measure a recovery CPU peak.
        time.sleep(report['settle_seconds'])
        cycle['checkpoint'] = data.ram_call(folder, 'checkpoint')
        before, after = previous['accounting'], cycle['checkpoint']['accounting']
        elapsed = after['started'] - before['started']
        delta_cpu = after['aggregate']['cpu_usage_usec'] - before['aggregate']['cpu_usage_usec']
        cycle['accounting_including_settle'] = {
            'seconds': elapsed, 'cpu_usec': delta_cpu,
            'cpu_mean_one_core_percent': delta_cpu / elapsed / 10000,
            'throttled_usec': after['aggregate']['cpu_throttled_usec'] - before['aggregate']['cpu_throttled_usec']}
        previous = cycle['checkpoint']
        aggregate = after['aggregate']
        print(f"Cycle {index}: cgroup {aggregate['memory_current_bytes'] / 1024**2:.3f} MiB, "
              f"PSS {aggregate['Pss_bytes'] / 1024**2:.3f} MiB, "
              f"ready {cycle['request_to_observed_ready_seconds']:.3f}s", flush=True)
    report['audit'] = data.ram_call(folder, 'audit')
    report['root_audit'] = boot.ram_call(folder, 'stop_and_audit')
    report['logs'] = data.ram_call(folder, 'logs')
    report['runtime_after'] = data.ram_call(folder, 'runtime_hashes')
    checkpoints = [initial, *[cycle['checkpoint'] for cycle in report['cycles']]]
    report['memory_trend'] = memory_trend(checkpoints)
    checks = {
        'all_requested_cycles_completed': len(report['cycles']) == report['requested_cycles'],
        'runtime_unchanged': report['runtime_before'] == report['runtime_after'],
        'only_three_controllers_after_each_settle': all(point['accounting']['only_controller_processes'] for point in checkpoints),
        'no_swap_at_settled_checkpoints': all(point['accounting']['aggregate']['Swap_bytes'] == 0 for point in checkpoints),
        'root_durable_data': report['root_audit']['prefix_matches'],
        'root_writable': report['root_audit']['root_write_fsync'],
        'data_durable_hashes': all(value['hash_matches'] for value in report['audit']['audit'].values()),
    }
    for cycle in report['cycles']:
        point, root = cycle['checkpoint'], cycle['root_ready']
        state_checks = [
            point['snapshot']['boot_id'] == initial['snapshot']['boot_id'],
            same_process(point['snapshot']['pid1'], initial['snapshot']['pid1']),
            same_process(root['worker_process'], report['root_worker']),
            root['root_mount'] == report['root_initial']['root_mount'],
            root['state']['recoveries'] == cycle['index'],
            root['state']['owner_epoch'] == report['root_initial']['state']['owner_epoch'],
            len(point['recovery_windows']) == 3,
        ]
        for name in names:
            old, new = initial['snapshot']['devices'][name], point['snapshot']['devices'][name]
            state_checks += [same_process(old['process'], new['process']), old['mount'] == new['mount'],
                             new['state']['recoveries'] == cycle['index'], not new['errors'],
                             old['state']['owner_epoch'] == new['state']['owner_epoch']]
        for name, owner in point['accounting']['controllers'].items():
            state_checks.append(same_process(owner['process'], initial['accounting']['controllers'][name]['process']))
        checks[f'cycle_{cycle["index"]}_same_owners_workloads_mounts_and_successful_recovery'] = all(state_checks)
    first_fault = checkpoints[1]['accounting']['controllers']
    checks['settled_fd_and_thread_counts_do_not_grow_after_first_recovery'] = all(
        all(owner[field] == first_fault[name][field] for field in ('fd_count', 'threads'))
        for point in checkpoints[1:] for name, owner in point['accounting']['controllers'].items())
    report['checks'] = checks
    data.ram_call(folder, 'prepare_shutdown')
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    vm.wait(timeout=150)
    checks['clean_shutdown'] = vm.returncode == 0
    report['passed'] = all(checks.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles', type=int, default=10)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    if not 2 <= args.cycles <= 50:
        parser.error('--cycles must be between 2 and 50')
    build_dir, seed_path, build, seed, before_hash = validate_inputs(args)
    folder = WORK / ('soak-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = source_hashes()
    report = {'schema': 1, 'requested_cycles': args.cycles, 'settle_seconds': .75,
              'build': build, 'source_sha256': sources, 'source_image_sha256_before': before_hash,
              'scope': 'root service + aggregate data slice; process PSS/RSS separate from cgroup charges; settled checkpoints, not peaks',
              'passed': False}
    print('Recovery soak VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            image = create_initrd(folder, build_dir / 'initrd.img')
            report['runtime_payload_sha256'] = {path.name: boot.sha256(path) for path in
                                                sorted((folder / 'data-overlay/opt/data-guard').glob('*.py'))}
            report['command'] = data.vm_command(folder, build_dir, image, overlay, seed)
            vm = subprocess.Popen(report['command'], stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            scenarios(folder, qmp, vm, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                for action in ('checkpoint', 'logs'):
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
                report['filesystem_audits'] = data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged']
                and not report.get('filesystem_audit_error')
                and all(value['returncode'] == 0 for value in report.get('filesystem_audits', {}).values()))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Recovery soak report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Recovery soak acceptance failed')


if __name__ == '__main__':
    # The Python runtime was retired; replay this historical experiment intact.
    from historical import run_legacy
    raise SystemExit(run_legacy(__file__))
