#!/usr/bin/env python3
"""Summarize complete Python/C++ Guard trials; optionally draw their comparison.

Inputs are cpp_guard_probe performance reports. Failed reports, changed
payloads, differing budgets/images and mixed native builds are refused.
Matplotlib and NumPy are needed only when --plot is requested.
"""
import argparse
import json
from pathlib import Path
import statistics

import performance_report

IMPLEMENTATIONS = ('python', 'cpp')
PHASES = ('idle', 'unrelated', 'relevant', 'recovery')


def series(values):
    return {'median': statistics.median(values), 'min': min(values), 'max': max(values), 'runs': values}


def load_trials(paths, repetitions):
    reports = performance_report.load_reports(paths)
    # The shared loader validates raw counters, payloads, checks, filesystem
    # audits and duplicate contents before any value enters this comparison.
    trials = reports
    if any(report.get('scenario') != 'performance' or report.get('implementation') not in IMPLEMENTATIONS
           for _path, report, _content in trials):
        raise ValueError('Expected full-runtime Python/C++ performance reports')
    if any(sum(report['implementation'] == name for _path, report, _content in trials) != repetitions for name in IMPLEMENTATIONS):
        raise ValueError(f'Require exactly {repetitions} trials of each implementation')
    for key in ('quota_percent', 'baseline_ref', 'source_image_sha256_before'):
        if len({report[key] for _path, report, _content in trials}) != 1:
            raise ValueError('Different comparison policy or input: ' + key)
    if len({report['build']['kernel_sha256'] for _path, report, _content in trials}) != 1:
        raise ValueError('Different comparison kernel')
    if any(not report.get('recovery_rpc_quiet') for _path, report, _content in trials):
        raise ValueError('Recovery trials must use quiet sampling')
    native = [report for _path, report, _content in trials if report['implementation'] == 'cpp']
    if len({report['experiment_manifest']['binary_sha256'] for report in native}) != 1:
        raise ValueError('Cannot combine different native builds')
    closures = [report['experiment_manifest'].get('library_sha256') for report in native]
    if any(closure != closures[0] for closure in closures):
        raise ValueError('Cannot combine different native library closures')
    trials.sort(key=lambda item: item[1]['fault']['root']['removed'])
    return trials


def summarize(trials):
    comparison = {}
    for implementation in IMPLEMENTATIONS:
        selected = [report for _path, report, _content in trials if report['implementation'] == implementation]
        result = {'trials': len(selected), 'phases': {}}
        for label in PHASES:
            phases = [next(phase for phase in report['phases'] if phase['label'] == label) for report in selected]
            result['phases'][label] = {key: series([phase['groups']['aggregate'][key] for phase in phases])
                for key in ('cpu_mean_percent', 'cpu_total_usec', 'cpu_peak_20ms_percent',
                            'cpu_peak_100ms_percent', 'memory_sampled_peak_bytes')}
            result['phases'][label]['Pss_after_bytes'] = series([
                sum(phase['after'][name]['process_totals_bytes']['Pss'] for name in ('root', 'data_slice'))
                for phase in phases])
        windows = [next(phase for phase in report['phases'] if phase['label'] == 'recovery')
                   ['recovery_windows']['aggregate'] for report in selected]
        result['active_recovery'] = {'journal_seconds': series([window['journal_seconds'] for window in windows]),
            **{key: series([window['accounting'][key] for window in windows])
               for key in ('cpu_total_usec', 'cpu_mean_percent')}}
        result['workload_stall_seconds'] = {
            'root': series([report['root_audit']['max_write_seconds'] for report in selected]),
            **{name: series([report['audit']['audit'][name]['max_write_and_direct_read_seconds'] for report in selected])
               for name in ('rr-data-vm-ext4', 'rr-data-vm-vfat')}}
        comparison[implementation] = result
    return comparison


