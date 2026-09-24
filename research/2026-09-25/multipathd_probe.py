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

INIT = r'''#!/bin/sh
set -eu
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs -o noswap tmpfs /run
mkdir -p /run/udev /run/lock/lvm /run/lvm /var/log /etc/multipath /data
/usr/lib/systemd/systemd-udevd --daemon
for module in xhci_pci usb_storage uas sd_mod dm_mod dm_multipath dm_round_robin ext4; do
    /sbin/modprobe "$module"
done
udevadm trigger --action=add
udevadm settle --timeout=15
python3 /opt/probe.py
exec /bin/sh
'''

GUEST = r"""import json, os, pathlib, subprocess, threading, time, traceback, tty
def run(args, timeout=20):
    r=subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if r.returncode: raise RuntimeError(str(args)+': '+r.stdout+r.stderr)
    return r.stdout
def read(path):
    try: return pathlib.Path(path).read_text()
    except OSError: return ''
wwid=dict(line.split('=',1) for line in run(['/usr/lib/udev/scsi_id','--export','--whitelisted','--device=/dev/sda']).splitlines() if '=' in line)['ID_SERIAL']
assert wwid
pathlib.Path('/etc/multipath/wwids').write_text('# Multipath wwids, Version : 1.0\n/'+wwid+'/\n')
pathlib.Path('/etc/multipath.conf').write_text('''defaults {
 allow_usb_devices yes
 find_multipaths strict
 polling_interval 1
 max_polling_interval 4
 path_checker tur
 detect_prio no
 prio const
 path_selector "round-robin 0"
 no_path_retry 8
 flush_on_last_del no
 recheck_wwid yes
 user_friendly_names no
}
blacklist {
 wwid ".*"
}
blacklist_exceptions {
 property "^ID_SERIAL$"
 wwid "'''+wwid+'''"
}
multipaths {
 multipath {
  wwid "'''+wwid+'''"
  alias probe
 }
}
''')
log=open('/run/multipath.log','w')
daemon_args=['/usr/sbin/multipathd','-d','-v3']
if 'probe_deny_rt=1' in read('/proc/cmdline'):
    daemon_args=['/usr/bin/setpriv','--bounding-set=-sys_nice',*daemon_args]
daemon=subprocess.Popen(daemon_args, stdout=log, stderr=log)
for _ in range(100):
    run(['/sbin/dmsetup','mknodes'])
    if pathlib.Path('/dev/mapper/probe').exists(): break
    time.sleep(.1)
else: raise RuntimeError('no multipath map: '+read('/run/multipath.log'))
subprocess.run(['/sbin/sfdisk','/dev/mapper/probe'],input='label: gpt\nsize=, type=linux-lvm\n',text=True,check=True)
run(['/usr/sbin/kpartx','-a','-s','/dev/mapper/probe'])
run(['/sbin/dmsetup','mknodes'])
part='/dev/mapper/probe1'
if not pathlib.Path(part).exists(): part='/dev/mapper/probep1'
assert pathlib.Path(part).exists(), run(['/sbin/dmsetup','ls'])
for cmd in [['pvcreate','-ff','-y',part],['vgcreate','mpstudy',part],['lvcreate','-L','256M','-n','data','mpstudy']]:
    run(['/sbin/lvm',*cmd,'--devices',part])
run(['/sbin/dmsetup','mknodes'])
run(['/sbin/mkfs.ext4','-F','/dev/mapper/mpstudy-data'])
run(['/bin/mount','/dev/mapper/mpstudy-data','/data'])
state={'ok':0,'errors':[],'max_seconds':0,'running':True}
def workload():
    fd=os.open('/data/records',os.O_CREAT|os.O_WRONLY|os.O_APPEND,0o600)
    while state['running']:
        start=time.monotonic()
        try:
            os.write(fd, (str(state['ok'])+'\n').encode().ljust(4096,b'x'))
            os.fsync(fd)
            state['ok']+=1
            state['max_seconds']=max(state['max_seconds'],time.monotonic()-start)
        except OSError as e: state['errors'].append(str(e))
        time.sleep(.1)
    os.close(fd)
threading.Thread(target=workload,daemon=True).start()
def snapshot():
    d=dict(state); d.update(clock_ticks=os.sysconf('SC_CLK_TCK'),pid=daemon.pid,wwid=wwid,daemon_status=read('/proc/'+str(daemon.pid)+'/status'),
      daemon_stat=read('/proc/'+str(daemon.pid)+'/stat'),sched_policy=os.sched_getscheduler(daemon.pid),
      sched_priority=os.sched_getparam(daemon.pid).sched_priority,
      maps=run(['/sbin/dmsetup','table']),status=run(['/sbin/dmsetup','status']),
      topology=run(['/usr/sbin/multipathd','show','topology']),
      devices={p.name:read(str(p/'device/serial')) for p in pathlib.Path('/sys/class/block').glob('sd*')},
      multipath_log=read('/run/multipath.log')[-16000:])
    return d
print('UPSTREAM_READY',flush=True)
fd=os.open('/dev/ttyS1',os.O_RDWR|os.O_NOCTTY)
tty.setraw(fd)
with os.fdopen(fd,'r+b',buffering=0) as serial:
    for raw in serial:
        req=json.loads(raw); ans={'id':req['id']}
        try:
            if req['action']=='snapshot': ans['snapshot']=snapshot()
            elif req['action']=='validate':
                n=state['ok']; fd=os.open('/data/records',os.O_RDONLY)
                os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED)
                data=os.read(fd,n*4096); os.close(fd)
                ans['validated']=len(data)==n*4096 and all(data[i*4096:(i+1)*4096]==(str(i)+'\n').encode().ljust(4096,b'x') for i in range(n))
                ans['records']=n
            else: raise ValueError('unknown action')
        except Exception: ans['error']=traceback.format_exc()
        serial.write((json.dumps(ans)+'\n').encode())
"""

