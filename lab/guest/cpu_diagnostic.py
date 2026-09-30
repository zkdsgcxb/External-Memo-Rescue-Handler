"""VM-only CPU accounting and scheduler trace investigation, never production."""
import base64
import gzip


CPU_DIAG = Path('/run/cpu-diagnostic')
TRACE_ROOT = Path('/proc/1/root/sys/kernel/tracing')


def cpu_diag_task(tid):
    try:
        raw = Path(f'/proc/{tid}/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        return {'tid': tid, 'comm': raw[raw.find('(') + 1:raw.rfind(')')],
                'policy': os.sched_getscheduler(tid), 'priority': os.sched_getparam(tid).sched_priority,
                'nice': int(fields[16]), 'start_ticks': int(fields[19]),
                'cgroup': Path(f'/proc/{tid}/cgroup').read_text().strip(),
                'cmdline': Path(f'/proc/{tid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')}
    except (FileNotFoundError, ProcessLookupError):
        return None


def cpu_diag_collect():
    config = json.loads((CPU_DIAG / 'config.json').read_text())
    samples, tasks = [], {}
    started = time.monotonic()
    marker = TRACE_ROOT / 'trace_marker'
    marker.write_text('CPU_DIAG_START\n')
    while not (CPU_DIAG / 'stop').exists() and time.monotonic() - started < 120:
        row = {'start': time.monotonic(), 'groups': {}}
        for name, location in config['groups'].items():
            group = Path(location)
            begin = time.monotonic()
            cpu = dict((key, int(value)) for key, value in
                       (line.split() for line in (group / 'cpu.stat').read_text().splitlines()))
            end = time.monotonic()
            row['groups'][name] = {'before': begin, 'after': end, 'cpu': cpu}
            if name != 'data_slice':
                for number in (group / 'cgroup.threads').read_text().split():
                    tid = int(number)
                    observed = cpu_diag_task(tid)
                    if observed and observed != tasks.get(number, {}).get('last'):
                        record = tasks.setdefault(number, {'observations': []})
                        record['last'] = observed
                        record['observations'].append({'time': end, **observed})
        row['stat'] = Path('/proc/stat').read_text().splitlines()[:3]
        row['end'] = time.monotonic()
        samples.append(row)
        time.sleep(max(0, started + len(samples) * .02 - time.monotonic()))
    marker.write_text('CPU_DIAG_STOP\n')
    (TRACE_ROOT / 'tracing_on').write_text('0\n')
    stats = {path.parent.name: path.read_text() for path in
             sorted((TRACE_ROOT / 'per_cpu').glob('cpu*/stats'))}
    trace = (TRACE_ROOT / 'trace').read_bytes()
    (CPU_DIAG / 'trace.txt.gz').write_bytes(gzip.compress(trace, compresslevel=1))
    report = {'config': config, 'samples': samples, 'tasks': tasks, 'trace_stats': stats,
              'started': started, 'ended': time.monotonic()}
    (CPU_DIAG / 'accounting.json.gz').write_bytes(gzip.compress(json.dumps(report).encode(), compresslevel=1))
    (CPU_DIAG / 'done').write_text('complete\n')


