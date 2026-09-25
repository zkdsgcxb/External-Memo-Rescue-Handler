#!/usr/bin/env python3
"""Measure the Guard cgroup in a disposable Ubuntu VM, never host services."""
import base64
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

from run import qemu_command, WORK

# Runs inside the RAM shell; sampler itself is outside lab-guard.service.
SAMPLE = r'''
import os,time,json
from pathlib import Path
pid=int(Path('/run/path-guard.pid').read_text())
relative=Path('/proc/%s/cgroup'%pid).read_text().strip().split('0::',1)[1]
cg=Path('/proc/1/root/sys/fs/cgroup')/relative.lstrip('/')
assert cg.joinpath('cpu.max').read_text().strip()=='4000 20000'
def stat():
    return {k:int(v) for k,v in (line.split() for line in (cg/'cpu.stat').read_text().splitlines())}
def tasks():
    threads=list(Path('/proc/%s/task'%pid).iterdir())
    children=set()
    for thread in threads:
        try:
            children.update(int(child) for child in (thread/'children').read_text().split())
        except FileNotFoundError:
            pass  # A task ending during observation still counts in this sample.
    return {'thread_count':len(threads),'child_pids':sorted(children)}
def proc():
    s=Path('/proc/%s/stat'%pid).read_text().split()
    return {'self_ticks':int(s[13])+int(s[14]),'child_ticks':int(s[15])+int(s[16]),'start_ticks':s[21],'rss_bytes':int(s[23])*os.sysconf('SC_PAGE_SIZE'),**tasks()}
def probing_events():
    return sum(json.loads(line).get('state')=='probing' for line in Path('/run/path-events.jsonl').read_text().splitlines() if line.strip())
phases=[]
for name,count in [('idle',300),('events_100_per_second',100)]:
    before=stat();p0=proc();probes0=probing_events();old=before;t0=time.monotonic();last=t0;samples=[]
    for i in range(count):
        if name!='idle':
            for _ in range(10):
                Path('/sys/class/block/dm-0/uevent').write_text('change\n')
        time.sleep(.1)
        new=stat();now=time.monotonic()
        samples.append({'seconds':now-last,'cpu_percent':(new['usage_usec']-old['usage_usec'])/(now-last)/10000,**tasks()})
        last=now;old=new
    end=proc()
    phases.append({'name':name,'seconds':last-t0,'mean_cpu_percent':(old['usage_usec']-before['usage_usec'])/(last-t0)/10000,'peak_cpu_percent':max(s['cpu_percent'] for s in samples),'p95_cpu_percent':sorted(s['cpu_percent'] for s in samples)[int(.95*len(samples))-1],'peak_thread_count':max(p0['thread_count'],end['thread_count'],*(s['thread_count'] for s in samples)),'probing_events_before':probes0,'probing_events_after':probing_events(),'cpu_stat_before':before,'cpu_stat_after':old,'proc_before':p0,'proc_after':end,'samples':samples})
print('CPU_RESULT='+json.dumps({'pid':pid,'cgroup':str(cg),'cpu_max':(cg/'cpu.max').read_text().strip(),'phases':phases,'guard_state':json.loads(Path('/run/path-state.json').read_text())}),flush=True)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path,default=WORK,help='Select an isolated kernel/initramfs/build.json')
    args=parser.parse_args()
    if os.geteuid()==0:
        parser.error('run as a normal user')
    for name in ['vmlinuz','initramfs.cpio.gz','build.json']:
        if not (args.build_dir/name).is_file():
            parser.error('build the guest first')
    folder=WORK/(time.strftime('%Y%m%d-%H%M%S')+'-guard-budget')
    folder.mkdir(mode=0o700)
    (folder/'sampler.py').write_text(SAMPLE)
    for name in ['usb.raw','decoy.raw']:
        with (folder/name).open('xb') as stream:
            stream.truncate(8*1024**3)
    command=qemu_command(folder,extra_kernel_args='ram_rescue_mpath=1 ram_rescue_queue_seconds=8 ram_rescue_ubuntu=1 root=/dev/mapper/labrescue-ubuntu rw',ubuntu=True,
        kernel=args.build_dir/'vmlinuz',initramfs=args.build_dir/'initramfs.cpio.gz')
    (folder/'command.json').write_text(json.dumps(command))
    print(folder,flush=True)
    with (folder/'qemu.log').open('w') as log:
        vm=subprocess.Popen(command,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+360
            while time.monotonic()<deadline:
                console=folder/'console.log'
                if console.exists() and 'LAB_ROOT_READY:' in console.read_text(errors='replace'):
                    break
                if vm.poll() is not None:
                    raise RuntimeError('VM exited')
                time.sleep(1)
            else:
                raise TimeoutError('Ubuntu boot')
            time.sleep(15)
            encoded=base64.b64encode(SAMPLE.encode()).decode()
            command="/usr/bin/python3 -c \"import base64;exec(base64.b64decode(open('/run/cpu-sample.b64','rb').read()))\"\n"
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
                sock.settimeout(60);sock.connect(str(folder/'rescue.sock'))
                sock.sendall(b'stty -echo\n');time.sleep(.3)
                sock.sendall(b': > /run/cpu-sample.b64\n');time.sleep(.1)
                # Short shell lines avoid both UART overrun and shell line limits.
                for i in range(0,len(encoded),128):
                    sock.sendall(("printf '%s' '"+encoded[i:i+128]+"' >> /run/cpu-sample.b64\n").encode())
                    time.sleep(.1)
                sock.sendall(command.encode())
                output=b''
                while True:
                    chunk=sock.recv(65536)
                    if not chunk:
                        raise RuntimeError('RAM shell closed')
                    output+=chunk
                    (folder/'measurement.log').write_bytes(output)
                    if b'CPU_RESULT=' in output and b'\n' in output.split(b'CPU_RESULT=',1)[1]:
                        break
            report=json.loads(output.split(b'CPU_RESULT=',1)[1].split(b'\n',1)[0])
            report.update(run=folder.name,build=json.loads((args.build_dir/'build.json').read_text()),scope='100 percent = one vCPU; cgroup includes all guard children; sampler is outside cgroup; nominal 100 ms CPU/task windows; short unsampled tasks are not excluded; probing events and child CPU ticks are cumulative; no fault injected')
            report['runner_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            report['sampler_sha256']=hashlib.sha256(SAMPLE.encode()).hexdigest()
            report['checks']={
                'ready':report['guard_state']['state']=='ready',
                'no_recoveries':report['guard_state']['recoveries']==0,
                'same_process':all(p['proc_before']['start_ticks']==p['proc_after']['start_ticks'] for p in report['phases']),
                'no_probe_events':all(p['probing_events_before']==p['probing_events_after']==0 for p in report['phases']),
                'single_thread_observed':all(p['peak_thread_count']==1 for p in report['phases']),
                'no_child_processes_observed':all(not s['child_pids'] for p in report['phases'] for s in [p['proc_before'],*p['samples'],p['proc_after']]),
                'no_child_cpu':all(p['proc_before']['child_ticks']==p['proc_after']['child_ticks'] for p in report['phases']),
            }
            report['passed']=all(report['checks'].values())
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({**{k:v for k,v in report.items() if k not in ['phases','build']},'phases':[{k:v for k,v in p.items() if k!='samples'} for p in report['phases']]},indent=2))
            if not report['passed']:
                raise RuntimeError('CPU observation invariants failed')
        finally:
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill();vm.wait()


if __name__=='__main__':main()
