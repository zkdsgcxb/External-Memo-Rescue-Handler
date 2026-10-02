#!/usr/bin/env python3
"""Boot an existing Ubuntu PV without formatting, in disposable QEMU images.

Append an experiment overlay to verified initramfs images. The seed alone may
format a newly created raw file. Later boots activate that existing PV through
the stable map and write only new qcow2 overlays. This is not a host installer
or an initramfs-to-systemd Guard ownership handoff implementation. The default
normal-only mode initializes once, shuts down, then boots the same filesystem.
The earlier fault matrix is retained only as an explicit opt-in experiment.
"""
import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
import traceback
import uuid

from auto_run import wait_for
from run import WORK, qemu_command, shell_probe


GUEST = r'''#!/usr/bin/python3
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

sys.path.insert(0, '/opt/lab')
from agent import guard, run

SETTING = Path('/etc/boot-evolution.json')
STATE = Path('/run/boot-evolution.json')
SENTINEL = 'boot-evolution-sentinel.json'


def read(path):
    path = Path(path)
    return json.loads(path.read_text()) if path.exists() else None


def emit(phase, **extra):
    record = {**(read(STATE) or {}), 'phase': phase,
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'guest_time': time.monotonic(), **extra}
    STATE.write_text(json.dumps(record))
    print('BOOT_EVOLUTION=' + json.dumps(record), flush=True)


def durable_json(path, value):
    with path.open('w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def base_preflight(setting):
    actual = os.uname().release
    if actual != setting['kernel_release']:
        raise RuntimeError('Kernel release differs from staged manifest: ' + actual)
    if not actual.startswith('7.0.'):
        raise RuntimeError('This prototype supports only the current Linux 7.0 baseline')
    for filename in ['/opt/lab/path_guard.py', '/opt/lab/guard_state.py',
                     '/opt/lab/admission.py', '/opt/lab/dm_monitor.py',
                     '/sbin/dmsetup', '/sbin/lvm', '/sbin/blkid']:
        if not Path(filename).is_file():
            raise RuntimeError('Missing RAM dependency: ' + filename)
    from dm_monitor import DeviceMapper
    mapper = DeviceMapper()
    version = mapper.target_version('multipath')
    if version < (1, 15, 0):
        raise RuntimeError('DM_MPATH_PROBE_PATHS capability is unavailable')
    return {'kernel_release': actual, 'multipath_target_version': version}


def activate_existing_root(setting):
    # No sfdisk, pvcreate, vgcreate or mkfs in this branch. Enrollment must exist
    # before even creating the stable map or activating the existing root LV.
    identity = read('/etc/rescue/identity.json')
    config = read('/etc/rescue/path-guard.json')
    if not identity or not config:
        raise RuntimeError('Required enrollment or Guard configuration is missing')
    from admission import Admission, readonly
    from rescue import Recovery
    from path_guard import dm, table, DEVICE, NAME, UUID
    deadline = time.monotonic() + 20
    recovery = Recovery(identity, runner=readonly)
    while True:
        try:
            recovery.candidate_node()
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)
    with Admission(config, recovery).verify(deadline, 'boot-preflight') as candidate:
        candidate.revalidate('boot-preflight')
        Path('/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs').write_text(
            str(config['queue_seconds'] + 2))
        dm('create', NAME, '--uuid', UUID, '--table', table(candidate.partition_sectors, candidate.node))
        dm('mknodes', NAME)
        config.update(initial_node=candidate.node, initial_sys_path=candidate.sys_path,
                      initial_diskseq=candidate.diskseq)
    Path('/etc/rescue/path-guard.json').write_text(json.dumps(config))
    run('/sbin/lvm', 'vgchange', '-ay', '--devices', DEVICE, identity['vg_name'])
    stable_dev = os.stat(DEVICE).st_rdev
    stable_sys = (Path('/sys/dev/block')/f'{os.major(stable_dev)}:{os.minor(stable_dev)}').resolve()
    for name in ['ubuntu', 'shared']:
        mapping = recovery.mapping(name)
        if [path.name for path in (mapping/'slaves').iterdir()] != [stable_sys.name]:
            raise RuntimeError('Root/shared LV bypasses the stable map')
    run('/bin/mount', '-o', 'errors=remount-ro', '/dev/labrescue/ubuntu', '/newroot')
    run('/bin/mount', '-o', 'errors=remount-ro', '/dev/labrescue/shared', '/newroot/shared')
    sentinel = read('/newroot/root/' + SENTINEL)
    if sentinel != setting['sentinel']:
        raise RuntimeError('Persistent sentinel differs: refusing reused/new filesystem')


def install_lab_units():
    # The stock test preparer creates enablement symlinks once. Reinstall only
    # its known lab links so the selected initramfs can replace failed units.
    wants = Path('/newroot/etc/systemd/system/multi-user.target.wants')
    for name in ['lab-agent', 'lab-guard', 'lab-shell', 'lab-ready']:
        (wants/(name + '.service')).unlink(missing_ok=True)
    from ubuntu import prepare
    prepare()


def prepare():
    guard()
    setting = read(SETTING)
    capability = base_preflight(setting)
    if setting['scenario'] == 'preparation-failure':
        raise RuntimeError('Injected initramfs preparation failure before root activation')
    if setting['scenario'] == 'seed':
        from agent import setup
        setup()
        durable_json(Path('/newroot/root/' + SENTINEL), setting['sentinel'])
    else:
        activate_existing_root(setting)
        # Normal repeat boot retains the existing Ubuntu installation and its
        # units/fstab/machine-id. Only the explicit upgrade prototype rewrites
        # its known lab units; that preparer is never an installed-disk boot API.
        if setting['scenario'] != 'existing-boot':
            install_lab_units()
    # A deliberately broken service tests failure after the gate and pivot.
    # The gate does not promise that Type=simple means the Guard is ready.
    if setting['scenario'] == 'guard-start-failure':
        unit = Path('/newroot/etc/systemd/system/lab-guard.service')
        source = unit.read_text()
        before = 'ExecStart=/usr/bin/python3 /opt/lab/path_guard.py\n'
        if source.count(before) != 1:
            raise RuntimeError('Guard service injection anchor differs')
        unit.write_text(source.replace(before, 'ExecStart=/bin/false\n'))
        durable_json(Path('/newroot/root/boot-evolution-upgrade.json'),
                     {'token': setting['sentinel']['token'], 'failed_candidate': True})
    from dm_monitor import DeviceMapper
    from path_guard import checked_snapshot
    snapshot = checked_snapshot(DeviceMapper())
    if snapshot['inactive'] or snapshot['info']['suspended']:
        raise RuntimeError('Prepared stable map has inactive or suspended state')
    identity = read('/etc/rescue/identity.json')
    config = read('/etc/rescue/path-guard.json')
    if not identity or not config:
        raise RuntimeError('Prepared enrollment is missing')
    filesystem_uuid = run('/sbin/blkid', '-s', 'UUID', '-o', 'value',
                          '/dev/labrescue/ubuntu').strip()
    run('/bin/sync')
    emit('preflight_passed', capability=capability, root_switched=False,
         protected=False, identity=identity, config=config,
         filesystem_uuid=filesystem_uuid, stable_map=snapshot,
         sentinel=read('/newroot/root/' + SENTINEL),
         candidate_marker=read('/newroot/root/boot-evolution-upgrade.json'))


def observe():
    guard()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    state = read('/run/path-state.json')
    transaction = read('/run/path-transaction.json')
    supervisor = read('/run/path-supervisor.json')
    root_switched = Path('/proc/1/comm').read_text().strip() == 'systemd'
    unit = {}
    if root_switched:
        output = subprocess.check_output(['/bin/chroot', '/proc/1/root', '/usr/bin/systemctl',
            'show', 'lab-guard.service', '-p', 'ActiveState,SubState,MainPID,ExecMainStatus'],
            text=True, stderr=subprocess.STDOUT, timeout=8)
        unit = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    pid = int(unit.get('MainPID', 0))
    process = None
    if pid and Path('/proc/' + str(pid) + '/stat').exists():
        proc = Path('/proc/' + str(pid))
        before = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        cmdline = proc.joinpath('cmdline').read_bytes().split(b'\0')
        after = proc.joinpath('stat').read_text().rsplit(')', 1)[1].split()
        process = {'pid': pid, 'start_ticks': before[19], 'state': after[0],
                   'same_instance': before[19] == after[19],
                   'is_guard': b'/opt/lab/path_guard.py' in cmdline}
    gate = read(STATE) or {}
    # This sampled predicate expires when the owner exits/changes. It is not a
    # permanent readiness token or proof of the filesystem/application history.
    clauses = {'root_switched': root_switched,
        'gate_same_boot': gate.get('boot_id') == boot and gate.get('phase') == 'preflight_passed',
        'state_ready': bool(state and state.get('state') == 'ready'),
        'transaction_same_boot': bool(transaction and transaction.get('boot_id') == boot),
        'same_owner_epoch': bool(state and transaction and
            state.get('owner_epoch') == transaction.get('owner_epoch')),
        'transaction_owner_is_service': bool(transaction and pid and transaction.get('owner_pid') == pid),
        'service_active': unit.get('ActiveState') == 'active' and unit.get('SubState') == 'running',
        'owner_process_live': bool(process and process['same_instance'] and process['is_guard']
                                   and process['state'] not in ('Z', 'X'))}
    events_path = Path('/run/path-events.jsonl')
    events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
    root = Path('/proc/1/root') if root_switched else Path('/newroot')
    mounts = Path('/proc/1/mountinfo').read_text()
    root_rw = any(line.split()[4] == '/' and 'rw' in line.split()[5].split(',')
                  for line in mounts.splitlines())
    return {'boot_id': boot, 'gate': gate, 'root_switched': root_switched,
            'guard_ready': clauses['state_ready'], 'protected': all(clauses.values()),
            'readiness_clauses': clauses, 'service': unit, 'owner_process': process,
            'path_state': state, 'transaction': transaction, 'supervisor': supervisor,
            'ready_events': sum(event.get('state') == 'ready' for event in events),
            'sentinel': read(root/'root'/SENTINEL),
            'candidate_marker': read(root/'root/boot-evolution-upgrade.json'),
            'machine_id': (root/'etc/machine-id').read_text().strip() if root_switched else None,
            'fstab_sha256': hashlib.sha256((root/'etc/fstab').read_bytes()).hexdigest() if root_switched else None,
            'pid1_comm': Path('/proc/1/comm').read_text().strip(),
            'root_rw': root_switched and root_rw, 'pid1_mountinfo': mounts}


def normal_write():
    guard()
    observation = observe()
    if not observation['protected'] or not observation['root_rw']:
        raise RuntimeError('Normal write check requires qualified Guard and writable root')
    setting = read(SETTING)
    value = {'token':setting['sentinel']['token'], 'boot_id':observation['boot_id']}
    path = Path('/proc/1/root/root/boot-evolution-normal-write.json')
    durable_json(path, value)
    return {'file_and_directory_fsync':True, 'readback_matches':read(path) == value,
            'value':value}


def restart():
    guard()
    before = observe()
    if not before['protected']:
        raise RuntimeError('Restart injection requires a live qualified owner')
    prefix = ['/bin/chroot', '/proc/1/root', '/usr/bin/systemctl']
    stopped = subprocess.run(prefix + ['stop', 'lab-guard.service'], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    after_stop = observe()
    started = subprocess.run(prefix + ['start', 'lab-guard.service'], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        after = observe()
        if after['service'].get('ActiveState') == 'failed':
            break
        time.sleep(.1)
    return {'before': before, 'stop_returncode': stopped.returncode,
            'after_stop': after_stop, 'start_returncode': started.returncode,
            'start_output': started.stdout, 'after': after}


if __name__ == '__main__':
    guard()
    if sys.argv[1:] == ['prepare']:
        try:
            prepare()
        except BaseException:
            emit('preflight_failed', root_switched=False, protected=False,
                 error=traceback.format_exc())
            raise SystemExit(1)
    else:
        raise SystemExit('Only the prepare entry point is executable')
'''


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verified_build(directory):
    directory = directory.resolve()
    if not directory.is_relative_to(WORK.resolve()):
        raise ValueError('Build must be an isolated directory below lab/work')
    record = json.loads((directory/'build.json').read_text())
    for name, key in [('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')]:
        if sha256(directory/name) != record[key]:
            raise ValueError('Build hash mismatch: ' + name)
    if sha256(directory/'rootfs/init') != record['source_sha256']['lab/guest/init.sh']:
        raise ValueError('Unpacked init does not match build manifest')
    if not record['kernel_release'].startswith('7.0.'):
        raise ValueError('This prototype uses only the current Linux 7.0 baseline')
    return directory, record


