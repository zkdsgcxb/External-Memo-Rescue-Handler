#!/usr/bin/env python3
"""Disposable stock-multipathd study. Host uses only regular files; no host block operations."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lab'))
from run import Channel

# Share the exact baseline workload and sparse-disk oracle with the old-version study.
from multipathd_probe import INIT, GUEST, inspect_sparse_records


def copy_staged_runtime(payload, stage):
    """Resolve newly built private libraries without copying build paths into guest."""
    env=os.environ.copy()
    env['LD_LIBRARY_PATH']=str(stage/'lib')
    sources=[stage/'usr/sbin'/name for name in ['multipathd','multipath','kpartx']]
    sources += list((stage/'lib').glob('*.so*'))
    sources += [stage/'lib/multipath/libchecktur.so',stage/'lib/multipath/libprioconst.so']
    for source in sources:
        payload.copy_file(source,'/'+str(source.relative_to(stage)))
        result=subprocess.run(['ldd',str(source)],capture_output=True,text=True,env=env)
        if result.returncode or 'not found' in result.stdout+result.stderr:
            raise RuntimeError(result.stdout+result.stderr)
        for dep in re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)',result.stdout,re.M):
            path=Path(dep)
            dest='/'+str(path.relative_to(stage)) if path.is_relative_to(stage) else dep
            payload.copy_file(path,dest)


def guest_program(modern):
    guest=GUEST.replace('import json, os, pathlib,', 'import hashlib, resource, json, os, pathlib,')
    if modern:
        guest=guest.replace('flush_on_last_del no', 'flush_on_last_del never')
    guest=guest.replace("daemon=subprocess.Popen(daemon_args, stdout=log, stderr=log)",
        "if 'probe_deny_rt=1' in read('/proc/cmdline'): resource.setrlimit(resource.RLIMIT_RTPRIO,(0,0))\n"
        "daemon=subprocess.Popen(daemon_args, stdout=log, stderr=log)")
    guest=guest.replace("multipath_log=read('/run/multipath.log')[-16000:])",
        "multipath_log=read('/run/multipath.log')[-16000:],kernel=os.uname().release,"
        "daemon_sha256=hashlib.sha256(pathlib.Path('/usr/sbin/multipathd').read_bytes()).hexdigest(),"
        "rtprio_limit=resource.getrlimit(resource.RLIMIT_RTPRIO),"
        "effective_config=run(['/usr/sbin/multipathd','show','config']),"
        "version_output=subprocess.run(['/usr/sbin/multipath','-h'],capture_output=True,text=True).stderr)")
    return guest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deny-rt',action='store_true')
    parser.add_argument('--stage-root',type=Path,help='Private 0.15.0 install root; omit for host packaged 0.9.4')
    parser.add_argument('--kernel',type=Path,default=ROOT/'lab/work/vmlinuz')
    parser.add_argument('--initramfs',type=Path,default=ROOT/'lab/work/initramfs.cpio.gz')
    parser.add_argument('--wrong',action='store_true',help='Return independent blank disk with same presented identities')
    args=parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit('Run unprivileged; only guest may operate block devices')
    folder=ROOT/'lab/work/version-study'/time.strftime('%Y%m%d-%H%M%S')
    if args.stage_root: args.stage_root=args.stage_root.resolve()
    args.kernel=args.kernel.resolve(); args.initramfs=args.initramfs.resolve()
    folder.mkdir(parents=True,mode=0o700)
    overlay=folder/'overlay'; overlay.mkdir()
    spec=importlib.util.spec_from_file_location('payload',ROOT/'ram-rescue-demo/build.py')
    payload=importlib.util.module_from_spec(spec); spec.loader.exec_module(payload); payload.ROOT=overlay
    for source in ['/usr/bin/udevadm','/usr/lib/systemd/systemd-udevd',
                   '/usr/lib/udev/scsi_id','/usr/bin/setpriv','/lib/x86_64-linux-gnu/libgcc_s.so.1']:
        payload.with_libs(source)
    if args.stage_root:
        copy_staged_runtime(payload,args.stage_root)
    else:
        for source in ['/usr/sbin/multipathd','/usr/sbin/multipath','/usr/sbin/kpartx',
                       '/lib/multipath/libchecktur.so','/lib/multipath/libprioconst.so']:
            payload.with_libs(source)
    guest=guest_program(bool(args.stage_root))
    payload.write('/init',INIT,0o755)
    payload.write('/opt/probe.py',guest)
    payload.write('/etc/udev/rules.d/60-probe.rules',
       'ACTION=="add|change", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", KERNEL=="sd*", IMPORT{program}="/usr/lib/udev/scsi_id --export --whitelisted -d $devnode"\n')
    for f in ['/usr/lib/udev/rules.d/55-dm.rules','/usr/lib/udev/rules.d/60-persistent-storage-dm.rules','/usr/lib/udev/rules.d/95-dm-notify.rules']:
        if Path(f).exists(): payload.copy_file(f)
    payload.write('/etc/multipath.conf','defaults {\n allow_usb_devices yes\n}\n')
    with subprocess.Popen(['find','.','-print0'],cwd=overlay,stdout=subprocess.PIPE) as find:
        archive=subprocess.check_output(['cpio','--null','-o','-H','newc','--owner=0:0'],cwd=overlay,stdin=find.stdout,stderr=subprocess.DEVNULL)
        find.stdout.close(); find.wait()
    initrd=folder/'initramfs.cpio.gz'
    initrd.write_bytes(args.initramfs.read_bytes()+gzip.compress(archive,compresslevel=3))
    disk=folder/'disk.raw'
    with disk.open('xb') as stream: stream.truncate(1024**3)
    wrong=folder/'wrong.raw'
    with wrong.open('xb') as stream: stream.truncate(1024**3)
    command=['qemu-system-x86_64','-machine','q35','-accel','kvm','-m','1024','-smp','2','-display','none',
      '-nodefaults','-no-reboot','-nic','none','-kernel',str(args.kernel),'-initrd',str(initrd),
      '-append','console=ttyS0 rdinit=/init panic=-1'+(' probe_deny_rt=1' if args.deny_rt else ''),'-serial','file:'+str(folder/'console.log'),
      '-serial','unix:'+str(folder/'agent.sock')+',server=on,wait=off',
      '-qmp','unix:'+str(folder/'qmp.sock')+',server=on,wait=off','-device','qemu-xhci,id=xhci',
      '-blockdev',json.dumps({'driver':'raw','node-name':'disk','file':{'driver':'file','filename':str(disk)}}),
      '-device','usb-uas,bus=xhci.0,id=stick,serial=UPSTREAM-USB-001,port=1',
      '-device','scsi-hd,bus=stick.0,id=lun,drive=disk,serial=UPSTREAM-SCSI-001']
    command += ['-blockdev',json.dumps({'driver':'raw','node-name':'wrong','file':{'driver':'file','filename':str(wrong)}})]
    report={'wrong_initial_allocated_bytes':wrong.stat().st_blocks*512,'wrong_disk':args.wrong,'deny_rt':args.deny_rt,'scope':'RAM root, version-selected multipathd, whole USB disk -> kpartx -> LVM -> ext4 data workload; not Ubuntu root recovery',
      'command':command,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'baseline_script_sha256':hashlib.sha256(Path(__file__).with_name('multipathd_probe.py').read_bytes()).hexdigest(),
      'stage_root':str(args.stage_root) if args.stage_root else None,
      'kernel_sha256':hashlib.sha256(args.kernel.read_bytes()).hexdigest(),
      'guest_sha256':hashlib.sha256(guest.encode()).hexdigest(),
      'base_initramfs_sha256':hashlib.sha256(args.initramfs.read_bytes()).hexdigest(),
      'initramfs_sha256':hashlib.sha256(initrd.read_bytes()).hexdigest(),'cycles':[]}
    print(folder,flush=True)
    with (folder/'qemu.log').open('w') as log, (folder/'qmp.jsonl').open('w') as qlog, (folder/'agent.jsonl').open('w') as alog:
        vm=subprocess.Popen(command,stdout=log,stderr=log); channels=[]
        try:
            deadline=time.monotonic()+60
            while not (folder/'qmp.sock').exists():
                if vm.poll() is not None or time.monotonic()>deadline: raise RuntimeError('QEMU startup')
                time.sleep(.1)
            qmp=Channel(folder/'qmp.sock',qlog,qmp=True); channels.append(qmp)
            while 'UPSTREAM_READY' not in (folder/'console.log').read_text(errors='replace'):
                if vm.poll() is not None or time.monotonic()>deadline or 'Kernel panic' in (folder/'console.log').read_text(errors='replace'): raise RuntimeError('guest setup failed')
                time.sleep(.1)
            agent=Channel(folder/'agent.sock',alog); channels.append(agent)
            report['before']=agent.call('snapshot')['snapshot']; t=time.monotonic(); time.sleep(10)
            report['healthy_seconds']=time.monotonic()-t; report['healthy_end']=agent.call('snapshot')['snapshot']
            report['blockstats_before']=qmp.call('query-blockstats',**{'query-nodes':True})
            for cycle in range(1 if args.wrong else 3):
                event={'start':time.monotonic()}; qmp.call('device_del',id='stick')
                deadline=time.monotonic()+10
                while not any(e['host_time']>=event['start'] and e['message'].get('event')=='DEVICE_DELETED' and e['message'].get('data',{}).get('device')=='stick' for e in qmp.events):
                    if time.monotonic()>deadline: raise RuntimeError('device delete timeout')
                    time.sleep(.01)
                event['deleted']=time.monotonic()
                qmp.call('device_add',driver='usb-uas',bus='xhci.0',id='stick',serial='UPSTREAM-USB-001',port='1',attached=False)
                qmp.call('device_add',driver='scsi-hd',bus='stick.0',id='lun',drive='wrong' if args.wrong else 'disk',serial='UPSTREAM-SCSI-001')
                qmp.call('qom-set',path='/machine/peripheral/stick',property='attached',value=True)
                event['reattached']=time.monotonic(); time.sleep(5)
                event['after']=agent.call('snapshot')['snapshot']; report['cycles'].append(event)
            report['blockstats_after']=qmp.call('query-blockstats',**{'query-nodes':True})
            report['wrong_allocated_bytes']=wrong.stat().st_blocks*512
            report['validation']=agent.call('validate')
            report['passed']=bool(report['validation'].get('validated') and all(not c['after']['errors'] for c in report['cycles']) and report['cycles'][-1]['after']['ok']>report['before']['ok'])
        except Exception as exc:
            report['error']=repr(exc); report['passed']=False
        finally:
            for channel in channels: channel.close()
            vm.terminate()
            try: vm.wait(timeout=5)
            except subprocess.TimeoutExpired: vm.kill(); vm.wait()
            if args.wrong: report['wrong_disk_contents']=inspect_sparse_records(wrong)
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'folder':str(folder),'passed':report['passed'],'error':report.get('error'), 'wrong_record_count':len(report.get('wrong_disk_contents',{}).get('records',[]))}),flush=True)

if __name__=='__main__': main()
