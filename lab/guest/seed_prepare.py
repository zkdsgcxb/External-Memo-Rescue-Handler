#!/usr/bin/python3
"""Prepare a fresh USB/LVM Ubuntu seed. This program may run only in QEMU."""
import json
import os
from pathlib import Path
import subprocess
import sys
import termios
import time
import traceback
import tty

sys.path.insert(0, '/opt/seed/guard')
sys.path.insert(0, '/opt/seed')
from admin.admission import layout
from admin.dm import DeviceMapper
from rescue import rows


def run(*arguments, **kwargs):
    try:
        return subprocess.check_output(arguments, text=True, stderr=subprocess.STDOUT,
                                       timeout=240, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'{arguments}: {exc.output[-6000:]}') from exc


def emit_result(result):
    # ttyS0 also carries asynchronous kernel printk messages. Keep the
    # machine-readable result on its own serial port, including during shutdown.
    with open('/dev/ttyS1', 'w') as serial:
        tty.setraw(serial.fileno(), when=termios.TCSANOW)
        serial.write(json.dumps(result) + '\n')
        serial.flush()
        termios.tcdrain(serial.fileno())


def prepare():
    flags = Path('/proc/cmdline').read_text().split()
    if ('ram_rescue_lab=1' not in flags or 'ram_rescue_seed_create=1' not in flags or
            Path('/sys/class/dmi/id/product_name').read_text().strip() != 'RAMRescueLab'):
        raise RuntimeError('Disposable seed creation requires both explicit VM gates')
    if not os.uname().release.startswith('7.0.'):
        raise RuntimeError('Only the current Linux 7.0 baseline is supported')
    if DeviceMapper().target_version('multipath') < (1, 15, 0):
        raise RuntimeError('Current DM multipath probe interface is required')
    deadline = time.monotonic() + 30
    while not Path('/dev/sda').exists() or not Path('/dev/vda').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Disposable USB disk or read-only Ubuntu source absent')
        time.sleep(.1)
    disk = Path('/sys/class/block/sda')
    usb = next(parent for parent in disk.resolve().parents if (parent/'idVendor').exists())
    if ((usb/'serial').read_text().strip() != 'RAMRESCUE-LAB-001' or
            int((disk/'size').read_text()) != 8 * 1024**3 // 512 or
            Path('/sys/class/block/vda/ro').read_text().strip() != '1'):
        raise RuntimeError('Disposable disk capacity/identity or read-only source differs')
    run('/sbin/sfdisk', '/dev/sda', input='label: dos\n, ,8e\n')
    deadline = time.monotonic() + 10
    while not Path('/dev/sda1').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Disposable partition did not appear')
        time.sleep(.1)
    node = '/dev/sda1'
    run('/sbin/lvm', 'pvcreate', '--devices', node, node)
    run('/sbin/lvm', 'vgcreate', '--devices', node, 'labrescue', node)
    for name, size in [('ubuntu', '6144M'), ('shared', '256M')]:
        run('/sbin/lvm', 'lvcreate', '--devices', node, '-L', size, '-n', name,
            'labrescue', '--zero', 'n', '--wipesignatures', 'n')
        run('/sbin/mkfs.ext4', '-F', '-E', 'lazy_itable_init=0,lazy_journal_init=0',
            '/dev/labrescue/' + name)
    root = Path('/newroot')
    run('/bin/mount', '/dev/labrescue/ubuntu', str(root))
    run('/usr/bin/tar', '--xattrs', '--xattrs-include=*', '--acls', '--numeric-owner',
        '-xJf', '/dev/vda', '-C', str(root),
        env={**os.environ, 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'})
    if not (root/'usr/lib/systemd/systemd').is_file():
        raise RuntimeError('Signed source is not a full Ubuntu systemd root')
    (root/'etc/fstab').write_text('/dev/mapper/labrescue-ubuntu / ext4 defaults,errors=remount-ro 0 0\n'
                                '/dev/mapper/labrescue-shared /shared ext4 defaults 0 0\n')
    (root/'shared').mkdir(exist_ok=True)
    (root/'etc/hostname').write_text('ram-rescue-disposable\n')
    (root/'etc/machine-id').write_text('')
    (root/'etc/cloud/cloud-init.disabled').touch()
    units = root/'etc/systemd/system'
    for name in ('systemd-networkd-wait-online.service', 'multipathd.service', 'multipathd.socket',
                 'lvm2-monitor.service', 'lvm2-pvscan@.service', 'serial-getty@ttyS1.service',
                 'serial-getty@ttyS2.service'):
        path = units/name
        path.unlink(missing_ok=True)
        path.symlink_to('/dev/null')
    sentinel = {'schema': 1, 'source': 'clean-checkout-seed', 'token': os.urandom(16).hex()}
    (root/'root/boot-evolution-sentinel.json').write_text(json.dumps(sentinel)+'\n')
    props = dict(line.split('=', 1) for line in run('/sbin/blkid', '-p', '-o', 'export', node).splitlines()
                 if '=' in line)
    pv = rows(run('/sbin/lvm', 'pvs', '--readonly', '--devices', node, '--reportformat', 'json',
                  '-o', 'pv_uuid,vg_uuid,vg_name'), 'pv')[0]
    lvs = {}
    for path in Path('/sys/class/block').glob('dm-*'):
        name = (path/'dm/name').read_text().strip().removeprefix('labrescue-')
        if name in ('ubuntu', 'shared'):
            lvs[name] = {'dm_uuid': (path/'dm/uuid').read_text().strip()}
    if set(lvs) != {'ubuntu', 'shared'}:
        raise RuntimeError('Disposable LV creation is incomplete')
    identity = {'vid': (usb/'idVendor').read_text().strip(), 'pid': (usb/'idProduct').read_text().strip(),
                'usb_serial': 'RAMRESCUE-LAB-001', 'sectors': int((disk/'size').read_text()),
                'partition_number': 1, 'partuuid': props['PART_ENTRY_UUID'], 'pv_uuid': props['UUID'],
                'vg_name': 'labrescue', 'vg_uuid': pv['vg_uuid'].strip().replace('-', ''), 'lvs': lvs}
    config = {'schema': 1, 'profile': 'host', 'kernel_release': os.uname().release,
              'map_name': 'ram-rescue-path', 'map_uuid': 'RAMRESCUE-HOST-VMTEST',
              'run_dir': '/run/ram-rescue-guard/state', 'identity_path': '/etc/rescue/identity.json',
              'queue_seconds': 8, 'partition_sectors': int(Path('/sys/class/block/sda1/size').read_text()),
              'logical_block_size': int((disk/'queue/logical_block_size').read_text()),
              'layout': layout(node), 'root_lv': 'ubuntu',
              'root_fs_uuid': run('/sbin/blkid', '-s', 'UUID', '-o', 'value', '/dev/labrescue/ubuntu').strip()}
    run('/bin/sync')
    run('/bin/umount', str(root))
    run('/sbin/lvm', 'vgchange', '--devices', node, '-an', 'labrescue')
    run('/bin/sync')
    return {'schema': 1, 'identity': identity, 'guard': config, 'sentinel': sentinel,
            'full_ubuntu_extracted': True, 'unmounted_cleanly': True}


if __name__ == '__main__':
    try:
        value = prepare()
        result = {'ok': True, 'value': value}
    except BaseException:
        result = {'ok': False, 'error': traceback.format_exc()}
    try:
        emit_result(result)
    except BaseException:
        traceback.print_exc()
    subprocess.run(['/bin/poweroff', '-f'], check=False)
    while True:
        time.sleep(60)
