"""Disposable VM driver for native A only; it is not a product enable CLI."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import termios
import time
import traceback
import tty

PAYLOAD = Path('/run/data-lifecycle')
FIXTURE = json.loads((PAYLOAD / 'fixture.json').read_text())
SPEC = FIXTURE['spec']
NAME = SPEC['name']
DEVICE = '/dev/mapper/' + NAME
DIRECTORY = Path('/run/ram-rescue-data') / NAME
STATE = DIRECTORY / 'state'
RECORD = DIRECTORY / 'record.json'
SERVICE = 'ram-rescue-data-' + NAME + '.service'
UNIT = Path('/run/systemd/system') / SERVICE
ROOT = Path('/run/ram-rescue-manager/rootfs')
RUNTIME = '/opt/guard-runtime/guard-runtime'
worker = None
baseline = None


def gate():
    flags = Path('/proc/cmdline').read_text().split()
    assert {'ram_rescue_lab=1', 'ram_rescue_data_lifecycle_test=1'} <= set(flags)
    assert 'ram_rescue_guard=1' not in flags
    assert Path('/sys/class/dmi/id/product_name').read_text().strip() == 'RAMRescueLab'


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def execute(*args, check=True, timeout=50):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    value = {'args': args, 'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    if check and result.returncode:
        raise RuntimeError(json.dumps(value))
    return value


def host(*args):
    return execute(*args)['stdout'].strip()


def load(path):
    return json.loads(path.read_text()) if path.exists() else None


def system_property(key):
    return host('/usr/bin/systemctl', 'show', SERVICE, '--property=' + key, '--value')


def snapshot():
    pid = int(system_property('MainPID'))
    try:
        stat = Path('/proc/%d/stat' % pid).read_text().split() if pid else None
        executable = sha('/proc/%d/exe' % pid) if pid else None
    except (FileNotFoundError, ProcessLookupError):
        stat = executable = None  # An exit observation, never a live identity.
        pid = int(system_property('MainPID'))
        if pid:
            time.sleep(.02)
            return snapshot()
    cgroup = system_property('ControlGroup')
    group = Path('/sys/fs/cgroup') / cgroup.lstrip('/')
    log = Path('/run/' + NAME + '-workload.jsonl')
    records = [json.loads(line) for line in log.read_text().splitlines()] if worker and log.exists() else []
    mounts = Path('/proc/1/mountinfo').read_text().splitlines()
    return {'pid': pid, 'start_ticks': stat[21] if stat else None,
        'exe_sha256': executable,
        'service': system_property('ActiveState'), 'cgroup': cgroup,
        'cgroup_procs': (group / 'cgroup.procs').read_text() if cgroup and group.exists() else '',
        'journal': load(STATE / 'path-transaction.json'), 'state': load(STATE / 'path-state.json'),
        'stop': load(STATE / 'safe-stop.json'), 'archive': load(STATE / 'safe-stop-archive.json'),
        'invocation': load(STATE / 'manager-invocation.json'), 'supervisor': load(STATE / 'path-supervisor.json'),
        'lock_inode': (STATE / 'path-owner.lock').stat().st_ino,
        'table': host('/usr/sbin/dmsetup', 'table', NAME), 'status': host('/usr/sbin/dmsetup', 'status', NAME),
        'ram_mounts': [line for line in mounts if 'ram-rescue' in line],
        'fstab_sha256': sha('/etc/fstab'), 'root_enrollment_exists': Path('/etc/ram-rescue-guard.json').exists(),
        'worker_pid': worker.pid if worker else None, 'worker_poll': worker.poll() if worker else None,
        'acks': len(records), 'errors': [row for row in records if not row.get('ok')], 'records': records[-8:]}


def native_stop():
    value = execute('/usr/sbin/chroot', str(ROOT), RUNTIME, 'safe-stop', '--record', str(RECORD), check=False)
    value['value'] = json.loads(value['stdout'] or value['stderr'])
    return value


def wait_ready():
    until = time.monotonic() + 25
    while time.monotonic() < until:
        current = load(STATE / 'path-state.json') or {}
        if current.get('state') == 'ready' and system_property('ActiveState') == 'active':
            return snapshot()
        time.sleep(.1)
    raise RuntimeError('Owner not ready: ' + json.dumps(diagnostics()))


def prepare():
    global baseline
    gate()
    assert sha(PAYLOAD / 'handler.deb') == FIXTURE['package']['sha256']
    installed = execute('/usr/bin/dpkg', '-i', str(PAYLOAD / 'handler.deb'))
    package = Path('/usr/lib/ram-rescue-handler') / FIXTURE['package']['administration_version']
    sys.path.insert(0, str(package / 'guard'))
    import data
    from admin.registry import record_from_profile
    from admin.data import collect
    from host_files import atomic
    node = str((Path('/dev/disk/by-partuuid') / SPEC['partuuid']).resolve(strict=True))
    sectors = (Path('/sys/class/block') / Path(node).name / 'size').read_text().strip()
    table = '0 ' + sectors + ' multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 ' + node + ' 1'
    host('/usr/sbin/dmsetup', '--noudevsync', 'create', NAME, '--uuid', SPEC['map_uuid'], '--table', table)
    host('/usr/sbin/dmsetup', '--noudevsync', 'mknodes', NAME)
    host('/usr/bin/udevadm', 'trigger', '--action=change', '--settle')
    profile = collect(NAME, node)
    data.require_ram()
    runtime = data.stage_runtime()
    STATE.mkdir(mode=0o700, parents=True)
    for name, value in [('identity.json', profile['identity']), ('config.json', profile['guard']),
                        ('enrollment.json', profile), ('record.json', record_from_profile(profile))]:
        atomic(DIRECTORY / name, (json.dumps(value) + '\n').encode())
    # Unmodified temporary startup template/security policy. host-data run is
    # a native alias to maintain; this driver does not test first-enable B.
    unit = data.service_unit(NAME, runtime)
    atomic(UNIT, unit.encode(), mode=0o644)
    atomic(Path('/run/systemd/system') / data.SLICE, data.slice_unit().encode(), mode=0o644)
    rule = Path('/run/udev/rules.d') / ('58-ram-rescue-data-' + NAME + '.rules')
    rule.parent.mkdir(parents=True, exist_ok=True)
    atomic(rule, data.udev_rules(profile).encode(), mode=0o644)
    host('/usr/bin/udevadm', 'control', '--reload-rules')
    host('/usr/bin/udevadm', 'trigger', '--action=change', '--settle', profile['guard']['initial_sys_path'])
    st = os.stat(DEVICE)
    host('/usr/bin/udevadm', 'trigger', '--action=change', '--settle', '/sys/dev/block/%d:%d' % (os.major(st.st_rdev), os.minor(st.st_rdev)))
    assert collect(NAME, node) == profile
    host('/usr/bin/systemctl', 'daemon-reload')
    started = execute('/usr/bin/systemctl', 'start', SERVICE)
    baseline = wait_ready()
    assert baseline['exe_sha256'] == FIXTURE['package']['runtime']['binary_sha256']
    return {'installed': installed, 'started': started, 'snapshot': baseline, 'unit': unit,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'kernel': os.uname().release,
            'qualification_consumed': False, 'startup': 'unmodified_temporary_template_native_maintain_alias_lab_driver'}


def busy_cases():
    results = {}
    mount = Path('/mnt/a-lifecycle'); mount.mkdir(exist_ok=True)
    def reject(name):
        before = snapshot(); stopped = native_stop(); after = snapshot()
        assert stopped['returncode'] != 0 and stopped['value']['state'] == 'blocked', stopped
        assert (before['pid'], before['start_ticks'], before['journal'], before['table']) == (
            after['pid'], after['start_ticks'], after['journal'], after['table'])
        results[name] = {'stop': stopped, 'before': before, 'after': after}
    host('/usr/bin/mount', DEVICE, str(mount))
    try:
        reject('mounted')
    finally:
        host('/usr/bin/umount', str(mount))
    fd = os.open(DEVICE, os.O_RDONLY | os.O_NONBLOCK)
    try:
        reject('dm_fd_open')
    finally:
        os.close(fd)
    sectors = baseline['journal']['snapshot']['active'][0][1]
    host('/usr/sbin/dmsetup', '--noudevsync', 'create', 'a-lifecycle-holder', '--table', '0 %d linear %s 0' % (sectors, DEVICE))
    try:
        reject('upper_holder')
    finally:
        host('/usr/sbin/dmsetup', '--noudevsync', 'remove', 'a-lifecycle-holder')
    original_table = host('/usr/sbin/dmsetup', 'table', NAME)
    changed_table = original_table.split(); changed_table[1] = str(int(changed_table[1]) - 1)
    host('/usr/sbin/dmsetup', '--noudevsync', 'load', NAME, '--table', ' '.join(changed_table))
    host('/usr/sbin/dmsetup', '--noudevsync', 'resume', NAME)
    try:
        reject('unknown_geometry_table')
    finally:
        host('/usr/sbin/dmsetup', '--noudevsync', 'load', NAME, '--table', original_table)
        host('/usr/sbin/dmsetup', '--noudevsync', 'resume', NAME)
    helper = subprocess.Popen(['/usr/bin/sleep', '50'])
    try:
        group = Path('/sys/fs/cgroup') / system_property('ControlGroup').lstrip('/')
        (group / 'cgroup.procs').write_text(str(helper.pid))
        reject('residual_test_helper')
    finally:
        helper.terminate(); helper.wait(timeout=3)
    # Changed identity must not become new enrollment for the running owner.
    identity = DIRECTORY / 'identity.json'; original = identity.read_bytes()
    value = json.loads(original); value['usb_serial'] += '-changed'
    identity.write_text(json.dumps(value))
    try:
        reject('identity_changed')
    finally:
        identity.write_bytes(original)
    return results


def safe_stop():
    before = snapshot(); result = native_stop()
    assert result['returncode'] == 0 and result['value']['state'] == 'stopped', result
    after = snapshot(); repeated = native_stop()
    assert repeated['returncode'] == 0, repeated
    assert after['pid'] == 0 and after['cgroup_procs'] == '' and 'queue_if_no_path' not in after['table']
    assert before['lock_inode'] == after['lock_inode'] and before['ram_mounts'] == after['ram_mounts']
    assert before['fstab_sha256'] == after['fstab_sha256'] and after['root_enrollment_exists'] is False
    assert after['journal']['phase'] == 'safe_stopped' and after['supervisor']['state'] == 'safe_stopped'
    return {'before': before, 'result': result, 'after': after, 'repeated': repeated}


def prepare_rearm_unit():
    text = UNIT.read_text()
    lines = [('ExecStart=' + RUNTIME + ' maintain --record ' + str(RECORD) + ' --rearm')
             if line.startswith('ExecStart=') else line for line in text.splitlines()]
    UNIT.write_text('\n'.join(lines) + '\n')
    host('/usr/bin/systemctl', 'daemon-reload')
    reset = execute('/usr/bin/systemctl', 'reset-failed', SERVICE, check=False)
    assert reset['returncode'] == 0 or 'not loaded' in reset['stderr'], reset


def rearm():
    before = snapshot()
    prepare_rearm_unit()
    result = execute('/usr/bin/systemctl', 'start', SERVICE)
    after = wait_ready()
    assert after['journal']['owner_epoch'] != before['journal']['owner_epoch']
    assert after['lock_inode'] == before['lock_inode'] and 'queue_if_no_path' in after['table']
    assert after['exe_sha256'] == FIXTURE['package']['runtime']['binary_sha256']
    assert after['archive']['journal'] == before['journal']
    return {'before': before, 'start': result, 'after': after}


def fence_busy_reentry():
    before = snapshot()
    fd = os.open(STATE / 'path-owner.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        incomplete = native_stop()
        assert incomplete['returncode'] == 2 and incomplete['value']['state'] == 'incomplete', incomplete
        prepare_rearm_unit()
        refused = execute('/usr/bin/systemctl', 'start', SERVICE, check=False)
        after = snapshot()
        assert refused['returncode'] != 0 and after['journal'] == before['journal']
        assert after['table'] == before['table'] and after['lock_inode'] == before['lock_inode']
        assert after['ram_mounts'] == before['ram_mounts']
    finally:
        os.close(fd)
    retried = native_stop()
    assert retried['returncode'] == 0, retried
    return {'before': before, 'incomplete_while_fenced': incomplete, 'rearm_refused': refused,
            'after': after, 'retry_after_fence_release': retried, 'actual_D_state_injected': False}


def start_workload():
    global worker
    mount = Path('/mnt/a-lifecycle'); mount.mkdir(exist_ok=True)
    host('/usr/bin/mount', DEVICE, str(mount))
    worker = subprocess.Popen(['/usr/bin/python3', str(PAYLOAD / 'workload.py'), NAME, str(mount)])
    until = time.monotonic() + 10
    while time.monotonic() < until:
        value = snapshot()
        if value['acks'] >= 3:
            assert not value['errors'] and value['worker_poll'] is None
            return value
        time.sleep(.1)
    raise RuntimeError('Workload did not acknowledge')


def gap_stop():
    before = snapshot()
    old = json.loads((DIRECTORY / 'config.json').read_text())['initial_sys_path']
    assert not Path(old).exists() or ' F ' in before['status'] or before['state']['state'] != 'ready'
    result = native_stop()
    after = snapshot()
    assert result['returncode'] != 0 and result['value']['state'] == 'blocked', result
    assert before['pid'] == after['pid'] and after['service'] == 'active'
    return {'before': before, 'stop': result, 'after': after,
            'recovery_specific_reason': 'recovering' in result['value'].get('reason', '')}


def finish_workload():
    value = snapshot()
    assert value['state']['state'] == 'ready' and value['state']['recoveries'] >= 1 and not value['errors']
    assert worker.poll() is None
    worker.terminate(); worker.wait(timeout=15)
    records = [json.loads(line) for line in Path('/run/' + NAME + '-workload.jsonl').read_text().splitlines()]
    assert all(row['ok'] for row in records)
    with Path('/mnt/a-lifecycle/held-fd.data').open('rb') as stream:
        for row in records:
            assert stream.read(4096) == (str(row['seq']) + '\n').encode().ljust(4096, b'x')
        assert stream.read(1) == b''
    host('/usr/bin/umount', '/mnt/a-lifecycle')
    return {'snapshot': value, 'workload_exit': worker.returncode, 'records': records, 'file_verified': True}


def kill_rearm_reject():
    before = snapshot(); os.kill(before['pid'], signal.SIGKILL)
    until = time.monotonic() + 20
    while time.monotonic() < until:
        value = snapshot()
        if value['pid'] == 0 and value['journal']['phase'] == 'interrupted' and 'queue_if_no_path' not in value['table']:
            break
        time.sleep(.1)
    else:
        raise RuntimeError('Killed owner not reconciled')
    reset = execute('/usr/bin/systemctl', 'reset-failed', SERVICE, check=False)
    assert reset['returncode'] == 0 or 'not loaded' in reset['stderr'], reset
    result = execute('/usr/bin/systemctl', 'start', SERVICE, check=False)
    after = snapshot()
    assert result['returncode'] != 0 and after['journal'] == value['journal'] and 'queue_if_no_path' not in after['table']
    assert after['lock_inode'] == before['lock_inode']
    return {'before': before, 'takeover': value, 'rearm_refused': result, 'after': after}


def diagnostics():
    return {'snapshot': snapshot(), 'journalctl': execute('/usr/bin/journalctl', '-b', '-u', SERVICE, '--no-pager', check=False),
            'dmesg': execute('/usr/bin/dmesg', check=False)}


def shutdown():
    subprocess.Popen(['/usr/bin/systemctl', 'poweroff'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'requested': True}


def serve():
    gate(); tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
    print(json.dumps({'ready': True, 'protocol': 'data-lifecycle-a-v1'}), flush=True)
    actions = {name: globals()[name] for name in ('prepare', 'snapshot', 'busy_cases', 'safe_stop', 'rearm',
        'fence_busy_reentry', 'start_workload', 'gap_stop', 'finish_workload', 'kill_rearm_reject', 'diagnostics', 'shutdown')}
    while line := sys.stdin.buffer.readline(4097):
        request = json.loads(line)
        assert len(line) <= 4096 and set(request) == {'id', 'action'} and request['action'] in actions
        try:
            result = {'id': request['id'], 'ok': True, 'value': actions[request['action']]()}
        except BaseException:
            result = {'id': request['id'], 'ok': False, 'error': traceback.format_exc()}
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    serve()
