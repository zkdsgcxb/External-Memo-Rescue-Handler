#!/usr/bin/env python3
"""Disposable VM probes for limits of path status and no-path timeouts."""
import argparse
import base64
import hashlib
import json
import socket
import subprocess
import time
from pathlib import Path

from run import WORK, Channel, qemu_command, shell_probe
from auto_run import wait_for, result


def ram_python(folder,code,timeout=15):
    encoded=base64.b64encode(code.encode()).decode()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout);sock.connect(str(folder/'rescue.sock'))
        sock.sendall(b'stty -echo\n');time.sleep(.1)
        sock.sendall(b': > /run/research.b64\n');time.sleep(.05)
        for i in range(0,len(encoded),128):
            sock.sendall(("printf '%s' '"+encoded[i:i+128]+"' >> /run/research.b64\n").encode());time.sleep(.03)
        sock.sendall(b"python3 -c \"import base64;exec(base64.b64decode(open('/run/research.b64','rb').read()))\"\n")
        output=b''
        while True:
            chunk=sock.recv(65536)
            if not chunk:raise RuntimeError('RAM shell closed')
            output+=chunk
            if b'RESEARCH_RESULT=' in output and b'\n' in output.split(b'RESEARCH_RESULT=',1)[1]:
                break
        return json.loads(output.split(b'RESEARCH_RESULT=',1)[1].split(b'\n',1)[0])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario',choices=['slow-backend','suspended-manager-death'])
    args=parser.parse_args()
    folder=WORK/(time.strftime('%Y%m%d-%H%M%S')+'-probe-'+('stall' if args.scenario=='slow-backend' else 'suspend'))
    folder.mkdir(mode=0o700)
    for name in ['usb.raw','decoy.raw']:
        with (folder/name).open('xb') as f:f.truncate(2*1024**3)
    command=qemu_command(folder,extra_kernel_args='ram_rescue_mpath=1 ram_rescue_queue_seconds=4',same_port=True)
    report={'scenario':args.scenario,'build':json.loads((WORK/'build.json').read_text()),'command':command,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    print(folder,flush=True)
    channels=[]
    with (folder/'qemu.log').open('w') as log,(folder/'qmp.jsonl').open('w') as qlog,(folder/'agent.jsonl').open('w') as alog:
        vm=subprocess.Popen(command,stdout=log,stderr=log)
        try:
            wait_for(lambda:(folder/'qmp.sock').exists(),10,'QMP')
            qmp=Channel(folder/'qmp.sock',qlog,qmp=True);channels.append(qmp)
            wait_for(lambda:'LAB_ROOT_READY:' in (folder/'console.log').read_text(errors='replace'),120,'boot')
            guest=Channel(folder/'agent.sock',alog);channels.append(guest)
            def snapshot():return result(guest.call('snapshot'))
            report['before']=wait_for(lambda:s if ((s:=snapshot())['path_guard'] or {}).get('state')=='ready' else None,10,'guard')
            result(guest.call('workload'));time.sleep(1)
            report['pre_fault']=snapshot()
            report['fault_host_time']=time.monotonic()
            if args.scenario=='slow-backend':
                report['injection']=qmp.call('block_set_io_throttle',id='lun',bps=1,bps_rd=0,bps_wr=0,iops=0,iops_rd=0,iops_wr=0)
                report['injection_kind']='QEMU backend throttled to 1 byte/s; not a physical USB unplug or exact driver hang'
                time.sleep(2);report['stalled_early']=snapshot()
                time.sleep(10);report['stalled_late']=snapshot()
                report['shell_stalled']=shell_probe(folder/'rescue.sock')
                report['stall_host_seconds']=time.monotonic()-report['fault_host_time']
                report['release']=qmp.call('block_set_io_throttle',id='lun',bps=0,bps_rd=0,bps_wr=0,iops=0,iops_rd=0,iops_wr=0)
                wait_for(lambda:len((s:=snapshot())['workload'].splitlines())>len(report['stalled_late']['workload'].splitlines()),15,'writes resume')
                time.sleep(1);report['after']=snapshot()
                writes=[json.loads(l) for l in report['after']['workload'].splitlines()]
                report['checks']={'stall_exceeded_kernel_nopath_timeout':report['stall_host_seconds']>6,
                    'no_completions_during_stall':report['stalled_early']['workload']==report['stalled_late']['workload'],
                    'guard_still_ready':report['stalled_late']['path_guard']['state']=='ready' and report['stalled_late']['path_guard']['recoveries']==0,
                    'dm_path_still_active':' F ' not in report['stalled_late']['dm_status'] and ' A ' in report['stalled_late']['dm_status'],
                    'same_application_process':report['pre_fault']['workload_process']==report['after']['workload_process'],
                    'writes_resumed_without_errors':all(w['ok'] for w in writes) and len(writes)>len(report['stalled_late']['workload'].splitlines()),
                    'ram_shell':report['shell_stalled']}
                report['max_write_latency']=max(w['elapsed'] for w in writes)
            else:
                # STOP prevents normal recovery while reproducing the exact DM suspended postcondition.
                report['stop_guard']=ram_python(folder,"import os,signal,json;from pathlib import Path;os.kill(int(Path('/run/path-guard.pid').read_text()),signal.SIGSTOP);print('RESEARCH_RESULT='+json.dumps({'stopped':True}))")
                qmp.call('device_del',id='stick')
                wait_for(lambda:' F ' in snapshot()['dm_status'],10,'failed DM path')
                report['suspend_and_kill']=ram_python(folder,"import os,signal,json,subprocess;from pathlib import Path;subprocess.run(['/sbin/dmsetup','--noudevsync','suspend','--noflush','--nolockfs','lab-path'],check=True);os.kill(int(Path('/run/path-guard.pid').read_text()),signal.SIGKILL);print('RESEARCH_RESULT='+json.dumps({'suspended_then_killed':True}))")
                report['suspended_early']=snapshot();time.sleep(9);report['suspended_late']=snapshot()
                report['shell_stalled']=shell_probe(folder/'rescue.sock')
                report['dm_suspended']=ram_python(folder,"import subprocess,json;print('RESEARCH_RESULT='+json.dumps({'suspended':subprocess.check_output(['/sbin/dmsetup','info','-c','--noheadings','-o','suspended','lab-path'],text=True).strip()}))")
                report['checks']={'still_suspended_beyond_kernel_timeout':report['dm_suspended']['suspended']=='Suspended',
                    'no_completions_during_suspend':report['suspended_early']['workload']==report['suspended_late']['workload'],
                    'ram_shell':report['shell_stalled']}
                report['injection_kind']='Guest-side fault schedule reproduces manager death after noflush suspend; not randomized crash inside Guard'
            report['passed']=all(report['checks'].values())
        except BaseException as exc:
            report['error']=repr(exc);raise
        finally:
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            vm.terminate()
            try:vm.wait(timeout=10)
            except subprocess.TimeoutExpired:vm.kill();vm.wait()
            for channel in channels:channel.close()
    print(json.dumps({k:report[k] for k in ['scenario','passed','checks']},indent=2),flush=True)
    if not report['passed']:raise SystemExit('Probe observation failed')


if __name__=='__main__':main()