def inspect_sparse_records(path):
    result={'allocated_bytes':path.stat().st_blocks*512,'data_ranges':[],'records':[]}
    fd=os.open(path,os.O_RDONLY)
    try:
        offset=0
        while offset<path.stat().st_size:
            try: start=os.lseek(fd,offset,os.SEEK_DATA)
            except OSError: break
            end=os.lseek(fd,start,os.SEEK_HOLE)
            result['data_ranges'].append([start,end]); pos=start
            while pos<end:
                data=os.pread(fd,min(1024**2,end-pos),pos)
                for i in range(0,len(data)-4095,4096):
                    block=data[i:i+4096]
                    match=re.match(rb'([0-9]{1,9})\n',block)
                    if match and block==(match.group(1)+b'\n').ljust(4096,b'x'):
                        result['records'].append({'offset':pos+i,'number':int(match.group(1)),
                            'sha256':hashlib.sha256(block).hexdigest()})
                pos+=len(data)
            offset=end
    finally: os.close(fd)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--deny-rt',action='store_true')
    parser.add_argument('--wrong',action='store_true',help='Return independent blank disk with same presented identities')
    args=parser.parse_args()
    if os.geteuid() == 0:
        raise SystemExit('Run unprivileged; only guest may operate block devices')
    folder=ROOT/'lab/work/upstream-study'/time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True,mode=0o700)
    overlay=folder/'overlay'; overlay.mkdir()
    spec=importlib.util.spec_from_file_location('payload',ROOT/'ram-rescue-demo/build.py')
    payload=importlib.util.module_from_spec(spec); spec.loader.exec_module(payload); payload.ROOT=overlay
    for source in ['/usr/sbin/multipathd','/usr/sbin/multipath','/usr/sbin/kpartx','/usr/bin/udevadm',
                   '/usr/lib/systemd/systemd-udevd','/usr/lib/udev/scsi_id','/usr/bin/setpriv',
                   '/lib/multipath/libchecktur.so','/lib/multipath/libprioconst.so','/lib/x86_64-linux-gnu/libgcc_s.so.1']:
        payload.with_libs(source)
    payload.write('/init',INIT,0o755)
    payload.write('/opt/probe.py',GUEST)
    payload.write('/etc/udev/rules.d/60-probe.rules',
       'ACTION=="add|change", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", KERNEL=="sd*", IMPORT{program}="/usr/lib/udev/scsi_id --export --whitelisted -d $devnode"\n')
    for f in ['/usr/lib/udev/rules.d/55-dm.rules','/usr/lib/udev/rules.d/60-persistent-storage-dm.rules','/usr/lib/udev/rules.d/95-dm-notify.rules']:
        if Path(f).exists(): payload.copy_file(f)
    payload.write('/etc/multipath.conf','defaults {\n allow_usb_devices yes\n}\n')
    with subprocess.Popen(['find','.','-print0'],cwd=overlay,stdout=subprocess.PIPE) as find:
        archive=subprocess.check_output(['cpio','--null','-o','-H','newc','--owner=0:0'],cwd=overlay,stdin=find.stdout,stderr=subprocess.DEVNULL)
        find.stdout.close(); find.wait()
    initrd=folder/'initramfs.cpio.gz'
    initrd.write_bytes((ROOT/'lab/work/initramfs.cpio.gz').read_bytes()+gzip.compress(archive,compresslevel=3))
    disk=folder/'disk.raw'
    with disk.open('xb') as stream: stream.truncate(1024**3)
    wrong=folder/'wrong.raw'
    with wrong.open('xb') as stream: stream.truncate(1024**3)
    command=['qemu-system-x86_64','-machine','q35','-accel','kvm','-m','1024','-smp','2','-display','none',
      '-nodefaults','-no-reboot','-nic','none','-kernel',str(ROOT/'lab/work/vmlinuz'),'-initrd',str(initrd),
      '-append','console=ttyS0 rdinit=/init panic=-1'+(' probe_deny_rt=1' if args.deny_rt else ''),'-serial','file:'+str(folder/'console.log'),
      '-serial','unix:'+str(folder/'agent.sock')+',server=on,wait=off',
      '-qmp','unix:'+str(folder/'qmp.sock')+',server=on,wait=off','-device','qemu-xhci,id=xhci',
      '-blockdev',json.dumps({'driver':'raw','node-name':'disk','file':{'driver':'file','filename':str(disk)}}),
      '-device','usb-uas,bus=xhci.0,id=stick,serial=UPSTREAM-USB-001,port=1',
      '-device','scsi-hd,bus=stick.0,id=lun,drive=disk,serial=UPSTREAM-SCSI-001']
    command += ['-blockdev',json.dumps({'driver':'raw','node-name':'wrong','file':{'driver':'file','filename':str(wrong)}})]
    report={'wrong_initial_allocated_bytes':wrong.stat().st_blocks*512,'wrong_disk':args.wrong,'deny_rt':args.deny_rt,'scope':'RAM root, stock multipathd, whole USB disk -> kpartx -> LVM -> ext4 data workload; not Ubuntu root recovery',
      'command':command,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'base_initramfs_sha256':hashlib.sha256((ROOT/'lab/work/initramfs.cpio.gz').read_bytes()).hexdigest(),
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
    print(json.dumps({'folder':str(folder),'passed':report['passed'],'error':report.get('error')}),flush=True)

if __name__=='__main__': main()
