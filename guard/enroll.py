#!/usr/bin/python3
"""Read an explicitly identified root disk; write a private boot profile only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from trusted_paths import open_directory, open_trusted, read_trusted_json, trusted_sha256

BASE=Path(__file__).resolve().parent
ENROLLMENTS=Path('/var/lib/ram-rescue-enrollments')
BOOT=Path('/boot')
LVM_CONFIG=Path('/etc/lvm/lvmlocal.conf')
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
from admin.admission import Admission, layout, readonly
from admin.identity import LVMIdentity
from admin.dm import DeviceMapper, expected_table, table_digest
from rescue import rows


def root_backing(identity, recovery, node, sys_path, config):
    """Accept one raw PV or its already protected, exclusive stable mapping."""
    dependencies = [
        [path.resolve() for path in (recovery.mapping(name) / 'slaves').iterdir()]
        for name in identity['lvs']
    ]
    if all(paths == [sys_path] for paths in dependencies):
        return sys_path

    flags = Path('/proc/cmdline').read_text().split()
    if not {'ram_rescue_guard=1', 'nompath'}.issubset(flags):
        raise RuntimeError('Indirect root backing requires an explicit protected boot')
    matches = []
    for mapping in Path('/sys/class/block').glob('dm-*'):
        if ((mapping / 'dm/name').read_text().strip() == config['map_name'] or
                (mapping / 'dm/uuid').read_text().strip() == config['map_uuid']):
            matches.append(mapping.resolve())
    if len(matches) != 1:
        raise RuntimeError('Protected root map must have one unique reserved name and UUID')
    stable = matches[0]
    if ((stable / 'dm/name').read_text().strip() != config['map_name'] or
            (stable / 'dm/uuid').read_text().strip() != config['map_uuid'] or
            any(paths != [stable] for paths in dependencies)):
        raise RuntimeError('Enrolled LVs do not share the expected protected root map')

    snapshot = DeviceMapper().snapshot(config['map_name'])
    info = snapshot['info']
    device = os.stat(node)
    if not stat.S_ISBLK(device.st_mode):
        raise RuntimeError('Enrolled PV must remain a block partition')
    expected = expected_table(config['partition_sectors'],
                              f'{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}')
    if (snapshot['uuid'] != config['map_uuid'] or snapshot['inactive'] or
            any(info[key] for key in ('suspended', 'internal_suspend', 'deferred_remove', 'read_only')) or
            table_digest(snapshot['active']) != table_digest(expected)):
        raise RuntimeError('Protected root map is not the ready enrolled single-path table')
    words = snapshot['active'][0][3].split()
    if 'queue_if_no_path' not in words[1:1 + int(words[0])]:
        raise RuntimeError('Protected root map must retain queue_if_no_path')
    observed = (Path('/sys/dev/block') / f"{info['major']}:{info['minor']}").resolve(strict=True)
    if (observed != stable or
            {path.resolve() for path in (stable / 'slaves').iterdir()} != {sys_path}):
        raise RuntimeError('Protected root map backing partition differs')
    return stable


def collect(identity):
    recovery=LVMIdentity(identity,runner=readonly)
    node=recovery.verify()
    sys_path=(Path('/sys/class/block')/Path(node).name).resolve(strict=True)
    enrolled_layout=layout(node)
    if not enrolled_layout or any(row['segtype']!='linear' for row in enrolled_layout):
        raise RuntimeError('Only the current linear LVM layout is supported')
    # The root mount must be one of the previously enrolled LVs on this PV.
    root_dev=subprocess.check_output(['findmnt','-nro','MAJ:MIN','-T','/'],text=True).strip()
    root_sys=(Path('/sys/dev/block')/root_dev).resolve(strict=True)
    root_uuid=(root_sys/'dm/uuid').read_text().strip()
    roots=[name for name,item in identity['lvs'].items() if item['dm_uuid']==root_uuid]
    if len(roots)!=1:
        raise RuntimeError('Current root is not the previously enrolled LV')
    config={'schema':1,'profile':'host','kernel_release':os.uname().release,
        'map_name':'ram-rescue-path',
        'map_uuid':'RAMRESCUE-HOST-'+hashlib.sha256(identity['partuuid'].encode()).hexdigest()[:24],
        'run_dir':'/run/ram-rescue-guard/state','identity_path':'/etc/rescue/identity.json',
        'queue_seconds':8,'partition_sectors':int((sys_path/'size').read_text()),
        'logical_block_size':int((sys_path.parent/'queue/logical_block_size').read_text()),
        'layout':enrolled_layout,'root_lv':roots[0],
        'root_fs_uuid':subprocess.check_output(['findmnt','-nro','UUID','-T','/'],text=True).strip()}
    backing = root_backing(identity, recovery, node, sys_path, config)
    with Admission(config,recovery).verify(time.monotonic()+15) as candidate:
        candidate.revalidate()
        if (candidate.node != node or Path(candidate.sys_path) != sys_path or
                root_backing(identity, recovery, node, sys_path, config) != backing):
            raise RuntimeError('Root backing changed during enrollment')
    return {'schema':1,'identity':identity,'guard':config}


def identify(partition, expected_serial):
    """Discover only the explicitly selected USB PV, then let collect revalidate it."""
    node = Path(partition).resolve(strict=True)
    device = node.stat()
    if not stat.S_ISBLK(device.st_mode):
        raise ValueError('partition must be an explicitly selected block device')
    entry = (Path('/sys/dev/block') / f'{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}').resolve(strict=True)
    if not (entry/'partition').is_file():
        raise ValueError('Root enrollment requires one USB LVM partition')
    disk = entry.parent
    usb = next((p for p in disk.parents if (p/'idVendor').is_file()), None)
    if usb is None or (usb/'serial').read_text().strip() != expected_serial:
        raise ValueError('Selected partition does not match the explicit USB serial')
    props = dict(line.split('=', 1) for line in
                 readonly(['/sbin/blkid', '-p', '-o', 'export', str(node)]).splitlines() if '=' in line)
    if props.get('TYPE') != 'LVM2_member':
        raise ValueError('Selected partition is not a recognized LVM PV')
    pvs = rows(readonly(['/sbin/lvm', 'pvs', '--readonly', '--devices', str(node),
                         '--reportformat', 'json', '-o', 'pv_uuid,vg_uuid,vg_name']), 'pv')
    if len(pvs) != 1 or pvs[0]['pv_uuid'].strip() != props['UUID']:
        raise ValueError('The selected PV is not uniquely identified')
    pv = pvs[0]
    vg_uuid = pv['vg_uuid'].strip().replace('-', '')
    lvs = {}
    for item in layout(str(node)):
        uuid = 'LVM-' + vg_uuid + item['lv_uuid'].replace('-', '')
        lvs[item['lv_name']] = {'dm_uuid': uuid}
    if not lvs:
        raise ValueError('Selected PV has no supported logical volumes')
    return {'vid': (usb/'idVendor').read_text().strip().lower(),
            'pid': (usb/'idProduct').read_text().strip().lower(), 'usb_serial': expected_serial,
            'sectors': int((disk/'size').read_text()),
            'partition_number': int((entry/'partition').read_text()),
            'partuuid': props['PART_ENTRY_UUID'], 'pv_uuid': props['UUID'],
            'vg_name': pv['vg_name'].strip(), 'vg_uuid': vg_uuid, 'lvs': lvs}


def enrollment_name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value) or value in ('.', '..'):
        raise argparse.ArgumentTypeError('name must be one safe directory name, at most 64 characters')
    return value


def enrollment_store():
    """Create only the fixed private store below a verified root-owned parent."""
    parent = open_directory(ENROLLMENTS.parent)
    try:
        try:
            os.mkdir(ENROLLMENTS.name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    directory = open_directory(ENROLLMENTS)
    if stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
        os.close(directory)
        raise RuntimeError('Enrollment store must be private (mode 0700)')
    return directory


def copy_boot_file(source, directory, name):
    """Read one verified boot inode and write only into the held private directory."""
    with open_trusted(source) as source_fd:
        before = os.fstat(source_fd)
        destination = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                              os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        checksum = hashlib.sha256()
        with os.fdopen(destination, 'wb') as output:
            while block := os.read(source_fd, 1024**2):
                output.write(block)
                checksum.update(block)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError('Boot input changed while copying enrollment')
        return checksum.hexdigest()


def enroll(name, identity):
    """Leave a root-private receipt/export source; never write into a user's tree."""
    name = enrollment_name(name)
    store = enrollment_store()
    directory = None
    try:
        try:
            os.stat(name, dir_fd=store, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError('Enrollment already exists; choose a new name')
        profile = collect(identity)
        release = profile['guard']['kernel_release']
        if not re.fullmatch(r'[A-Za-z0-9+_.-]+', release):
            raise RuntimeError('Invalid enrolled kernel release')
        os.mkdir(name, mode=0o700, dir_fd=store)
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=store)
        profile['baseline'] = {
            'kernel_sha256': copy_boot_file(BOOT / ('vmlinuz-' + release), directory, 'vmlinuz'),
            'initrd_sha256': copy_boot_file(BOOT / ('initrd.img-' + release), directory, 'original-initrd.img'),
            'grub_default_path': '/boot/grub/grub.cfg',
            'lvmlocal_sha256': trusted_sha256(LVM_CONFIG) if os.path.lexists(LVM_CONFIG) else None,
        }
        descriptor = os.open('enrollment.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(profile, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
        os.fsync(store)
        return {'enrollment': 'verified', 'kernel_release': release,
                'output': str(ENROLLMENTS / name), 'lvs': sorted(profile['identity']['lvs']),
                'disk_mutations': False, 'boot_changes': False,
                'export_files': ['enrollment.json', 'vmlinuz', 'original-initrd.img']}
    finally:
        if directory is not None:
            os.close(directory)
        os.close(store)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name',type=enrollment_name,required=True,
                        help='New private record under /var/lib/ram-rescue-enrollments')
    identity_source=parser.add_mutually_exclusive_group(required=True)
    identity_source.add_argument('--identity',type=Path,help='Explicit USB/LVM identity JSON')
    identity_source.add_argument('--partition',type=Path,help='Explicit root PV partition to identify read-only')
    parser.add_argument('--usb-serial',help='Required expected USB serial with --partition')
    identity_source.add_argument('--base-rescue-dir',type=Path,
                                 help='Explicit older rescue bundle with a recorded identity')
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('Read-only disk enrollment requires local administrator authentication')
    if bool(args.partition) != bool(args.usb_serial):
        parser.error('--partition and --usb-serial must be supplied together')
    identity=(identify(args.partition,args.usb_serial) if args.partition else
        read_trusted_json(args.identity) if args.identity else
        read_trusted_json(args.base_rescue_dir/'manifest.json').get('identity'))
    if not isinstance(identity,dict) or not identity:
        parser.error('Explicit root identity is required; generic tools do not enroll devices')
    print(json.dumps(enroll(args.name, identity)))


if __name__=='__main__':
    main()
