"""VM-only, sequential C++/Python observer measurement with equal health work.

Called by the RAM probe, outside the measured systemd cgroups. Payload directory
must contain guard-observe, reference.py and runtime/dm_monitor.py. Results keep
raw 100 ms windows, startup, warmup and steady state distinct.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import time


ROOT = Path('/proc/1/root')
# The Ubuntu fixture mounts /run with noexec. This isolated guest-only payload
# uses its writable overlay; the experiment never detaches the root disk.
PAYLOAD = Path('/usr/local/lib/ram-rescue-native-benchmark')


def host(*args, check=True):
    result = subprocess.run(['/bin/chroot', str(ROOT), *args], text=True, check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    if check and result.returncode:
        raise RuntimeError(f'Guest command failed {args!r}: {result.stderr} {result.stdout}')
    return result


def systemctl(*args):
    return host('/usr/bin/systemctl', *args).stdout


def sample(group, pid):
    clock = time.monotonic()
    cpu = dict(line.split() for line in (group / 'cpu.stat').read_text().splitlines())
    memory = int((group / 'memory.current').read_text())
    try:
        rollup = Path(f'/proc/{pid}/smaps_rollup').read_text()
        private = {key: int(value) * 1024 for key, value in
                   re.findall(r'^(Rss|Pss|Private_Clean|Private_Dirty):\s+(\d+) kB$', rollup, re.M)}
    except FileNotFoundError:
        private = {}
    return {'time': clock, 'cpu_usec': int(cpu['usage_usec']), 'memory_bytes': memory,
            'process_bytes': private}


def summarize(samples):
    if len(samples) < 2:
        raise RuntimeError('Insufficient benchmark samples')
    duration = samples[-1]['time'] - samples[0]['time']
    windows = [(b['cpu_usec'] - a['cpu_usec']) / (b['time'] - a['time']) / 10000
               for a, b in zip(samples, samples[1:])]
    memory = [row['memory_bytes'] for row in samples]
    process = {}
    for key in ['Rss', 'Pss', 'Private_Clean', 'Private_Dirty']:
        values = [row['process_bytes'][key] for row in samples if key in row['process_bytes']]
        process[key] = {'mean_bytes': sum(values) / len(values), 'peak_bytes': max(values)} if values else None
    return {'duration_seconds': duration,
            'cpu_mean_one_core_percent': (samples[-1]['cpu_usec'] - samples[0]['cpu_usec']) / duration / 10000,
            'cpu_peak_sample_window_percent': max(windows),
            'memory_mean_bytes': sum(memory) / len(memory), 'memory_peak_sample_bytes': max(memory),
            'process_bytes': process, 'samples': samples}


def observer_command(kind, config, seconds):
    runtime = str(PAYLOAD / ('guard-observe' if kind == 'cpp' else 'reference.py'))
    command = [runtime] if kind == 'cpp' else ['/usr/bin/python3', runtime]
    return [*command, '--map', config['map_name'], '--uuid', config['map_uuid'],
            '--node', config['initial_node'], '--sys-path', config['initial_sys_path'],
            '--diskseq', str(config['initial_diskseq']), '--seconds', str(seconds)]


def run_one(kind, config, sequence, *, warmup=5, seconds=20):
    unit = f'ram-rescue-native-bench-{kind}-{sequence}.service'
    command = observer_command(kind, config, warmup + seconds + 2)
    log = str(PAYLOAD / f'{kind}-{sequence}.jsonl')
    launched = time.monotonic()
    host('/usr/bin/systemd-run', '--unit', unit, '--quiet',
         '--property=Type=exec', '--property=RemainAfterExit=yes',
         '--property=CPUAccounting=yes', '--property=MemoryAccounting=yes',
         '--property=MemorySwapMax=0', '--property=MemoryMax=128M',
         '--property=StandardOutput=append:' + log,
         '--property=StandardError=append:' + log, *command)
    properties = dict(line.split('=', 1) for line in systemctl(
        'show', unit, '-p', 'MainPID,ControlGroup,ExecMainStartTimestampMonotonic').splitlines())
    pid = int(properties['MainPID'])
    group = ROOT / 'sys/fs/cgroup' / properties['ControlGroup'].lstrip('/')
    samples = []
    try:
        while time.monotonic() < launched + warmup + seconds:
            samples.append(sample(group, pid))
            time.sleep(.1)
        before_stop = systemctl('show', unit, '-p',
                               'MainPID,CPUUsageNSec,MemoryPeak,Result,ExecMainStatus')
        rows = [json.loads(line) for line in (ROOT / log.lstrip('/')).read_text().splitlines()]
        if not rows or any(row.get('state') != 'ready' for row in rows):
            raise RuntimeError('Observer did not remain healthy: ' + repr(rows))
        steady = [row for row in samples if row['time'] >= launched + warmup]
        process_start = int(properties['ExecMainStartTimestampMonotonic']) / 1e6
        startup_duration = samples[0]['time'] - process_start
        return {'kind': kind, 'unit': unit,
                'startup': {'duration_to_first_sample_seconds': startup_duration,
                            'cpu_usec': samples[0]['cpu_usec'],
                            'cpu_mean_one_core_percent': samples[0]['cpu_usec'] / startup_duration / 10000,
                            'memory_bytes': samples[0]['memory_bytes'],
                            'process_bytes': samples[0]['process_bytes']},
                'whole_observation': summarize(samples), 'steady': summarize(steady),
                'systemd_before_stop': before_stop, 'health_checks': len(rows), 'observations': rows,
                'one_logical_core_equals_percent': 100, 'nominal_sample_seconds': .1}
    finally:
        systemctl('stop', unit)
        host('/usr/bin/systemctl', 'reset-failed', unit, check=False)


def prepare(payload):
    product = Path('/sys/class/dmi/id/product_name').read_text().strip()
    if product != 'RAMRescueLab' or 'ram_rescue_lab=1' not in Path('/proc/cmdline').read_text().split():
        raise RuntimeError('Native benchmark only runs in the disposable QEMU fixture')
    destination = ROOT / str(PAYLOAD).lstrip('/')
    shutil.copytree(payload, destination)
    (destination / 'guard-observe').chmod(0o755)


def compare(payload, config, *, repeats=3, warmup=5, seconds=20):
    prepare(payload)
    results = []
    for sequence in range(repeats):
        # Alternate order to reduce cache/warmup bias between implementations.
        order = ('cpp', 'python') if sequence % 2 == 0 else ('python', 'cpp')
        for kind in order:
            results.append(run_one(kind, config, sequence, warmup=warmup, seconds=seconds))
    return {'scope': 'read-only observers only; excludes recovery, Guard, workload and sampler',
            'repeats': repeats, 'warmup_seconds': warmup, 'requested_steady_seconds': seconds,
            'same_map_name': config['map_name'], 'results': results,
            'limitation': 'C++ is not a recovery service; these numbers do not measure full Guard savings'}


def start_fault(payload, config):
    """Correctness only: the real Python Guard remains the sole controller."""
    prepare(payload)
    rejected = {}
    for key, bad in [('map_uuid', 'UNREGISTERED-VM-UUID'),
                     ('initial_diskseq', config['initial_diskseq'] + 1)]:
        wrong = {**config, key: bad}
        result = host(*observer_command('cpp', wrong, .2), check=False)
        rejected[key] = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    units = []
    for kind in ('cpp', 'python'):
        unit = f'ram-rescue-native-fault-{kind}.service'
        log = str(PAYLOAD / f'fault-{kind}.jsonl')
        host('/usr/bin/systemd-run', '--unit', unit, '--quiet', '--property=Type=exec',
             '--property=RemainAfterExit=yes', '--property=StandardOutput=append:' + log,
             '--property=StandardError=append:' + log, *observer_command(kind, config, 8))
        units.append(unit)
    return {'units': units, 'rejected': rejected}


def finish_fault():
    result = {}
    for kind in ('cpp', 'python'):
        unit = f'ram-rescue-native-fault-{kind}.service'
        log = ROOT / str(PAYLOAD / f'fault-{kind}.jsonl').lstrip('/')
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        result[kind] = {'observations': rows, 'systemd': systemctl(
            'show', unit, '-p', 'MainPID,Result,ExecMainStatus'),
            'ready_before_fault': bool(rows) and rows[0]['state'] == 'ready',
            'detected_unavailable': any(row['state'] == 'path-unavailable' for row in rows)}
        systemctl('stop', unit)
        host('/usr/bin/systemctl', 'reset-failed', unit, check=False)
    return result
