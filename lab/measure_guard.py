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

# Kept separate so parsers and accounting can be tested without booting a guest.
# The same source is sent to the RAM shell; no extra service or dependency is added.
MEMORY_HELPERS = r'''
import os,re,time,json
from pathlib import Path

def byte_fields(text):
    return {key:int(value)*1024 for key,value in
            re.findall(r'^([^:\n]+):\s+(\d+)\s+kB\s*$',text,re.M)}

def key_values(text):
    return {key:int(value) for key,value in
            (line.split() for line in text.splitlines() if line.strip())}

def read_optional(path,errors):
    try:
        return path.read_text()
    except OSError as exc:
        errors.append({'path':str(path),'errno':exc.errno})
        return None

def process_memory(pid,proc_root=Path('/proc')):
    folder=proc_root/str(pid)
    errors=[]
    first=read_optional(folder/'stat',errors)
    status=read_optional(folder/'status',errors)
    rollup=read_optional(folder/'smaps_rollup',errors)
    last=read_optional(folder/'stat',errors)
    # Fields after the final ')' start with state (field 3), not pid/comm.
    def identity(text):
        return text.rsplit(')',1)[1].split()[19] if text else None
    start=identity(first)
    stable=start is not None and start==identity(last)
    return {'pid':pid,'start_ticks':start,'stable_instance':stable,
            'status_bytes':byte_fields(status or '') if stable else {},
            'smaps_rollup_bytes':byte_fields(rollup or '') if stable else {},
            'errors':errors}

def cgroup_memory(cg,proc_root=Path('/proc')):
    errors=[]
    values={}
    for name in ['memory.current','memory.peak','memory.max','memory.high',
                 'memory.low','memory.min','memory.swap.current','memory.swap.peak',
                 'memory.swap.max']:
        text=read_optional(cg/name,errors)
        value=text.strip() if text is not None else None
        values[name]=int(value) if value is not None and value.isdigit() else value
    for name in ['memory.events','memory.events.local','memory.stat','cgroup.events']:
        text=read_optional(cg/name,errors)
        values[name]=key_values(text) if text is not None else None
    # cgroup.procs is local membership; include descendant cgroups explicitly.
    pids=set()
    for file in sorted({cg/'cgroup.procs',*cg.glob('**/cgroup.procs')}):
        text=read_optional(file,errors)
        if text is not None:
            pids.update(int(value) for value in text.split())
    processes=[process_memory(pid,proc_root) for pid in sorted(pids)]
    totals={}
    for key in ['Rss','Pss','Swap','SwapPss','Locked']:
        readings=[p['smaps_rollup_bytes'].get(key) for p in processes]
        totals[key]=sum(readings) if readings and all(v is not None for v in readings) else None
    for key in ['VmLck','VmSwap']:
        readings=[p['status_bytes'].get(key) for p in processes]
        totals[key]=sum(readings) if readings and all(v is not None for v in readings) else None
    return {'files':values,'processes':processes,'process_totals_bytes':totals,
            'process_pids':sorted(pids),'membership_atomic':False,'errors':errors}

def tmpfs_memory(mountinfo_path=Path('/proc/1/mountinfo'),guest_root=Path('/proc/1/root')):
    errors=[]
    text=read_optional(mountinfo_path,errors)
    rows=[]
    seen={}
    for line in (text or '').splitlines():
        left,right=line.split(' - ',1)
        fields=left.split();filesystem,source,super_options=right.split()[:3]
        mountpoint=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),fields[4])
        if filesystem!='tmpfs' or mountpoint not in {'/run','/run/rescue'}:
            continue
        path=guest_root/mountpoint.lstrip('/')
        try:
            info=os.statvfs(path)
            device=os.stat(path).st_dev
        except OSError as exc:
            errors.append({'path':str(path),'errno':exc.errno})
            continue
        options=sorted(set(fields[5].split(',')+super_options.split(',')))
        rows.append({'mountpoint':mountpoint,'filesystem':filesystem,'source':source,
                     'device_number':device,'duplicate_of':seen.get(device),
                     'options':options,'noswap_option_present':'noswap' in options,
                     'capacity_bytes':info.f_blocks*info.f_frsize,
                     'used_bytes':(info.f_blocks-info.f_bfree)*info.f_frsize,
                     'available_bytes':info.f_bavail*info.f_frsize})
        seen.setdefault(device,mountpoint)
    return {'mounts':rows,'errors':errors}

def memory_snapshot(cg):
    start=time.monotonic()
    errors=[]
    meminfo=read_optional(Path('/proc/meminfo'),errors)
    swaps=read_optional(Path('/proc/swaps'),errors)
    result={'monotonic':start,'guard_cgroup':cgroup_memory(cg),
            'tmpfs':tmpfs_memory(),'guest_meminfo_bytes':byte_fields(meminfo or ''),
            'guest_swaps':swaps,'errors':errors}
    result['sampling_seconds']=time.monotonic()-start
    return result
'''

