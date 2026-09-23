#!/usr/bin/python3
"""VM-only serial control channel. Never execute this on the host."""
import contextlib
import io
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

sys.path.insert(0, '/opt/lab')


def guard():
    if 'ram_rescue_lab=1' not in Path('/proc/cmdline').read_text().split():
        raise SystemExit('Refusing: not booted as disposable RAM rescue lab')
    if Path('/sys/class/dmi/id/product_name').read_text().strip() != 'RAMRescueLab':
        raise SystemExit('Refusing: missing QEMU lab DMI marker')


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=30, **kwargs)


def setup():
    from rescue import rows
    deadline = time.monotonic()+30
    while not Path('/dev/sda').exists():
        if time.monotonic()>deadline:
            raise RuntimeError('USB disk did not appear')
        time.sleep(0.1)
    disk = Path('/sys/class/block/sda')
    usb = next(p for p in disk.resolve().parents if (p/'idVendor').exists())
    if (usb/'serial').read_text().strip() != 'RAMRESCUE-LAB-001':
        raise RuntimeError('Refusing to format a non-lab disk')
    run('/sbin/sfdisk', '/dev/sda', input='label: dos\n, ,8e\n')
    for _ in range(50):
        if Path('/dev/sda1').exists():
            break
        time.sleep(0.1)
    run('/sbin/lvm', 'pvcreate', '/dev/sda1')
    run('/sbin/lvm', 'vgcreate', 'labrescue', '/dev/sda1')
    for name, size in [('ubuntu', '1024M'), ('shared', '256M')]:
        run('/sbin/lvm', 'lvcreate', '-L', size, '-n', name, 'labrescue', '--zero', 'n', '--wipesignatures', 'n')
        run('/sbin/mkfs.ext4', '-F', '-E', 'lazy_itable_init=0,lazy_journal_init=0', '/dev/labrescue/'+name)
    run('/bin/mount', '-o', 'errors=remount-ro', '/dev/labrescue/ubuntu', '/newroot')
    for item in ['bin', 'sbin', 'usr', 'lib', 'lib64', 'etc', 'opt']:
        if Path('/'+item).exists():
            run('/bin/cp', '-a', '/'+item, '/newroot/')
    for item in ['dev','proc','sys','run','tmp','root','shared','var/log']:
        Path('/newroot/'+item).mkdir(parents=True, exist_ok=True)
    run('/bin/mount', '-o', 'errors=remount-ro', '/dev/labrescue/shared', '/newroot/shared')
    (Path('/newroot/root')/'sentinel').write_bytes(b'RAM rescue lab sentinel\n')
    run('/bin/sync')
    props = dict(line.split('=',1) for line in run('/sbin/blkid','-p','-o','export','/dev/sda1').splitlines() if '=' in line)
    pv = rows(run('/sbin/lvm','pvs','--reportformat','json','-o','pv_uuid,vg_uuid,vg_name'), 'pv')[0]
    lvs = {}
    for path in Path('/sys/class/block').glob('dm-*'):
        name = (path/'dm/name').read_text().strip().removeprefix('labrescue-')
        lvs[name] = {'dm_uuid': (path/'dm/uuid').read_text().strip()}
    config = {'vid': (usb/'idVendor').read_text().strip(), 'pid': (usb/'idProduct').read_text().strip(),
              'usb_serial': 'RAMRESCUE-LAB-001', 'sectors': int((disk/'size').read_text()),
              'partition_number': 1, 'partuuid': props['PART_ENTRY_UUID'], 'pv_uuid': props['UUID'],
              'vg_name': 'labrescue', 'vg_uuid': pv['vg_uuid'].strip().replace('-',''), 'lvs': lvs}
    Path('/etc/rescue/identity.json').write_text(json.dumps(config))
    print('LAB_SETUP_COMPLETE', flush=True)


