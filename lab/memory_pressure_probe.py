#!/usr/bin/env python3
"""Exercise the Guard's local memory cgroup limit in a disposable minimal VM.

A disposable allocator joins only the Guard cgroup and touches up to 200 MiB.
The host does not allocate pressure memory, and the guest as a whole is not put
under global memory pressure. An OOM survivor must still pass ordinary replug
and acknowledged-data checks; a killed owner must end conservatively instead.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

from auto_run import result, wait_for
from measure_guard import MEMORY_HELPERS
from run import Channel, WORK, qemu_command, shell_probe
from transaction_probe import (attach, detach, is_terminal, no_inactive,
                               not_suspended, ram_action, sha256)


PRESSURE = r'''
pid = owner_pid()
relative = Path(f'/proc/{pid}/cgroup').read_text().strip().split('0::', 1)[1]
assert relative == '/lab-guard', relative
cg = Path('/proc/1/root/sys/fs/cgroup') / relative.lstrip('/')
assert (cg/'memory.max').read_text().strip() == '134217728'
assert (cg/'memory.swap.max').read_text().strip() == '0'
assert Path('/proc/self/cgroup').read_text() != Path(f'/proc/{pid}/cgroup').read_text()
def lightweight():
    value = {'guest_time': time.monotonic()}
    for name in ('memory.current', 'memory.peak', 'memory.swap.current', 'memory.swap.max', 'memory.max'):
        value[name] = int((cg/name).read_text())
    value['events'] = {key: int(count) for key, count in
                       (line.split() for line in (cg/'memory.events').read_text().splitlines())}
    value['pids'] = [int(p) for p in (cg/'cgroup.procs').read_text().split()]
    return value
before = lightweight()
collector = os.fork()
if collector == 0:
    os.setsid()
    devnull = os.open('/dev/null', os.O_RDWR)
    for descriptor in (0, 1, 2):
        os.dup2(devnull, descriptor)
    victim = os.fork()
    if victim == 0:
        (cg/'cgroup.procs').write_text(str(os.getpid()))
        Path('/run/memory-pressure-joined.json').write_text(json.dumps({
            'pid': os.getpid(), 'cgroup': Path('/proc/self/cgroup').read_text(),
            'oom_score_adj': Path('/proc/self/oom_score_adj').read_text().strip(),
            'requested_bytes': 200*1024*1024}))
        chunks = []
        for index in range(200):
            block = bytearray(1024*1024)
            # Explicitly dirty each page; address-space reservation is not pressure.
            block[::4096] = b'x'*256
            chunks.append(block)
            if index % 8 == 0:
                Path('/run/memory-pressure-progress.json').write_text(json.dumps({
                    'pid': os.getpid(), 'touched_bytes': (index+1)*1024*1024,
                    'guest_time': time.monotonic()}))
            time.sleep(.002)
        Path('/run/memory-pressure-unexpected-success').write_text('200 MiB allocated')
        os._exit(0)
    samples = []
    deadline = time.monotonic()+25
    forced = False
    while True:
        samples.append(lightweight())
        done, status = os.waitpid(victim, os.WNOHANG)
        if done:
            break
        if time.monotonic() >= deadline:
            forced = True
            os.kill(victim, signal.SIGKILL)
            _, status = os.waitpid(victim, 0)
            break
        time.sleep(.05)
    outcome = {'allocator_pid': victim, 'wait_status': status,
        'signal': os.WTERMSIG(status) if os.WIFSIGNALED(status) else None,
        'exit_code': os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
        'forced_cleanup': forced, 'before': before, 'after': lightweight(), 'samples': samples}
    temporary = Path('/run/memory-pressure-result.tmp')
    temporary.write_text(json.dumps(outcome))
    temporary.replace('/run/memory-pressure-result.json')
    os._exit(0)
answer = {'collector_pid': collector, 'owner_pid': pid, 'cgroup': str(cg),
          'sampler_cgroup': Path('/proc/self/cgroup').read_text(), 'before': before}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=WORK)
    parser.add_argument('--tcg', action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as a normal user; no host privileges are needed')
    build = json.loads((args.build_dir/'build.json').read_text())
    for name, key in (('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')):
        if sha256(args.build_dir/name) != build[key]:
            parser.error('Build hash mismatch: '+name)
    folder = WORK/(time.strftime('%Y%m%d-%H%M%S')+'-oom-'+str(os.getpid()))
    folder.mkdir(mode=0o700)
    for name in ('usb.raw', 'decoy.raw'):
        with (folder/name).open('xb') as stream:
            stream.truncate(2*1024**3)
    sources = [Path(__file__), Path(__file__).with_name('transaction_probe.py'),
               Path(__file__).with_name('measure_guard.py'), Path(__file__).with_name('run.py')]
    for source in sources:
        (folder/source.name).write_bytes(source.read_bytes())
    command = qemu_command(folder, tcg=args.tcg, same_port=True,
        extra_kernel_args='ram_rescue_mpath=1 ram_rescue_queue_seconds=12',
        kernel=args.build_dir/'vmlinuz', initramfs=args.build_dir/'initramfs.cpio.gz')
    report = {'build': build, 'command': command,
        'source_sha256': {source.name: sha256(source) for source in sources},
        'scope': 'Local Guard cgroup OOM; not whole-machine low memory, root storage pressure, or arbitrary OOM victim coverage',
        'passed': False, 'checks': {}}
    print('Guard cgroup memory experiment:', folder, flush=True)
    channels = []
    with (folder/'qemu.log').open('w') as log, (folder/'qmp.jsonl').open('w') as qlog, (folder/'agent.jsonl').open('w') as alog:
        vm = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            wait_for(lambda: (folder/'qmp.sock').exists(), 10, 'QMP')
            qmp = Channel(folder/'qmp.sock', qlog, qmp=True)
            channels.append(qmp)
            def booted():
                console = (folder/'console.log').read_text(errors='replace')
                if vm.poll() is not None or 'Kernel panic' in console:
                    raise RuntimeError('VM boot failed')
                return 'LAB_ROOT_READY:' in console
            wait_for(booted, 120, 'guest root')
            guest = Channel(folder/'agent.sock', alog)
            channels.append(guest)
            wait_for(lambda: any(e['message'].get('event') == 'heartbeat' for e in guest.events), 10, 'RAM agent')
            def snapshot():
                return result(guest.call('snapshot'))
            report['before'] = wait_for(lambda: s if ((s := snapshot()).get('path_guard') or {}).get('state') == 'ready' else None,
                                       10, 'Guard ready')
            result(guest.call('workload'))
            wait_for(lambda: len(snapshot()['workload'].splitlines()) >= 3, 10, 'baseline fsync writes')
            memory_body = MEMORY_HELPERS + '\nanswer = memory_snapshot(Path("/proc/1/root/sys/fs/cgroup/lab-guard"))\n'
            report['memory_before'] = ram_action(folder, '01-memory-before', memory_body)
            report['injection'] = ram_action(folder, '02-pressure', PRESSURE)
            report['shell_during_pressure'] = shell_probe(folder/'rescue.sock')
            report['pressure'] = ram_action(folder, '03-pressure-result',
                'deadline = time.monotonic()+30\n'
                'while not Path("/run/memory-pressure-result.json").exists():\n'
                ' if time.monotonic() >= deadline: raise TimeoutError("pressure collector")\n'
                ' time.sleep(.05)\n'
                'answer = json.loads(Path("/run/memory-pressure-result.json").read_text())\n'
                'answer["joined"] = json.loads(Path("/run/memory-pressure-joined.json").read_text())\n'
                'answer["progress"] = json.loads(Path("/run/memory-pressure-progress.json").read_text())\n', timeout=35)
            report['memory_after_oom'] = ram_action(folder, '04-memory-after-oom', memory_body)
            report['guard_after_pressure'] = snapshot()
            report['oom_observation'] = ram_action(folder, '05-oom-observe', 'answer = observe()\n')
            original = report['injection']['owner_pid']
            owner_live = any(p['pid'] == original and any(t['state'] not in ('Z', 'X') for t in p['tasks'])
                             for p in report['oom_observation']['processes'])
            report['owner_survived_oom'] = owner_live
            if owner_live:
                if report['guard_after_pressure']['path_guard']['state'] != 'ready':
                    raise RuntimeError('Surviving owner is not ready; do not disguise failed recovery as success')
                report['replug_host_time'] = time.monotonic()
                detach(qmp)
                time.sleep(.2)
                attach(qmp)
                report['replug_outcome'] = wait_for(lambda: s if is_terminal(s := snapshot()) or
                    s['path_guard']['state'] == 'ready' and s['path_guard'].get('recoveries') == 1 else None, 25, 'replug outcome')
                time.sleep(1)
                report['after'] = snapshot()
                report['block_after'] = result(guest.call('block_probe'))
                report['data_audit'] = result(guest.call('audit_workload'))
                report['filesystem_after'] = result(guest.call('filesystem_state'))
            else:
                report['after'] = wait_for(lambda: s if is_terminal(s := snapshot()) else None, 12, 'conservative owner takeover')
            report['final_observation'] = ram_action(folder, '06-final-observe', 'answer = observe()\n')
            report['memory_final'] = ram_action(folder, '07-memory-final', memory_body)
            report['shell_after'] = shell_probe(folder/'rescue.sock')
            after = report['after']
            pressure = report['pressure']
            pre, post = pressure['before'], pressure['after']
            heartbeats = [e['host_time'] for e in guest.events if e['message'].get('event') == 'heartbeat']
            report['max_heartbeat_gap'] = max((b-a for a, b in zip(heartbeats, heartbeats[1:])), default=None)
            kernel = report['guard_after_pressure']['kernel']
            report['oom_kernel_lines'] = [line for line in kernel.splitlines() if any(word in line.lower() for word in ('oom', 'out of memory', 'killed process'))]
            allocator_in_kernel = bool(re.search(r'\bKilled process\s+'+str(pressure['allocator_pid'])+r'\b', kernel))
            guard_processes = [p for p in report['final_observation']['processes'] if '/opt/lab/path_guard.py' in p['cmdline'] or p['pid'] == original]
            checks = report['checks']
            checks.update(local_memory_limit_fixed=post['memory.max'] == 128*1024**2,
                no_guard_swap=post['memory.swap.max'] == 0 and all(s['memory.swap.current'] == 0 for s in pressure['samples']),
                allocator_joined_only_guard_group=pressure['joined']['cgroup'].strip() == '0::/lab-guard',
                cgroup_oom_recorded=post['events']['oom'] > pre['events']['oom'] and post['events']['oom_kill'] > pre['events']['oom_kill'],
                disposable_allocator_killed_by_oom=pressure['signal'] == 9 and not pressure['forced_cleanup'] and allocator_in_kernel,
                ram_shell=report['shell_during_pressure'] and report['shell_after'],
                ram_agent=len(heartbeats) >= 3 and report['max_heartbeat_gap'] < 2,
                at_most_one_owner=len(guard_processes) <= 1,
                no_permanent_dm_suspend=not_suspended(report['final_observation']),
                no_inactive_table_left=no_inactive(report['final_observation']))
            if owner_live:
                writes = [json.loads(line) for line in after['workload'].splitlines()]
                checks.update(recovered_after_oom=after['path_guard']['state'] == 'ready' and after['path_guard'].get('recoveries') == 1,
                    same_owner_epoch=after['path_guard']['owner_epoch'] == report['before']['path_guard']['owner_epoch'],
                    zero_application_errors=all(row['ok'] for row in writes),
                    same_application_process=after['workload_process'] == report['guard_after_pressure']['workload_process'],
                    acknowledged_data_present=report['data_audit']['prefix_matches'],
                    block_read_valid=report['block_after']['bytes'] == 4096 and report['block_after']['ext4_magic'] == '53ef',
                    filesystem_writable=report['filesystem_after']['write_fsync_ok'])
            else:
                checks['owner_loss_was_not_reported_as_recovery'] = is_terminal(after) and after['path_guard'].get('recoveries', 0) == 0
                checks['supervisor_recorded_terminal'] = bool(after.get('path_supervisor'))
            report.update(completed=True, passed=all(checks.values()))
        except Exception as exc:
            report.update(completed=False, error=repr(exc))
        finally:
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill()
                vm.wait()
            for channel in channels:
                channel.close()
            (folder/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'report': str(folder/'report.json'), 'passed': report['passed'],
                      'checks': report['checks'], 'error': report.get('error')}, indent=2), flush=True)
    if not report['passed']:
        raise SystemExit('Local cgroup memory acceptance failed')


if __name__ == '__main__':
    main()
