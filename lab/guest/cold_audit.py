#!/usr/bin/python3
"""Cold-read a disposable experiment's ACK prefix, never initialize its disk."""
import hashlib
import json
from pathlib import Path
import time
import traceback

from agent import guard, run

guard()
args=dict(word.split('=',1) for word in Path('/proc/cmdline').read_text().split() if '=' in word)
assert args['ram_rescue_cold_audit']=='1'
try:
    expected_bytes=int(args['ram_rescue_audit_bytes'])
    expected_sha=args['ram_rescue_audit_sha']
    if not 0<expected_bytes<=64*1024*1024 or len(expected_sha)!=64:
        raise ValueError('Invalid bounded ACK prefix')
    deadline=time.monotonic()+20
    while not Path('/dev/sda1').exists():
        if time.monotonic()>=deadline:
            raise RuntimeError('Audit partition absent')
        time.sleep(.1)
    disk=Path('/sys/class/block/sda').resolve()
    usb=next(parent for parent in disk.parents if (parent/'serial').exists())
    if (usb/'serial').read_text().strip()!='RAMRESCUE-LAB-001':
        raise RuntimeError('Audit disk marker differs')
    run('/sbin/lvm','vgchange','-ay','--devices','/dev/sda1','labrescue')
    # A fresh disposable qcow2 overlay permits journal replay; the evidence
    # image remains a backing file. New kernel, no old guest page cache.
    run('/bin/mount','-o','ro','/dev/labrescue/ubuntu','/newroot')
    with open('/newroot/root/workload.data','rb') as data:
        actual=data.read(expected_bytes)
    actual_sha=hashlib.sha256(actual).hexdigest()
    answer={'passed':len(actual)==expected_bytes and actual_sha==expected_sha,
            'expected_bytes':expected_bytes,'read_bytes':len(actual),
            'expected_sha256':expected_sha,'actual_sha256':actual_sha,
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'scope':'cold guest ACK prefix with ext4 journal replay on disposable overlay'}
except BaseException:
    answer={'passed':False,'error':traceback.format_exc()}
print('COLD_AUDIT_RESULT='+json.dumps(answer),flush=True)
while True:
    time.sleep(60)
