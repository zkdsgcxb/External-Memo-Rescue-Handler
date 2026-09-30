#!/usr/bin/env python3
"""Exercise native systemd FAT mount on a disposable Ubuntu USB disk.

The protected Ubuntu root uses a fresh qcow2 overlay. A separate disposable
GPT USB image contains the 64 MiB FAT partition. The final case removes both
virtual devices to exercise root recovery during EFI re-enumeration. No host
block device is accepted or opened.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import socket
import sys
import subprocess
import time
import traceback

from auto_run import wait_for
import host_boot_probe as boot
from run import Channel, WORK, qemu_command

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'guard'))
RENDERER = Path(__file__).resolve().parents[1] / 'guard/efi_mount.py'
RENDERER_SHA256 = boot.sha256(RENDERER)
from efi_mount import fsck_unit, render_fsck, render_path, render_rule
if boot.sha256(RENDERER) != RENDERER_SHA256:
    raise RuntimeError('Production EFI renderer changed while loading')

UUID = '1BAD-C0DE'
OPTIONS = 'umask=0077'
PARTUUID = '57f23024-de51-4ab3-a15a-71e443fc2d6f'

GUEST = r'''
EFI_UUID='1BAD-C0DE'
EFI_UNITS=['boot-efi.mount','ram-rescue-efi.path']
def efi_host(*args,timeout=20):
    started=time.monotonic()
    result=subprocess.run(['/bin/chroot','/proc/1/root',*args],text=True,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    return {'returncode':result.returncode,'stdout':result.stdout,
        'stderr':result.stderr,'elapsed':time.monotonic()-started}
def efi_snapshot():
    gate()
    units={name:dict(line.split('=',1) for line in systemctl('show',name,'-p',
        'ActiveState,SubState,Result,After,BindsTo,Requires,TimeoutUSec,TimeoutIdleUSec,JobTimeoutUSec,JobRunningTimeoutUSec').splitlines() if '=' in line)
        for name in EFI_UNITS}
    link=Path('/dev/disk/by-uuid')/EFI_UUID
    return {'units':units,'device':str(link.resolve()) if link.exists() else None,
        'enrolled_link':str(Path('/dev/ram-rescue-efi').resolve()) if Path('/dev/ram-rescue-efi').exists() else None,
        'udev':efi_host('/usr/bin/udevadm','info','--query=property','--name='+str(link)) if link.exists() else None,
        'fsck':efi_host('/usr/bin/systemctl','show',EFI_FSCK_UNIT,'-p','Id,ActiveState,SubState,Result,ExecMainStartTimestampMonotonic,BindsTo'),
        'fsck_journal':efi_host('/usr/bin/journalctl','-b','--no-pager','-o','short-monotonic','-u',EFI_FSCK_UNIT),
        'delayed_umount':Path('/run/efi-delayed-umount.log').read_text() if Path('/run/efi-delayed-umount.log').exists() else None,
        'udev_events':Path('/run/efi-udev-events.log').read_text() if Path('/run/efi-udev-events.log').exists() else None,
        'mounts':[line for line in Path('/proc/1/mountinfo').read_text().splitlines()
            if line.split()[4]=='/boot/efi']}
def efi_action(action):
    gate()
    if action=='configure':
        root=Path('/proc/1/root')
        (root/'boot/efi').mkdir(exist_ok=True)
        tools=efi_host('/bin/sh','-c','command -v fsck.vfat; systemd --version | head -n 1')
        if not (root/'usr/sbin/fsck.vfat').exists() and not (root/'sbin/fsck.vfat').exists():
            raise RuntimeError('Ubuntu fixture needs dosfstools before pass=1 validation: '+str(tools))
        result=efi_host('/usr/bin/systemd-run','--wait','--pipe','/bin/mount','-t','vfat','/dev/disk/by-uuid/'+EFI_UUID,'/boot/efi')
        if result['returncode']:raise RuntimeError(str(result)+'\n'+subprocess.check_output(['/bin/dmesg'],text=True)[-10000:])
        payload=b'EFI native mount reconnect sentinel\n'*128
        with (root/'boot/efi/sentinel.bin').open('wb') as output:
            output.write(payload);output.flush();os.fsync(output.fileno())
        result=efi_host('/usr/bin/systemd-run','--wait','--pipe','/bin/umount','/boot/efi')
        if result['returncode']:raise RuntimeError(str(result)+'\n'+subprocess.check_output(['/bin/dmesg'],text=True)[-10000:])
        with (root/'etc/fstab').open('a') as output:
            output.write('\nUUID='+EFI_UUID+' /boot/efi vfat '+EFI_OPTIONS+' 0 1\n')
        (root/'etc/udev/rules.d/90-ram-rescue-efi.rules').write_text(EFI_RULE+'\n')
        (root/'etc/systemd/system/ram-rescue-efi.path').write_text(EFI_PATH)
        if EFI_FSCK_DROPIN:
            dropin=root/'etc/systemd/system'/(EFI_FSCK_UNIT+'.d')
            dropin.mkdir(exist_ok=True)
            (dropin/'50-ram-rescue-efi.conf').write_text(EFI_FSCK_DROPIN)
        efi_host('/usr/bin/udevadm','control','--reload-rules')
        systemctl('daemon-reload')
        systemctl('stop',EFI_UNITS[0]);systemctl('stop',EFI_FSCK_UNIT)
        systemctl('start',EFI_UNITS[0])
        systemctl('enable','--now',EFI_UNITS[1])
        device=(Path('/dev/disk/by-uuid')/EFI_UUID).resolve(strict=True).name
        result=efi_host('/usr/bin/udevadm','trigger','--action=change','/sys/class/block/'+device)
        if result['returncode']:raise RuntimeError(str(result))
        result=efi_host('/usr/bin/udevadm','settle','--timeout=10')
        if result['returncode']:raise RuntimeError(str(result))
        return {'tools':tools,'expected_sha256':hashlib.sha256(payload).hexdigest(),
            'fstab_line':'UUID='+EFI_UUID+' /boot/efi vfat '+EFI_OPTIONS+' 0 1',
            'units':systemctl('cat',*EFI_UNITS),'snapshot':efi_snapshot()}
    if action=='delay_umount':
        root=Path('/proc/1/root')
        binary=root/'usr/bin/umount'
        real=root/'usr/bin/umount.efi-probe-real'
        if real.exists():raise RuntimeError('VM umount fixture already installed')
        binary.rename(real)
        binary.write_text('#!/bin/sh\ncase " $* " in *" /boot/efi "*) read now rest </proc/uptime; printf "start %s\\n" "$now" >>/run/efi-delayed-umount.log; sleep 2; read now rest </proc/uptime; printf "end %s\\n" "$now" >>/run/efi-delayed-umount.log ;; esac\nexec /usr/bin/umount.efi-probe-real "$@"\n')
        binary.chmod(0o755)
        efi_host('/usr/bin/umount.efi-probe-real','--version')
        efi_host('/bin/sync')
        log=open('/run/efi-udev-events.log','ab',buffering=0)
        try:
            monitor=subprocess.Popen(['/bin/chroot','/proc/1/root','/usr/bin/udevadm','monitor','--udev','--property','--subsystem-match=block'],
                stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        finally:log.close()
        Path('/run/efi-udev-monitor.pid').write_text(str(monitor.pid))
        return {'vm_only_umount_delay_seconds':2,'monitor_pid':monitor.pid}
    if action=='snapshot':return efi_snapshot()
    if action=='journal':return efi_host('/usr/bin/journalctl','-b','--no-pager',
        '-o','short-monotonic','-u',EFI_UNITS[0],'-u',EFI_UNITS[1])
    if action=='read':
        result=efi_host('/usr/bin/python3','-c',
            'import hashlib;print(hashlib.sha256(open("/boot/efi/sentinel.bin","rb").read()).hexdigest())')
        result['snapshot']=efi_snapshot()
        return result
    if action=='repeat_start':
        before=efi_snapshot()
        systemctl('start',EFI_UNITS[0])
        return {'before':before,'after':efi_snapshot(),'read':efi_action('read')}
    if action=='failure_limit':
        systemctl('stop',EFI_UNITS[1]);systemctl('stop',EFI_UNITS[0])
        root=Path('/proc/1/root')
        directory=root/'etc/systemd/system/boot-efi.mount.d'
        directory.mkdir(exist_ok=True)
        dropin=directory/'99-vm-only-failure.conf'
        dropin.write_text('[Mount]\nType=ram_rescue_vm_missing\n')
        systemctl('daemon-reload');systemctl('reset-failed',*EFI_UNITS)
        systemctl('start',EFI_UNITS[1])
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            failed=efi_snapshot()
            if failed['units'][EFI_UNITS[1]]['ActiveState']=='failed':break
            time.sleep(.05)
        time.sleep(1)
        quiet=efi_snapshot()
        dropin.unlink();systemctl('daemon-reload');systemctl('reset-failed',*EFI_UNITS)
        systemctl('start',EFI_UNITS[0]);systemctl('start',EFI_UNITS[1])
        return {'failed':failed,'quiet':quiet,'restored':efi_snapshot(),'read':efi_action('read')}
    if action=='restart_path':
        # The deliberate failure test consumed the path's 30-second budget.
        # Restore the ordinary mount explicitly before re-enabling its watcher,
        # just as production installation and maintenance do.
        systemctl('start',EFI_UNITS[0])
        systemctl('start',EFI_UNITS[1])
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=efi_snapshot()
            if result['units'][EFI_UNITS[0]]['ActiveState']=='active':return result
            time.sleep(.05)
        raise RuntimeError('Restarted native path did not remount EFI')
    if action=='stop':
        systemctl('stop',EFI_UNITS[1])
        systemctl('stop',EFI_UNITS[0])
        return {'snapshot':efi_snapshot(),'journal':efi_host('/usr/bin/journalctl','-b','--no-pager',
            '-o','short-monotonic','-u',EFI_UNITS[0],'-u',EFI_UNITS[1])}
    raise ValueError('unknown EFI action')
'''


def ram_call(folder, action):
    source=("import sys,json,traceback;sys.path.insert(0,'/opt/vmprobe');import probe\ntry:\n"
            " value=probe.efi_action("+repr(action)+")\n response={'ok':True,'value':value}\n"
            "except BaseException:\n response={'ok':False,'error':traceback.format_exc()}\n"
            "print('EFI_REPLY='+json.dumps(response),flush=True)\n")
    encoded=base64.b64encode(source.encode()).decode()
    command="python3 -c \"import base64;exec(base64.b64decode('"+encoded+"'))\"\n"
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(35);sock.connect(str(folder/'rescue.sock'))
        sock.sendall(b'stty -echo\n');time.sleep(.05);sock.sendall(command.encode())
        output=b''
        while True:
            chunk=sock.recv(65536)
            if not chunk:raise RuntimeError('VM serial shell closed')
            output+=chunk
            if b'EFI_REPLY=' in output and b'\n' in output.split(b'EFI_REPLY=',1)[1]:
                result=json.loads(output.split(b'EFI_REPLY=',1)[1].split(b'\n',1)[0])
                with (folder/'actions.jsonl').open('a') as stream:
                    stream.write(json.dumps({'action':action,'response':result})+'\n')
                if not result['ok']:raise RuntimeError(result['error'])
                return result['value']


def fsck_start(snapshot):
    match=re.search(r'^ExecMainStartTimestampMonotonic=(\d+)$',
        snapshot['fsck']['stdout'],re.MULTILINE)
    return int(match[1]) if match else 0


def arrival_during_unmount(snapshot):
    markers=(snapshot.get('delayed_umount') or '').splitlines()
    if len(markers)<2:return False
    start=float(markers[0].split()[1]);end=float(markers[1].split()[1])
    for event in (snapshot.get('udev_events') or '').split('\n\n'):
        if '\nID_FS_UUID='+UUID+'\n' not in event:continue
        match=re.search(r'^UDEV\s+\[([\d.]+)\]\s+add\s',event,re.MULTILINE)
        if match and start<float(match[1])<end:return True
    return False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path,default=WORK/'hb-build-v4')
    parser.add_argument('--enrollment',type=Path,default=WORK/'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report',type=Path,default=WORK/'bootevo-0930-173036-39819/report.json')
    args=parser.parse_args()
    if os.geteuid()==0:parser.error('Run as an ordinary user; host root is never required')
    build_dir=args.build_dir.resolve();enrollment=args.enrollment.resolve();seed_path=args.seed_report.resolve()
    for path in [build_dir,enrollment,seed_path]:
        if not path.is_relative_to(WORK.resolve()):parser.error('Inputs must resolve below lab/work')
    build=json.loads((build_dir/'build.json').read_text())
    for name,key in [('initrd.img','initramfs_sha256'),('vmlinuz','kernel_sha256')]:
        if boot.sha256(build_dir/name)!=build[key]:parser.error('Build digest differs')
    profile=json.loads(enrollment.read_text());seed=json.loads(seed_path.read_text())
    if (boot.sha256(enrollment)!=build['enrollment_sha256'] or not seed['passed'] or
            profile['identity']!=seed['cases']['seed']['observation']['gate']['identity'] or
            profile['identity']['usb_serial']!='RAMRESCUE-LAB-001' or
            profile['guard']['map_uuid']!='RAMRESCUE-HOST-VMTEST'):
        parser.error('Only the matching enrolled disposable VM seed is accepted')
    disk=seed_path.parent/'s0/usb.raw'
    if disk.is_symlink() or not disk.is_file():parser.error('Seed must be a regular raw file')
    before_hash=boot.sha256(disk)
    if before_hash!=seed['source_image_sha256_after']:parser.error('Seed digest differs')
    folder=WORK/('efi-'+time.strftime('%m%d-%H%M%S')+'-'+str(os.getpid()))
    folder.mkdir(mode=0o700)
    if len(str(folder/'rescue.sock').encode())>=108:parser.error('Socket pathname too long')
    overlay=folder/'usb.qcow2'
    subprocess.run(['qemu-img','create','-q','-f','qcow2','-F','raw','-b',str(disk),str(overlay)],check=True)
    for filename,size in [('decoy.raw',16*1024**2),('efi.raw',80*1024**2)]:
        with (folder/filename).open('xb') as stream:stream.truncate(size)
    subprocess.run(['sfdisk',str(folder/'efi.raw')],input='label: gpt\nstart=2048,size=131072,type=U,uuid='+PARTUUID+'\n',text=True,stdout=subprocess.DEVNULL,check=True)
    subprocess.run(['mkfs.fat','--offset=2048','-F','32','-i',UUID.replace('-',''),'-n','EFI-VM-ONLY',str(folder/'efi.raw'),'65536'],check=True)
    identity={'ID_FS_UUID':UUID,'ID_PART_ENTRY_UUID':PARTUUID,'ID_USB_SERIAL_SHORT':'EFI-VM-ONLY'}
    rule=render_rule(identity)
    path_unit=render_path()
    fsck_name=fsck_unit(identity)
    fsck_dropin=render_fsck()
    boot.GUEST+='\nEFI_OPTIONS='+repr(OPTIONS)+'\nEFI_RULE='+repr(rule)+'\nEFI_PATH='+repr(path_unit)+'\nEFI_FSCK_UNIT='+repr(fsck_name)+'\nEFI_FSCK_DROPIN='+repr(fsck_dropin)+'\n'+GUEST
    # The seed root does not contain this newer kernel's optional NLS modules.
    # Load its packaged initramfs copy before switch_root, as a real host can.
    boot.HOOK+='\nmodprobe nls_iso8859-1\n'
    image=boot.overlay_initrd(folder,build_dir/'initrd.img')
    masks=' '.join('systemd.mask='+name+'.service' for name in ['lab-agent','lab-guard','lab-shell','lab-ready','lab-workload'])
    command=qemu_command(folder,same_port=True,kernel=build_dir/'vmlinuz',initramfs=image,
        extra_kernel_args='root=/dev/mapper/labrescue-ubuntu ro nompath ram_rescue_guard=1 '+masks)
    command[command.index('-m')+1]='3072'
    command[command.index('-append')+1]=command[command.index('-append')+1].replace('rdinit=/init ','').replace('panic=-1 ','')
    for index,word in enumerate(command):
        if word=='-blockdev':
            value=json.loads(command[index+1])
            if value['node-name']=='usbdisk':
                value.update(driver='qcow2',file={'driver':'file','filename':str(overlay)},
                    backing={'driver':'raw','read-only':True,'file':{'driver':'file','filename':str(disk)}})
                command[index+1]=json.dumps(value)
    command+=['-blockdev',json.dumps({'driver':'raw','node-name':'efidisk',
        'file':{'driver':'file','filename':str(folder/'efi.raw')}}),
        '-device','usb-storage,bus=xhci.0,port=2,id=efistick,drive=efidisk,serial=EFI-VM-ONLY']
    report={'schema':1,'scope':'native EFI path-triggered mount',
        'rule_renderer_sha256':RENDERER_SHA256,
        'build':build,'runner_sha256':boot.sha256(Path(__file__)),'options':OPTIONS,
        'fsck_dropin':fsck_dropin,'udev_rule':rule,'path_unit':path_unit,'command':command,'seed_report':str(seed_path),'source_image_sha256_before':before_hash,'passed':False}
    (folder/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    print('EFI native mount VM:',folder,flush=True)
    qmp=None
    with (folder/'qemu.log').open('w') as log,(folder/'qmp.jsonl').open('w') as qlog:
        vm=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
        try:
            wait_for(lambda:(folder/'qmp.sock').exists(),10,'QMP socket');qmp=Channel(folder/'qmp.sock',qlog,qmp=True)
            def booted():
                text=(folder/'console.log').read_text(errors='replace') if (folder/'console.log').exists() else ''
                if vm.poll() is not None or 'Kernel panic' in text or 'RAM Guard protected boot stopped:' in text:
                    raise RuntimeError('Protected Ubuntu boot failed')
                return 'VM_HOST_BOOT_SHELL_READY' in text
            wait_for(booted,180,'Ubuntu serial observer')
            report['boot']=boot.ram_call(folder,'snapshot')
            report['configure']=ram_call(folder,'configure')
            mount='boot-efi.mount'
            expected=report['configure']['expected_sha256']
            def mounted(snapshot):return snapshot['units'][mount]['ActiveState']=='active'
            def correct(result):return result['returncode']==0 and result['stdout'].strip()==expected
            report['initial_read']=ram_call(folder,'read')
            def remove(wait_unmounted=True):
                started=time.monotonic();qmp.call('device_del',id='efistick')
                wait_for(lambda:any(e['host_time']>=started and e['message'].get('event')=='DEVICE_DELETED' and
                    e['message'].get('data',{}).get('device')=='efistick' for e in qmp.events),10,'EFI USB deletion')
                if not wait_unmounted:return {'deleted_host_time':time.monotonic()}
                return wait_for(lambda:s if not (s:=ram_call(folder,'snapshot'))['device'] and not mounted(s) else None,15,'systemd device removal')
            def add():
                qmp.call('device_add',driver='usb-storage',bus='xhci.0',port='2',id='efistick',drive='efidisk',serial='EFI-VM-ONLY')
                return wait_for(lambda:s if (s:=ram_call(folder,'snapshot'))['device'] and mounted(s) else None,25,'udev mount after EFI USB re-enumeration')
            report['before_mounted_removal']=ram_call(folder,'snapshot')
            report['absent_mounted']=remove()
            report['absent_read']=ram_call(folder,'read')
            qmp.call('device_add',driver='usb-storage',bus='xhci.0',port='3',id='efi-decoy',drive='decoydisk',serial='EFI-DECOY')
            time.sleep(.3)
            report['reattached_mounted']=add();report['recovered_mounted']=ram_call(folder,'read')
            report['second_absent']=remove(wait_unmounted=False);time.sleep(.2);report['second_reattached']=add()
            report['second_recovered']=ram_call(folder,'read')
            report['delay_umount']=ram_call(folder,'delay_umount')
            report['joint_before']=boot.ram_call(folder,'snapshot')
            joint_start=time.monotonic()
            qmp.call('device_del',id='stick')
            report['joint_deleted']=remove(wait_unmounted=False)
            wait_for(lambda:any(e['host_time']>=joint_start and e['message'].get('event')=='DEVICE_DELETED' and
                e['message'].get('data',{}).get('device')=='stick' for e in qmp.events),10,'root USB deletion')
            time.sleep(.2)
            qmp.call('device_add',driver='usb-storage',bus='xhci.0',port='2',id='efistick',drive='efidisk',serial='EFI-VM-ONLY')
            report['joint_efi_added_host_time']=time.monotonic()
            time.sleep(1)
            qmp.call('device_add',driver='usb-uas',bus='xhci.0',port='1',id='stick',serial='RAMRESCUE-LAB-001',attached=False)
            qmp.call('device_add',driver='scsi-hd',bus='stick.0',id='lun',drive='usbdisk')
            qmp.call('qom-set',path='/machine/peripheral/stick',property='attached',value=True)
            report['joint_root_added_host_time']=time.monotonic()
            report['joint_root_recovered']=wait_for(lambda:s if (s:=boot.ram_call(folder,'snapshot'))['guard_ready'] and
                s['state'].get('recoveries',0)>=1 else None,25,'protected root recovery')
            report['joint_efi_recovered']=wait_for(lambda:s if mounted(s:=ram_call(folder,'snapshot')) else None,25,'EFI mount after root recovery')
            report['joint_read']=ram_call(folder,'read')
            report['repeat_start']=ram_call(folder,'repeat_start')
            report['failure_limit']=ram_call(folder,'failure_limit')
            report['stop']=ram_call(folder,'stop')
            report['checks']={'production_guard_ready':report['boot']['guard_ready'],
                'initial_plain_mount':mounted(report['configure']['snapshot']),
                'initial_read_hash':correct(report['initial_read']),
                'absent_access_bounded':report['absent_read']['returncode']!=0 and report['absent_read']['elapsed']<2,
                'mounted_disconnect_unmounted':not mounted(report['absent_mounted']),
                'new_device_name':report['before_mounted_removal']['device']!=report['reattached_mounted']['device'],
                'mounted_disconnect_reconnect_hash':correct(report['recovered_mounted']),
                'second_reconnect_hash':correct(report['second_recovered']),
                'joint_reconnect_hash':correct(report['joint_read']),
                'mounted_repeat_start_keeps_mount':report['repeat_start']['before']['mounts']==report['repeat_start']['after']['mounts'] and correct(report['repeat_start']['read']),
                'failure_limit_stops_retries':report['failure_limit']['failed']['units']['ram-rescue-efi.path']['ActiveState']=='failed' and
                    'limit' in report['failure_limit']['failed']['units']['ram-rescue-efi.path']['Result'] and
                    fsck_start(report['failure_limit']['failed'])==fsck_start(report['failure_limit']['quiet']),
                'failure_limit_manual_mount_and_reset_recovers':correct(report['failure_limit']['read']) and
                    report['failure_limit']['restored']['units']['ram-rescue-efi.path']['ActiveState']=='active',
                'joint_root_guard_ready':report['joint_root_recovered']['guard_ready'],
                'enrolled_link_matches_new_device':report['joint_efi_recovered']['enrolled_link']==report['joint_efi_recovered']['device'],
                'path_watch_active':report['joint_efi_recovered']['units']['ram-rescue-efi.path']['ActiveState']=='active',
                'efi_arrival_overlapped_old_unmount':arrival_during_unmount(report['joint_efi_recovered']),
                'fsck_reran_every_reconnect':all(a<b for a,b in zip(
                    [fsck_start(report[key]['snapshot'] if key=='configure' else report[key]) for key in
                        ['configure','reattached_mounted','second_reattached']],
                    [fsck_start(report[key]) for key in
                        ['reattached_mounted','second_reattached','joint_efi_recovered']])),
                'joint_same_boot_pid1':report['joint_before']['boot_id']==report['joint_root_recovered']['boot_id'] and
                    report['joint_before']['pid1']==report['joint_root_recovered']['pid1'],
                'no_autofs_mount':all(' - vfat ' in line for line in report['second_recovered']['snapshot']['mounts']),
                'clean_final_unmount':not mounted(report['stop']['snapshot'])}
            # Maintenance stop was checked above. Re-enable the actual watcher
            # before poweroff to validate native shutdown ordering as well.
            report['pre_shutdown']=ram_call(folder,'restart_path')
            report['checks']['watcher_and_mount_active_before_shutdown']=(mounted(report['pre_shutdown']) and
                report['pre_shutdown']['units']['ram-rescue-efi.path']['ActiveState']=='active')
            report['shutdown']=boot.ram_call(folder,'shutdown')
            try:vm.wait(timeout=150);report['checks']['graceful_shutdown']=vm.returncode==0
            except subprocess.TimeoutExpired:report['checks']['graceful_shutdown']=False
            report['passed']=all(report['checks'].values())
        except BaseException:
            report['error']=traceback.format_exc()
            try:
                report['failure_snapshot']=ram_call(folder,'snapshot')
                report['failure_journal']=ram_call(folder,'journal')
            except Exception:pass
            raise
        finally:
            if vm.poll() is None:
                vm.terminate()
                try:vm.wait(timeout=10)
                except subprocess.TimeoutExpired:vm.kill();vm.wait()
            if qmp:qmp.close()
            report['source_image_sha256_after']=boot.sha256(disk)
            report['source_image_unchanged']=report['source_image_sha256_after']==before_hash
            report['renderer_unchanged']=boot.sha256(RENDERER)==RENDERER_SHA256
            with (folder/'efi.raw').open('rb') as source,(folder/'efi-partition.raw').open('xb') as target:
                source.seek(2048*512);target.write(source.read(131072*512))
            fat=subprocess.run(['fsck.fat','-n',str(folder/'efi-partition.raw')],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            report['fat_readonly_audit']={'returncode':fat.returncode,'output':fat.stdout}
            report['passed']=(report['passed'] and report['source_image_unchanged'] and
                report['renderer_unchanged'] and fat.returncode==0)
            (folder/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            print('EFI native mount report:',folder/'report.json',flush=True)
    if not report['passed']:raise SystemExit('EFI native mount acceptance failed')


if __name__=='__main__':main()
