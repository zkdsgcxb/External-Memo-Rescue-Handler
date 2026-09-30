#!/usr/bin/env python3
"""Boot the real mkinitramfs integration on an existing disposable Ubuntu PV.

Only a new qcow2 overlay is writable. The added serial shell is explicitly
gated to the QEMU lab and is not part of the production image or service.
Exercise two 0.2-second same-device reconnects while a root process fsyncs.
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

from auto_run import wait_for
from run import Channel, WORK, qemu_command, shell_probe


GUEST = r'''import hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,'/opt/guard')
RUNTIME=Path('/run/ram-rescue-guard/state')
WORKER=Path('/run/host-boot-workload.json')
LOG=Path('/run/host-boot-workload.jsonl')

def gate():
    if ('ram_rescue_lab=1' not in Path('/proc/cmdline').read_text().split() or
            Path('/sys/class/dmi/id/product_name').read_text().strip()!='RAMRescueLab'):
        raise RuntimeError('VM-only test control refused')

def read(path):
    path=Path(path)
    return json.loads(path.read_text()) if path.exists() else None

def process(pid):
    path=Path('/proc')/str(pid)
    try:
        values=(path/'stat').read_text().rsplit(')',1)[1].split()
        return {'pid':pid,'start_ticks':values[19],'state':values[0],
                'cmdline':(path/'cmdline').read_bytes().replace(b'\0',b' ').decode()}
    except FileNotFoundError:
        return None

def systemctl(*args):
    return subprocess.check_output(['/bin/chroot','/proc/1/root','/usr/bin/systemctl',*args],
        text=True,stderr=subprocess.STDOUT,timeout=10)

def unit(name):
    props=dict(line.split('=',1) for line in systemctl('show',name+'.service','-p',
        'ActiveState,SubState,MainPID,LoadState,ExecMainStatus,Type').splitlines() if '=' in line)
    props['process']=process(int(props['MainPID'])) if int(props['MainPID']) else None
    return props

def snapshot():
    gate()
    import path_guard
    from dm_monitor import DeviceMapper
    config=read('/run/ram-rescue-guard/config.json')
    path_guard.configure(config)
    mapper=DeviceMapper()
    dm=path_guard.checked_snapshot(mapper)
    status=mapper.query(config['map_name'])[1]
    state=read(RUNTIME/'path-state.json')
    transaction=read(RUNTIME/'path-transaction.json')
    services={name:unit(name) for name in ['ram-rescue-guard','dbus','systemd-journald']}
    masked={name:unit(name)['LoadState'] for name in ['lab-agent','lab-guard','lab-shell','lab-ready']}
    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    owner=services['ram-rescue-guard']
    ready=bool(state and transaction and state.get('state')=='ready' and
        transaction.get('boot_id')==boot_id and state.get('owner_epoch')==transaction.get('owner_epoch') and
        owner['ActiveState']=='active' and owner['SubState']=='running' and owner['process'] and
        owner['process']['pid']==transaction.get('owner_pid') and
        owner['process']['state'] not in ('Z','X') and '/opt/guard/path_guard.py' in owner['process']['cmdline'])
    mappings={}
    udev_data={}
    uuid_links={}
    for entry in Path('/sys/class/block').glob('dm-*'):
        name=(entry/'dm/name').read_text().strip()
        map_uuid=(entry/'dm/uuid').read_text().strip()
        mappings[name]={'uuid':map_uuid,'kernel_name':entry.name,
            'dev':(entry/'dev').read_text().strip(),
            'slaves':[p.name for p in (entry/'slaves').iterdir()]}
        database=Path('/run/udev/data')/('b'+mappings[name]['dev'])
        udev_data[name]=database.read_text() if database.exists() else None
        if map_uuid.startswith('LVM-'):
            link=Path('/dev/disk/by-id')/('dm-uuid-'+map_uuid)
            node=Path('/dev/mapper')/name
            uuid_links[name]={'path':str(link),'is_symlink':link.is_symlink(),
                'exists':link.exists(),'same_device':link.exists() and node.exists() and
                    os.stat(link).st_rdev==os.stat(node).st_rdev}
    worker=read(WORKER)
    running=process(worker['pid']) if worker else None
    records=[json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []
    mounts=Path('/proc/1/mountinfo').read_text()
    root=next((line for line in mounts.splitlines() if line.split()[4]=='/'),None)
    shared=next((line for line in mounts.splitlines() if line.split()[4]=='/shared'),None)
    return {'boot_id':boot_id,'pid1':process(1),'state':state,'transaction':transaction,
        'prepared':read(RUNTIME/'boot.json'),'supervisor':read(RUNTIME/'path-supervisor.json'),
        'guard_ready':ready,'services':services,'masked_lab_units':masked,
        'multi_user':systemctl('show','multi-user.target','-p','ActiveState','--value').strip(),
        'udev_data':udev_data,
        'dm':dm,'dm_status':status,'mappings':mappings,'uuid_links':uuid_links,
        'root_mount':root,'shared_mount':shared,
        'root_rw':bool(root and 'rw' in root.split()[5].split(',')),
        'worker_registration':worker,'worker_process':running,'writes':records,
        'sentinel':read('/proc/1/root/root/boot-evolution-sentinel.json'),
        'kernel_release':os.uname().release}

def start_workload():
    gate()
    if WORKER.exists():
        raise RuntimeError('Workload already registered')
    source=Path('/opt/vmprobe/workload.py').read_bytes()
    target=Path('/proc/1/root/root/host-boot-workload.py')
    target.write_bytes(source)
    log=open('/run/host-boot-workload-stderr.log','ab',buffering=0)
    try:
        child=subprocess.Popen(['/bin/chroot','/proc/1/root','/usr/bin/python3',
            '/root/host-boot-workload.py'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,
            start_new_session=True)
    finally:
        log.close()
    record=process(child.pid)
    WORKER.write_text(json.dumps(record))
    return record

def stop_and_audit():
    gate()
    registered=read(WORKER)
    live=process(registered['pid'])
    if not live or live['start_ticks']!=registered['start_ticks']:
        raise RuntimeError('Original workload process vanished')
    os.kill(registered['pid'],signal.SIGTERM)
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        live=process(registered['pid'])
        if not live or live['state']=='Z':
            break
        time.sleep(.02)
    records=[json.loads(line) for line in LOG.read_text().splitlines()]
    if not records or len(records)>10000 or any(not row['ok'] or row['seq']!=index for index,row in enumerate(records,1)):
        raise RuntimeError('ACK stream is empty, noncontiguous or contains failed writes')
    expected=b''.join((str(row['seq'])+'\n').encode()+b'x'*4096 for row in records)
    with open('/proc/1/root/root/host-boot-fsync.data','rb') as data:
        actual=data.read(len(expected))
    write_path=Path('/proc/1/root/root/host-boot-final-write')
    with write_path.open('wb') as output:
        output.write(b'root remains writable\n');output.flush();os.fsync(output.fileno())
    directory=os.open(write_path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)
    return {'acknowledged_records':len(records),'acknowledged_bytes':len(expected),
        'prefix_matches':actual==expected,'prefix_sha256':hashlib.sha256(actual).hexdigest(),
        'root_write_fsync':True,'max_write_seconds':max(row['elapsed'] for row in records),
        'kernel_log':subprocess.check_output(['/bin/dmesg'],text=True)}
'''

HOOK = r'''#!/bin/sh
set -eu
case "${1:-}" in prereqs) exit 0 ;; esac
case " $(cat /proc/cmdline) " in *" ram_rescue_lab=1 "*) ;; *) exit 0 ;; esac
[ "$(cat /sys/class/dmi/id/product_name)" = RAMRescueLab ] || exit 0
TOOLS=/run/ram-rescue-demo
[ -x "$TOOLS/usr/bin/python3" ]
mkdir -p "$TOOLS/opt/vmprobe" /run/systemd/system/sysinit.target.wants
cp /opt/host-boot-probe/probe.py /opt/host-boot-probe/workload.py \
   /opt/host-boot-probe/shell.sh "$TOOLS/opt/vmprobe/"
cp /opt/host-boot-probe/vm-host-boot-shell.service /run/systemd/system/
ln -s ../vm-host-boot-shell.service \
    /run/systemd/system/sysinit.target.wants/vm-host-boot-shell.service
'''

SHELL = r'''#!/bin/sh
set -eu
case " $(cat /proc/cmdline) " in *" ram_rescue_lab=1 "*) ;; *) exit 1 ;; esac
[ "$(cat /sys/class/dmi/id/product_name)" = RAMRescueLab ]
echo VM_HOST_BOOT_SHELL_READY >/dev/ttyS0
exec /bin/sh -i
'''

UNIT = '''[Unit]
Description=VM-only RAM serial experiment shell
DefaultDependencies=no
After=ram-rescue-guard.service systemd-remount-fs.service
Before=sysinit.target shutdown.target
Conflicts=shutdown.target
ConditionKernelCommandLine=ram_rescue_lab=1
[Service]
Type=simple
RootDirectory=/run/ram-rescue-demo
ExecStart=/bin/sh /opt/vmprobe/shell.sh
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/ttyS2
TimeoutStopSec=3
Restart=no
'''


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def overlay_initrd(folder, original):
    extracted=folder/'unpacked'
    subprocess.run(['unmkinitramfs',str(original),str(extracted)],check=True,
                   stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    candidates=list(extracted.glob('*/scripts/init-bottom/ORDER'))
    if len(candidates)!=1:
        raise RuntimeError('Cannot locate unique init-bottom order in original initramfs')
    order=candidates[0].read_text()
    if 'ram-rescue-guard' not in order:
        raise RuntimeError('Production guard integration is absent from init-bottom')
    staging=folder/'overlay'
    payload=staging/'opt/host-boot-probe'
    payload.mkdir(parents=True)
    (staging/'scripts/init-bottom').mkdir(parents=True)
    (payload/'probe.py').write_text(GUEST)
    workload=(Path(__file__).parent/'guest/workload.py').read_text()
    workload=workload.replace('/run/workload.jsonl','/run/host-boot-workload.jsonl').replace(
        '/root/workload.data','/root/host-boot-fsync.data')
    (payload/'workload.py').write_text(workload)
    (payload/'shell.sh').write_text(SHELL)
    (payload/'vm-host-boot-shell.service').write_text(UNIT)
    hook=staging/'scripts/init-bottom/zz-host-boot-probe'
    hook.write_text(HOOK);hook.chmod(0o755)
    (staging/'scripts/init-bottom/ORDER').write_text(order+'\n/scripts/init-bottom/zz-host-boot-probe "$@"\n')
    paths=[Path('.'),*sorted(path.relative_to(staging) for path in staging.rglob('*'))]
    archive=subprocess.run(['cpio','--null','-o','--format=newc','--owner=0:0'],cwd=staging,
        input=b'\0'.join(str(path).encode() for path in paths)+b'\0',
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True).stdout
    image=folder/'initrd.img'
    with image.open('xb') as output,original.open('rb') as source:
        shutil.copyfileobj(source,output);output.write(gzip.compress(archive,mtime=0))
    return image


def ram_call(folder, action, timeout=20):
    if action not in ('snapshot','start_workload','stop_and_audit','shutdown'):
        raise ValueError('Unknown VM action')
    source="import sys,json,traceback;sys.path.insert(0,'/opt/vmprobe')\nimport probe\ntry:\n"
    if action=='shutdown':
        source+=(" probe.gate();import subprocess\n subprocess.run(['/bin/sync'],check=True)\n"
                 " subprocess.Popen(['/bin/chroot','/proc/1/root','/usr/bin/systemctl','poweroff'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                 " value={'requested':True}\n")
    else:
        source+=' value=probe.'+action+'()\n'
    source+=(" response={'ok':True,'value':value}\nexcept BaseException:\n"
             " response={'ok':False,'error':traceback.format_exc()}\n"
             "print('HOST_BOOT_REPLY='+json.dumps(response),flush=True)\n")
    encoded=base64.b64encode(source.encode()).decode()
    command="python3 -c \"import base64;exec(base64.b64decode('"+encoded+"'))\"\n"
    if len(command)>=4096:
        raise ValueError('VM serial command exceeds the terminal line limit')
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout);sock.connect(str(folder/'rescue.sock'))
        sock.sendall(b'stty -echo\n');time.sleep(.05);sock.sendall(command.encode())
        output=b''
        while True:
            chunk=sock.recv(65536)
            if not chunk:raise RuntimeError('VM shell closed')
            output+=chunk
            if b'HOST_BOOT_REPLY=' in output and b'\n' in output.split(b'HOST_BOOT_REPLY=',1)[1]:
                answer=json.loads(output.split(b'HOST_BOOT_REPLY=',1)[1].split(b'\n',1)[0])
                with (folder/'ram-actions.jsonl').open('a') as log:
                    log.write(json.dumps({'host_time':time.monotonic(),'action':action,'response':answer})+'\n')
                if not answer['ok']:raise RuntimeError(answer['error'])
                return answer['value']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path,required=True)
    parser.add_argument('--enrollment',type=Path,required=True)
    parser.add_argument('--seed-report',type=Path,default=WORK/'bootevo-0930-173036-39819/report.json')
    args=parser.parse_args()
    if os.geteuid()==0:parser.error('Use an ordinary user; the experiment needs no host root access')
    build_dir=args.build_dir.resolve();enrollment_path=args.enrollment.resolve();seed_path=args.seed_report.resolve()
    for path in [build_dir,enrollment_path,seed_path]:
        if not path.is_relative_to(WORK.resolve()):parser.error('All inputs must be below lab/work')
    build=json.loads((build_dir/'build.json').read_text())
    for filename,key in [('vmlinuz','kernel_sha256'),('initrd.img','initramfs_sha256')]:
        if sha256(build_dir/filename)!=build[key]:parser.error('Build checksum differs: '+filename)
    if sha256(enrollment_path)!=build['enrollment_sha256']:parser.error('Enrollment differs from built profile')
    enrollment=json.loads(enrollment_path.read_text());seed=json.loads(seed_path.read_text())
    registration=seed['cases']['seed']['observation']['gate']
    if not seed['passed'] or registration['identity']!=enrollment['identity']:
        parser.error('Requires the matching successful disposable seed enrollment')
    if enrollment['identity']['usb_serial']!='RAMRESCUE-LAB-001' or enrollment['guard']['map_uuid']!='RAMRESCUE-HOST-VMTEST':
        parser.error('VM-only identity/map required; never attach a real host profile')
    disk=seed_path.parent/'s0/usb.raw'
    if disk.is_symlink() or not disk.is_file():parser.error('Seed must be a regular recorded raw image')
    original_hash=sha256(disk)
    if original_hash!=seed['source_image_sha256_after']:parser.error('Seed raw checksum differs from prior result')
    folder=WORK/('hb-'+time.strftime('%m%d-%H%M%S')+'-'+str(os.getpid()))
    folder.mkdir(mode=0o700)
    if len(str(folder/'rescue.sock').encode())>=108:parser.error('VM socket path is too long')
    overlay=folder/'usb.qcow2'
    subprocess.run(['qemu-img','create','-q','-f','qcow2','-F','raw','-b',str(disk),str(overlay)],check=True)
    with (folder/'decoy.raw').open('xb') as stream:stream.truncate(16*1024**2)
    image=overlay_initrd(folder,build_dir/'initrd.img')
    masks=' '.join('systemd.mask='+name+'.service' for name in ['lab-agent','lab-guard','lab-shell','lab-ready','lab-workload'])
    command=qemu_command(folder,same_port=True,kernel=build_dir/'vmlinuz',initramfs=image,
        extra_kernel_args='root=/dev/mapper/labrescue-ubuntu ro nompath ram_rescue_guard=1 '+masks)
    command[command.index('-m')+1]='3072'
    # Use the distribution's /init entrypoint. No synthetic lab /init or agent.
    command[command.index('-append')+1]=command[command.index('-append')+1].replace(
        'rdinit=/init ','').replace('panic=-1 ','')
    for index,word in enumerate(command):
        if word=='-blockdev':
            value=json.loads(command[index+1])
            if value['node-name']=='usbdisk':
                value.update(driver='qcow2',file={'driver':'file','filename':str(overlay)},
                    backing={'driver':'raw','read-only':True,'file':{'driver':'file','filename':str(disk)}})
                command[index+1]=json.dumps(value)
    report={'schema':1,'scope':'distribution initramfs integration with VM-only observation shell',
        'build':build,'runner_sha256':sha256(Path(__file__)),'overlay_initrd_sha256':sha256(image),
        'qemu_runner_sha256':sha256(Path(__file__).with_name('run.py')),
        'workload_source_sha256':sha256(Path(__file__).parent/'guest/workload.py'),
        'qemu_version':subprocess.check_output(['qemu-system-x86_64','--version'],text=True).splitlines()[0],
        'seed_report':str(seed_path),'seed_report_sha256':sha256(seed_path),
        'source_image_sha256_before':original_hash,'command':command,'cycles':[],'passed':False}
    (folder/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    print('Host integration VM:',folder,flush=True)
    qmp=None
    with (folder/'qemu.log').open('w') as log,(folder/'qmp.jsonl').open('w') as qlog:
        vm=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
        try:
            wait_for(lambda:(folder/'qmp.sock').exists(),10,'QMP socket')
            qmp=Channel(folder/'qmp.sock',qlog,qmp=True)
            def booted():
                path=folder/'console.log'
                text=path.read_text(errors='replace') if path.exists() else ''
                if (vm.poll() is not None or 'Kernel panic' in text or
                        'RAM Guard protected boot stopped:' in text):
                    raise RuntimeError('Protected boot failed; inspect the recorded console')
                return 'VM_HOST_BOOT_SHELL_READY' in text
            wait_for(booted,180,'protected Ubuntu boot and VM observation shell')
            report['initial_observation']=ram_call(folder,'snapshot')
            def ready():
                value=ram_call(folder,'snapshot')
                report['last_boot_observation']=value
                return value if value['guard_ready'] and value['multi_user']=='active' else None
            report['before']=wait_for(ready,20,'Guard notify-ready and multi-user Ubuntu')
            report['worker']=ram_call(folder,'start_workload')
            report['pre_fault']=wait_for(lambda:s if len((s:=ram_call(folder,'snapshot'))['writes'])>=5 else None,15,'fsync ACKs')
            for cycle in range(2):
                entry={'index':cycle+1,'fault_host_time':time.monotonic()};report['cycles'].append(entry)
                qmp.call('device_del',id='stick')
                wait_for(lambda:any(e['host_time']>=entry['fault_host_time'] and e['message'].get('event')=='DEVICE_DELETED' and
                    e['message'].get('data',{}).get('device')=='stick' for e in qmp.events),10,'USB removal')
                entry['deleted_host_time']=time.monotonic();time.sleep(.2)
                entry['reattach_request_host_time']=time.monotonic()
                qmp.call('device_add',driver='usb-uas',bus='xhci.0',port='1',id='stick',serial='RAMRESCUE-LAB-001',attached=False)
                qmp.call('device_add',driver='scsi-hd',bus='stick.0',id='lun',drive='usbdisk')
                qmp.call('qom-set',path='/machine/peripheral/stick',property='attached',value=True)
                entry['reattached_host_time']=time.monotonic()
                def recovered():
                    value=ram_call(folder,'snapshot')
                    state=value['state'] or {}
                    if state.get('state') in {'expired','failed','interrupted','blocked'}:
                        raise RuntimeError('Guard reached terminal state: '+json.dumps(state))
                    return value if value['guard_ready'] and state.get('recoveries',0)>=cycle+1 else None
                entry['after']=wait_for(recovered,25,'automatic reconnect')
                entry['recovered_host_time']=time.monotonic()
                entry['ram_shell']=shell_probe(folder/'rescue.sock')
                count=len(entry['after']['writes'])
                entry['progress']=wait_for(lambda:s if len((s:=ram_call(folder,'snapshot'))['writes'])>count+2 else None,10,'same process fsync progress')
            report['after']=ram_call(folder,'snapshot')
            report['audit']=ram_call(folder,'stop_and_audit')
            before=report['pre_fault'];after=report['after']
            records=after['writes']
            report['successful_writes']=sum(row['ok'] for row in records)
            report['failed_writes']=sum(not row['ok'] for row in records)
            report['max_write_seconds']=max((row['elapsed'] for row in records),default=None)
            def identity(process):
                return {key:process[key] for key in ['pid','start_ticks']} if process else None
            root_lvs=['labrescue-ubuntu','labrescue-shared']
            report['checks']={'prepared_by_production_boot':before['prepared']['phase']=='prepared',
                'guard_notify_ready':before['guard_ready'] and after['guard_ready'] and
                    before['services']['ram-rescue-guard']['Type']=='notify',
                'old_lab_units_masked':all(value=='masked' for value in before['masked_lab_units'].values()),
                'same_boot_and_pid1':before['boot_id']==after['boot_id'] and identity(before['pid1'])==identity(after['pid1']),
                'same_worker_pid':identity(before['worker_process'])==identity(after['worker_process']) and after['worker_process']['state'] not in ('Z','X'),
                'two_recoveries':after['state']['recoveries']==2,
                'same_guard_owner':before['transaction']['owner_epoch']==after['transaction']['owner_epoch'],
                'stable_root_and_shared_maps':all(before['mappings'][name]==after['mappings'][name] for name in root_lvs),
                'both_lvs_on_stable_map':all(after['mappings'][name]['slaves']==
                    [after['mappings']['ram-rescue-path']['kernel_name']] for name in root_lvs),
                'lvm_uuid_symlinks_present':all(snapshot['uuid_links'][name]['is_symlink'] and
                    snapshot['uuid_links'][name]['same_device'] for snapshot in [before,after] for name in root_lvs),
                'standard_services_survived':all(identity(before['services'][name]['process'])==
                    identity(after['services'][name]['process']) and after['services'][name]['ActiveState']=='active'
                    for name in ['dbus','systemd-journald']),
                'zero_application_errors':bool(records) and all(row['ok'] for row in records),
                'writes_continued_each_cycle':all(len(entry['progress']['writes'])>len(entry['after']['writes']) for entry in report['cycles']),
                'root_rw':after['root_rw'],'same_sentinel':after['sentinel']==registration['sentinel'],
                'multi_user_active_after':after['multi_user']=='active',
                'root_on_enrolled_lv':bool(after['root_mount'] and
                    after['root_mount'].split()[2]==after['mappings']['labrescue-ubuntu']['dev']),
                'shared_mounted_from_enrolled_lv':all(s['shared_mount'] and
                    s['shared_mount'].split()[2]==s['mappings']['labrescue-shared']['dev'] for s in [before,after]),
                'fsync_after_reconnect':report['audit']['root_write_fsync'],
                'ack_prefix_matches':report['audit']['prefix_matches'],
                'ram_shell_after_cycles':all(entry['ram_shell'] for entry in report['cycles']),
                'no_ext4_abort_observed':'Aborting journal' not in report['audit']['kernel_log']}
            report['passed']=all(report['checks'].values())
            report['shutdown']=ram_call(folder,'shutdown')
            try:vm.wait(timeout=150);report['graceful_shutdown']=vm.returncode==0
            except subprocess.TimeoutExpired:report['graceful_shutdown']=False
            report['checks']['graceful_shutdown']=report['graceful_shutdown']
            report['passed']=all(report['checks'].values())
        except BaseException:
            report['error']=traceback.format_exc();raise
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:vm.wait(timeout=10)
                except subprocess.TimeoutExpired:vm.kill();vm.wait()
            if qmp:qmp.close()
            report['source_image_sha256_after']=sha256(disk)
            report['source_image_unchanged']=report['source_image_sha256_after']==original_hash
            report['passed']=report['passed'] and report['source_image_unchanged']
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print('Host boot report:',folder/'report.json',flush=True)
    if not report['passed']:raise SystemExit('Host integration continuity checks failed')


if __name__=='__main__':
    main()