# Runs inside the RAM shell; sampler itself is outside lab-guard.service.
SAMPLE = MEMORY_HELPERS + r'''
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
    memory_before=memory_snapshot(cg)
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
    memory_after=memory_snapshot(cg)
    phases.append({'name':name,'seconds':last-t0,'mean_cpu_percent':(old['usage_usec']-before['usage_usec'])/(last-t0)/10000,'peak_cpu_percent':max(s['cpu_percent'] for s in samples),'p95_cpu_percent':sorted(s['cpu_percent'] for s in samples)[int(.95*len(samples))-1],'p99_cpu_percent':sorted(s['cpu_percent'] for s in samples)[int(.99*len(samples))-1],'peak_thread_count':max(p0['thread_count'],end['thread_count'],*(s['thread_count'] for s in samples)),'probing_events_before':probes0,'probing_events_after':probing_events(),'cpu_stat_before':before,'cpu_stat_after':old,'proc_before':p0,'proc_after':end,'memory_before':memory_before,'memory_after':memory_after,'samples':samples})
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
            report['memory_scope']={
                'sampling':'Phase boundaries only, outside CPU windows; not a sampled memory-peak test.',
                'processes':'All observed Guard cgroup processes and descendant cgroups; membership and /proc reads are not atomic. Vanished or replaced processes remain unknown, not zero.',
                'rss':'RSS sums can count shared pages more than once. PSS divides mapped shared pages among processes.',
                'cgroup':'memory.current/peak include charged memory beyond process mappings; memory.peak is the cgroup lifetime peak, not this phase alone. memory.stat contains overlapping categories.',
                'tmpfs':'statvfs allocated usage and capacity for /run/rescue and /run; contents are shared by Guard, rescue shell, agent and sampler. This is not Guard-exclusive memory.',
                'overlap':'Do not add process PSS, cgroup usage and tmpfs usage: mapped/charged pages overlap. No unprotected baseline was measured, so total incremental protection cost is unknown.',
                'swapping':'tmpfs noswap protects file pages, not Python anonymous heap or stacks. Guard memory.swap.max=0 separately prevents anonymous swapping for the group and descendants; memory.max=128MiB caps charged memory. Neither is mlock or a pressure-survival guarantee, and not all tmpfs pages preloaded by other cgroups are charged here.',
                'limits':'No memory-pressure, OOM, long disconnection, full fault-domain budget or independent rescue scheduling validation in this healthy run.',
            }
            report['checks']={
                'ready':report['guard_state']['state']=='ready',
                'no_recoveries':report['guard_state']['recoveries']==0,
                'same_process':all(p['proc_before']['start_ticks']==p['proc_after']['start_ticks'] for p in report['phases']),
                'no_probe_events':all(p['probing_events_before']==p['probing_events_after']==0 for p in report['phases']),
                'single_thread_observed':all(p['peak_thread_count']==1 for p in report['phases']),
                'no_child_processes_observed':all(not s['child_pids'] for p in report['phases'] for s in [p['proc_before'],*p['samples'],p['proc_after']]),
                'no_child_cpu':all(p['proc_before']['child_ticks']==p['proc_after']['child_ticks'] for p in report['phases']),
            }
            memory=[p[stage]['guard_cgroup'] for p in report['phases'] for stage in ['memory_before','memory_after']]
            report['memory_checks']={
                'swap_disabled_for_guard_cgroup':all(m['files']['memory.swap.max']==0 for m in memory),
                'charged_memory_limit_128mib':all(m['files']['memory.max']==128*1024**2 for m in memory),
                'guard_process_observed':all(report['pid'] in m['process_pids'] for m in memory),
                'pss_rss_swap_observed':all(all(m['process_totals_bytes'][k] is not None for k in ['Pss','Rss','Swap','SwapPss']) for m in memory),
            }
            report['passed']=all(report['checks'].values()) and all(report['memory_checks'].values())
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({**{k:v for k,v in report.items() if k not in ['phases','build']},'phases':[{k:v for k,v in p.items() if k!='samples'} for p in report['phases']]},indent=2))
            if not report['passed']:
                raise RuntimeError('CPU or memory observation invariants failed')
        finally:
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill();vm.wait()


if __name__=='__main__':main()
