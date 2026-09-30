#!/usr/bin/env python3
"""Publish compact, reproducible evidence from explicit Guard VM reports.

The raw reports remain local. Public output keeps checks, hashes, accounting
summaries and process-memory snapshots, without thousands of raw samples or
guest logs. No VM, service, block device or network operation is performed.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PHASES = {'idle', 'unrelated', 'relevant', 'recovery'}
MEMORY_FILES = ('memory.current', 'memory.peak', 'memory.max', 'memory.high',
                'memory.swap.current', 'memory.swap.peak', 'memory.swap.max',
                'memory.events', 'memory.events.local')


def digest(content):
    return hashlib.sha256(content).hexdigest()


def display_path(path):
    path = Path(path).resolve()
    return str(path.relative_to(REPO)) if path.is_relative_to(REPO) else str(path)


def select(value, names):
    return {name: value[name] for name in names if name in value}


def process_memory(snapshot):
    files = snapshot.get('files', {})
    return {
        'cgroup': select(files, MEMORY_FILES),
        'memory_stat_bytes': select(files.get('memory.stat') or {},
                                    ('anon', 'file', 'shmem', 'slab', 'sock', 'kernel', 'pagetables')),
        'process_totals_bytes': snapshot.get('process_totals_bytes', {}),
        'processes': [{
            **select(process, ('pid', 'start_ticks', 'stable_instance')),
            'status_bytes': select(process.get('status_bytes', {}), ('VmRSS', 'VmHWM', 'VmLck', 'VmSwap')),
            'smaps_rollup_bytes': select(process.get('smaps_rollup_bytes', {}),
                ('Rss', 'Pss', 'Private_Clean', 'Private_Dirty', 'Shared_Clean', 'Shared_Dirty', 'Swap', 'SwapPss', 'Locked')),
            'read_errors': process.get('errors', []),
        } for process in snapshot.get('processes', [])],
        'membership_atomic': snapshot.get('membership_atomic', False),
        'read_errors': snapshot.get('errors', []),
    }


def validate_phase(phase):
    """Catch stale summaries and double-counted parent slices before publication."""
    samples = phase.get('samples', [])
    if len(samples) < 6:
        raise ValueError('Phase requires at least six samples: ' + str(phase.get('label')))
    if any(b['time'] <= a['time'] for a, b in zip(samples, samples[1:])):
        raise ValueError('Sampling clock is not strictly increasing')
    if not {'root', 'data_slice', 'aggregate'} <= set(phase.get('groups', {})):
        raise ValueError('Missing root/data/aggregate accounting')
    duration = samples[-1]['time'] - samples[0]['time']
    for name, summary in phase['groups'].items():
        members = ('root', 'data_slice') if name == 'aggregate' else (name,)
        values = [sum(row['groups'][member]['cpu']['usage_usec'] for member in members) for row in samples]
        if any(b < a for a, b in zip(values, values[1:])):
            raise ValueError('CPU counter went backwards: ' + name)
        total = values[-1] - values[0]
        mean = total / duration / 10000
        if summary['cpu_total_usec'] != total or not math.isclose(
                summary['cpu_mean_percent'], mean, rel_tol=1e-8, abs_tol=1e-10):
            raise ValueError('CPU summary differs from raw accounting: ' + name)


def validate_report(report, *, allow_failed=False):
    if type(report.get('schema')) is not int or report['schema'] != 1 or report.get('variant') not in ('baseline', 'current'):
        raise ValueError('Expected a version 1 performance_probe report')
    if type(report.get('quota_percent')) is not int or report['quota_percent'] not in (5, 10, 20):
        raise ValueError('Unexpected experiment CPU quota')
    if report.get('passed') is not True:
        if not allow_failed:
            raise ValueError('Refusing a failed/incomplete report; use --allow-failed for explicitly labelled diagnostics')
        return
    checks = report.get('checks', {})
    if not checks or not all(value is True for value in checks.values()):
        raise ValueError('Report claims success but acceptance checks contradict it')
    if report.get('source_image_unchanged') is not True or report.get('sources_unchanged') is not True:
        raise ValueError('Passing evidence must preserve source/image hashes')
    if 'runtime_payload_sha256' in report:
        expected = report['runtime_payload_sha256']
        before, after = report.get('runtime_before'), report.get('runtime_after')
        if (not expected or not before or before != after or
                any(any(hashes.get(name) != value for name, value in expected.items())
                    for hashes in before.values())):
            raise ValueError('Running payload hashes differ from the selected experiment')
    audits = report.get('filesystem_audits', {})
    if not audits or any(value.get('returncode') != 0 for value in audits.values()):
        raise ValueError('Passing evidence requires successful filesystem audits')
    phases = report.get('phases', [])
    if len(phases) != len(PHASES) or {phase.get('label') for phase in phases} != PHASES:
        raise ValueError('Passing evidence must contain all four distinct measurement phases')
    for phase in phases:
        validate_phase(phase)


def compact_phase(phase):
    result = select(phase, ('label', 'period_seconds', 'seconds', 'window_seconds',
                           'emitted_events', 'events_per_second', 'cpu_max',
                           'data_slice_cpu_max', 'groups', 'recovery_windows'))
    result['sample_count'] = len(phase.get('samples', []))
    samples = phase.get('samples', [])
    if len(samples) >= 6:
        # Keep the two extreme aggregate counter intervals reviewable even
        # though the public artifact omits the thousands of ordinary samples.
        total = lambda row: sum(row['groups'][name]['cpu']['usage_usec'] for name in ('root', 'data_slice'))
        result['aggregate_peak_windows'] = {}
        for stride, label in ((1, 'nominal_20ms'), (5, 'nominal_100ms')):
            def percent(pair):
                a, b = pair
                return (total(b) - total(a)) / (b['time'] - a['time']) / 10000
            a, b = max(zip(samples, samples[stride:]), key=percent)
            result['aggregate_peak_windows'][label] = {
                'guest_monotonic_start': a['time'], 'guest_monotonic_end': b['time'],
                'seconds': b['time'] - a['time'], 'cpu_delta_usec': total(b) - total(a),
                'cpu_percent': percent((a, b)),
            }
        result['rolling_5_interval_window_seconds'] = {
            'min': min(b['time'] - a['time'] for a, b in zip(samples, samples[5:])),
            'max': max(b['time'] - a['time'] for a, b in zip(samples, samples[5:])),
        }
    result['memory_snapshots'] = {moment: {
        name: process_memory(snapshot) for name, snapshot in phase.get(moment, {}).items()
    } for moment in ('before', 'after')}
    return result


def compact_report(path, report, content):
    root = report.get('root_audit', {})
    workloads = {'root': select(root, ('acknowledged_records', 'acknowledged_bytes',
                                     'prefix_matches', 'prefix_sha256', 'root_write_fsync', 'max_write_seconds'))}
    for name, audit in report.get('audit', {}).get('audit', {}).items():
        workloads[name] = select(audit, ('ack_count', 'bytes', 'hash_matches', 'sha256',
                                        'max_write_and_direct_read_seconds'))
    return {
        'source_report': {'path': display_path(path), 'sha256': digest(content)},
        **select(report, ('variant', 'baseline_ref', 'quota_percent', 'passed', 'scope',
                          'initrd_sha256', 'source_sha256', 'source_image_sha256_before',
                          'source_image_unchanged', 'sources_unchanged', 'runtime_payload_sha256',
                          'runtime_before', 'runtime_after', 'healthy_warmup_seconds',
                          'quiet_wait_seconds', 'recovery_rpc_quiet_window')),
        'recovery_rpc_quiet': report.get('recovery_rpc_quiet', False),
        'kernel': select(report.get('build', {}), ('kernel_release', 'kernel_sha256')),
        'phases': [compact_phase(phase) for phase in report.get('phases', [])],
        'faults': {name: select(value, ('gap_seconds', 'clock', 'delete_requested', 'removed', 'attached'))
                   for name, value in report.get('fault', {}).items()},
        'workloads': workloads,
        'checks': report.get('checks', {}),
        'filesystem_audits': {name: {'returncode': value.get('returncode')}
                              for name, value in report.get('filesystem_audits', {}).items()},
        'failure_reason': report.get('error'),
    }


def load_reports(paths, *, allow_failed=False):
    loaded, identities, contents = [], set(), set()
    for path in paths:
        path = Path(path).resolve(strict=True)
        if path in identities:
            raise ValueError('Duplicate report path: ' + str(path))
        identities.add(path)
        content = path.read_bytes()
        fingerprint = digest(content)
        if fingerprint in contents:
            raise ValueError('Duplicate report content cannot count as another experiment: ' + str(path))
        contents.add(fingerprint)
        def reject_constant(value):
            raise ValueError('Non-finite JSON number: ' + value)
        def finite_float(value):
            parsed = float(value)
            return parsed if math.isfinite(parsed) else reject_constant(value)
        report = json.loads(content, parse_constant=reject_constant, parse_float=finite_float)
        validate_report(report, allow_failed=allow_failed)
        loaded.append((path, report, content))
    return loaded


def summarize(loaded):
    return {
        'schema': 1, 'kind': 'guard-performance-evidence',
        'generator': {'path': display_path(__file__), 'sha256': digest(Path(__file__).read_bytes())},
        'conventions': {
            'cpu_one_logical_core_equals_percent': 100,
            'cpu_source': 'cgroup cpu.stat cumulative accounting, not isolated application instruction time',
            'cpu_irq_accounting_boundary': 'This guest kernel can charge interrupt time to the interrupted task; serial RPC interrupt activity may enter Guard or child cgroup counters',
            'cpu_counter_samples_atomic': False,
            'cpu_sample_timestamp': 'Guest monotonic time before sequential cgroup reads; per-counter read timing is not captured by this sampler',
            'aggregate_members': ['root', 'data_slice'],
            'data_slice_already_contains_both_data_controllers': True,
            'quota_percent_applies_separately_to_root_and_data_slice': True,
            'memory_unit': 'bytes',
            'cpu_peaks_are_window_averages_not_instantaneous': True,
            'process_memory_is_before_after_snapshots_not_continuous_peaks': True,
            'memory_peak_and_VmHWM_are_lifetime_counters_not_reset_for_each_phase': True,
            'recovery_phase_includes_idle_before_and_after_fault': True,
            'recovery_windows_follow_guest_journal_waiting_to_ready_with_adjacent_sample_bounds': True,
            'excluded': ['sampler', 'workload', 'udev', 'other services', 'kernel workers outside controller cgroups'],
        },
        'reports': [compact_report(*entry) for entry in loaded],
    }


def plot_recovery(loaded, output):
    """Render raw rolling-window accounting; do not interpolate missing peaks."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError('--plot requires optional matplotlib; JSON generation has no plotting dependency') from error
    phases = [(report, phase) for _, report, _ in loaded for phase in report.get('phases', [])
              if phase.get('label') == 'recovery' and phase.get('samples')]
    if not phases:
        raise ValueError('No recovery timeline in the supplied reports')
    plt.style.use('default')
    figure, axes = plt.subplots(len(phases), 2, figsize=(13, 3.2 * len(phases)), squeeze=False,
                                sharex=True, sharey='col', constrained_layout=True)
    for row, (report, phase) in enumerate(phases):
        samples = phase['samples']
        start = samples[0]['time']
        clock = [sample['time'] - start for sample in samples]
        total = [sum(sample['groups'][name]['cpu']['usage_usec'] for name in ('root', 'data_slice'))
                 for sample in samples]
        cpu = [(total[i] - total[i - 5]) / (clock[i] - clock[i - 5]) / 10000 for i in range(5, len(clock))]
        memory = [sum(sample['groups'][name]['memory_bytes'] for name in ('root', 'data_slice')) / 1024**2
                  for sample in samples]
        label = f"{report['variant']} | {report['quota_percent']}% per root / data-slice budget"
        axes[row, 0].plot(clock[5:], cpu, color='#2166ac', linewidth=1.3)
        axes[row, 1].plot(clock, memory, color='#27814d', linewidth=1.3)
        for column in range(2):
            axis = axes[row, column]
            interval = phase.get('recovery_windows', {}).get('aggregate')
            if interval:
                axis.axvspan(interval['waiting'] - start, interval['ready'] - start,
                             color='#e9bd57', alpha=.24, label='Journal: first waiting to last ready')
            axis.set_title(label, loc='left', fontsize=10)
            axis.grid(alpha=.18)
            axis.set_xlim(0, clock[-1])
        axes[row, 0].set_ylabel('CPU % (one logical core = 100%)')
        axes[row, 1].set_ylabel('Aggregate cgroup memory (MiB)')
    if axes[0, 0].get_legend_handles_labels()[0]:
        axes[0, 0].legend(loc='upper right', fontsize=8)
    # Apply shared limits only after all curves contributed to autoscaling.
    for axis in axes[0]:
        axis.set_ylim(bottom=0)
    for axis in axes[-1]:
        axis.set_xlabel('Seconds from recovery sampler start')
    figure.suptitle('Root + two data Guards: recovery scenario\nCPU uses rolling 5-interval (~100 ms) averages; shaded interval comes from guest journals', fontsize=13)
    figure.savefig(output, dpi=180, metadata={'Software': 'ram-rescue performance_report.py'})
    plt.close(figure)
    return {'matplotlib_version': matplotlib.__version__, 'font_family': 'DejaVu Sans',
            'cpu_window': 'five adjacent sample intervals, nominally 100 ms'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', help='Describe the exact experiment revision, especially historical comparisons')
    parser.add_argument('--allow-failed', action='store_true')
    parser.add_argument('--plot', type=Path, help='Optional standalone recovery PNG, requires matplotlib')
    parser.add_argument('--plot-report', type=Path, action='append', default=[],
                        help='Plot only this input report; repeat to choose representative rows without dropping JSON evidence')
    args = parser.parse_args()
    try:
        loaded = load_reports(args.reports, allow_failed=args.allow_failed)
        summary = summarize(loaded)
        if args.label:
            summary['experiment'] = args.label
        if args.output.resolve() in {path for path, _, _ in loaded}:
            raise ValueError('Output must not replace an input report')
        if args.plot:
            if (args.plot.suffix.lower() != '.png' or args.plot.resolve() == args.output.resolve()
                    or args.plot.resolve() in {path for path, _, _ in loaded}):
                raise ValueError('--plot must name a separate PNG file')
            args.plot.parent.mkdir(parents=True, exist_ok=True)
            selected = [path.resolve(strict=True) for path in args.plot_report]
            if len(selected) != len(set(selected)) or not set(selected) <= {path for path, _, _ in loaded}:
                raise ValueError('--plot-report must choose distinct reports from the main input list')
            indexed = {entry[0]: entry for entry in loaded}
            plotted = [indexed[path] for path in selected] if selected else loaded
            renderer = plot_recovery(plotted, args.plot)
            summary['plot'] = {'path': display_path(args.plot), 'sha256': digest(args.plot.read_bytes()),
                               'source_reports': [display_path(path) for path, _, _ in plotted],
                               **renderer}
        elif args.plot_report:
            raise ValueError('--plot-report requires --plot')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n')
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({'output': str(args.output), 'reports': len(loaded),
                      'all_passed': all(report.get('passed') is True for _, report, _ in loaded)}))


if __name__ == '__main__':
    main()
