#!/usr/bin/env python3
"""Hold real candidate metadata I/O beyond the VM Guard's recovery deadline.

The NBD request gate is released explicitly by the host, never by a timer.
Clean guest caches are dropped before closing it. A run is accepted only when
a Guard worker or metadata helper is observed in D state with requests held;
pausing at a phase hook alone is not an I/O-stall test.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import zlib

from auto_run import result, wait_for
from run import Channel, WORK, qemu_command, shell_probe
from transaction_probe import (NBDGate, attach, detach, no_inactive,
                               not_suspended, ram_action, sha256, stable_table)


PROCESS_TRACE = r'''
tracked_owner = int(Path('/run/path-guard.pid').read_text())
saved_group = Path('/run/admission-stall-cgroup')
if saved_group.exists():
    relative = saved_group.read_text()
else:
    relative = Path(f'/proc/{tracked_owner}/cgroup').read_text().strip().split('0::',1)[1]
    saved_group.write_text(relative)
cg = Path('/proc/1/root/sys/fs/cgroup')/relative.lstrip('/')
def group_processes():
    pids = {int(pid) for pid in (cg/'cgroup.procs').read_text().split()}
    # Include the exited leader while an uninterruptible thread still lives.
    if Path(f'/proc/{tracked_owner}').exists():
        pids.add(tracked_owner)
    processes = []
    for pid in sorted(pids):
        path = Path(f'/proc/{pid}')
        try:
            arguments = (path/'cmdline').read_bytes().split(b'\0')
            tasks = []
            for task in (path/'task').iterdir():
                stat = (task/'stat').read_text().rsplit(')',1)[1].split()
                tasks.append({'tid':int(task.name),'state':stat[0], 'start_ticks':stat[19],
                              'wchan':(task/'wchan').read_text().strip()})
            processes.append({'pid':pid,'argv':[arg.decode() for arg in arguments if arg],
                              'tasks':tasks})
        except (FileNotFoundError,ProcessLookupError):
            pass
    lockstat = Path('/run/path-owner.lock').stat()
    lock_key = f'{os.major(lockstat.st_dev):02x}:{os.minor(lockstat.st_dev):02x}:{lockstat.st_ino}'
    locks = [line for line in Path('/proc/locks').read_text().splitlines()
             if len(line.split())>5 and line.split()[5] == lock_key]
    return {'guest_time':time.monotonic(),'processes':processes,'owner_locks':locks}
'''


def metadata_blocked(trace, owner_pid):
    for process in trace['processes']:
        args = process['argv']
        relevant = process['pid'] == owner_pid or any(arg in ('/sbin/blkid', '/sbin/lvm') for arg in args)
        if relevant and any(task['state'] == 'D' for task in process['tasks']):
            return True
    return False


def run_case(args, stage):
    folder = WORK/(time.strftime('%Y%m%d-%H%M%S')+'-adm-'+('v' if stage == 'before_verify' else 'r')+'-'+str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = [Path(__file__), Path(__file__).with_name('transaction_probe.py'),
               Path(__file__).with_name('run.py'), Path(__file__).with_name('auto_run.py')]
    for source in sources:
        (folder/source.name).write_bytes(source.read_bytes())
    for name in ('usb.raw', 'decoy.raw'):
        with (folder/name).open('xb') as stream:
            stream.truncate(2*1024**3)
    gate = NBDGate(folder)
    command = qemu_command(folder, tcg=args.tcg, same_port=True,
        extra_kernel_args=f'ram_rescue_mpath=1 ram_rescue_queue_seconds={args.queue_seconds}',
        kernel=args.build_dir/'vmlinuz', initramfs=args.build_dir/'initramfs.cpio.gz')
    for index, value in enumerate(command):
        if value == '-blockdev' and json.loads(command[index+1]).get('node-name') == 'usbdisk':
            command[index+1] = json.dumps({'driver':'nbd','node-name':'usbdisk',
                'server':{'type':'unix','path':str(folder/'nbd-gate.sock')}})
    report = {'stage':stage,'queue_seconds':args.queue_seconds,'build':json.loads((args.build_dir/'build.json').read_text()),
        'command':command,'source_sha256':{p.name:sha256(p) for p in sources},'passed':False,'checks':{},
        'scope':'Disposable minimal UAS guest; retained metadata request models lower I/O noncompletion, not exact USB driver behavior',
        'data_scope':'Expected terminal outcome; original raw disk and ACK log retained, no post-error consistency or durability claim'}
    channels = []
    print('Admission I/O stall experiment:',folder,flush=True)
    with (folder/'qemu.log').open('w') as log, (folder/'qmp.jsonl').open('w') as qlog, (folder/'agent.jsonl').open('w') as alog:
        vm = subprocess.Popen(command,stdout=log,stderr=log)
        try:
            wait_for(lambda:(folder/'qmp.sock').exists(),10,'QMP')
            qmp = Channel(folder/'qmp.sock',qlog,qmp=True);channels.append(qmp)
            def booted():
                console = (folder/'console.log').read_text(errors='replace')
                if vm.poll() is not None or 'Kernel panic' in console:
                    raise RuntimeError('VM boot failed')
                return 'LAB_ROOT_READY:' in console
            wait_for(booted,120,'guest root')
            guest = Channel(folder/'agent.sock',alog);channels.append(guest)
            wait_for(lambda:any(e['message'].get('event') == 'heartbeat' for e in guest.events),10,'RAM agent')
            def snapshot():
                return result(guest.call('snapshot'))
            report['before'] = wait_for(lambda:s if ((s:=snapshot()).get('path_guard') or {}).get('state') == 'ready' else None,
                                       10,'Guard ready')
            result(guest.call('workload'))
            wait_for(lambda:len(snapshot()['workload'].splitlines()) >= 3,10,'baseline fsync writes')
            token = f'admission-{stage}-{os.getpid()}'
            config = {'stage':stage,'action':'pause','token':token}
            report['armed'] = ram_action(folder,'01-arm',
                "Path('/run/lab-fault-config.json').write_text("+repr(json.dumps(config))+")\nanswer = observe()\n")
            report['owner_pid'] = report['before']['path_transaction']['owner_pid']
            report['fault_host_time'] = time.monotonic()
            detach(qmp);attach(qmp)
            def hook_ready():
                s = snapshot();hook = s.get('fault_hook') or {}
                return s if hook.get('token') == token and hook.get('stage') == stage else None
            report['at_hook'] = wait_for(hook_ready,args.queue_seconds+5,'phase hook')
            report['cache_preparation'] = ram_action(folder,'02-clean-cache',
                'import sys\nsys.path.insert(0,"/opt/lab")\nfrom rescue import Recovery\n'
                'recovery = Recovery(json.loads(Path("/etc/rescue/identity.json").read_text()))\n'
                'deadline = time.monotonic()+6\n'
                'while True:\n'
                ' try:\n'
                '  node = recovery.candidate_node()\n'
                '  if Path(node).exists(): break\n'
                ' except (OSError,RuntimeError,ValueError): pass\n'
                ' if time.monotonic() >= deadline: raise TimeoutError("candidate partition readiness")\n'
                ' time.sleep(.02)\n'
                'Path("/proc/sys/vm/drop_caches").write_text("3\\n")\n'
                'answer = {"node":node,"clean_caches_dropped":True,"observation":observe()}\n')
            # Collector is outside the Guard cgroup and stores a bounded trace.
            report['collector'] = ram_action(folder,'03-start-trace', PROCESS_TRACE + r'''
collector = os.fork()
if collector == 0:
    os.setsid()
    null = os.open('/dev/null',os.O_RDWR)
    for fd in (0,1,2): os.dup2(null,fd)
    with open('/run/admission-process-trace.jsonl','a',buffering=1) as output:
        for _ in range(500):
            output.write(json.dumps(group_processes())+'\n')
            if Path('/run/admission-trace-stop').exists(): break
            time.sleep(.1)
    os._exit(0)
answer = {'pid':collector,'cgroup':str(cg),'owner_pid':tracked_owner}
''')
            gate.hold();report['gate_closed'] = gate.snapshot()
            report['release_hook'] = ram_action(folder,'04-release-hook',
                "Path('/run/lab-fault-release.json').write_text("+repr(json.dumps({'token':token}))+")\nanswer = observe()\n")
            wait_for(lambda:gate.snapshot()['held_bytes']>0,5,'real lower request held')
            report['early'] = snapshot()
            report['early_processes'] = ram_action(folder,'05-early-processes',PROCESS_TRACE+'\nanswer = group_processes()\n')
            report['gate_early'] = gate.snapshot()
            report['shell_while_pending'] = shell_probe(folder/'rescue.sock')
            # The host gate itself has no expiry. Keep it shut beyond both the
            # Guard deadline and kernel no-path backstop before reading state.
            time.sleep(args.queue_seconds+3)
            report['late'] = snapshot()
            report['late_observation'] = ram_action(folder,'06-late-observe',PROCESS_TRACE+'\nanswer = {"control":observe(),"group":group_processes()}\n')
            report['gate_late'] = gate.snapshot()
            encoded_trace = ram_action(folder,'07-read-trace',
                'import base64,zlib\n'
                'Path("/run/admission-trace-stop").touch()\n'
                'time.sleep(.15)\n'
                'answer = base64.b64encode(zlib.compress(Path("/run/admission-process-trace.jsonl").read_bytes())).decode()\n')
            trace_bytes = zlib.decompress(base64.b64decode(encoded_trace))
            (folder/'process-trace.jsonl').write_bytes(trace_bytes)
            report['process_trace_sha256'] = sha256(folder/'process-trace.jsonl')
            report['trace_before_release'] = [json.loads(line) for line in trace_bytes.splitlines() if line]
            gate.release();report['gate_released'] = gate.snapshot()
            def taken_over():
                s = snapshot()
                supervisor = s.get('path_supervisor') or {}
                return s if supervisor.get('state') == 'expired' and supervisor.get('owner_epoch') != report['before']['path_guard']['owner_epoch'] else None
            report['takeover'] = wait_for(taken_over,20,'post-I/O terminal takeover')
            time.sleep(1)
            report['after'] = snapshot()
            report['final_observation'] = ram_action(folder,'08-final-observe',PROCESS_TRACE+'\nanswer = {"control":observe(),"group":group_processes()}\n')
            report['shell_after'] = shell_probe(folder/'rescue.sock')
            late, after = report['late'],report['after']
            traces = report['trace_before_release']
            blocked = [sample for sample in traces if metadata_blocked(sample,report['owner_pid'])]
            report['blocked_samples'] = len(blocked)
            deadline = report['at_hook']['path_transaction']['deadline']
            relevant_pids = set()
            helper_pids = set()
            supervisor_pids = set()
            for sample in traces:
                for process in sample['processes']:
                    if '/opt/lab/path_guard.py' in process['argv'] and '--takeover' in process['argv']:
                        supervisor_pids.add(process['pid'])
                    elif process['pid'] == report['owner_pid'] or '/opt/lab/path_guard.py' in process['argv']:
                        relevant_pids.add(process['pid'])
                    if any(arg in ('/sbin/blkid','/sbin/lvm','/sbin/dmsetup') for arg in process['argv']):
                        helper_pids.add(process['pid'])
            report['observed_owner_pids'] = sorted(relevant_pids)
            report['observed_helper_pids'] = sorted(helper_pids)
            report['observed_supervisor_pids'] = sorted(supervisor_pids)
            heartbeats = [e['host_time'] for e in guest.events if e['message'].get('event') == 'heartbeat']
            report['max_heartbeat_gap'] = max((b-a for a,b in zip(heartbeats,heartbeats[1:])),default=None)
            events = [json.loads(line) for line in after['path_events'].splitlines() if line]
            original = stable_table(report['armed']['active']['stdout'])
            final = report['final_observation']
            checks = report['checks']
            checks.update(real_metadata_io_pending=bool(blocked),
                gate_not_released_by_timeout=not report['gate_late']['gate_open'] and report['gate_late']['held_bytes']>0 and
                    report['gate_late']['forwarded_request_bytes'] == report['gate_early']['forwarded_request_bytes'],
                state_expired_while_io_pending=late['path_guard']['state'] == 'expired',
                journal_expired_while_io_pending=late['path_transaction']['phase'] == 'expired',
                deadline_published_within_one_second=late['path_guard']['time'] <= deadline+1,
                one_owner_throughout=relevant_pids == {report['owner_pid']},
                one_helper_at_most=len(helper_pids)<=1,
                owner_threads_bounded=all(len(p['tasks'])<=2 for s in traces for p in s['processes'] if p['pid'] == report['owner_pid']),
                owner_lock_retained=bool(report['late_observation']['group']['owner_locks']),
                supervisor_did_not_take_ownership_while_pending=(late.get('path_supervisor') or {}).get('state') in (None,'waiting_for_owner') and late['path_transaction']['owner_epoch'] == report['before']['path_guard']['owner_epoch'],
                no_candidate_commit=original == stable_table(report['late_observation']['control']['active']['stdout']) == stable_table(final['control']['active']['stdout']),
                no_late_ready=not any(e.get('state') == 'ready' and e.get('time',0) >= deadline for e in events) and after['path_guard']['state'] == 'expired' and after['path_guard'].get('recoveries',0) == 0,
                terminal_supervisor_finished=after['path_supervisor']['state'] == 'expired',
                old_worker_lock_released=not final['group']['owner_locks'],
                no_inactive_table_left=no_inactive(final['control']),
                no_permanent_dm_suspend=not_suspended(final['control']),
                upper_mappings_unchanged=report['before']['mappings'] == after['mappings'],
                ram_shell=report['shell_while_pending'] and report['shell_after'],
                ram_heartbeat=len(heartbeats)>=3 and report['max_heartbeat_gap']<2)
            if stage == 'before_revalidate':
                checks['inactive_existed_before_stall'] = not no_inactive(report['cache_preparation']['observation'])
            report.update(completed=True,passed=all(checks.values()))
        except Exception as exc:
            report.update(completed=False,error=repr(exc))
        finally:
            gate.release();vm.terminate()
            try: vm.wait(timeout=10)
            except subprocess.TimeoutExpired: vm.kill();vm.wait()
            for channel in channels: channel.close()
            report['gate_final'] = gate.snapshot();gate.close()
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'report':str(folder/'report.json'),'passed':report['passed'],'checks':report['checks'],'error':report.get('error')},indent=2),flush=True)
    return folder/'report.json',report['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path,default=WORK)
    parser.add_argument('--stage',choices=['before_verify','before_revalidate','both'],default='before_verify')
    parser.add_argument('--queue-seconds',type=int,default=12)
    parser.add_argument('--tcg',action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0: parser.error('Run without host root privileges')
    if not 6 <= args.queue_seconds <= 20: parser.error('queue-seconds must be 6..20')
    build = json.loads((args.build_dir/'build.json').read_text())
    for name,key in [('vmlinuz','kernel_sha256'),('initramfs.cpio.gz','initramfs_sha256')]:
        if sha256(args.build_dir/name) != build[key]: parser.error('Build hash mismatch: '+name)
    stages = ['before_verify','before_revalidate'] if args.stage == 'both' else [args.stage]
    reports = [run_case(args,stage) for stage in stages]
    summary = WORK/(time.strftime('%Y%m%d-%H%M%S')+'-admission-stall-'+str(os.getpid())+'.json')
    summary.write_text(json.dumps({'build':build,'passed':all(ok for _,ok in reports),
        'reports':[{'path':str(path),'sha256':sha256(path),'passed':ok} for path,ok in reports]},indent=2)+'\n')
    print('Summary:',summary,flush=True)
    if not all(ok for _,ok in reports): raise SystemExit('Admission I/O stall acceptance failed')


if __name__ == '__main__': main()