def draw(comparison, trials, destination):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np

    python = next(report for _path, report, _content in trials if report['implementation'] == 'python')
    native = next(report for _path, report, _content in trials if report['implementation'] == 'cpp')
    names = {'python': 'Python ' + python['baseline_ref'][:7],
             'cpp': 'C++ ' + native['experiment_manifest']['binary_sha256'][:8]}
    count = comparison['python']['trials']
    colors = {'python': '#777C87', 'cpp': '#137A72'}
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.grid': True, 'grid.alpha': .18, 'axes.axisbelow': True, 'font.size': 10})
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout='constrained')
    for index, implementation in enumerate(IMPLEMENTATIONS):
        positions = np.arange(4) + (index - .5) * .34
        data = comparison[implementation]['phases']
        medians = [data[phase]['cpu_mean_percent']['median'] for phase in PHASES]
        low = [value - data[phase]['cpu_mean_percent']['min'] for phase, value in zip(PHASES, medians)]
        high = [data[phase]['cpu_mean_percent']['max'] - value for phase, value in zip(PHASES, medians)]
        axes[0, 0].bar(positions, medians, .32, color=colors[implementation], label=names[implementation],
                       yerr=[low, high], capsize=3)
        axes[0, 1].bar(positions, [data[phase]['cpu_peak_100ms_percent']['max'] for phase in PHASES],
                       .32, color=colors[implementation])
        memory = [data[phase]['Pss_after_bytes']['median'] / 2**20 for phase in ('idle', 'recovery')]
        axes[1, 0].bar(np.arange(2) + (index - .5) * .34, memory, .32, color=colors[implementation])
        for position, value in zip(np.arange(2) + (index - .5) * .34, memory):
            axes[1, 0].text(position, value + .6, f'{value:.1f}', ha='center', fontsize=9)
        recovery = comparison[implementation]['active_recovery']['cpu_total_usec']
        axes[1, 1].bar(index, recovery['median'] / 1000, .55, color=colors[implementation],
                       yerr=[[(recovery['median'] - recovery['min']) / 1000],
                             [(recovery['max'] - recovery['median']) / 1000]], capsize=4)
        axes[1, 1].text(index, recovery['max'] / 1000 + 4, f"{recovery['median'] / 1000:.1f}",
                       ha='center', fontsize=9)
    for axis in axes[0]:
        axis.set_xticks(range(4), ['Idle', 'Unrelated events', 'Relevant events', 'Recovery (16 s)'], rotation=12)
        axis.set_ylabel('One CPU = 100%')
    axes[0, 0].set_title(f'CPU mean: median and full range of {count} trials')
    axes[0, 0].legend(frameon=False)
    axes[0, 1].set_title(f'Largest ~100 ms CPU window across all {count} trials')
    axes[1, 0].set_title('Aggregate process PSS: median boundary snapshots')
    axes[1, 0].set_xticks(range(2), ['After idle phase', 'After recovery phase'])
    axes[1, 0].set_ylabel('MiB')
    axes[1, 0].set_ylim(top=max(comparison[name]['phases'][phase]['Pss_after_bytes']['median']
                              for name in IMPLEMENTATIONS for phase in ('idle', 'recovery')) / 2**20 * 1.17)
    axes[1, 1].set_title('CPU time during actual recovery: median and range')
    axes[1, 1].set_xticks(range(2), [names[name] for name in IMPLEMENTATIONS])
    axes[1, 1].set_ylabel('CPU milliseconds')
    axes[1, 1].set_ylim(top=max(comparison[name]['active_recovery']['cpu_total_usec']['max']
                              for name in IMPLEMENTATIONS) / 1000 * 1.12)
    fig.suptitle('Full Guard runtime: Python vs C++\nRoot + ext4 + FAT controllers, identical Ubuntu QEMU fixtures', fontsize=15)
    fig.savefig(destination, dpi=180)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--plot', type=Path)
    args = parser.parse_args()
    if args.repetitions < 2:
        parser.error('At least two independent trials are required')
    try:
        trials = load_trials(args.reports, args.repetitions)
        comparison = summarize(trials)
        output = performance_report.summarize(trials)
        output.update({'schema': 1, 'scope': 'three complete root/ext4/FAT recovery controllers; one CPU=100%; root cgroup+data slice descendants once',
                  'method': {'repetitions': args.repetitions, 'chronological_order': [report['implementation'] for _path, report, _content in trials],
                             'quota_percent_per_root_and_data_slice': trials[0][1]['quota_percent'],
                             'recovery_rpc_quiet': True, 'range_is_confidence_interval': False},
                  'comparison_generator': {'path': performance_report.display_path(__file__),
                                           'sha256': performance_report.digest(Path(__file__).read_bytes())},
                  'comparison': comparison})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
        if args.plot:
            args.plot.parent.mkdir(parents=True, exist_ok=True)
            draw(comparison, trials, args.plot)
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
