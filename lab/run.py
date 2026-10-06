#!/usr/bin/env python3
"""Shared QEMU transport helpers; manual recovery runs use the frozen release."""
import json
from pathlib import Path
import queue
import socket
import threading
import time

from guest.wire import decode

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
                    message = decode(json.loads(line))
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


def qemu_command(run_dir, transport='uas', tcg=False, extra_kernel_args='', ubuntu=False, git_source=False, same_port=False,
                 kernel=None, initramfs=None):
    command = ['qemu-system-x86_64','-machine','q35','-accel','tcg' if tcg else 'kvm',
               '-m','3072' if ubuntu else '1536','-smp','2','-display','none','-nodefaults','-no-reboot','-nic','none',
               '-smbios','type=1,product=RAMRescueLab',
               '-kernel',str(kernel or WORK/'vmlinuz'),'-initrd',str(initramfs or WORK/'initramfs.cpio.gz'),
               '-append','console=ttyS0 rdinit=/init ram_rescue_lab=1 panic=-1 '+extra_kernel_args,
               '-serial','file:'+str(run_dir/'console.log'),
               '-serial','unix:'+str(run_dir/'agent.sock')+',server=on,wait=off',
               '-serial','unix:'+str(run_dir/'rescue.sock')+',server=on,wait=off',
               '-qmp','unix:'+str(run_dir/'qmp.sock')+',server=on,wait=off',
               '-device','qemu-xhci,id=xhci']
    for name,node in [('usb.raw','usbdisk'),('decoy.raw','decoydisk')]:
        command += ['-blockdev',json.dumps({'driver':'raw','node-name':node,
                    'file':{'driver':'file','filename':str(run_dir/name)}})]
    port = ',port=1' if same_port else ''
    if transport == 'uas':
        command += ['-device','usb-uas,bus=xhci.0,id=stick,serial=RAMRESCUE-LAB-001'+port,
                    '-device','scsi-hd,bus=stick.0,id=lun,drive=usbdisk']
    else:
        command += ['-device','usb-storage,bus=xhci.0,id=stick,drive=usbdisk,serial=RAMRESCUE-LAB-001'+port]
    if ubuntu:
        command += ['-blockdev',json.dumps({'driver':'raw','node-name':'ubuntu-seed',
            'read-only':True,'file':{'driver':'file','filename':str(WORK/'ubuntu/rootfs.raw')}}),
            '-device','virtio-blk-pci,drive=ubuntu-seed,serial=UBUNTU-ROOTFS-SEED']
    if git_source:
        command += ['-blockdev',json.dumps({'driver':'raw','node-name':'git-seed',
            'read-only':True,'file':{'driver':'file','filename':str(WORK/'git-source.raw')}}),
            '-device','virtio-blk-pci,drive=git-seed,serial=LINUX-GIT-SOURCE']
    return command


if __name__ == '__main__':
    from historical import run_legacy
    raise SystemExit(run_legacy(__file__))
