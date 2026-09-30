#!/usr/bin/python3
"""Read the already enrolled host disk; write a private boot profile only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from host_files import sha256

BASE=Path(__file__).resolve().parent
sys.path[:0]=[str(BASE/'runtime'),str(BASE.parent/'ram-rescue-demo/src')]
from admission import Admission, layout, readonly
from rescue import Recovery


def collect(identity):
    recovery=Recovery(identity,runner=readonly)
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
    for name in identity['lvs']:
        mapping=recovery.mapping(name)
        if [p.resolve() for p in (mapping/'slaves').iterdir()] != [sys_path]:
            raise RuntimeError('Enrolled LV has an unexpected live backing device')
    config={'schema':1,'profile':'host','kernel_release':os.uname().release,
        'map_name':'ram-rescue-path',
        'map_uuid':'RAMRESCUE-HOST-'+hashlib.sha256(identity['partuuid'].encode()).hexdigest()[:24],
        'run_dir':'/run/ram-rescue-guard/state','identity_path':'/etc/rescue/identity.json',
        'queue_seconds':8,'partition_sectors':int((sys_path/'size').read_text()),
        'logical_block_size':int((sys_path.parent/'queue/logical_block_size').read_text()),
        'layout':enrolled_layout,'root_lv':roots[0],
        'root_fs_uuid':subprocess.check_output(['findmnt','-nro','UUID','-T','/'],text=True).strip()}
    with Admission(config,recovery).verify(time.monotonic()+15,'host-enrollment') as candidate:
        candidate.revalidate('host-enrollment')
    return {'schema':1,'identity':identity,'guard':config}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--uid',type=int,required=True)
    parser.add_argument('--gid',type=int,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:
        parser.error('Read-only disk enrollment requires local administrator authentication')
    work=(BASE.parent/'lab/work').resolve()
    out=args.output.resolve()
    if not out.is_relative_to(work) or out.exists():
        parser.error('output must be a new directory under lab/work')
    manifest=json.loads(Path('/usr/local/lib/ram-rescue-demo/manifest.json').read_text())
    profile=collect(manifest['identity'])
    out.mkdir(mode=0o700,parents=True)
    release=profile['guard']['kernel_release']
    for source,dest in [(Path('/boot')/('vmlinuz-'+release),'vmlinuz'),
                        (Path('/boot')/('initrd.img-'+release),'original-initrd.img')]:
        shutil.copyfile(source,out/dest)
    profile['baseline']={'kernel_sha256':sha256(out/'vmlinuz'),
        'initrd_sha256':sha256(out/'original-initrd.img'),
        'grub_default_path':'/boot/grub/grub.cfg',
        'lvmlocal_sha256':sha256(Path('/etc/lvm/lvmlocal.conf'))}
    (out/'enrollment.json').write_text(json.dumps(profile,indent=2)+'\n')
    for path in [out,*out.iterdir()]:
        os.chmod(path,0o700 if path.is_dir() else 0o600)
        os.chown(path,args.uid,args.gid)
    print(json.dumps({'enrollment':'verified','kernel_release':release,
        'output':str(out),'lvs':sorted(profile['identity']['lvs']),
        'disk_mutations':False,'boot_changes':False}))


if __name__=='__main__':
    main()
