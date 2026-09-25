#!/usr/bin/env python3
"""Unannounced USB loss with a preinstalled single-path multipath queue, VM only."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time
import threading

from run import WORK, Channel, qemu_command, shell_probe


def wait_for(test, seconds, description):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=test()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(description)


def result(response):
    if not response.get('ok'):
        raise RuntimeError(response)
    return response['result']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workload', choices=['fsync','git-clone'], default='fsync')
    parser.add_argument('--guest', choices=['minimal','ubuntu'], default='minimal')
    parser.add_argument('--transport',choices=['uas','bot'],default='uas')
    parser.add_argument('--gap',type=float,default=0.2)
    parser.add_argument('--same-port',action='store_true',help='Reuse virtual USB port 1 on every reconnect')
    parser.add_argument('--queue-seconds',type=int,default=8)
    parser.add_argument('--cycles',type=int,default=1)
    parser.add_argument('--reconnect',choices=['same','none','wrong','late'],default='same')
    parser.add_argument('--tcg',action='store_true')
    parser.add_argument('--kill-manager',action='store_true',help='Test kernel queue timeout with userspace guard killed')
    parser.add_argument('--build-dir',type=Path,default=WORK,help='Select an isolated kernel/initramfs/build.json')
    args=parser.parse_args()
    if os.geteuid()==0:
        parser.error('run as a normal user')
    if not 0<=args.gap<=60 or not 2<=args.queue_seconds<=60 or not 0<=args.cycles<=10:
        parser.error('gap: 0..60; queue-seconds: 2..60; cycles: 0..10')
    if args.reconnect!='same' and args.cycles!=1:
        parser.error('negative tests require one cycle')
    if args.reconnect=='late' and args.gap<args.queue_seconds+2:
        parser.error('late reconnection requires gap >= queue-seconds + 2')
    if args.kill_manager and args.reconnect!='none':
        parser.error('--kill-manager requires --reconnect none')
    for name in ['vmlinuz','initramfs.cpio.gz','build.json']:
        if not (args.build_dir/name).is_file():
            parser.error('build the guest first')
    git_clone=args.workload=='git-clone'
    if git_clone and (args.guest!='ubuntu' or args.reconnect!='same'):
        parser.error('Git clone requires --guest ubuntu and --reconnect same')
    git_source=None
    if git_clone:
        from fetch_ubuntu import sha256
        source=WORK/'git-source.json'
        image=WORK/'git-source.raw'
        if not source.is_file() or not image.is_file():
            parser.error('Run lab/prepare_git.py first')
        git_source=json.loads(source.read_text())
        if sha256(image)!=git_source['image_sha256']:
            parser.error('Git source disk hash mismatch')
    ubuntu=args.guest=='ubuntu'
    ubuntu_source=None
    if ubuntu:
        from fetch_ubuntu import sha256
        metadata=WORK/'ubuntu/source.json'
        seed=WORK/'ubuntu/rootfs.raw'
        if not metadata.is_file() or not seed.is_file():
            parser.error('Run lab/fetch_ubuntu.py first')
        ubuntu_source=json.loads(metadata.read_text())
        if sha256(seed)!=ubuntu_source['seed_sha256']:
            parser.error('Ubuntu seed hash differs; fetch it again')
    folder=WORK/(time.strftime('%Y%m%d-%H%M%S')+'-auto-'+str(os.getpid()))
    folder.mkdir(mode=0o700)
    for name in ['usb.raw','decoy.raw']:
        with (folder/name).open('xb') as stream:
            stream.truncate((8 if ubuntu else 2)*1024**3)
    command=qemu_command(folder,args.transport,args.tcg,
        f'ram_rescue_mpath=1 ram_rescue_queue_seconds={args.queue_seconds}' +
        (' ram_rescue_ubuntu=1 root=/dev/mapper/labrescue-ubuntu rw' if ubuntu else '') + (' ram_rescue_git=1' if git_clone else ''), ubuntu=ubuntu, git_source=git_clone, same_port=args.same_port,
        kernel=args.build_dir/'vmlinuz',initramfs=args.build_dir/'initramfs.cpio.gz')
    (folder/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    report={'workload':args.workload, 'git_source':git_source, 'guest':args.guest, 'ubuntu_source':ubuntu_source, 'mode':'automatic-multipath','transport':args.transport,'gap':args.gap,
        'queue_seconds':args.queue_seconds,'reconnect':args.reconnect,
        'same_port':args.same_port,
        'kill_manager':args.kill_manager,
        'build':json.loads((args.build_dir/'build.json').read_text()),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'qemu_runner_sha256':hashlib.sha256((Path(__file__).parent/'run.py').read_bytes()).hexdigest(),
        'cycles':[]}
    print('Automatic-path experiment:',folder,flush=True)
    with (folder/'qemu.log').open('w') as stderr, (folder/'qmp.jsonl').open('w') as qlog, (folder/'agent.jsonl').open('w') as alog:
        vm=subprocess.Popen(command,stdout=stderr,stderr=subprocess.STDOUT)
        channels=[]
        try:
            wait_for(lambda:(folder/'qmp.sock').exists(),10,'QMP socket')
            qmp=Channel(folder/'qmp.sock',qlog,qmp=True); channels.append(qmp)
            def booted():
                text=(folder/'console.log').read_text(errors='replace')
                if 'Kernel panic' in text or vm.poll() is not None:
                    raise RuntimeError('Guest boot failed; see console.log')
                return 'LAB_ROOT_READY:' in text
            wait_for(booted,360 if ubuntu else 120,'guest boot')
            guest=Channel(folder/'agent.sock',alog);channels.append(guest)
            wait_for(lambda:any(e['message'].get('event')=='heartbeat' for e in guest.events),10,'heartbeat')
            def snapshot():
                return result(guest.call('snapshot'))
            def ready():
                snap=snapshot()
                return snap if (snap.get('path_guard') or {}).get('state')=='ready' else None
            report['before']=wait_for(ready,10,'RAM path manager ready')
            assert any(line.split()[4]=='/' and ' - ext4 ' in line for line in report['before']['pid1_mounts'].splitlines())
            assert report['before']['path_guard']['state']=='ready'
            assert shell_probe(folder/'rescue.sock')
            report['verify_before']=result(guest.call('verify'))
            if args.reconnect=='wrong':
                qmp.call('device_add',driver='usb-storage',bus='xhci.0',id='imposter',drive='decoydisk',serial='LAB-IMPOSTER')
                def prepare():
                    response=guest.call('prepare_wrong_disk')
                    return response if response['ok'] else None
                report['imposter_prepared']=wait_for(prepare,10,'impostor disk initialization')
                started=time.monotonic()
                qmp.call('device_del',id='imposter')
                wait_for(lambda:any(e['host_time']>=started and e['message'].get('event')=='DEVICE_DELETED' and
                    e['message'].get('data',{}).get('device')=='imposter' for e in qmp.events),10,'impostor removal')
            if ubuntu:
                def ubuntu_boot_ready():
                    state=result(guest.call('ubuntu_status'))
                    report['ubuntu_boot_observed']=state
                    return state if state['multi_user']=='active' else None
                wait_for(ubuntu_boot_ready,180,'Ubuntu multi-user target')
            result(guest.call('workload'))
            time.sleep(1)
            if ubuntu:
                def ubuntu_ready():
                    state=result(guest.call('ubuntu_status'))
                    return state if state['multi_user']=='active' and all(s['ActiveState']=='active' for s in state['services'].values()) else None
                report['ubuntu_before']=wait_for(ubuntu_ready,60,'Ubuntu multi-user and services')
            report['pre_fault']=snapshot()
            if git_clone:
                result(guest.call('git_clone_start'))
                def cloning():
                    state=result(guest.call('git_status'))
                    report['git_last_observed']=state
                    if state.get('exit_code') is not None:
                        report['git_failure_diagnostics']=guest.call('git_diagnostics')
                        raise RuntimeError('Git exited before fault injection: '+str(state))
                    progress=state.get('progress_tail','')
                    started=any(int(p)>0 for p in re.findall(r'(?:Receiving objects|Updating files):\s+(\d+)%',progress))
                    return state if started and state.get('same_process_alive') else None
                report['git_before']=wait_for(cloning,180,'Git clone actively receiving/checking out')
            for cycle in range(args.cycles):
                entry={'index':cycle+1,'fault_host_time':time.monotonic()}
                report['cycles'].append(entry)
                if git_clone:
                    entry['git_at_fault']=result(guest.call('git_status'))
                    if not entry['git_at_fault'].get('same_process_alive') or entry['git_at_fault']['exit_code'] is not None:
                        raise RuntimeError('Git already exited; fault would not exercise cloning')
                # No guest command announces the fault or suspends any LV.
                qmp.call('device_del',id='stick')
                wait_for(lambda:any(e['host_time']>=entry['fault_host_time'] and
                    e['message'].get('event')=='DEVICE_DELETED' and e['message'].get('data',{}).get('device')=='stick'
                    for e in qmp.events),10,'device removal')
                entry['deleted_host_time']=time.monotonic()
                reconnect_errors=[]
                def timed_reconnect():
                    time.sleep(max(0,args.gap-(time.monotonic()-entry['deleted_host_time'])))
                    try:
                        entry['reattach_request_host_time']=time.monotonic()
                        backend='usbdisk' if args.reconnect in ['same','late'] else 'decoydisk'
                        port={'port':'1'} if args.same_port else {}
                        if args.transport=='uas':
                            qmp.call('device_add',driver='usb-uas',bus='xhci.0',id='stick',serial='RAMRESCUE-LAB-001',attached=False,**port)
                            qmp.call('device_add',driver='scsi-hd',bus='stick.0',id='lun',drive=backend)
                            qmp.call('qom-set',path='/machine/peripheral/stick',property='attached',value=True)
                        else:
                            qmp.call('device_add',driver='usb-storage',bus='xhci.0',id='stick',drive=backend,serial='RAMRESCUE-LAB-001',**port)
                        entry['reattached_host_time']=time.monotonic()
                    except Exception as exc:
                        reconnect_errors.append(exc)
                reconnect_thread=None
                if args.reconnect!='none':
                    reconnect_thread=threading.Thread(target=timed_reconnect,daemon=True)
                    reconnect_thread.start()
                # Observe RAM/shell while the real root is waiting on queued I/O.
                entry['observation_started_host_time']=time.monotonic()
                entry['shell_while_absent']=shell_probe(folder/'rescue.sock')
                entry['shell_observed_host_time']=time.monotonic()
                entry['absent']=snapshot()
                if git_clone:
                    entry['git_while_absent']=result(guest.call('git_status'))
                if args.kill_manager:
                    report['kill_manager_response']=result(guest.call('kill_path_guard'))
                if reconnect_thread:
                    reconnect_thread.join(timeout=args.gap+35)
                    if reconnect_thread.is_alive():
                        raise TimeoutError('Timed reconnect did not finish')
                    if reconnect_errors:
                        raise reconnect_errors[0]
                entry['shell_completed_before_reattach']=entry['shell_observed_host_time']<entry.get('reattach_request_host_time',float('inf'))
                def finished():
                    snap=snapshot()
                    if args.kill_manager:
                        return snap if any(not json.loads(line)['ok'] for line in snap['workload'].splitlines()) else None
                    state=snap['path_guard']
                    if state['state']=='expired' or state['state']=='ready' and state['recoveries']>=cycle+1:
                        return snap
                    return None
                entry['outcome']=wait_for(finished,args.queue_seconds+15,'automatic path outcome')
                entry['outcome_host_time']=time.monotonic()
                if entry['outcome']['path_guard']['state']=='expired':
                    break
                time.sleep(1)
            if git_clone:
                def git_done():
                    state=result(guest.call('git_status'))
                    return state if state.get('exit_code') is not None else None
                report['git_after']=wait_for(git_done,600,'Git clone completion')
                report['git_validation']=result(guest.call('git_validate',timeout=240))
            time.sleep(2)
            report['after']=snapshot()
            report['shell_after']=shell_probe(folder/'rescue.sock')
            if args.reconnect=='same' and report['after']['path_guard']['state']=='ready':
                report['block_after']=guest.call('block_probe')
                report['root_after']=guest.call('probe')
                report['filesystem_after']=guest.call('filesystem_state')
                report['data_audit']=guest.call('audit_workload')
                if ubuntu:
                    report['ubuntu_after']=result(guest.call('ubuntu_status'))
            heartbeats=[e['host_time'] for e in guest.events if e['message'].get('event')=='heartbeat']
            report['max_heartbeat_gap']=max((b-a for a,b in zip(heartbeats,heartbeats[1:])),default=None)
            before=report['pre_fault'];after=report['after']
            writes=[json.loads(line) for line in after['workload'].splitlines()]
            report['successful_writes']=sum(w['ok'] for w in writes)
            report['failed_writes']=sum(not w['ok'] for w in writes)
            report['max_write_latency']=max((w['elapsed'] for w in writes),default=None)
            checks={'ram_heartbeat':len(heartbeats)>=3 and report['max_heartbeat_gap']<2,
                'ram_shell':report['shell_after'] and all(e['shell_while_absent'] for e in report['cycles']),
                'stable_upper_mappings':before['mappings']==after['mappings'],
                'same_process_alive':before['workload_process']==after['workload_process'] and after['workload_process']['exit_code'] is None}
            if args.reconnect=='same':
                checks.update(automatic_recovery=after['path_guard']['state']=='ready' and after['path_guard']['recoveries']==args.cycles,
                    zero_application_errors=report['failed_writes']==0,
                    writes_continued=len(writes)>len(before['workload'].splitlines()),
                    block_read=report.get('block_after',{}).get('ok',False) and
                        report['block_after'].get('result',{}).get('bytes')==4096 and
                        report['block_after'].get('result',{}).get('ext4_magic')=='53ef',
                    root_read=report.get('root_after',{}).get('ok',False),
                    filesystem_writable=report.get('filesystem_after',{}).get('result',{}).get('write_fsync_ok',False),
                    no_journal_abort='Aborting journal' not in after['kernel'])
                if ubuntu:
                    ub=report['ubuntu_before'];ua=report.get('ubuntu_after',{})
                    checks['ubuntu_systemd']=ua.get('pid1_comm')=='systemd' and 'Ubuntu' in ua.get('os_release','') and ua.get('installed_packages',0)>100
                    checks['same_boot']=ua.get('boot_id')==ub['boot_id'] and ua.get('pid1_start_ticks')==ub['pid1_start_ticks']
                    checks['services_survived']=ua.get('services')==ub['services']
                    checks['no_failed_units']=not ub['failed_units'].strip() and not ua.get('failed_units','missing').strip()
                checks['acknowledged_data_present']=report.get('data_audit',{}).get('result',{}).get('prefix_matches',False)
                checks['post_swap_confirmation']=all(
                    e['outcome']['path_guard'].get('confirmation') in
                        ('kernel-probe-and-state','state-only-unsupported') and
                    e['outcome']['path_guard'].get('kernel_probe',{}).get('token')==e['index']
                    for e in report['cycles'])
            else:
                checks.update(deadline_released_queue=(args.kill_manager or after['path_guard']['state']=='expired') and report['failed_writes']>0,
                    no_recovery_after_failure=after['path_guard']['recoveries']==0,
                    bounded_wait=report['max_write_latency']<args.queue_seconds+10)
                if args.reconnect=='wrong':
                    checks['wrong_pv_rejected']='UUID does not match' in after['path_events']
            if git_clone:
                before_git=report['git_before'];after_git=report['git_after']
                checks['git_clone_succeeded']=after_git['exit_code']==0
                checks['same_git_process']=all(before_git[k]==after_git[k] for k in ['pid','start_ticks'])
                checks['git_active_during_fault']=all(e['git_while_absent'].get('same_process_alive',False) for e in report['cycles'])
                checks['git_integrity']=all(report['git_validation'][k] for k in ['head_matches','fsck_passed','worktree_clean'])
            report.update(completed=True,checks=checks,passed=all(checks.values()))
        except BaseException as exc:
            report.update(completed=False,error=repr(exc))
            raise
        finally:
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            vm.terminate()
            try: vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill();vm.wait()
            for channel in channels: channel.close()
    print(json.dumps({k:report.get(k) for k in ['passed','checks','successful_writes','failed_writes','max_write_latency','max_heartbeat_gap']},indent=2))
    print('Report:',folder/'report.json')
    if not report['passed']:
        raise SystemExit('Automatic-path acceptance failed')


if __name__=='__main__':
    main()