def serve():
    from rescue import Recovery, command
    import termios
    import tty
    serial = os.open('/dev/ttyS1', os.O_RDWR | os.O_NOCTTY)
    tty.setraw(serial, termios.TCSANOW)
    stream = os.fdopen(serial, 'r+b', buffering=0)
    lock = threading.Lock()
    def send(value):
        with lock:
            stream.write((json.dumps(value)+'\n').encode())
    def heartbeat():
        while True:
            send({'event':'heartbeat','uptime':time.monotonic()})
            time.sleep(0.5)
    threading.Thread(target=heartbeat, daemon=True).start()
    def traced_command(args, timeout=12):
        entry = {'args':args, 'time':time.monotonic()}
        try:
            entry['output'] = command(args, timeout=timeout)
            return entry['output']
        except Exception as exc:
            entry['error'] = str(exc)
            raise
        finally:
            with open('/run/helper-commands.jsonl','a') as log:
                log.write(json.dumps(entry)+'\n')
    recovery = Recovery(json.loads(Path('/etc/rescue/identity.json').read_text()), runner=traced_command)
    worker = None
    def snapshot():
        return {'guest_time':time.monotonic(), 'pid1_mounts': Path('/proc/1/mountinfo').read_text(),
                'workload_process': {'pid':worker.pid, 'exit_code':worker.poll(),
                    'start_ticks':Path('/proc/'+str(worker.pid)+'/stat').read_text().split()[21]
                        if Path('/proc/'+str(worker.pid)+'/stat').exists() else None} if worker else None,
                'dm_table': run('/sbin/dmsetup','table'),
                'dm_status': run('/sbin/dmsetup','status'),
                'dm_info': run('/sbin/dmsetup','info','-c'),
                'identity': recovery.c,
                'helper_commands': Path('/run/helper-commands.jsonl').read_text() if Path('/run/helper-commands.jsonl').exists() else '',
                'disks': [p.name for p in recovery.candidates()],
                'mappings': {name: {'node': recovery.mapping(name).name,
                    'slaves': [p.name for p in (recovery.mapping(name)/'slaves').iterdir()]}
                    for name in recovery.c['lvs']},
                'workload': Path('/run/workload.jsonl').read_text() if Path('/run/workload.jsonl').exists() else '',
                'kernel': run('/bin/dmesg')}
    while line := stream.readline():
        try:
            request = json.loads(line)
        except ValueError:
            continue  # Discard UART startup noise before raw mode is established.
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                action = request['action']
                if action == 'snapshot':
                    result = snapshot()
                elif action == 'verify':
                    result = recovery.verify()
                elif action == 'refresh':
                    # Explicit host experiment request authorizes only this disposable LV.
                    result = recovery.refresh(request.get('target','ubuntu'), confirm=lambda _: 'REFRESH labrescue/'+request.get('target','ubuntu'))
                elif action == 'workload':
                    if worker is None or worker.poll() is not None:
                        worker = subprocess.Popen(['/bin/chroot','/proc/1/root','/usr/bin/python3','/opt/lab/workload.py'],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    result = worker.pid
                elif action in ['suspend','resume']:
                    # Best-case interception experiment: only guest lab LVs, no host devices.
                    result=[]
                    for target in ['ubuntu','shared']:
                        node='/dev/'+recovery.mapping(target).name
                        result.append(run('/sbin/dmsetup','--noudevsync','--nolockfs','--noflush',action,node))
                elif action == 'probe':
                    # Evict clean cached file data so the read checks the mapping, not RAM.
                    Path('/proc/sys/vm/drop_caches').write_text('3\n')
                    result = run('/bin/chroot','/proc/1/root','/bin/cat','/root/sentinel')
                elif action == 'block_probe':
                    node = '/dev/' + recovery.mapping('ubuntu').name
                    fd = os.open(node, os.O_RDONLY | os.O_DIRECT)
                    try:
                        with mmap.mmap(-1,4096) as buffer:
                            count = os.preadv(fd,[buffer],0)
                            result = {'bytes':count, 'ext4_magic':bytes(buffer[1080:1082]).hex()}
                    finally:
                        os.close(fd)
                elif action == 'filesystem_state':
                    root='/proc/1/root'
                    result={'statvfs_readonly':bool(os.statvfs(root).f_flag & os.ST_RDONLY)}
                    try:
                        with open(root+'/root/post-recovery-write','wb',buffering=0) as out:
                            out.write(b'post-recovery write probe\n')
                            os.fsync(out.fileno())
                        result['write_fsync_ok']=True
                    except OSError as exc:
                        result.update(write_fsync_ok=False,errno=exc.errno,error=str(exc))
                else:
                    raise ValueError('Unknown action')
            send({'id':request['id'], 'ok':True, 'result':result, 'output':output.getvalue()})
        except Exception as exc:
            send({'id':request['id'], 'ok':False, 'error':str(exc), 'output':output.getvalue(),
                  'command_output':getattr(exc,'output',None), 'traceback':traceback.format_exc()})


if __name__ == '__main__':
    guard()
    if sys.argv[1:] == ['--setup']:
        setup()
    else:
        serve()