class CpuDiagnostic(UnifiedProbe):
    def diagnostic_start(self):
        if CPU_DIAG.exists():
            raise RuntimeError('Use a fresh diagnostic VM')
        CPU_DIAG.mkdir()
        for unit in ('ram-rescue-guard.service', 'ramrescuedata.slice'):
            self.host('/usr/bin/systemctl', 'set-property', '--runtime', unit,
                      'CPUQuota=20%', 'CPUQuotaPeriodSec=20ms')
        if not (TRACE_ROOT / 'tracing_on').exists():
            self.host('/usr/bin/mount', '-t', 'tracefs', 'tracefs', '/sys/kernel/tracing')
        (TRACE_ROOT / 'tracing_on').write_text('0\n')
        (TRACE_ROOT / 'current_tracer').write_text('nop\n')
        (TRACE_ROOT / 'trace_clock').write_text('mono\n')
        (TRACE_ROOT / 'buffer_size_kb').write_text('16384\n')
        (TRACE_ROOT / 'trace').write_text('')
        events = ['sched/sched_switch', 'sched/sched_stat_runtime', 'sched/sched_process_fork',
                  'sched/sched_process_exec', 'sched/sched_process_exit', 'sched/sched_pi_setprio',
                  'cgroup/cgroup_attach_task', 'syscalls/sys_enter_ioctl', 'syscalls/sys_exit_ioctl',
                  'syscalls/sys_enter_sched_setscheduler', 'syscalls/sys_exit_sched_setscheduler',
                  'syscalls/sys_enter_sched_setattr', 'syscalls/sys_exit_sched_setattr',
                  'irq/irq_handler_entry', 'irq/irq_handler_exit', 'irq/softirq_entry', 'irq/softirq_exit']
        enabled = []
        for name in events:
            path = TRACE_ROOT / 'events' / name / 'enable'
            if path.exists():
                path.write_text('1\n')
                enabled.append(name)
        if not set(events[:5]) <= set(enabled):
            raise RuntimeError('Required scheduler tracepoints are unavailable')
        function = 'throttle_cfs_rq_work'
        available = (TRACE_ROOT / 'available_filter_functions').read_text().splitlines()
        if any(line.split()[0] == function for line in available):
            (TRACE_ROOT / 'set_ftrace_filter').write_text(function + '\n')
            (TRACE_ROOT / 'current_tracer').write_text('function\n')
        pids = {'root': int(Path('/run/ram-rescue-guard/state/path-guard.pid').read_text())}
        pids.update({spec['name']: int((self.directory(spec['name']) / 'state/path-guard.pid').read_text())
                     for spec in self.specs})
        root = Path('/proc/1/root/sys/fs/cgroup')
        groups = {name: root / Path(f'/proc/{pid}/cgroup').read_text().strip().split('0::', 1)[1].lstrip('/')
                  for name, pid in pids.items()}
        groups['data_slice'] = root / 'ramrescuedata.slice'
        config = {'pids': pids, 'groups': {name: str(path) for name, path in groups.items()},
                  'events': enabled, 'trace_clock': (TRACE_ROOT / 'trace_clock').read_text(),
                  'tracer': (TRACE_ROOT / 'current_tracer').read_text().strip(),
                  'cpu_max': {name: (path / 'cpu.max').read_text().strip() if (path / 'cpu.max').exists() else None
                              for name, path in groups.items()},
                  'cpu_max_burst': (root / 'ramrescuedata.slice/cpu.max.burst').read_text().strip(),
                  'initial_tasks': {name: [cpu_diag_task(int(tid)) for tid in (path / 'cgroup.threads').read_text().split()]
                                    for name, path in groups.items() if name != 'data_slice'},
                  'scope': 'Diagnostic tracing adds overhead; these are not production performance samples'}
        (CPU_DIAG / 'config.json').write_text(json.dumps(config))
        (TRACE_ROOT / 'tracing_on').write_text('1\n')
        code = "import sys;sys.path.insert(0,'/opt/vmprobe');import probe;probe.cpu_diag_collect()"
        with (CPU_DIAG / 'sampler.log').open('wb') as log:
            worker = subprocess.Popen([sys.executable, '-c', code], stdout=log, stderr=log,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
        return {'pid': worker.pid, 'config': config}

    def diagnostic_stop(self):
        (CPU_DIAG / 'stop').write_text('stop\n')
        deadline = time.monotonic() + 20
        while not (CPU_DIAG / 'done').exists():
            if time.monotonic() >= deadline:
                raise RuntimeError((CPU_DIAG / 'sampler.log').read_text())
            time.sleep(.05)
        return {'files': {name: base64.b64encode((CPU_DIAG / name).read_bytes()).decode()
                          for name in ('accounting.json.gz', 'trace.txt.gz')}}

    def action(self, action):
        self.gate()
        if action == 'diagnostic_start':
            return self.diagnostic_start()
        if action == 'diagnostic_stop':
            return self.diagnostic_stop()
        return super().action(action)
