#!/usr/bin/env python3
"""Run a disposable USB/LVM root-disk experiment. No host block devices attached."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time

BASE = Path(__file__).resolve().parent
WORK = BASE / 'work'


def shell_probe(path):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(b"printf '\\nRAM_%s\\n' SHELL_ALIVE\n")
        output=b''
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            chunk=sock.recv(4096)
            if not chunk:
                return False
            output+=chunk
            if b'\nRAM_SHELL_ALIVE' in output:
                return True
        return False


class Channel:
    def __init__(self, path, log, qmp=False):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(path))
        self.stream = self.socket.makefile('rwb', buffering=0)
        self.messages = queue.Queue()
        self.events = []
        self.log = log
        self.qmp = qmp
        self.counter = 0
        self.thread = threading.Thread(target=self.receive, daemon=True)
        self.thread.start()
        if qmp:
            self.call('qmp_capabilities')

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(timeout=2)
        self.stream.close()
        self.socket.close()

    def receive(self):
        try:
            for line in self.stream:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                entry = {'host_time':time.monotonic(), 'message':message}
                self.events.append(entry)
                self.log.write(json.dumps(entry)+'\n')
                self.log.flush()
                self.messages.put(message)
        except OSError:
            pass
        finally:
            self.messages.put({'closed':True})

    def call(self, action, timeout=45, **kwargs):
        self.counter += 1
        message = {'id':self.counter}
        if self.qmp:
            message.update(execute=action, arguments=kwargs)
        else:
            message.update(action=action, **kwargs)
        self.stream.write((json.dumps(message)+'\n').encode())
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                response = self.messages.get(timeout=max(0.01,deadline-time.monotonic()))
            except queue.Empty as exc:
                raise TimeoutError('VM request timed out: '+action) from exc
            if response.get('closed'):
                raise ConnectionError('VM control channel closed')
            if response.get('id') == self.counter:
                if self.qmp and 'error' in response:
                    raise RuntimeError(response)
                return response
        raise TimeoutError(action)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=['baseline','idle','write','queued-write'], default='idle')
    parser.add_argument('--gap', type=float, default=0.2, help='Seconds between confirmed removal and reattachment')
    parser.add_argument('--settle', type=float, default=3, help='Observation seconds after refresh')
    parser.add_argument('--tcg', action='store_true', help='Software emulation instead of KVM')
    parser.add_argument('--transport', choices=['bot','uas'], default='uas')
    args = parser.parse_args()
    if os.geteuid()==0:
        parser.error('Run as a normal user; this lab requires no host root privileges')
    if not 0 <= args.gap <= 60 or not 0 < args.settle <= 60:
        parser.error('gap must be 0..60; settle must be >0..60')
    for name in ['vmlinuz','initramfs.cpio.gz']:
        if not (WORK/name).is_file():
            parser.error('Run lab/build.py first')
    run_dir = WORK / (time.strftime('%Y%m%d-%H%M%S')+'-'+args.scenario+'-'+str(os.getpid()))
    run_dir.mkdir(mode=0o700)
    for name,size in [('usb.raw',2*1024**3),('decoy.raw',16*1024**2)]:
        with (run_dir/name).open('xb') as stream:
            stream.truncate(size)
    command = ['qemu-system-x86_64','-machine','q35','-accel','tcg' if args.tcg else 'kvm',
               '-m','1536','-smp','2','-display','none','-nodefaults','-no-reboot','-nic','none',
               '-smbios','type=1,product=RAMRescueLab',
               '-kernel',str(WORK/'vmlinuz'),'-initrd',str(WORK/'initramfs.cpio.gz'),
               '-append','console=ttyS0 rdinit=/init ram_rescue_lab=1 panic=-1',
               '-serial','file:'+str(run_dir/'console.log'),
               '-serial','unix:'+str(run_dir/'agent.sock')+',server=on,wait=off',
               '-serial','unix:'+str(run_dir/'rescue.sock')+',server=on,wait=off',
               '-qmp','unix:'+str(run_dir/'qmp.sock')+',server=on,wait=off',
               '-device','qemu-xhci,id=xhci']
    for name,node in [('usb.raw','usbdisk'),('decoy.raw','decoydisk')]:
        command += ['-blockdev',json.dumps({'driver':'raw','node-name':node,
                    'file':{'driver':'file','filename':str(run_dir/name)}})]
    if args.transport == 'uas':
        command += ['-device','usb-uas,bus=xhci.0,id=stick,serial=RAMRESCUE-LAB-001',
                    '-device','scsi-hd,bus=stick.0,id=lun,drive=usbdisk']
    else:
        command += ['-device','usb-storage,bus=xhci.0,id=stick,drive=usbdisk,serial=RAMRESCUE-LAB-001']
    (run_dir/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    report = {'scenario':args.scenario, 'transport':args.transport, 'requested_gap_seconds':args.gap,
              'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'qemu_version':subprocess.check_output(['qemu-system-x86_64','--version'],text=True).splitlines()[0],
              'build':json.loads((WORK/'build.json').read_text()), 'snapshots':{}}
    print('Experiment:',run_dir,flush=True)
    with (run_dir/'qemu.log').open('w') as qlog, (run_dir/'qmp.jsonl').open('w') as qmp_log, (run_dir/'agent.jsonl').open('w') as agent_log:
        vm = subprocess.Popen(command, stdout=qlog, stderr=subprocess.STDOUT)
        channels=[]
        try:
            deadline=time.monotonic()+30
            while not (run_dir/'agent.sock').exists():
                if vm.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('QEMU failed to start; see qemu.log')
                time.sleep(0.1)
            qmp = Channel(run_dir/'qmp.sock',qmp_log,qmp=True)
            channels.append(qmp)
            deadline=time.monotonic()+120
            while True:
                console=(run_dir/'console.log').read_text(errors='replace')
                if 'LAB_ROOT_READY:' in console:
                    break
                if 'Kernel panic' in console or vm.poll() is not None or time.monotonic()>deadline:
                    raise RuntimeError('Guest boot failed; see console.log')
                time.sleep(0.1)
            guest = Channel(run_dir/'agent.sock',agent_log)
            channels.append(guest)
            deadline=time.monotonic()+10
            while not any(e['message'].get('event')=='heartbeat' for e in guest.events):
                if time.monotonic()>deadline:
                    raise TimeoutError('RAM agent heartbeat')
                time.sleep(0.1)
            before = guest.call('snapshot',timeout=120)
            if not before.get('ok'):
                raise RuntimeError(before)
            # The agent starts just before switch_root. Wait for PID 1's disk root.
            for _ in range(50):
                mounts = before['result']['pid1_mounts'].splitlines()
                if any(line.split()[4]=='/' and ' - ext4 ' in line for line in mounts):
                    break
                time.sleep(0.1)
                before=guest.call('snapshot')
            else:
                raise RuntimeError('PID 1 did not switch to USB-backed ext4 root')
            report['snapshots']['before']=before
            report['verify_before']=guest.call('verify')
            report['probe_before']=guest.call('probe')
            report['block_before']=guest.call('block_probe')
            report['shell_before']=shell_probe(run_dir/'rescue.sock')
            if not report['verify_before']['ok'] or not report['probe_before']['ok']:
                raise RuntimeError('Initial disk verification/probe failed')
            if args.scenario in ['baseline','write','queued-write']:
                report['start_workload']=guest.call('workload')
                time.sleep(1)
            report['snapshots']['pre_fault']=guest.call('snapshot')
            if args.scenario=='queued-write':
                report['suspend']=guest.call('suspend')
                if not report['suspend']['ok']:
                    raise RuntimeError('Pre-fault suspension failed')
                report['snapshots']['suspended']=guest.call('snapshot')
            report['fault_start_host_time']=time.monotonic()
            if args.scenario!='baseline':
                qmp.call('device_del',id='stick')
                deadline=time.monotonic()+10
                while not any(e['message'].get('event')=='DEVICE_DELETED' and e['message'].get('data',{}).get('device')=='stick' for e in qmp.events):
                    if time.monotonic()>deadline:
                        raise TimeoutError('USB DEVICE_DELETED')
                    time.sleep(0.01)
                report['device_deleted_host_time']=time.monotonic()
                # Occupy a free SCSI slot to make name changes repeatable.
                qmp.call('device_add',driver='usb-storage',bus='xhci.0',id='decoy',drive='decoydisk',serial='LAB-DECOY')
                time.sleep(max(0,args.gap-(time.monotonic()-report['device_deleted_host_time'])))
                if args.transport == 'uas':
                    qmp.call('device_add',driver='usb-uas',bus='xhci.0',id='stick',serial='RAMRESCUE-LAB-001',attached=False)
                    qmp.call('device_add',driver='scsi-hd',bus='stick.0',id='lun',drive='usbdisk')
                    qmp.call('qom-set',path='/machine/peripheral/stick',property='attached',value=True)
                else:
                    qmp.call('device_add',driver='usb-storage',bus='xhci.0',id='stick',drive='usbdisk',serial='RAMRESCUE-LAB-001')
                report['device_added_host_time']=time.monotonic()
                for _ in range(100):
                    verified=guest.call('verify')
                    if verified['ok']:
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Reattached USB identity not verified')
                report['verify_after']=verified
                report['device_verified_host_time']=time.monotonic()
                report['snapshots']['reconnected']=guest.call('snapshot')
                report['shell_during_fault']=shell_probe(run_dir/'rescue.sock')
                if args.scenario!='queued-write':
                    report['probe_before_refresh']=guest.call('probe')
                    report['block_before_refresh']=guest.call('block_probe')
                for target in ['ubuntu','shared']:
                    report['refresh_'+target]=guest.call('refresh',target=target)
                if args.scenario=='queued-write':
                    report['resume']=guest.call('resume')
                report['snapshots']['after_refresh']=guest.call('snapshot')
            report['probe_after']=guest.call('probe')
            report['block_after']=guest.call('block_probe')
            time.sleep(args.settle)
            report['block_after_settle']=guest.call('block_probe')
            report['probe_after_settle']=guest.call('probe')
            report['filesystem_state']=guest.call('filesystem_state')
            report['snapshots']['after']=guest.call('snapshot')
            heartbeats=[e['host_time'] for e in guest.events if e['message'].get('event')=='heartbeat']
            report['heartbeat_count']=len(heartbeats)
            report['max_heartbeat_gap_seconds']=max((b-a for a,b in zip(heartbeats,heartbeats[1:])),default=None)
            after=report['snapshots']['after']['result']
            writes=[json.loads(line) for line in after['workload'].splitlines()]
            report['workload_successes']=sum(w['ok'] for w in writes)
            report['workload_failures']=sum(not w['ok'] for w in writes)
            report['max_write_latency_seconds']=max((w['elapsed'] for w in writes),default=None)
            if args.scenario!='baseline':
                cut=report['snapshots']['after_refresh']['result']['guest_time']
                report['writes_after_refresh']={'ok':sum(w['ok'] for w in writes if w['start']>=cut),
                    'failed':sum(not w['ok'] for w in writes if w['start']>=cut)}
                report['observed_reattach_seconds']=report['device_added_host_time']-report['device_deleted_host_time']
                report['observed_verified_seconds']=report['device_verified_host_time']-report['device_deleted_host_time']
            report['checks']={
                'ram_heartbeat':len(heartbeats)>=3 and report['max_heartbeat_gap_seconds']<2,
                'ram_shell':report['shell_before'] and report.get('shell_during_fault',True),
                'block_read_recovered':report['block_after_settle']['ok'] and report['block_after_settle'].get('result',{}).get('ext4_magic')=='53ef',
            }
            if args.scenario=='baseline':
                report['checks']['baseline_workload']=report['workload_successes']>0 and report['workload_failures']==0
            else:
                report['checks']['renumbered']=report['verify_before']['result']!=report['verify_after']['result']
                if args.scenario!='queued-write':
                    report['checks']['fault_reproduced']=not report['block_before_refresh']['ok']
                expected=Path(report['verify_after']['result']).name
                report['checks']['mapping_refreshed']=all(report['refresh_'+name]['ok'] and
                    after['mappings'][name]['slaves']==[expected] for name in ['ubuntu','shared'])
                if args.scenario=='queued-write':
                    before_worker=report['snapshots']['pre_fault']['result']['workload_process']
                    after_worker=after['workload_process']
                    report['checks']['same_process_survived']=before_worker==after_worker and after_worker['exit_code'] is None
                    report['checks']['writes_continued_without_error']=report['writes_after_refresh']['ok']>0 and report['workload_failures']==0
                    fs=report['filesystem_state']
                    report['checks']['filesystem_writable']=fs['ok'] and fs['result']['write_fsync_ok'] and not fs['result']['statvfs_readonly']
            report['recovery_checks_passed']=all(report['checks'].values())
            report['completed']=True
        except BaseException as exc:
            report.update(completed=False,error=repr(exc))
            raise
        finally:
            (run_dir/'report.json').write_text(json.dumps(report,indent=2)+'\n')
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill()
                vm.wait()
            for channel in channels:
                channel.close()
    print(json.dumps({k:report[k] for k in ['scenario','transport','completed','checks',
        'workload_successes','workload_failures','max_heartbeat_gap_seconds']},indent=2))
    print('Report:',run_dir/'report.json')
    if not report['recovery_checks_passed']:
        raise SystemExit('Recovery acceptance checks failed; inspect report.json')


if __name__=='__main__':
    main()
