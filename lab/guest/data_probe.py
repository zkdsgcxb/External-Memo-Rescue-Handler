"""VM-only orchestration for two precreated removable-data multipath maps.

Appended to the protected Ubuntu RAM observer. The workload retains its file
descriptors across USB removal and reads each durable write with O_DIRECT.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, '/opt/data-guard')


DATA_WORKLOAD = r'''import json,mmap,os,signal,sys,time
running=True
def stop(*_):
    global running
    running=False
signal.signal(signal.SIGTERM,stop)
name,mount=sys.argv[1:]
path=mount+'/held-fd.data'
fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
directory=os.open(mount,os.O_RDONLY|os.O_DIRECTORY)
os.fsync(directory);os.close(directory)
direct=os.open(path,os.O_RDONLY|os.O_DIRECT)
buffer=mmap.mmap(-1,4096)
with open('/run/'+name+'-workload.jsonl','a',buffering=1) as log:
    seq=0
    while running:
        seq+=1
        payload=(str(seq)+'\n').encode().ljust(4096,b'x')
        start=time.monotonic()
        record={'seq':seq,'start':start}
        try:
            pending=memoryview(payload)
            while pending:
                count=os.write(fd,pending)
                if count<=0:raise OSError('write made no progress')
                pending=pending[count:]
            os.fsync(fd)
            count=os.preadv(direct,[buffer],(seq-1)*4096)
            if count!=4096 or buffer[:]!=payload:raise OSError('direct read differs')
            record['ok']=True
        except BaseException as exc:
            record.update(ok=False,error=repr(exc))
            running=False
        record['elapsed']=time.monotonic()-start
        log.write(json.dumps(record)+'\n')
        time.sleep(.1)
os.close(direct);os.close(fd);buffer.close()
'''


class DataProbe:
    ROOT = Path('/proc/1/root')

    def __init__(self, gate, systemctl, process, specs):
        self.gate = gate
        self.systemctl = systemctl
        self.process = process
        self.specs = specs

    def host(self, *args, timeout=20, check=True):
        result = subprocess.run(
            ['/bin/chroot', str(self.ROOT), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        response = {'returncode': result.returncode, 'stdout': result.stdout,
                    'stderr': result.stderr}
        if check and result.returncode:
            raise RuntimeError(f'Guest command {args!r}: {response!r}')
        return response

    @staticmethod
    def read(path):
        path = Path(path)
        return json.loads(path.read_text()) if path.exists() else None

    @staticmethod
    def directory(name):
        return Path('/run/ram-rescue-data') / name

    def records(self, name):
        path = Path('/run') / (name + '-workload.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot(self):
        from dm_monitor import DeviceMapper
        mapper = DeviceMapper()
        mounts = Path('/proc/1/mountinfo').read_text().splitlines()
        devices = {}
        for spec in self.specs:
            name = spec['name']
            directory = self.directory(name)
            worker = self.read(directory / 'worker.json')
            records = self.records(name)
            sysnode = next((p for p in Path('/sys/class/block').glob('dm-*')
                            if (p / 'dm/name').read_text().strip() == name), None)
            devices[name] = {
                'state': self.read(directory / 'state/path-state.json'),
                'transaction': self.read(directory / 'state/path-transaction.json'),
                'worker': worker, 'process': self.process(worker['pid']) if worker else None,
                'ack_count': len(records), 'errors': [row for row in records if not row['ok']],
                'latest': records[-1] if records else None,
                'mount': next((line for line in mounts if line.split()[4] == '/mnt/' + name), None),
                'slaves': [p.name for p in (sysnode / 'slaves').iterdir()] if sysnode else [],
                'dm': mapper.query(name) if sysnode else None,
                'dm_active': mapper.snapshot(name)['active'] if sysnode else None,
            }
        return {'devices': devices, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'pid1': self.process(1)}

    def configure(self):
        from path_guard import table
        tools = self.host('/usr/bin/systemctl', 'is-active', 'multipathd.service', check=False)
        if tools['returncode'] == 0:
            raise RuntimeError('VM stock multipathd competes with the experiment')
        prepared = {}
        for spec in self.specs:
            name = spec['name']
            link = Path('/dev/disk/by-partuuid') / spec['partuuid']
            node = str(link.resolve(strict=True))
            sectors = int((Path('/sys/class/block') / Path(node).name / 'size').read_text())
            subprocess.run(['/sbin/dmsetup', '--noudevsync', 'create', name,
                            '--uuid', spec['map_uuid'], '--table', table(sectors, node)], check=True)
            subprocess.run(['/sbin/dmsetup', '--noudevsync', 'mknodes', name], check=True)
            enrollment_path = '/run/' + name + '-enrollment.json'
            enrolled = self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/usr/bin/python3', '/run/data-launcher-src/guard/data.py',
                                 'enroll', '--map', name, '--partition', node,
                                 '--output', enrollment_path)
            started = self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/usr/bin/python3', '/run/data-launcher-src/guard/data.py',
                                'start', '--enrollment', enrollment_path, timeout=45)
            prepared[name] = {'enrollment': self.read(enrollment_path),
                               'enroll_command': enrolled, 'start_command': started}
            (self.ROOT / 'mnt' / name).mkdir(parents=True)
            self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/mount',
                      '-t', spec['fs_type'], '/dev/mapper/' + name, '/mnt/' + name)
        return {'enrollments': prepared, 'snapshot': self.snapshot()}

    def start(self):
        script = self.ROOT / 'root/data-guard-workload.py'
        script.write_text(DATA_WORKLOAD)
        for spec in self.specs:
            name = spec['name']
            with (Path('/run') / (name + '-stderr.log')).open('ab', buffering=0) as log:
                child = subprocess.Popen(
                    ['/bin/chroot', str(self.ROOT), '/usr/bin/python3',
                     '/root/data-guard-workload.py', name, '/mnt/' + name],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            (self.directory(name) / 'worker.json').write_text(json.dumps(self.process(child.pid)))
        return self.snapshot()

    def measure_idle(self):
        """Observe the aggregate data slice; the observer runs outside it."""
        # The rescue /sys bind predates PID 1 mounting cgroup2. Follow PID 1's
        # root to observe its mounted hierarchy without adding a rescue mount.
        group = self.ROOT / 'sys/fs/cgroup/ramrescuedata.slice'
        time.sleep(2)
        samples = []
        for _ in range(201):
            counters = dict(line.split() for line in (group / 'cpu.stat').read_text().splitlines())
            samples.append({'time': time.monotonic(), 'cpu_usec': int(counters['usage_usec']),
                            'memory_bytes': int((group / 'memory.current').read_text())})
            if len(samples) < 201:
                time.sleep(.1)
        duration = samples[-1]['time'] - samples[0]['time']
        windows = [(b['cpu_usec'] - a['cpu_usec']) / 1e6 / (b['time'] - a['time']) * 100
                   for a, b in zip(samples, samples[1:])]
        return {'scope': 'aggregate of both data Guards, excludes root Guard and observer',
                'one_cpu_equals_percent': 100, 'duration_seconds': duration,
                'cpu_mean_percent': (samples[-1]['cpu_usec'] - samples[0]['cpu_usec']) / 1e6 / duration * 100,
                'cpu_peak_sample_window_percent': max(windows),
                'nominal_sample_seconds': .1,
                'memory_mean_bytes': sum(s['memory_bytes'] for s in samples) / len(samples),
                'memory_peak_bytes': max(s['memory_bytes'] for s in samples),
                'cpu_max': (group / 'cpu.max').read_text().strip(),
                'memory_max': (group / 'memory.max').read_text().strip(),
                'memory_swap_max': (group / 'memory.swap.max').read_text().strip(),
                'samples': samples}

    def audit(self):
        report = {}
        for spec in self.specs:
            name = spec['name']
            worker = self.read(self.directory(name) / 'worker.json')
            current = self.process(worker['pid'])
            if not current or current['start_ticks'] != worker['start_ticks'] or current['state'] in ('Z', 'X'):
                raise RuntimeError('Original data workload is no longer alive: ' + name)
            os.kill(worker['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(not (proc := self.process(self.read(self.directory(s['name']) / 'worker.json')['pid']))
                   or proc['state'] == 'Z' for s in self.specs):
                break
            time.sleep(.02)
        else:
            raise RuntimeError('Data workload did not stop after recovery')
        for spec in self.specs:
            name = spec['name']
            records = self.records(name)
            if not records or any(not row['ok'] or row['seq'] != i for i, row in enumerate(records, 1)):
                raise RuntimeError('Data ACK log empty, failed or noncontiguous: ' + name)
            expected = b''.join((str(row['seq']) + '\n').encode().ljust(4096, b'x') for row in records)
            actual = (self.ROOT / 'mnt' / name / 'held-fd.data').read_bytes()
            report[name] = {'ack_count': len(records), 'bytes': len(expected),
                            'hash_matches': actual == expected,
                            'sha256': hashlib.sha256(actual).hexdigest(),
                            'max_write_and_direct_read_seconds': max(row['elapsed'] for row in records),
                            'records': records}
        return {'audit': report, 'snapshot': self.snapshot()}

    def stop(self):
        from dm_monitor import DeviceMapper
        retained = {}
        for spec in self.specs:
            name = spec['name']
            self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/umount', '/mnt/' + name)
            result = self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/usr/bin/python3',
                               '/run/data-launcher-src/guard/data.py', 'stop', '--map', name)
            retained[name] = {'command': result, 'map': DeviceMapper().snapshot(name),
                              'rule_retained': (Path('/run/udev/rules.d') /
                                                ('58-ram-rescue-data-' + name + '.rules')).is_file()}
            subprocess.run(['/sbin/dmsetup', '--noudevsync', 'remove', name], check=True)
        return {'snapshot': self.snapshot(), 'after_launcher_stop': retained}

    def logs(self):
        result = {}
        for spec in self.specs:
            name = spec['name']
            folder = self.directory(name)
            result[name] = {'journal': self.host('/usr/bin/journalctl', '-b', '--no-pager', '-u', 'ram-rescue-data-' + name + '.service'),
                            'state_files': {p.name: p.read_text() for p in (folder / 'state').glob('*.json*')},
                            'workload_stderr': (Path('/run') / (name + '-stderr.log')).read_text()
                            if (Path('/run') / (name + '-stderr.log')).exists() else None}
        result['kernel_log'] = subprocess.check_output(['/bin/dmesg'], text=True)
        return result

    def action(self, action):
        self.gate()
        actions = {'configure': self.configure, 'start': self.start, 'snapshot': self.snapshot,
                   'measure_idle': self.measure_idle,
                   'audit': self.audit, 'stop': self.stop, 'logs': self.logs}
        if action not in actions:
            raise ValueError('Unknown data-probe action: ' + action)
        return actions[action]()
