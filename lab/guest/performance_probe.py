"""Disposable-guest accounting; this sampler never joins a Guard cgroup."""
import math


def resource_summary(samples, members):
    readings = [{'time': row['time'],
                 'cpu_usec': sum(row['groups'][member]['cpu']['usage_usec'] for member in members),
                 'memory_bytes': sum(row['groups'][member]['memory_bytes'] for member in members)}
                for row in samples]
    duration = readings[-1]['time'] - readings[0]['time']
    windows = [(b['cpu_usec'] - a['cpu_usec']) / (b['time'] - a['time']) / 10000
               for a, b in zip(readings, readings[1:])]
    hundred_ms = [(b['cpu_usec'] - a['cpu_usec']) / (b['time'] - a['time']) / 10000
                  for a, b in zip(readings, readings[5:])]
    ordered = sorted(windows)
    return {
        'seconds': duration,
        'cpu_mean_percent': (readings[-1]['cpu_usec'] - readings[0]['cpu_usec']) / duration / 10000,
        'cpu_total_usec': readings[-1]['cpu_usec'] - readings[0]['cpu_usec'],
        'cpu_peak_20ms_percent': max(windows),
        'cpu_p95_20ms_percent': ordered[math.ceil(len(ordered) * .95) - 1],
        'cpu_p99_20ms_percent': ordered[math.ceil(len(ordered) * .99) - 1],
        'cpu_peak_100ms_percent': max(hundred_ms) if hundred_ms else None,
        'memory_mean_bytes': sum(row['memory_bytes'] for row in readings) / len(readings),
        'memory_sampled_peak_bytes': max(row['memory_bytes'] for row in readings),
        'throttled_usec': sum(samples[-1]['groups'][member]['cpu'].get('throttled_usec', 0) -
                              samples[0]['groups'][member]['cpu'].get('throttled_usec', 0) for member in members),
    }



def process_counters(pid):
    """Read CPU counters and sample live helpers; short helpers can be missed."""
    folder = Path('/proc') / str(pid)
    try:
        fields = (folder / 'stat').read_text().rsplit(')', 1)[1].split()
        children = set()
        for thread in (folder / 'task').iterdir():
            try:
                children.update(int(child) for child in (thread / 'children').read_text().split())
            except FileNotFoundError:
                pass
        helpers = []
        for child in sorted(children):
            try:
                child_fields = (Path('/proc') / str(child) / 'stat').read_text().rsplit(')', 1)[1].split()
                helpers.append({'pid': child, 'start_ticks': child_fields[19]})
            except FileNotFoundError:
                pass
        return {'pid': pid, 'start_ticks': fields[19],
                'self_cpu_ticks': int(fields[11]) + int(fields[12]),
                'waited_child_cpu_ticks': int(fields[13]) + int(fields[14]),
                'threads': int(fields[17]), 'sampled_helpers': helpers}
    except (FileNotFoundError, ProcessLookupError):
        return {'pid': pid, 'unavailable': True}


def process_cost_summary(samples, names):
    result = {}
    for name in names:
        rows = [sample['processes'][name] for sample in samples]
        valid = [row for row in rows if not row.get('unavailable')]
        stable = bool(valid) and len(valid) == len(rows) and len({row['start_ticks'] for row in valid}) == 1
        helpers = {(child['pid'], child['start_ticks']) for row in valid for child in row['sampled_helpers']}
        result[name] = {'stable_controller': stable,
                       'self_cpu_ticks': valid[-1]['self_cpu_ticks'] - valid[0]['self_cpu_ticks'] if stable else None,
                       'waited_child_cpu_ticks': valid[-1]['waited_child_cpu_ticks'] - valid[0]['waited_child_cpu_ticks'] if stable else None,
                       'observed_helper_count_lower_bound': len(helpers),
                       'observed_helper_instances': sorted(helpers),
                       'sampled_max_helper_concurrency': max((len(row['sampled_helpers']) for row in valid), default=0),
                       'sampled_max_threads': max((row['threads'] for row in valid), default=0)}
    return result


