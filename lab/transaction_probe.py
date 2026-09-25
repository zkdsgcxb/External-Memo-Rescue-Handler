#!/usr/bin/env python3
"""Fault schedules for the VM-only Guard recovery transaction.

SIGKILL is synchronized with an explicit guest phase hook. The blocked-probe
case holds NBD requests at a host proxy until the experiment releases them;
there is no automatic timeout or finite bandwidth substitute. This models a
lower request that does not complete, not the USB driver's exact behavior.
Only newly created regular files below lab/work are attached to QEMU.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import subprocess
import threading
import time
import traceback

from auto_run import result, wait_for
from run import Channel, WORK, qemu_command, shell_probe


STAGES = ('before_load', 'after_load', 'before_commit', 'after_commit',
          'before_probe', 'probe_started', 'before_ready')
TERMINAL = {'expired', 'failed', 'interrupted', 'blocked'}
GUEST_COMMON = '''\
import hashlib, json, os, signal, subprocess, time
from pathlib import Path
assert 'ram_rescue_lab=1' in Path('/proc/cmdline').read_text().split()
assert Path('/sys/class/dmi/id/product_name').read_text().strip() == 'RAMRescueLab'
def owner_pid():
    pid = int(Path('/run/path-guard.pid').read_text())
    assert b'/opt/lab/path_guard.py' in Path(f'/proc/{pid}/cmdline').read_bytes()
    return pid
def query(*args):
    try:
        p = subprocess.run(['/sbin/dmsetup', '--noudevsync', *args], text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
        return {'returncode': p.returncode, 'stdout': p.stdout.strip(), 'stderr': p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {'timed_out': True}
def observe():
    answer = {'guest_time': time.monotonic(), 'files': {}, 'processes': [], 'devices': []}
    tracked_owner = int(Path('/run/path-guard.pid').read_text())
    for path in sorted(Path('/run').glob('path-*')):
        if path.is_file():
            data = path.read_bytes()
            answer['files'][path.name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            if path.suffix == '.json':
                answer['files'][path.name]['value'] = json.loads(data)
    for path in sorted(Path('/sys/class/block').glob('sd*')):
        try:
            resolved = path.resolve()
            disk = resolved.parent if (path/'partition').exists() else resolved
            answer['devices'].append({'name': path.name, 'sysfs': str(resolved),
                'dev': (path/'dev').read_text().strip(), 'diskseq': (disk/'diskseq').read_text().strip()})
        except FileNotFoundError:
            continue
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            arguments = (path/'cmdline').read_bytes().split(b'\\0')
            cmdline = b' '.join(arguments).decode()
            # An exited leader can have an empty cmdline while one of its
            # threads is still blocked in the kernel. Keep tracking its PID.
            if int(path.name) != tracked_owner and b'/opt/lab/path_guard.py' not in arguments and arguments[0] != b'/sbin/dmsetup':
                continue
            tasks = []
            for task in (path/'task').iterdir():
                stat = (task/'stat').read_text().rsplit(')', 1)[1].split()
                tasks.append({'tid': int(task.name), 'state': stat[0], 'start_ticks': stat[19],
                              'wchan': (task/'wchan').read_text().strip()})
            answer['processes'].append({'pid': int(path.name), 'cmdline': cmdline, 'tasks': tasks})
        except (FileNotFoundError, ProcessLookupError):
            continue
    inode = str(Path('/run/path-owner.lock').stat().st_ino)
    answer['owner_locks'] = [line for line in Path('/proc/locks').read_text().splitlines()
                            if len(line.split()) > 5 and line.split()[5].endswith(':'+inode)]
    for field, args in [('active', ('table', 'lab-path')),
                        ('inactive', ('table', '--inactive', 'lab-path')),
                        ('status', ('status', 'lab-path')),
                        ('suspended', ('info', '-c', '--noheadings', '-o', 'suspended', 'lab-path')),
                        ('uuid', ('info', '-c', '--noheadings', '-o', 'uuid', 'lab-path'))]:
        answer[field] = query(*args)
        answer[field]['sha256'] = hashlib.sha256(answer[field].get('stdout', '').encode()).hexdigest()
    return answer
'''


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ram_action(folder, name, body, timeout=15):
    """Keep source and raw serial output for each privileged VM-only action."""
    source = GUEST_COMMON + '\n' + body
    (folder / (name + '.py')).write_text(source)
    encoded = base64.b64encode(source.encode()).decode()
    wrapper = ('import base64,json,traceback\n'
               'scope={}\n'
               'try:\n'
               ' exec(base64.b64decode(' + repr(encoded) + '),scope)\n'
               ' response={"ok":True,"result":scope["answer"]}\n'
               'except BaseException:\n'
               ' response={"ok":False,"error":traceback.format_exc()}\n'
               'print("TRANSACTION_RESULT="+json.dumps(response),flush=True)\n')
    payload = base64.b64encode(wrapper.encode()).decode()
    output = b''
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(folder / 'rescue.sock'))
            sock.sendall(b'stty -echo\n')
            time.sleep(.05)
            sock.sendall(b': > /run/transaction-action.b64\n')
            for offset in range(0, len(payload), 256):
                sock.sendall(("printf '%s' '" + payload[offset:offset + 256] +
                              "' >> /run/transaction-action.b64\n").encode())
                time.sleep(.002)
            sock.sendall(b"python3 -c \"import base64;exec(base64.b64decode(open('/run/transaction-action.b64','rb').read()))\"\n")
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeError('RAM shell closed')
                output += chunk
                marker = b'TRANSACTION_RESULT='
                if marker in output and b'\n' in output.split(marker, 1)[1]:
                    return result(json.loads(output.split(marker, 1)[1].split(b'\n', 1)[0]))
    finally:
        (folder / (name + '.serial.log')).write_bytes(output)


class NBDGate:
    """Transparent, bounded-memory request gate in front of qemu-nbd."""
    def __init__(self, folder):
        self.folder = folder
        self.enabled = threading.Event()
        self.enabled.set()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.sockets = []
        self.counts = {'received_request_bytes': 0, 'forwarded_request_bytes': 0,
                       'reply_bytes': 0, 'held_bytes': 0, 'connections': 0}
        self.log = (folder / 'qemu-nbd.log').open('w')
        self.backend = subprocess.Popen(['qemu-nbd', '--persistent', '--shared=4',
            '--socket', str(folder / 'nbd-backend.sock'), '--format=raw', str(folder / 'usb.raw')],
            stdout=self.log, stderr=self.log)
        wait_for(lambda: (folder / 'nbd-backend.sock').exists(), 10, 'NBD backend')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(folder / 'nbd-gate.sock'))
        self.listener.listen(4)
        self.sockets.append(self.listener)
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stopped.is_set():
            try:
                front, _ = self.listener.accept()
                back = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                back.connect(str(self.folder / 'nbd-backend.sock'))
                self.sockets.extend((front, back))
                with self.lock:
                    self.counts['connections'] += 1
                for source, destination, request in ((front, back, True), (back, front, False)):
                    threading.Thread(target=self.copy, args=(source, destination, request), daemon=True).start()
            except OSError:
                if not self.stopped.is_set():
                    with self.lock:
                        self.counts['accept_error'] = traceback.format_exc()
                break

    def copy(self, source, destination, request):
        try:
            while not self.stopped.is_set():
                data = source.recv(65536)
                if not data:
                    break
                if request:
                    with self.lock:
                        self.counts['received_request_bytes'] += len(data)
                        self.counts['held_bytes'] += len(data)
                    self.enabled.wait()
                    if self.stopped.is_set():
                        break
                    destination.sendall(data)
                    with self.lock:
                        self.counts['forwarded_request_bytes'] += len(data)
                        self.counts['held_bytes'] -= len(data)
                else:
                    destination.sendall(data)
                    with self.lock:
                        self.counts['reply_bytes'] += len(data)
        except OSError:
            pass
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def hold(self):
        self.enabled.clear()

    def release(self):
        self.enabled.set()

    def snapshot(self):
        with self.lock:
            return {'host_time': time.monotonic(), 'gate_open': self.enabled.is_set(), **self.counts}

    def close(self):
        self.stopped.set()
        self.enabled.set()
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.backend.terminate()
        try:
            self.backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.backend.kill()
            self.backend.wait()
        self.thread.join(timeout=2)
        self.log.close()


def attach(qmp):
    qmp.call('device_add', driver='usb-uas', bus='xhci.0', id='stick',
             serial='RAMRESCUE-LAB-001', attached=False, port='1')
    qmp.call('device_add', driver='scsi-hd', bus='stick.0', id='lun', drive='usbdisk')
    qmp.call('qom-set', path='/machine/peripheral/stick', property='attached', value=True)


def detach(qmp):
    started = time.monotonic()
    qmp.call('device_del', id='stick')
    wait_for(lambda: any(e['host_time'] >= started and e['message'].get('event') == 'DEVICE_DELETED'
             and e['message'].get('data', {}).get('device') == 'stick' for e in qmp.events), 10, 'USB removal')


def is_terminal(snapshot):
    return (snapshot.get('path_guard') or {}).get('state') in TERMINAL


def no_inactive(observation):
    data = observation['inactive']
    return data.get('returncode') == 0 and not data.get('stdout', '').strip()


def not_suspended(observation):
    data = observation['suspended']
    return data.get('returncode') == 0 and data.get('stdout') == 'Active'


def stable_table(raw):
    """Ignore queue_if_no_path and selected group, not geometry or backends."""
    normalized = []
    for line in raw.splitlines():
        start, size, kind, params = line.split(maxsplit=3)
        words = params.split()
        if kind == 'multipath':
            count = int(words[0])
            features = [w for w in words[1:count + 1] if w != 'queue_if_no_path']
            tail = words[count + 1:]
            group_index = int(tail[0]) + 1
            if tail[group_index] != '1' or tail[group_index + 1] not in ('0', '1'):
                raise ValueError('Expected enrolled single path group')
            tail[group_index + 1] = '1'
            words = [str(len(features)), *features, *tail]
        normalized.append([start, size, kind, words])
    return normalized


def run_case(args, scenario, stage, random_case=None):
    short = {'kill': 'kill', 'manager-absent': 'abs', 'deadline': 'time',
             'candidate-replaced': 'inst', 'blocked-probe': 'stall', 'random-kill': 'rnd'}[scenario]
    # Linux AF_UNIX paths are limited to 107 bytes, including this project path.
    folder = WORK / (time.strftime('%Y%m%d-%H%M%S') + f'-tx-{short}-{STAGES.index(stage)}-{os.getpid()}')
    folder.mkdir(mode=0o700)
    (folder / 'runner.py').write_bytes(Path(__file__).read_bytes())
    for name in ('usb.raw', 'decoy.raw'):
        with (folder / name).open('xb') as stream:
            stream.truncate((8 if args.guest == 'ubuntu' else 2) * 1024**3)
    command = qemu_command(folder, tcg=args.tcg, same_port=True, ubuntu=args.guest == 'ubuntu',
        extra_kernel_args=f'ram_rescue_mpath=1 ram_rescue_queue_seconds={args.queue_seconds}' +
            (' ram_rescue_ubuntu=1 root=/dev/mapper/labrescue-ubuntu rw' if args.guest == 'ubuntu' else ''),
        kernel=args.build_dir / 'vmlinuz', initramfs=args.build_dir / 'initramfs.cpio.gz')
    gate = None
    if scenario == 'blocked-probe':
        gate = NBDGate(folder)
        for index, arg in enumerate(command):
            if arg == '-blockdev':
                block = json.loads(command[index + 1])
                if block.get('node-name') == 'usbdisk':
                    command[index + 1] = json.dumps({'driver': 'nbd', 'node-name': 'usbdisk',
                        'server': {'type': 'unix', 'path': str(folder / 'nbd-gate.sock')}})
    report = {'scenario': scenario, 'stage': stage, 'guest': args.guest, 'queue_seconds': args.queue_seconds,
              'random_schedule': random_case,
              'build': json.loads((args.build_dir / 'build.json').read_text()), 'command': command,
              'runner_sha256': sha256(Path(__file__)), 'qemu_runner_sha256': sha256(Path(__file__).parent / 'run.py'),
              'scope': 'Disposable UAS guest; transaction fault schedules, not hardware equivalence or exhaustive race coverage',
              'negative_data_scope': 'Retains raw disk and acknowledged-write log; no offline durability or post-error prefix audit',
              'passed': False, 'checks': {}}
    print('Transaction experiment:', folder, flush=True)
    channels = []
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog, (folder / 'agent.jsonl').open('w') as alog:
        vm = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            channels.append(qmp)
            def booted():
                console = (folder / 'console.log').read_text(errors='replace')
                if 'Kernel panic' in console or vm.poll() is not None:
                    raise RuntimeError('VM failed to boot')
                return 'LAB_ROOT_READY:' in console
            wait_for(booted, 360 if args.guest == 'ubuntu' else 120, 'guest root')
            guest = Channel(folder / 'agent.sock', alog)
            channels.append(guest)
            # Ubuntu's root-ready marker can precede the RAM agent's UART raw
            # setup; do not let that setup flush the first request from us.
            wait_for(lambda: any(e['message'].get('event') == 'heartbeat' for e in guest.events), 15, 'RAM agent heartbeat')
            def snapshot():
                return result(guest.call('snapshot'))
            report['before'] = wait_for(lambda: s if ((s := snapshot()).get('path_guard') or {}).get('state') == 'ready' else None,
                                       10, 'Guard ready')
            report['verify_before'] = result(guest.call('verify'))
            if args.guest == 'ubuntu':
                report['ubuntu_before'] = wait_for(lambda: s if (s := result(guest.call('ubuntu_status')))['multi_user'] == 'active' else None,
                                                  180, 'Ubuntu multi-user target')
                report['supervisor_service_before'] = ram_action(folder, '00-systemd-supervisor',
                    'p = subprocess.run(["/bin/chroot", "/proc/1/root", "/usr/bin/systemctl", "show",\n'
                    '  "lab-guard.service", "-p", "RootDirectory", "-p", "ExecStopPost", "-p", "ExecMainStatus", "-p", "Result", "-p", "ControlGroup"],\n'
                    '  text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)\n'
                    'answer = {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}\n')
            report['owner_contention'] = ram_action(folder, '00-owner-contention',
                'original = owner_pid()\n'
                'before_table = query("table", "lab-path")\n'
                'p = subprocess.run(["/usr/bin/python3", "/opt/lab/path_guard.py"], text=True,\n'
                '    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4)\n'
                'answer = {"original_owner": original, "owner_after": owner_pid(), "returncode": p.returncode,\n'
                '    "stdout": p.stdout, "stderr": p.stderr, "before_table": before_table, "after_table": query("table", "lab-path")}\n')
            result(guest.call('workload'))
            wait_for(lambda: len(snapshot()['workload'].splitlines()) >= 3, 10, 'baseline writes')
            token = f'{scenario}-{stage}-{os.getpid()}'
            config = {'stage': 'disabled' if scenario == 'random-kill' else stage, 'action': 'pause', 'token': token}
            report['armed'] = ram_action(folder, '01-arm',
                "Path('/run/lab-fault-config.json').write_text(" + repr(json.dumps(config)) + ")\nanswer = observe()\n")
            report['pre_fault'] = snapshot()
            report['fault_host_time'] = time.monotonic()
            if scenario == 'random-kill':
                delay = random_case['delay_after_trigger_seconds']
                report['random_armed'] = ram_action(folder, '02-random-arm',
                    'target = owner_pid()\n'
                    'pid = os.fork()\n'
                    'if pid == 0:\n'
                    ' os.setsid()\n'
                    ' devnull = os.open("/dev/null", os.O_RDWR)\n'
                    ' for descriptor in (0, 1, 2): os.dup2(devnull, descriptor)\n'
                    ' while not Path("/run/random-kill.go").exists(): time.sleep(.001)\n'
                    ' triggered = time.monotonic()\n'
                    f' time.sleep({delay!r})\n'
                    ' observation = observe()\n'
                    ' previous = json.loads(Path("/run/path-state.json").read_text())\n'
                    ' marker = {"killed_pid": target, "triggered_guest_time": triggered,\n'
                    '   "signal_guest_time": time.monotonic(), "observation": observation, "state_at_kill": previous}\n'
                    ' Path("/run/random-kill-result.json").write_text(json.dumps(marker))\n'
                    ' os.kill(target, signal.SIGKILL)\n'
                    ' os._exit(0)\n'
                    'answer = {"injector_pid": pid, "target_pid": target}\n')
                report['random_trigger'] = ram_action(folder, '03-random-trigger',
                    'Path("/run/random-kill.go").touch()\nanswer = {"guest_time": time.monotonic()}\n')
                report['fault_host_time'] = time.monotonic()
                detach(qmp)
                time.sleep(.2)
                attach(qmp)
                time.sleep(delay + .1)
                report['kill'] = ram_action(folder, '04-random-result',
                    'answer = json.loads(Path("/run/random-kill-result.json").read_text())\n')
            elif scenario == 'manager-absent':
                report['kill'] = ram_action(folder, '02-kill-healthy', 'pid = owner_pid()\nos.kill(pid, signal.SIGKILL)\nanswer = {"killed_pid": pid}\n')
                wait_for(lambda: is_terminal(snapshot()), 10, 'supervisor detects missing owner')
                detach(qmp)
            else:
                detach(qmp)
                attach(qmp)
                def reached():
                    snap = snapshot()
                    hook = snap.get('fault_hook') or {}
                    return snap if hook.get('token') == token and hook.get('stage') == stage else None
                report['at_hook'] = wait_for(reached, args.queue_seconds + 5, 'phase hook ' + stage)
                report['hook_host_time'] = time.monotonic()
                report['hook_observation'] = ram_action(folder, '02-at-hook', 'answer = observe()\n')
                if scenario == 'kill':
                    report['kill'] = ram_action(folder, '03-kill', 'pid = owner_pid()\nos.kill(pid, signal.SIGKILL)\nanswer = {"killed_pid": pid}\n')
                else:
                    if scenario == 'candidate-replaced':
                        detach(qmp)
                        attach(qmp)
                        report['second_replug_host_time'] = time.monotonic()
                        # Linux must finish discovering a *new* instance before release.
                        time.sleep(.5)
                    elif scenario == 'deadline':
                        deadline = report['at_hook']['path_transaction']['deadline']
                        elapsed = time.monotonic() - report['hook_host_time']
                        remaining = deadline - report['at_hook']['guest_time'] - elapsed
                        time.sleep(max(0, remaining + 1))
                    elif scenario == 'blocked-probe':
                        gate.hold()
                        report['gate_closed'] = gate.snapshot()
                    report['release'] = ram_action(folder, '03-release',
                        "Path('/run/lab-fault-release.json').write_text(" + repr(json.dumps({'token': token})) + ")\nanswer = observe()\n")
            if scenario == 'blocked-probe':
                wait_for(lambda: gate.snapshot()['held_bytes'] > 0, 5, 'held lower I/O request')
                report['held_early'] = snapshot()
                report['held_processes_early'] = ram_action(folder, '04-held-early', 'answer = observe()\n')
                report['gate_early'] = gate.snapshot()
                # Hold beyond both userspace and kernel no-path budgets. Neither
                # budget is an end-to-end cancellation promise for this request.
                time.sleep(args.queue_seconds + 4)
                report['held_late'] = snapshot()
                report['held_processes_late'] = ram_action(folder, '05-held-late', 'answer = observe()\n')
                report['gate_late'] = gate.snapshot()
                report['shell_while_blocked'] = shell_probe(folder / 'rescue.sock')
                gate.release()
                report['gate_released'] = gate.snapshot()
            report['outcome'] = wait_for(lambda: s if is_terminal(s := snapshot()) or
                scenario == 'candidate-replaced' and s['path_guard'].get('state') == 'ready' and
                s['path_guard'].get('recoveries', 0) > 0 else None, args.queue_seconds + 15, 'transaction outcome')
            time.sleep(1)
            report['after'] = snapshot()
            report['final_observation'] = ram_action(folder, '06-final', 'answer = observe()\n')
            report['shell_after'] = shell_probe(folder / 'rescue.sock')
            if args.guest == 'ubuntu':
                report['ubuntu_post_fault_scope'] = 'RAM supervisor evidence only; no claim that normal Ubuntu services survive the expected failed-root outcome'
            before, after = report['pre_fault'], report['after']
            writes = [json.loads(line) for line in after['workload'].splitlines()]
            report['successful_writes'] = sum(row['ok'] for row in writes)
            report['failed_writes'] = sum(not row['ok'] for row in writes)
            report['max_write_latency'] = max((row['elapsed'] for row in writes), default=None)
            heartbeats = [e['host_time'] for e in guest.events if e['message'].get('event') == 'heartbeat']
            report['max_heartbeat_gap'] = max((b - a for a, b in zip(heartbeats, heartbeats[1:])), default=None)
            checks = report['checks']
            contention = report['owner_contention']
            checks.update(ram_shell=report['shell_after'], ram_heartbeat=len(heartbeats) >= 3 and report['max_heartbeat_gap'] < 2,
                          upper_mappings_unchanged=before['mappings'] == after['mappings'],
                          no_permanent_dm_suspend=not_suspended(report['final_observation']),
                          no_inactive_table_left=no_inactive(report['final_observation']),
                          duplicate_owner_refused=contention['returncode'] != 0 and
                            contention['original_owner'] == contention['owner_after'] and
                            contention['before_table'] == contention['after_table'])
            if scenario == 'candidate-replaced' and after['path_guard']['state'] == 'ready':
                report['block_after'] = result(guest.call('block_probe'))
                report['data_audit'] = result(guest.call('audit_workload'))
                checks.update(zero_application_errors=report['failed_writes'] == 0,
                    acknowledged_prefix_present=report['data_audit']['prefix_matches'],
                    block_length_and_magic=report['block_after']['bytes'] == 4096 and report['block_after']['ext4_magic'] == '53ef',
                    same_application_process=before['workload_process'] == after['workload_process'])
            else:
                recovered_before_kill = report.get('kill', {}).get('state_at_kill', {}).get('recoveries', 0)
                checks.update(terminal_is_not_recovery=is_terminal(after) and after['path_guard'].get('recoveries', 0) == recovered_before_kill,
                              no_late_ready=after['path_guard']['state'] != 'ready')
            if scenario in ('kill', 'manager-absent', 'random-kill'):
                killed = report['kill']['killed_pid']
                checks['dead_owner_not_running'] = all(p['pid'] != killed for p in report['final_observation']['processes'])
                checks['supervisor_recorded_outcome'] = bool(after.get('path_supervisor'))
            if scenario == 'deadline':
                final_active = report['final_observation']['active']['stdout']
                target_error = all(len(parts := line.split()) >= 3 and parts[2] == 'error'
                                   for line in final_active.splitlines()) and bool(final_active)
                original = report['armed']['active']['stdout']
                # fail_if_no_path legitimately changes the feature count and
                # queue policy; compare geometry and referenced device numbers.
                checks['deadline_did_not_commit_candidate'] = stable_table(original) == stable_table(final_active) or target_error
            if scenario == 'candidate-replaced':
                checks['injected_new_kernel_instance'] = report['hook_observation']['devices'] != report['release']['devices']
            if scenario == 'blocked-probe':
                early, late = report['held_early'], report['held_late']
                early_p, late_p = report['held_processes_early'], report['held_processes_late']
                checks.update(gate_held_indefinitely_until_explicit_release=not report['gate_late']['gate_open'] and
                    report['gate_late']['forwarded_request_bytes'] == report['gate_early']['forwarded_request_bytes'] and report['gate_late']['held_bytes'] > 0,
                    no_io_completion_while_held=early['workload'] == late['workload'],
                    same_active_table_while_held=stable_table(early_p['active']['stdout']) == stable_table(late_p['active']['stdout']),
                    bounded_guard_tasks=max((len(p['tasks']) for p in late_p['processes']), default=0) <= 2,
                    no_additional_owner_while_held=len(late_p['processes']) <= len(early_p['processes']),
                    one_probe_generation=early['path_transaction'].get('generation') == late['path_transaction'].get('generation') == 1,
                    probe_pending_at_deadline=late['path_guard'].get('probe_pending') is True,
                    owner_lock_retained_while_probe_pending=bool(late_p['owner_locks']),
                    terminal_while_lower_request_pending=is_terminal(late), ram_shell_while_pending=report['shell_while_blocked'])
            if args.guest == 'ubuntu':
                service = report['supervisor_service_before']
                supervisor = after.get('path_supervisor') or {}
                checks['systemd_ram_supervisor_ran'] = service['returncode'] == 0 and 'RootDirectory=/run/rescue' in service['stdout'] and '--takeover' in service['stdout'] and bool(supervisor.get('owner_epoch')) and supervisor['owner_epoch'] != before['path_guard']['owner_epoch']
            report.update(completed=True, passed=all(checks.values()))
        except Exception as exc:
            report.update(completed=False, error=repr(exc), traceback=traceback.format_exc())
        finally:
            if gate:
                gate.release()
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill()
                vm.wait()
            for channel in channels:
                channel.close()
            if gate:
                report['gate_final'] = gate.snapshot()
                gate.close()
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(folder / 'report.json'), 'passed': report['passed'],
                      'checks': report['checks'], 'error': report.get('error')}, indent=2), flush=True)
    return folder / 'report.json', report['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=['kill', 'manager-absent', 'deadline', 'candidate-replaced', 'blocked-probe', 'random-kill', 'matrix'])
    parser.add_argument('--stage', choices=STAGES, default='after_load')
    parser.add_argument('--build-dir', type=Path, default=WORK)
    parser.add_argument('--guest', choices=['minimal', 'ubuntu'], default='minimal')
    parser.add_argument('--seed', type=int, default=20260925)
    parser.add_argument('--random-count', type=int, default=5)
    parser.add_argument('--queue-seconds', type=int, default=12)
    parser.add_argument('--tcg', action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as a normal user; no host privileges are needed')
    if not 4 <= args.queue_seconds <= 30:
        parser.error('queue-seconds must be 4..30')
    if not 1 <= args.random_count <= 20:
        parser.error('random-count must be 1..20')
    for name in ('vmlinuz', 'initramfs.cpio.gz', 'build.json'):
        if not (args.build_dir / name).is_file():
            parser.error('Missing guest build: ' + name)
    build = json.loads((args.build_dir / 'build.json').read_text())
    for name, key in (('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')):
        if sha256(args.build_dir / name) != build[key]:
            parser.error('Build hash mismatch: ' + name)
    if args.guest == 'ubuntu':
        seed = json.loads((WORK / 'ubuntu/source.json').read_text())
        if sha256(WORK / 'ubuntu/rootfs.raw') != seed['seed_sha256']:
            parser.error('Ubuntu seed hash differs')
    cases = [('kill', stage) for stage in STAGES] if args.scenario == 'matrix' else [(args.scenario, args.stage)]
    if args.scenario == 'matrix':
        cases += [('manager-absent', 'before_load'), ('deadline', 'before_commit'),
                  ('candidate-replaced', 'before_commit'), ('blocked-probe', 'before_probe')]
    elif args.scenario in ('deadline', 'candidate-replaced'):
        cases = [(args.scenario, 'before_commit')]
    elif args.scenario == 'blocked-probe':
        cases = [(args.scenario, 'before_probe')]
    if args.scenario == 'random-kill':
        rng = random.Random(args.seed)
        results = [run_case(args, 'random-kill', args.stage,
            {'seed': args.seed, 'index': index, 'delay_after_trigger_seconds': rng.uniform(0, 1.2),
             'reattach_delay_seconds': .2, 'scope': 'Seeded small sample; observed phase is authoritative'})
            for index in range(args.random_count)]
    else:
        results = [run_case(args, scenario, stage) for scenario, stage in cases]
    summary = WORK / (time.strftime('%Y%m%d-%H%M%S') + '-transaction-summary-' + str(os.getpid()) + '.json')
    summary.write_text(json.dumps({'scenario': args.scenario, 'build': build,
        'reports': [{'path': str(path), 'sha256': sha256(path), 'passed': passed} for path, passed in results],
        'passed': all(passed for _, passed in results)}, indent=2) + '\n')
    print('Summary:', summary, flush=True)
    if not all(passed for _, passed in results):
        raise SystemExit('Transaction acceptance failed; see per-case raw reports')


if __name__ == '__main__':
    main()
