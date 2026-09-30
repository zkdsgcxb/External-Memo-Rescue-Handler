"""RAM-only ARM64 Linux guest; control via an isolated virtio serial port."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

sys.path.insert(0, '/opt/guard')
from data_guard import collect
from dm_monitor import DeviceMapper, probe_paths
from linux_abi import abi_report
from path_guard import table


NAME = 'rr-data-arm64'
DIRECTORY = Path('/run/ram-rescue-data') / NAME
guard = worker = None


def call(*arguments):
    return subprocess.check_output(arguments, text=True, stderr=subprocess.STDOUT,
                                   timeout=20)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def snapshot():
    records = Path('/run/' + NAME + '-workload.jsonl')
    rows = [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
    return {'kernel': os.uname().release, 'abi': abi_report(),
            'state': read_json(DIRECTORY / 'state/path-state.json'),
            'transaction': read_json(DIRECTORY / 'state/path-transaction.json'),
            'guard_pid': guard.pid if guard else None,
            'guard_running': guard is not None and guard.poll() is None,
            'worker_pid': worker.pid if worker else None,
            'worker_running': worker is not None and worker.poll() is None,
            'records': rows,
            'guard_log': Path('/run/guard.log').read_text() if guard else None,
            'mountinfo': Path('/proc/self/mountinfo').read_text(),
            'dm': DeviceMapper().snapshot(NAME) if guard else None}


def configure():
    global guard, worker
    deadline = time.monotonic() + 30
    while True:
        partitions = [Path('/dev') / path.name for path in Path('/sys/class/block').iterdir()
                      if (path / 'partition').exists()]
        if len(partitions) == 1:
            node = partitions[0]
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Expected exactly one USB test partition')
        time.sleep(.1)
    sectors = int((Path('/sys/class/block') / node.name / 'size').read_text())
    call('/sbin/dmsetup', '--noudevsync', 'create', NAME,
         '--uuid', 'RAMRESCUE-DATA-ARM64', '--table', table(sectors, str(node)))
    call('/sbin/dmsetup', '--noudevsync', 'mknodes', NAME)
    enrollment = collect(NAME, str(node))
    (DIRECTORY / 'state').mkdir(parents=True)
    (DIRECTORY / 'identity.json').write_text(json.dumps(enrollment['identity']))
    config = DIRECTORY / 'config.json'
    config.write_text(json.dumps(enrollment['guard']))
    log = Path('/run/guard.log').open('wb')
    guard = subprocess.Popen(['/usr/bin/python3.12', '/opt/guard/path_guard.py',
                              '--config', str(config)], stdout=log, stderr=log)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = read_json(DIRECTORY / 'state/path-state.json')
        if state and state['state'] == 'ready':
            break
        if guard.poll() is not None:
            raise RuntimeError(Path('/run/guard.log').read_text())
        time.sleep(.1)
    else:
        raise RuntimeError('Guard did not become ready')
    Path('/mnt/data').mkdir(parents=True)
    call('/bin/busybox', 'mount', '-t', 'ext4', '/dev/mapper/' + NAME, '/mnt/data')
    worker = subprocess.Popen(['/usr/bin/python3.12', '/opt/workload.py', NAME, '/mnt/data'])
    return {'enrollment': enrollment, 'probe': probe_paths('/dev/mapper/' + NAME, 1),
            'snapshot': snapshot()}


def audit():
    before = snapshot()
    worker.send_signal(signal.SIGTERM)
    worker.wait(timeout=10)
    result = snapshot()
    rows = result['records']
    with Path('/mnt/data/held-fd.data').open('rb') as stream:
        for row in rows:
            expected = (str(row['seq']) + '\n').encode().ljust(4096, b'x')
            if stream.read(4096) != expected:
                raise RuntimeError('Durable data differs from acknowledged sequence')
        if stream.read(1):
            raise RuntimeError('Unexpected data after last acknowledged sequence')
    result['original_worker_alive_before_stop'] = before['worker_running']
    result['direct_read_and_file_content_match'] = True
    call('/bin/busybox', 'umount', '/mnt/data')
    result['cleanly_unmounted'] = True
    return result


def main():
    port = os.open('/dev/vport0p1', os.O_RDWR)
    print('ARM64_GUEST_READY', flush=True)
    with os.fdopen(port, 'r+b', buffering=0) as channel:
        while True:
            line = channel.readline()
            # A virtio port reports EOF until its host socket connects. PID 1
            # must stay alive while the harness finishes opening the channel.
            if not line:
                time.sleep(.05)
                continue
            request = json.loads(line)
            response = {'id': request['id']}
            try:
                action = request['action']
                result = {'configure': configure, 'snapshot': snapshot, 'audit': audit}[action]()
                response.update(ok=True, value=result)
            except BaseException:
                response.update(ok=False, error=traceback.format_exc())
            pending = memoryview((json.dumps(response) + '\n').encode())
            while pending:
                count = channel.write(pending)
                if not count:
                    raise RuntimeError('Virtio control port made no write progress')
                pending = pending[count:]


if __name__ == '__main__':
    main()
