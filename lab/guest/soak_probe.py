"""VM-only settled memory checkpoints across repeated full recovery cycles."""
import re


def mapping_summary(text):
    """Group smaps evidence without attributing unnamed arenas to an allocator."""
    fields = ('Size', 'Rss', 'Pss', 'Anonymous', 'Private_Dirty')
    mappings = []
    current = None
    for line in text.splitlines():
        if re.match(r'^[0-9a-f]+-[0-9a-f]+ ', line):
            header = line.split(None, 5)
            name = header[5] if len(header) == 6 else ''
            kind = ('heap' if name == '[heap]' else 'stack' if name.startswith('[stack') else
                    'anonymous' if not name or name.startswith('[anon:') else
                    'special' if name.startswith('[') else 'file')
            current = {'kind': kind, 'name': name, 'permissions': header[1],
                       **{field: 0 for field in fields}}
            mappings.append(current)
        elif current is not None:
            pieces = line.split()
            if len(pieces) == 3 and pieces[0][:-1] in fields and pieces[2] == 'kB':
                current[pieces[0][:-1]] = int(pieces[1]) * 1024
    groups = {}
    for mapping in mappings:
        group = groups.setdefault(mapping['kind'], {'mapping_count': 0, **{field: 0 for field in fields}})
        group['mapping_count'] += 1
        for field in fields:
            group[field] += mapping[field]
    return {'groups_bytes': groups, 'mapping_count': len(mappings),
            'largest_resident_anonymous_regions': sorted(
                (mapping for mapping in mappings if mapping['kind'] in ('heap', 'stack', 'anonymous')),
                key=lambda mapping: mapping['Pss'], reverse=True)[:8]}


class SoakProbe(PerformanceProbe):
    def accounting(self):
        from memory_helpers import cgroup_memory
        started = time.monotonic()
        pids = {'root': int(Path('/run/ram-rescue-guard/state/path-guard.pid').read_text())}
        for spec in self.specs:
            pids[spec['name']] = int((self.directory(spec['name']) / 'state/path-guard.pid').read_text())
        root = self.ROOT / 'sys/fs/cgroup'
        relative = Path('/proc/' + str(pids['root']) + '/cgroup').read_text().strip().split('0::', 1)[1].lstrip('/')
        groups = {'root': root / relative, 'data_slice': root / 'ramrescuedata.slice'}
        if groups['root'] == groups['data_slice'] or groups['data_slice'] in groups['root'].parents:
            raise RuntimeError('Accounting groups overlap')
        values = {}
        for name, group in groups.items():
            memory = cgroup_memory(group)
            cpu = {key: int(value) for key, value in
                   (line.split() for line in (group / 'cpu.stat').read_text().splitlines())}
            values[name] = {'memory': memory, 'cpu': cpu,
                            'cpu_max': (group / 'cpu.max').read_text().strip()}
        controllers = {}
        for name, pid in pids.items():
            process = self.process(pid)
            if not process:
                raise RuntimeError('Controller vanished during checkpoint')
            status = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines())
            controllers[name] = {'process': process, 'fd_count': len(list(Path(f'/proc/{pid}/fd').iterdir())),
                                 'threads': int(status['Threads']),
                                 'mappings': mapping_summary(Path(f'/proc/{pid}/smaps').read_text())}
        actual_pids = [pid for value in values.values() for pid in value['memory']['process_pids']]
        if len(actual_pids) != len(set(actual_pids)):
            raise RuntimeError('Process would be counted twice across cgroups')
        aggregate = {
            'memory_current_bytes': sum(value['memory']['files']['memory.current'] for value in values.values()),
            'cpu_usage_usec': sum(value['cpu']['usage_usec'] for value in values.values()),
            'cpu_throttled_usec': sum(value['cpu'].get('throttled_usec', 0) for value in values.values()),
            'process_pids': sorted(actual_pids),
        }
        for field in ('Rss', 'Pss', 'Swap', 'SwapPss'):
            readings = [value['memory']['process_totals_bytes'][field] for value in values.values()]
            aggregate[field + '_bytes'] = sum(readings) if all(value is not None for value in readings) else None
        for field in ('anon', 'file', 'kernel', 'shmem', 'slab', 'pagetables', 'kernel_stack'):
            aggregate['memory_stat_' + field + '_bytes'] = sum(
                value['memory']['files']['memory.stat'].get(field, 0) for value in values.values())
        return {'started': started, 'finished': time.monotonic(), 'groups': values,
                'controllers': controllers, 'aggregate': aggregate,
                'only_controller_processes': set(actual_pids) == set(pids.values()),
                'scope': 'root service plus data parent slice; descendants included once; observer and workloads excluded'}

    def checkpoint(self):
        windows = {}
        events = {'root': Path('/run/ram-rescue-guard/state/path-events.jsonl')}
        events.update({spec['name']: self.directory(spec['name']) / 'state/path-events.jsonl' for spec in self.specs})
        for name, path in events.items():
            files = [path.with_name('path-events.previous.jsonl'), path]
            rows = [json.loads(line) for file in files if file.exists()
                    for line in file.read_text().splitlines()]
            waiting = next((row for row in reversed(rows) if row['state'] == 'waiting'), None)
            ready = next((row for row in reversed(rows) if row['state'] == 'ready'), None)
            if waiting and ready and ready['time'] >= waiting['time']:
                windows[name] = {'waiting': waiting['time'], 'ready': ready['time'],
                                 'seconds': ready['time'] - waiting['time'],
                                 'recoveries': ready.get('recoveries')}
        return {'accounting': self.accounting(), 'recovery_windows': windows,
                'snapshot': self.snapshot()}

    def action(self, action):
        self.gate()
        if action == 'checkpoint':
            return self.checkpoint()
        return super().action(action)
