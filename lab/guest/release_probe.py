"""Disposable release qualification. All device operations use the installed CLI.

The first engineering run supplies a local qualification fixture; its successful
report can then authorize a second run with the actual external release record.
"""
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

PAYLOAD = Path('/run/candidate')
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
    assert {'ram_rescue_lab=1', 'ram_rescue_candidate_test=1'} <= set(flags)
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


def cli(*args, check=True):
    return execute('/usr/bin/rescue-guard-admin', 'device', *args, check=check, timeout=180)


def parsed(*args):
    return json.loads(cli(*args)['stdout'])


def configuration():
    return parsed('status', '--name', NAME)['devices'][0]


def prepare():
    import shutil
    global baseline
    gate()
    assert sha(PAYLOAD / 'handler.deb') == FIXTURE['package']['sha256']
    result = execute('/usr/bin/dpkg', '-i', str(PAYLOAD / 'handler.deb'))
    assert not Path('/run/ram-rescue-manager/runtime-environment.json').exists()
    assert not Path('/etc/systemd/system/ram-rescue-devices.service').exists()
    node = str((Path('/dev/disk/by-partuuid') / SPEC['partuuid']).resolve(strict=True))
    refused = cli('plan', '--device', node, '--name', NAME, check=False)
    assert refused['returncode'] != 0
    reference = Path('/usr/share/ram-rescue-handler/kernels') / os.uname().release
    reference.mkdir(parents=True, mode=0o755)
    for name in ('source.json', 'InRelease', 'Packages'):
        shutil.copyfile(PAYLOAD / 'reference' / name, reference / name)
    shutil.copyfile(PAYLOAD / 'reference/ubuntu-archive-keyring.gpg', '/usr/share/keyrings/ubuntu-archive-keyring.gpg')
    shutil.copytree(PAYLOAD / 'kernel-files', '/', dirs_exist_ok=True)
    (PAYLOAD / 'workload.py').write_text(FIXTURE['workload'])
    subject = parsed('support')['subject']
    qualification = {'schema': 1, 'version': '0.0.1-beta', 'purpose': 'data_activation_release_acceptance',
                     'result': 'passed', 'filesystems': ['ext4'], 'subjects': [subject],
                     'evidence_sha256': FIXTURE.get('evidence_sha256', '0' * 64)}
    if 'acceptance' in FIXTURE:
        qualification = FIXTURE['acceptance']
        assert subject in qualification['subjects']
    target = Path('/usr/share/ram-rescue-handler/releases/0.0.1-beta.json')
    if 'support_package_sha256' in FIXTURE:
        assert sha(PAYLOAD / 'support.deb') == FIXTURE['support_package_sha256']
        execute('/usr/bin/dpkg', '-i', str(PAYLOAD / 'support.deb'))
        assert json.loads(target.read_text()) == qualification
        assert not Path('/run/ram-rescue-manager/runtime-environment.json').exists()
        assert not Path('/etc/systemd/system/ram-rescue-devices.service').exists()
    else:
        target.parent.mkdir(parents=True, mode=0o755)
        target.write_text(json.dumps(qualification))
    plan = parsed('plan', '--device', node, '--name', NAME)
    assert plan['existing_map'] is False
    bad = cli('enroll', '--device', node, '--name', NAME, '--expect-plan', '0' * 64, check=False)
    assert bad['returncode'] != 0
    assert not Path('/etc/ram-rescue-handler/devices/' + NAME + '.json').exists()
    enrolled = parsed('enroll', '--device', node, '--name', NAME, '--expect-plan', plan['plan_sha256'])
    assert enrolled['state'] == 'disabled'
    assert not Path(DEVICE).exists()
    assert not Path('/run/ram-rescue-manager/runtime-environment.json').exists()
    value = configuration()
    enabled = parsed('enable', '--name', NAME, '--expect-plan', value['config_sha256'])
    assert enabled['state'] == 'active'
    baseline = wait_ready()
    assert baseline['exe_sha256'] == FIXTURE['package']['runtime']['binary_sha256']
    return {'installed': result, 'subject': subject, 'plan': plan, 'enabled': enabled,
            'missing_qualification_refused': refused, 'stale_plan_refused': bad,
            'qualification_fixture': 'acceptance' not in FIXTURE, 'snapshot': baseline}