def pss_summary(samples):
    result = {}
    for name in ('root', 'data_slice', 'aggregate'):
        members = ('root', 'data_slice') if name == 'aggregate' else (name,)
        values = [sum(row['groups'][member]['process_totals_bytes']['Pss'] for member in members)
                  for row in samples if all(row['groups'][member]['process_totals_bytes'].get('Pss') is not None
                                            for member in members)]
        result[name] = {'valid_samples': len(values), 'total_samples': len(samples),
                       'mean_bytes': sum(values) / len(values) if values else None,
                       'sampled_peak_bytes': max(values) if values else None}
    return result

def sample_guard_groups(label, seconds, event_kind=None, period=.02, *, process_samples=False, pss_period=None):
    from memory_helpers import cgroup_memory
    root = Path('/proc/1/root/sys/fs/cgroup')
    pids = {'root': int(Path('/run/ram-rescue-guard/state/path-guard.pid').read_text())}
    for name in ('rr-data-vm-ext4', 'rr-data-vm-vfat'):
        pids[name] = int((Path('/run/ram-rescue-data') / name / 'state/path-guard.pid').read_text())
    groups = {name: root / Path(f'/proc/{pid}/cgroup').read_text().strip().split('0::', 1)[1].lstrip('/')
              for name, pid in pids.items()}
    groups['data_slice'] = root / 'ramrescuedata.slice'
    before = {name: cgroup_memory(group) for name, group in groups.items()}
    event = None
    if event_kind:
        if event_kind == 'unrelated':
            event = Path('/sys/class/block/loop0/uevent')
        else:
            config = json.loads(Path('/run/ram-rescue-guard/config.json').read_text())
            event = next(path / 'uevent' for path in Path('/sys/class/block').glob('dm-*')
                         if (path / 'dm/name').read_text().strip() == config['map_name'])
        if not event.exists():
            raise RuntimeError('VM event source missing')
    samples = []
    pss_samples = []
    started = time.monotonic()
    next_pss = started
    if pss_period is not None and pss_period < period:
        raise ValueError('PSS sampling must not be faster than accounting sampling')
    next_event = started
    emitted_events = 0
    while True:
        now = time.monotonic()
        values = {}
        for name, group in groups.items():
            cpu = {key: int(value) for key, value in
                   (line.split() for line in (group / 'cpu.stat').read_text().splitlines())}
            values[name] = {'cpu': cpu, 'memory_bytes': int((group / 'memory.current').read_text())}
        sample = {'time': now, 'groups': values}
        if process_samples:
            sample['processes'] = {name: process_counters(pid) for name, pid in pids.items()}
        samples.append(sample)
        if pss_period is not None and now >= next_pss:
            pss_samples.append({'time': time.monotonic(), 'groups': {
                name: cgroup_memory(groups[name]) for name in ('root', 'data_slice')}})
            next_pss = started + (int((now - started) / pss_period) + 1) * pss_period
        if now - started >= seconds:
            break
        if event is not None and now >= next_event:
            # Ten genuine kernel uevents every 100ms, from an observer outside Guard.
            for _ in range(10):
                event.write_text('change\n')
                emitted_events += 1
            next_event = started + (int((now - started) / .1) + 1) * .1
        time.sleep(max(0, started + len(samples) * period - time.monotonic()))
    after = {name: cgroup_memory(group) for name, group in groups.items()}
    result = {'label': label, 'period_seconds': period, 'seconds': samples[-1]['time'] - started,
              'pids': pids, 'before': before, 'after': after, 'samples': samples, 'groups': {},
              # CPU bandwidth can be enabled only on the parent data slice.
              # A missing leaf cpu.max means inheritance, not an unlimited slice.
              'cpu_max': {name: (group / 'cpu.max').read_text().strip()
                          if (group / 'cpu.max').exists() else None for name, group in groups.items()},
              'emitted_events': emitted_events,
              'events_per_second': emitted_events / (samples[-1]['time'] - started),
              'data_slice_cpu_max': (root / 'ramrescuedata.slice/cpu.max').read_text().strip()}
    durations = [b['time'] - a['time'] for a, b in zip(samples, samples[1:])]
    result['window_seconds'] = {'min': min(durations), 'max': max(durations)}
    for name in [*groups, 'aggregate']:
        # The parent slice includes both data services; never add it to its children.
        members = ['root', 'data_slice'] if name == 'aggregate' else [name]
        result['groups'][name] = resource_summary(samples, members)
    if process_samples:
        result['process_cost'] = process_cost_summary(samples, pids)
        result['process_clock_ticks_per_second'] = os.sysconf('SC_CLK_TCK')
        result['helper_count_scope'] = '20ms observed live helper instances, lower bound; waited-child CPU uses cumulative proc ticks'
    if pss_period is not None:
        result['pss_period_seconds'] = pss_period
        result['pss_samples'] = pss_samples
        result['pss'] = pss_summary(pss_samples)
        result['memory_peak_scope'] = 'cgroup memory.peak is service-lifetime; memory_sampled_peak and PSS sampled peaks are phase-local'
    if label == 'recovery':
        files = {'root': Path('/run/ram-rescue-guard/state/path-events.jsonl')}
        files.update({name: Path('/run/ram-rescue-data') / name / 'state/path-events.jsonl'
                      for name in pids if name != 'root'})
        events = {name: [json.loads(line) for line in path.read_text().splitlines()]
                  for name, path in files.items()}
        result['events'] = {name: [event for event in rows if started <= event['time'] <= samples[-1]['time']]
                            for name, rows in events.items()}
        intervals = {}
        for name, rows in result['events'].items():
            waiting = next((row['time'] for row in rows if row['state'] == 'waiting'), None)
            ready = next((row['time'] for row in rows if row['state'] == 'ready' and waiting is not None
                          and row['time'] >= waiting), None)
            if waiting is not None and ready is not None:
                intervals[name] = (waiting, ready)
        if len(intervals) == len(pids):
            intervals['aggregate'] = (min(value[0] for value in intervals.values()),
                                      max(value[1] for value in intervals.values()))
        result['recovery_windows'] = {}
        for name, (waiting, ready) in intervals.items():
            left = max(index for index, row in enumerate(samples) if row['time'] <= waiting)
            right = next(index for index, row in enumerate(samples) if row['time'] >= ready)
            members = ['root', 'data_slice'] if name == 'aggregate' else [name]
            result['recovery_windows'][name] = {
                'waiting': waiting, 'ready': ready, 'journal_seconds': ready - waiting,
                'accounting': resource_summary(samples[left:right + 1], members)}
    return result