def init_overlay(folder, build_dir, setting, enrollment):
    staging = folder/'overlay'
    (staging/'opt/lab').mkdir(parents=True)
    (staging/'etc/rescue').mkdir(parents=True)
    (staging/'opt/lab/boot_evolution.py').write_text(GUEST)
    (staging/'etc/boot-evolution.json').write_text(json.dumps(setting))
    if enrollment:
        (staging/'etc/rescue/identity.json').write_text(json.dumps(enrollment['identity']))
        if setting['scenario'] != 'missing-config':
            (staging/'etc/rescue/path-guard.json').write_text(json.dumps(enrollment['config']))
    source = (build_dir/'rootfs/init').read_text()
    replacements = [
        ('mkdir -p /run/lock/lvm /run/lvm\n',
         'mkdir -p /run/lock/lvm /run/lvm\n'
         'boot_halt() { echo BOOT_RAM_RESCUE_WAIT; while :; do /bin/sleep 60; done; }\n'
         '/bin/setsid /bin/sh -i </dev/ttyS2 >/dev/ttyS2 2>&1 &\n'
         'boot_shell_pid=$!\n'),
        ('    /sbin/modprobe "$module"\n',
         '    /sbin/modprobe "$module" || boot_halt\n'),
        ('python3 /opt/lab/agent.py --setup || { sleep 2; exit 1; }\n',
         'python3 /opt/lab/boot_evolution.py prepare || boot_halt\n'),
        ('for item in dev proc sys run; do mount --move',
         'kill -KILL "$boot_shell_pid" 2>/dev/null || true\n'
         'wait "$boot_shell_pid" 2>/dev/null || true\n'
         'for item in dev proc sys run; do mount --move')]
    for before, after in replacements:
        if source.count(before) != 1:
            raise ValueError('Init overlay anchor differs: ' + before)
        source = source.replace(before, after)
    (staging/'init').write_text(source)
    (staging/'init').chmod(0o755)
    paths = [Path('.'), *sorted(path.relative_to(staging) for path in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'],
        cwd=staging, input=b'\0'.join(str(path).encode() for path in paths)+b'\0',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    initramfs = folder/'initramfs.cpio.gz'
    with initramfs.open('xb') as output, (build_dir/'initramfs.cpio.gz').open('rb') as original:
        shutil.copyfileobj(original, output)
        output.write(gzip.compress(archive, mtime=0))
    return initramfs


def ram_call(folder, action, timeout=30):
    if action not in ('observe', 'restart', 'shutdown', 'normal_write'):
        raise ValueError('Unknown RAM action')
    body = ("import sys,json,traceback;sys.path.insert(0,'/opt/lab')\n"
            "import boot_evolution as probe\ntry:\n")
    if action == 'shutdown':
        body += (" probe.guard();probe.run('/bin/sync')\n"
                 " value={'requested':True}\n"
                 " import subprocess\n"
                 " subprocess.Popen(['/bin/chroot','/proc/1/root','/usr/bin/systemctl','poweroff'],"
                 "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n")
    else:
        body += ' value=probe.'+action+'()\n'
    body += (" response={'ok':True,'value':value}\nexcept BaseException:\n"
             " response={'ok':False,'error':traceback.format_exc()}\n"
             "print('BOOT_REPLY='+json.dumps(response),flush=True)\n")
    encoded = base64.b64encode(body.encode()).decode()
    output = b''
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(folder/'rescue.sock'))
        sock.sendall(b'stty -echo\n')
        time.sleep(.05)
        command = ("python3 -c \"import base64;exec(base64.b64decode('" + encoded + "'))\"\n")
        # The command stays below the canonical terminal's line-size bound.
        if len(command) >= 4096:
            raise ValueError('RAM action is too large for one terminal line')
        sock.sendall(command.encode())
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise RuntimeError('RAM shell closed')
            output += chunk
            if b'BOOT_REPLY=' in output and b'\n' in output.split(b'BOOT_REPLY=', 1)[1]:
                response = json.loads(output.split(b'BOOT_REPLY=', 1)[1].split(b'\n', 1)[0])
                with (folder/'ram-actions.jsonl').open('a') as stream:
                    stream.write(json.dumps({'action': action, 'response': response})+'\n')
                if not response['ok']:
                    raise RuntimeError(response['error'])
                return response['value']


def run_case(root, scenario, build_dir, build, sentinel, enrollment=None, backing=None,
             backing_format=None, tcg=False, shutdown_timeout=30):
    # AF_UNIX paths are limited to 108 bytes; keep nested case directories
    # short while retaining descriptive scenario names in every report.
    names = {'seed':'s0', 'preparation-failure':'s1', 'wrong-version':'s2',
             'missing-config':'s3', 'existing-transaction':'s4',
             'guard-start-failure':'s5', 'rollback':'s6', 'existing-boot':'n1'}
    folder = root/names[scenario]
    folder.mkdir()
    seed = scenario == 'seed'
    disk = folder/('usb.raw' if seed else 'usb.qcow2')
    if seed:
        with disk.open('xb') as stream:
            stream.truncate(8*1024**3)
    else:
        if not backing.is_relative_to(root) or not backing.is_file() or backing.is_symlink():
            raise ValueError('Only this run\'s newly created disk chain can be attached')
        subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', backing_format,
                        '-b', str(backing), str(disk)], check=True)
    with (folder/'decoy.raw').open('xb') as stream:
        stream.truncate(16*1024**2)
    setting = {'scenario': scenario, 'kernel_release': build['kernel_release'], 'sentinel': sentinel}
    if scenario == 'wrong-version':
        setting['kernel_release'] += '-INJECTED-MISMATCH'
    initramfs = init_overlay(folder, build_dir, setting, enrollment)
    command = qemu_command(folder, ubuntu=True, same_port=True, tcg=tcg,
        extra_kernel_args='ram_rescue_ubuntu=1 ram_rescue_mpath=1 ram_rescue_queue_seconds=6',
        kernel=build_dir/'vmlinuz', initramfs=initramfs)
    if not seed:
        for index, argument in enumerate(command):
            if argument == '-blockdev':
                device = json.loads(command[index+1])
                if device['node-name'] == 'usbdisk':
                    device.update(driver='qcow2', file={'driver':'file', 'filename':str(disk)},
                        backing={'driver':backing_format, 'read-only':True,
                                 'file':{'driver':'file', 'filename':str(backing)}})
                    command[index+1] = json.dumps(device)
    report = {'scenario': scenario, 'build': build, 'overlay_initramfs_sha256': sha256(initramfs),
              'setting': setting, 'backing': str(backing) if backing else None,
              'command': command, 'passed': False}
    (folder/'command.json').write_text(json.dumps(command, indent=2)+'\n')
    print('Boot experiment:', scenario, folder, flush=True)
    preflight_failure = scenario in ('preparation-failure', 'wrong-version', 'missing-config')
    with (folder/'qemu.log').open('w') as log:
        vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            def booted():
                path = folder/'console.log'
                text = path.read_text(errors='replace') if path.exists() else ''
                if vm.poll() is not None or 'Kernel panic' in text:
                    raise RuntimeError('Guest exited or panicked before observation')
                marker = 'BOOT_RAM_RESCUE_WAIT' if preflight_failure else 'LAB_ROOT_READY:'
                return marker in text
            wait_for(booted, 300 if seed else 120, 'boot outcome')
            report['ram_shell'] = shell_probe(folder/'rescue.sock')
            report['observation'] = ram_call(folder, 'observe')
            if not preflight_failure and scenario != 'guard-start-failure':
                report['observation'] = wait_for(
                    lambda: value if (value := ram_call(folder, 'observe'))['protected'] else None,
                    15, 'qualified Guard readiness')
            value = report['observation']
            if preflight_failure:
                report['checks'] = {'preparation_rejected': value['gate'].get('phase') == 'preflight_failed',
                    'did_not_switch_root': not value['root_switched'],
                    'not_protected': not value['protected'], 'no_guard_started': value['transaction'] is None,
                    'ram_shell': report['ram_shell']}
            elif scenario == 'guard-start-failure':
                report['observation'] = wait_for(
                    lambda: value if (value := ram_call(folder, 'observe'))['service'].get('ActiveState') == 'failed' else None,
                    15, 'Guard startup refusal')
                value = report['observation']
                report['checks'] = {'root_start_is_not_protection': value['root_switched'] and not value['protected'],
                    'guard_failed': value['service']['ActiveState'] == 'failed',
                    'no_ready_event': value['ready_events'] == 0,
                    'same_sentinel': value['sentinel'] == sentinel,
                    'failed_upgrade_marker_persisted': bool(value['candidate_marker']),
                    'ram_shell': report['ram_shell']}
            else:
                report['checks'] = {'qualified_guard_ready': value['protected'],
                    'same_sentinel': value['sentinel'] == sentinel, 'ram_shell': report['ram_shell']}
                if scenario == 'existing-boot':
                    report['write_check'] = ram_call(folder, 'normal_write')
                    report['checks'].update({'same_root_filesystem':value['gate']['filesystem_uuid'] == enrollment['filesystem_uuid'],
                        'same_pv':value['gate']['identity']['pv_uuid'] == enrollment['identity']['pv_uuid'],
                        'writable_root':value['root_rw'],
                        'file_and_directory_fsync':report['write_check']['file_and_directory_fsync'],
                        'written_file_readback':report['write_check']['readback_matches']})
                elif scenario == 'existing-transaction':
                    report['restart'] = ram_call(folder, 'restart', timeout=55)
                    restart = report['restart']
                    after = restart['after']
                    report['checks'].update({'stop_completed': restart['stop_returncode'] == 0,
                        'restart_refused': after['service'].get('ActiveState') == 'failed',
                        'not_protected_after_restart': not after['protected'],
                        'journal_retained': after['transaction'] is not None,
                        'no_extra_ready': after['ready_events'] == restart['before']['ready_events'],
                        'terminal_after_restart': (after['path_state'] or {}).get('state') in ('interrupted', 'blocked', 'failed', 'expired'),
                        'ram_after_restart': shell_probe(folder/'rescue.sock')})
                elif scenario == 'rollback':
                    report['checks']['failed_candidate_data_survived'] = bool(value['candidate_marker'] and
                        value['candidate_marker'].get('token') == sentinel['token'])
                    report['checks']['same_root_filesystem'] = value['gate']['filesystem_uuid'] == enrollment['filesystem_uuid']
                    report['checks']['same_pv'] = value['gate']['identity']['pv_uuid'] == enrollment['identity']['pv_uuid']
            report['passed'] = all(report['checks'].values())
            if report['observation']['root_switched']:
                report['shutdown'] = ram_call(folder, 'shutdown')
                try:
                    vm.wait(timeout=shutdown_timeout)
                    report['graceful_shutdown'] = vm.returncode == 0
                except subprocess.TimeoutExpired:
                    report['graceful_shutdown'] = False
        except BaseException:
            report['error'] = traceback.format_exc()
            raise
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:
                    vm.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    vm.kill()
                    vm.wait()
            (folder/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    if not report['passed']:
        raise RuntimeError('Boot experiment checks failed: ' + scenario)
    return report, disk


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-build', type=Path, default=WORK/'route-refactor-v4')
    parser.add_argument('--candidate-build', type=Path,
                        help='Defaults to the baseline build; must use the identical kernel')
    parser.add_argument('--tcg', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--normal-only', dest='normal_only', action='store_true', default=True,
                      help='Default: initialize once, then boot the same PV without formatting')
    mode.add_argument('--fault-matrix', dest='normal_only', action='store_false',
                      help='Explicit opt-in to the earlier seven-case fault/rollback prototype')
    return parser, parser.parse_args(argv)


def main():
    parser, args = parse_args()
    if os.geteuid() == 0:
        parser.error('Run as a normal user; no host root access is required')
    baseline_dir, baseline = verified_build(args.baseline_build)
    candidate_dir, candidate = verified_build(args.candidate_build or args.baseline_build)
    if baseline['kernel_sha256'] != candidate['kernel_sha256']:
        parser.error('Keep the exact current kernel constant; this tests userspace/initramfs rollback')
    root = WORK/('bootevo-'+time.strftime('%m%d-%H%M%S')+'-'+str(os.getpid()))
    root.mkdir(mode=0o700)
    if len(str(root/'s0/rescue.sock').encode()) >= 108:
        parser.error('Workspace path leaves insufficient room for QEMU UNIX sockets')
    sentinel = {'schema':1, 'token':uuid.uuid4().hex,
                'purpose':'same-PV normal boot' if args.normal_only else 'same-PV upgrade rollback'}
    report = {'schema':1, 'scope':'isolated initramfs overlay prototype, not installed startup integration',
              'mode':'normal-only' if args.normal_only else 'fault-matrix',
              'baseline_build':baseline, 'candidate_build':candidate,
              'runner_sha256':sha256(Path(__file__)),
              'qemu_runner_sha256':sha256(Path(__file__).with_name('run.py')),
              'qemu_version':subprocess.check_output(['qemu-system-x86_64', '--version'], text=True).splitlines()[0],
              'sentinel':sentinel, 'cases':{}, 'passed':False}
    try:
        seed, disk = run_case(root, 'seed', baseline_dir, baseline, sentinel, tcg=args.tcg,
                             shutdown_timeout=150 if args.normal_only else 30)
        report['cases']['seed'] = seed
        enrollment = seed['observation']['gate']
        (root/'enrollment.json').write_text(json.dumps(enrollment, indent=2)+'\n')
        report['source_image_sha256_before'] = sha256(disk)
        if args.normal_only:
            if not seed['graceful_shutdown']:
                raise RuntimeError('Normal boot experiment requires a clean seed shutdown')
            repeated, _ = run_case(root, 'existing-boot', candidate_dir, candidate, sentinel,
                enrollment, disk, 'raw', args.tcg, shutdown_timeout=150)
            report['cases']['existing-boot'] = repeated
            report['source_image_sha256_after'] = sha256(disk)
            report['checks'] = {'seed_contract':seed['passed'], 'existing_boot_contract':repeated['passed'],
                'both_clean_shutdowns':seed['graceful_shutdown'] and repeated['graceful_shutdown'],
                'new_boot_id':seed['observation']['boot_id'] != repeated['observation']['boot_id'],
                'machine_id_preserved':seed['observation']['machine_id'] == repeated['observation']['machine_id'],
                'fstab_preserved':seed['observation']['fstab_sha256'] == repeated['observation']['fstab_sha256'],
                'seed_backing_unchanged':report['source_image_sha256_before'] == report['source_image_sha256_after']}
            report['passed'] = all(report['checks'].values())
            if not report['passed']:
                raise RuntimeError('Normal existing-PV boot contract failed')
            return
        for scenario in ['preparation-failure', 'wrong-version', 'missing-config', 'existing-transaction', 'guard-start-failure']:
            result, candidate_disk = run_case(root, scenario, candidate_dir, candidate, sentinel,
                enrollment, disk, 'raw', args.tcg)
            report['cases'][scenario] = result
        # Use the failed candidate as backing. A sibling of the seed would hide
        # the candidate's persistent changes and would not demonstrate rollback.
        report['failed_candidate_sha256_before'] = sha256(candidate_disk)
        rollback, _ = run_case(root, 'rollback', baseline_dir, baseline, sentinel,
            enrollment, candidate_disk, 'qcow2', args.tcg)
        report['cases']['rollback'] = rollback
        report['failed_candidate_sha256_after'] = sha256(candidate_disk)
        report['source_image_sha256_after'] = sha256(disk)
        report['checks'] = {'all_case_contracts':all(case['passed'] for case in report['cases'].values()),
            'seed_backing_unchanged':report['source_image_sha256_before'] == report['source_image_sha256_after'],
            'failed_candidate_backing_unchanged':report['failed_candidate_sha256_before'] == report['failed_candidate_sha256_after'],
            'new_boot_for_every_case':len({case['observation']['boot_id'] for case in report['cases'].values()}) == len(report['cases']),
            'rollback_used_baseline_initramfs':rollback['build']['initramfs_sha256'] == baseline['initramfs_sha256']}
        report['passed'] = all(report['checks'].values())
    except BaseException:
        report['error'] = traceback.format_exc()
        raise
    finally:
        (root/'report.json').write_text(json.dumps(report, indent=2)+'\n')
        print('Boot evolution report:', root/'report.json', flush=True)
    print(json.dumps({'passed':report['passed'], 'checks':report['checks']}, indent=2))
    if not report['passed']:
        raise SystemExit('Boot evolution contract failed')


if __name__ == '__main__':
    # The Python runtime was retired; replay this historical experiment intact.
    from historical import run_legacy
    raise SystemExit(run_legacy(__file__))