def lifecycle_checks():
    before = snapshot()
    rejected = execute('/usr/bin/dpkg', '--remove', 'ram-rescue-handler', check=False)
    assert rejected['returncode'] != 0
    assert snapshot()['pid'] == before['pid']
    stopped = parsed('stop', '--name', NAME)
    assert stopped['enabled_at_boot'] is True
    assert snapshot()['pid'] == 0 and 'queue_if_no_path' not in snapshot()['table']
    value = configuration()
    restarted = parsed('enable', '--name', NAME, '--expect-plan', value['config_sha256'])
    after = wait_ready()
    assert after['pid'] != before['pid'] and after['lock_inode'] == before['lock_inode']
    host('/usr/bin/mkdir', '-p', '/mnt/a-lifecycle')
    host('/usr/bin/mount', DEVICE, '/mnt/a-lifecycle')
    busy = cli('disable', '--name', NAME, check=False)
    assert busy['returncode'] != 0
    value = configuration()
    assert value['enabled_at_boot'] is False and snapshot()['pid'] == after['pid']
    host('/usr/bin/umount', '/mnt/a-lifecycle')
    disabled = parsed('disable', '--name', NAME)
    assert disabled['state'] == 'stopped'
    # Re-enable to verify next-boot activation later in the scenario.
    value = configuration()
    parsed('enable', '--name', NAME, '--expect-plan', value['config_sha256'])
    return {'package_removal_refused': rejected, 'stopped': stopped,
            'restarted': restarted, 'busy_disable': busy, 'disabled': disabled}


def disable_for_boot():
    result = parsed('disable', '--name', NAME)
    assert result['enabled_at_boot'] is False
    Path('/var/lib/ram-rescue-handler/release-test-boot').write_text(Path('/proc/sys/kernel/random/boot_id').read_text())
    return result


def reboot_disabled():
    assert Path('/var/lib/ram-rescue-handler/release-test-boot').read_text() != Path('/proc/sys/kernel/random/boot_id').read_text()
    assert not Path(DEVICE).exists()
    assert not Path('/run/ram-rescue-manager/runtime-environment.json').exists()
    value = configuration()
    assert value['enabled_at_boot'] is False
    parsed('enable', '--name', NAME, '--expect-plan', value['config_sha256'])
    return {'disabled_persisted': True, 'now': configuration()}


def reboot_enabled():
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            value = configuration()
            if value['service'] == 'active' and value['native']['state'] == 'ready':
                break
        except (RuntimeError, TypeError):
            pass
        time.sleep(.5)
    else:
        raise RuntimeError(host('/usr/bin/journalctl', '-b', '-u', 'ram-rescue-devices.service'))
    assert snapshot()['exe_sha256'] == FIXTURE['package']['runtime']['binary_sha256']
    parsed('disable', '--name', NAME)
    value = configuration()
    removed = parsed('remove', '--name', NAME, '--expect-plan', value['config_sha256'])
    assert not Path(DEVICE).exists()
    assert not parsed('status')['devices']
    refused = cli('uninstall', check=False)
    assert refused['returncode'] != 0  # RAM is kept for this boot.
    return {'boot_activated': value, 'removed': removed, 'live_ram_removal_refused': refused}


def uninstall_reinstall():
    result = parsed('uninstall')
    if 'support_package_sha256' in FIXTURE:
        execute('/usr/bin/dpkg', '--remove', 'ram-rescue-handler-support')
    removed = execute('/usr/bin/dpkg', '--remove', 'ram-rescue-handler')
    assert not Path('/usr/bin/rescue-guard-admin').exists()
    execute('/usr/bin/dpkg', '-i', str(PAYLOAD / 'handler.deb'))
    if 'support_package_sha256' in FIXTURE:
        execute('/usr/bin/dpkg', '-i', str(PAYLOAD / 'support.deb'))
    assert not Path('/etc/systemd/system/ram-rescue-devices.service').exists()
    assert not Path('/run/ram-rescue-manager/runtime-environment.json').exists()
    node = str((Path('/dev/disk/by-partuuid') / SPEC['partuuid']).resolve(strict=True))
    plan = parsed('plan', '--device', node, '--name', NAME)
    parsed('enroll', '--device', node, '--name', NAME, '--expect-plan', plan['plan_sha256'])
    value = configuration()
    parsed('remove', '--name', NAME, '--expect-plan', value['config_sha256'])
    parsed('uninstall')
    if 'support_package_sha256' in FIXTURE:
        execute('/usr/bin/dpkg', '--remove', 'ram-rescue-handler-support')
    execute('/usr/bin/dpkg', '--remove', 'ram-rescue-handler')
    return {'uninstalled': result, 'package_removed': removed, 'reinstalled_and_removed': True}


def release_diagnostics():
    return {'journal': execute('/usr/bin/journalctl', '-b', '--no-pager', '-n', '160', check=False),
            'dmesg': execute('/usr/bin/dmesg', check=False), 'status': cli('status', check=False)}


def serve():
    gate(); tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
    print(json.dumps({'ready': True, 'protocol': 'release-lifecycle-v1'}), flush=True)
    names = ('prepare', 'snapshot', 'busy_cases', 'start_workload', 'gap_stop', 'finish_workload',
             'lifecycle_checks', 'disable_for_boot', 'reboot_disabled', 'reboot_enabled',
             'uninstall_reinstall', 'release_diagnostics', 'shutdown')
    actions = {name: globals()[name] for name in names}
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