class PerformanceProbe(UnifiedProbe):
    def measure(self, name):
        kind = {'unrelated': 'unrelated', 'relevant': 'relevant'}.get(name)
        return sample_guard_groups(name, 20, kind)

    def start_sampler(self):
        output = Path('/run/performance-recovery.json')
        if output.exists():
            raise RuntimeError('Recovery measurement already exists')
        code = ("import sys,json;sys.path.insert(0,'/opt/vmprobe');import probe;"
                "result=probe.sample_guard_groups('recovery',16);"
                "open('/run/performance-recovery.json','w').write(json.dumps(result))")
        log = open('/run/performance-sampler.log', 'ab', buffering=0)
        try:
            worker = subprocess.Popen([sys.executable, '-c', code], stdout=log, stderr=log,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            log.close()
        return {'pid': worker.pid}

    def action(self, action):
        self.gate()
        if action == 'runtime_hashes':
            return {name: {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(directory.glob('*.py'))}
                    for name, directory in [('root', Path('/opt/guard')), ('data', Path('/opt/manager'))]}
        if action in ('quota5', 'quota10', 'quota20'):
            quota = action.removeprefix('quota') + '%'
            return {unit: self.host('/usr/bin/systemctl', 'set-property', '--runtime', unit,
                                    'CPUQuota=' + quota, 'CPUQuotaPeriodSec=20ms') for unit in
                    ('ram-rescue-guard.service', 'ramrescuedata.slice')}
        if action in ('idle', 'unrelated', 'relevant'):
            return self.measure(action)
        if action == 'start_sampler':
            return self.start_sampler()
        if action == 'sampler_result':
            return self.read('/run/performance-recovery.json')
        return super().action(action)
