#!/usr/bin/env python3
"""Compare cgroup deltas with scheduler residency in CPU diagnostic traces."""
import argparse
import collections
import gzip
import json
from pathlib import Path
import re


LINE = re.compile(r'-(\d+)\s+\[(\d+)\]\s+\S+\s+(\d+\.\d+):\s+(.*)')


def number(text, key):
    match = re.search(r'\b' + re.escape(key) + r'=(\d+)', text)
    return int(match.group(1)) if match else None


def overlap(start, end, left, right):
    return max(0, min(end, right) - max(start, left))


def analyze(folder):
    accounting = json.loads(gzip.decompress((folder / 'accounting.json.gz').read_bytes()))
    raw = gzip.decompress((folder / 'trace.txt.gz').read_bytes()).decode()
    events = []
    for line in raw.splitlines():
        match = LINE.search(line)
        if match:
            pid, cpu, timestamp, body = match.groups()
            kind, separator, details = body.partition(': ')
            events.append((float(timestamp), int(cpu), int(pid), kind if separator else 'function',
                           details if separator else body))
    events.sort(key=lambda event: event[0])
    membership, metadata = {}, {}
    for name, tasks in accounting['config']['initial_tasks'].items():
        for task in tasks:
            if task:
                membership[task['tid']] = name
                metadata[task['tid']] = task
    for _, _, _, kind, details in events:
        if kind == 'sched_process_fork':
            parent, child = number(details, 'pid'), number(details, 'child_pid')
            if parent in membership:
                membership[child] = membership[parent]
    for record in accounting['tasks'].values():
        task = record['last']
        metadata[task['tid']] = task
        for name, location in accounting['config']['groups'].items():
            if name != 'data_slice' and task['cgroup'].endswith(location.split('/sys/fs/cgroup', 1)[1]):
                membership[task['tid']] = name
    switches, intervals, irq_intervals = {}, [], []
    irq_stacks = collections.defaultdict(list)
    runtime, callbacks, policies, cgroup_moves = [], [], [], []
    switch_priorities = collections.defaultdict(set)
    unknown_switch_state = 0
    for timestamp, cpu, pid, kind, details in events:
        if kind == 'sched_switch':
            previous, next_pid = number(details, 'prev_pid'), number(details, 'next_pid')
            if previous in membership:
                switch_priorities[previous].add(number(details, 'prev_prio'))
            if next_pid in membership:
                switch_priorities[next_pid].add(number(details, 'next_prio'))
            if cpu in switches:
                start, current = switches[cpu]
                if previous != current:
                    unknown_switch_state += 1
                if current in membership:
                    intervals.append((start, timestamp, cpu, current))
            switches[cpu] = (timestamp, next_pid)
        elif kind in ('irq_handler_entry', 'softirq_entry'):
            irq_stacks[cpu].append(timestamp)
        elif kind in ('irq_handler_exit', 'softirq_exit') and irq_stacks[cpu]:
            start = irq_stacks[cpu].pop()
            if not irq_stacks[cpu]:
                irq_intervals.append((start, timestamp, cpu))
        elif kind == 'sched_stat_runtime':
            task = number(details, 'pid')
            if task in membership:
                runtime.append({'time': timestamp, 'cpu': cpu, 'pid': task,
                                'runtime_ns': number(details, 'runtime'), 'details': details})
        elif kind == 'function' and 'throttle_cfs_rq_work' in details and pid in membership:
            callbacks.append({'time': timestamp, 'pid': pid, 'cpu': cpu})
        elif (kind in ('sys_enter_sched_setscheduler', 'sys_enter_sched_setattr') or
              kind.startswith(('sys_sched_setscheduler(', 'sys_sched_setattr(')) or
              (kind == 'function' and details.startswith(('sys_sched_setscheduler(', 'sys_sched_setattr(')))) and pid in membership:
            policies.append({'time': timestamp, 'pid': pid, 'event': kind, 'details': details})
        elif kind == 'cgroup_attach_task' and number(details, 'pid') in membership:
            cgroup_moves.append({'time': timestamp, 'details': details})

    def residence(left, right, members):
        selected = [(a, b, cpu, pid) for a, b, cpu, pid in intervals
                    if membership[pid] in members and a < right and b > left]
        gross = sum(overlap(a, b, left, right) for a, b, _, _ in selected)
        irq = sum(overlap(max(a, left), min(b, right), x, y)
                  for a, b, cpu, _ in selected for x, y, irq_cpu in irq_intervals if cpu == irq_cpu)
        task_times = collections.defaultdict(float)
        for a, b, _, pid in selected:
            task_times[pid] += overlap(a, b, left, right)
        return {'scheduled_residence_usec': gross * 1e6, 'traced_irq_overlap_usec': irq * 1e6,
                'residence_minus_traced_irq_usec': (gross - irq) * 1e6,
                'top_tasks': [{'pid': pid, 'residence_usec': value * 1e6, 'metadata': metadata.get(pid)}
                              for pid, value in sorted(task_times.items(), key=lambda item: -item[1])[:8]]}

    summaries = {}
    for name in accounting['config']['groups']:
        members = set(accounting['config']['pids']) - {'root'} if name == 'data_slice' else {name}
        windows = []
        for before, after in zip(accounting['samples'], accounting['samples'][1:]):
            a, b = before['groups'][name], after['groups'][name]
            delta = b['cpu']['usage_usec'] - a['cpu']['usage_usec']
            midpoint_a, midpoint_b = (a['before'] + a['after']) / 2, (b['before'] + b['after']) / 2
            windows.append({'start': midpoint_a, 'end': midpoint_b, 'usage_usec': delta,
                            'percent_midpoints': delta / (midpoint_b - midpoint_a) / 10000,
                            'percent_widest_read_bounds': delta / (b['after'] - a['before']) / 10000,
                            'percent_narrowest_read_bounds': delta / (b['before'] - a['after']) / 10000
                                if b['before'] > a['after'] else None,
                            'first_read_usec': (a['after'] - a['before']) * 1e6,
                            'second_read_usec': (b['after'] - b['before']) * 1e6,
                            'nr_periods_delta': b['cpu'].get('nr_periods', 0) - a['cpu'].get('nr_periods', 0),
                            'nr_throttled_delta': b['cpu'].get('nr_throttled', 0) - a['cpu'].get('nr_throttled', 0)})
        best = sorted(windows, key=lambda item: -item['percent_midpoints'])[:5]
        for window in best:
            left, right = window['start'], window['end']
            window.update(residence(left, right, members))
            window['runtime_events_usec'] = sum(event['runtime_ns'] / 1000 for event in runtime
                if left < event['time'] <= right and membership[event['pid']] in members)
            window['throttle_callbacks'] = [event for event in callbacks
                if left - .05 < event['time'] < right + .1 and membership[event['pid']] in members]
        summaries[name] = {'peak_windows': best,
                           'max_cpu_stat_read_usec': max(row['groups'][name]['after'] - row['groups'][name]['before']
                                                       for row in accounting['samples']) * 1e6}
    longest_irqs = sorted(irq_intervals, key=lambda entry: entry[1] - entry[0], reverse=True)[:10]
    return {'schema': 1, 'config': accounting['config'], 'samples': len(accounting['samples']),
            'trace_events': len(events), 'trace_stats': accounting['trace_stats'],
            'sched_switch_continuity_errors': unknown_switch_state,
            'tracked_tasks': len(membership), 'policies_observed': sorted({task['policy'] for task in metadata.values()}),
            'policy_change_syscalls': policies, 'cgroup_moves': cgroup_moves,
            'scheduler_priorities_observed': sorted({value for values in switch_priorities.values() for value in values}),
            'longest_traced_interrupts': [{'start': a, 'end': b, 'cpu': cpu, 'usec': (b - a) * 1e6}
                                         for a, b, cpu in longest_irqs],
            'throttle_callback_count': len(callbacks), 'runtime_event_count': len(runtime),
            'first_cpu_stat': accounting['samples'][0]['stat'], 'last_cpu_stat': accounting['samples'][-1]['stat'],
            'groups': summaries,
            'limitations': ['Diagnostic tracing adds overhead.',
                            'sched_switch residency includes VM steal and untraced interrupt overhead.',
                            'Subtracting traced IRQ/softirq duration leaves an upper bound, not exact CPU cycles.',
                            'cpu.stat counters can account runtime accrued before a read interval.',
                            'This run cannot retroactively prove the cause of a separate historical outlier.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    result = analyze(args.folder)
    target = args.folder / 'analysis.json'
    target.write_text(json.dumps(result, indent=2) + '\n')
    print(target)


if __name__ == '__main__':
    main()
