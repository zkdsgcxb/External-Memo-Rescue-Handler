#!/usr/bin/python3
"""Install one optional GRUB entry and initrd; preserve the normal boot entry."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from enroll import collect
from host_files import atomic, sha256

STATE=Path('/var/lib/ram-rescue-guard')
HOOK=Path('/etc/grub.d/42_ram_rescue_guard')
GRUB=Path('/boot/grub/grub.cfg')
PROJECT=Path(__file__).resolve().parent.parent


@contextmanager
def deployment_lock(state=None):
    """Serialize boot installation, upgrade and removal without creating a file."""
    from trusted_paths import open_directory
    directory = STATE if state is None else state
    descriptor = open_directory(directory)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Another protection deployment is already running') from error
        yield
    finally:
        os.close(descriptor)


def optional_hash(path):
    if path.is_symlink():
        raise RuntimeError('Unexpected configuration symlink: ' + str(path))
    return sha256(path) if path.exists() else None


def verify_sources(build):
    sources = build.get('source_sha256')
    if not isinstance(sources, dict) or not sources:
        raise RuntimeError('Candidate source manifest is missing')
    for name, expected in sources.items():
        path = PROJECT / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or path.is_symlink()
                or not path.resolve().is_relative_to(PROJECT.resolve()) or sha256(path) != expected):
            raise RuntimeError('Candidate differs from the installed administration release: ' + name)


def menu(profile,image_name):
    config=profile['guard']
    vg=profile['identity']['vg_name']
    lv=config['root_lv']
    release=config['kernel_release']
    uuid=config['root_fs_uuid']
    for value in [vg,lv,release,image_name]:
        if not re.fullmatch(r'[A-Za-z0-9+_.-]+',value):
            raise ValueError('Unsafe boot argument')
    if not re.fullmatch(r'[0-9a-fA-F-]+',uuid):
        raise ValueError('Invalid root filesystem UUID')
    root='/dev/mapper/'+vg.replace('-','--')+'-'+lv.replace('-','--')
    return ("menuentry 'Ubuntu USB root protection ("+release+")' --id ram-rescue-guard {\n"
        "    insmod part_gpt\n    insmod lvm\n    insmod ext2\n"
        "    search --no-floppy --fs-uuid --set=root '"+uuid+"'\n"
        "    linux /boot/vmlinuz-"+release+' root='+root+' ro ram_rescue_guard=1 nompath noresume\n'
        '    initrd /boot/'+image_name+'\n}\n')


def first_entry(text):
    """Read the ordinary top-level Ubuntu entry, preserving its boot commands.

    This installer adds an entry to the known Ubuntu-generated menu; it does
    not interpret arbitrary GRUB programs. Unknown structures need review.
    """
    lines=text.splitlines()
    start=next((index for index,line in enumerate(lines)
                if re.match(r'^menuentry\s+.*\{\s*$',line)),None)
    if start is None or any(re.match(r'^submenu\s',line) for line in lines[:start]):
        raise RuntimeError('Cannot identify the normal/default top-level menu entry')
    end=next((index for index in range(start+1,len(lines))
              if re.fullmatch(r'}\s*',lines[index])),None)
    if end is None:
        raise RuntimeError('Normal/default menu entry is not complete')
    entry=[line.strip() for line in lines[start:end+1]
           if line.strip() and not line.lstrip().startswith('#')]
    if (not any(re.match(r'linux\s',line) for line in entry) or
            not any(re.match(r'initrd\s',line) for line in entry)):
        raise RuntimeError('Normal/default menu entry is not the expected Ubuntu boot')
    return entry


def validate(build_dir,enrollment,vm_report, *, trusted=False):
    if trusted:
        from trusted_paths import read_trusted_json, trusted_sha256
        profile = read_trusted_json(enrollment)
        build = read_trusted_json(build_dir / 'build.json', limit=1024**2)
        vm = read_trusted_json(vm_report, limit=32 * 1024**2)
        checksum = trusted_sha256
    else:
        profile=json.loads(enrollment.read_text())
        build=json.loads((build_dir/'build.json').read_text())
        vm=json.loads(vm_report.read_text())
        checksum = sha256
    if vm.get('passed') is not True:
        raise RuntimeError('A passed normal-boot/reconnect VM report is required')
    tested=vm.get('build',{})
    if (tested.get('source_sha256')!=build['source_sha256'] or
            tested.get('kernel_sha256')!=build['kernel_sha256'] or
            tested.get('base_rescue_payload_sha256')!=build['base_rescue_payload_sha256']):
        raise RuntimeError('VM did not validate these runtime/integration sources, kernel and base tools')
    if build['enrollment_sha256']!=checksum(enrollment):
        raise RuntimeError('Build enrollment differs')
    if build['initramfs_sha256']!=checksum(build_dir/'initrd.img'):
        raise RuntimeError('Candidate initrd checksum differs')
    if build['kernel_sha256']!=profile['baseline']['kernel_sha256']:
        raise RuntimeError('Candidate kernel differs from enrollment')
    image_name='initrd.img-'+profile['guard']['kernel_release']+'-ram-rescue'
    return profile,build,image_name


def install(build_dir,enrollment,vm_report):
    if os.geteuid()!=0:
        raise RuntimeError('Installation requires local administrator authentication')
    from trusted_paths import open_directory
    os.close(open_directory(build_dir))
    profile,build,image_name=validate(build_dir,enrollment,vm_report,trusted=True)
    verify_sources(build)
    image=Path('/boot')/image_name
    if STATE.exists() or HOOK.exists() or image.exists():
        raise RuntimeError('Protection installation already exists; refusing to overwrite it')
    current=collect(profile['identity'])
    if current!= {k:profile[k] for k in ('schema','identity','guard')}:
        raise RuntimeError('Live root layout/configuration no longer matches enrollment')
    release=profile['guard']['kernel_release']
    if sha256(Path('/boot')/('vmlinuz-'+release))!=build['kernel_sha256']:
        raise RuntimeError('Installed kernel changed')
    baseline=profile['baseline']
    if sha256(Path('/boot')/('initrd.img-'+release))!=baseline['initrd_sha256']:
        raise RuntimeError('Normal initrd changed since enrollment')
    if optional_hash(Path('/etc/lvm/lvmlocal.conf'))!=baseline['lvmlocal_sha256']:
        raise RuntimeError('Host LVM configuration changed since enrollment')
    if "set default=\"0\"" not in GRUB.read_text():
        raise RuntimeError('Review the current GRUB default before adding a new entry')
    if shutil.disk_usage('/boot').free < (build_dir/'initrd.img').stat().st_size*2+512*1024**2:
        raise RuntimeError('Insufficient free space to stage the new boot image safely')
    entry=menu(profile,image_name)
    hook=('#!/bin/sh\ncat <<\'RAM_RESCUE_MENU\'\n'+entry+'RAM_RESCUE_MENU\n').encode()
    STATE.mkdir(mode=0o700)
    with deployment_lock():
        original=GRUB.read_bytes()
        atomic(STATE/'grub.cfg.before',original)
        record={'state':'preparing','kernel_release':release,'image':str(image),
            'image_sha256':build['initramfs_sha256'],'hook_sha256':hashlib.sha256(hook).hexdigest(),
            'normal_grub_sha256':hashlib.sha256(original).hexdigest(),
            'normal_initrd_sha256':baseline['initrd_sha256'],
            'vm_report_sha256':sha256(vm_report),'build':build}
        atomic(STATE/'install.json',(json.dumps(record,indent=2)+'\n').encode())
        try:
            atomic(image,(build_dir/'initrd.img').read_bytes())
            if sha256(image)!=build['initramfs_sha256']:
                raise RuntimeError('Installed image checksum differs')
            atomic(HOOK,hook,0o755)
            candidate=STATE/'grub.cfg.candidate'
            with (STATE/'grub-generation.log').open('wb') as log:
                subprocess.run(['grub-mkconfig','-o',str(candidate)],check=True,
                               stdout=log,stderr=subprocess.STDOUT)
            subprocess.run(['grub-script-check',str(candidate)],check=True)
            text=candidate.read_text()
            normal_image='/boot/initrd.img-'+release
            normal_lines=[line.strip() for line in original.decode().splitlines()
                          if line.strip().startswith('initrd ') or line.strip().startswith('initrd\t')]
            normal_lines=[line for line in normal_lines if normal_image in line.split()[1:]]
            if (text.count('--id ram-rescue-guard')!=1 or 'set default="0"' not in text
                    or not normal_lines or any(line not in text for line in normal_lines)
                    or first_entry(text)!=first_entry(original.decode())):
                raise RuntimeError('Generated menu does not preserve the expected normal/default entry')
            record['protected_grub_sha256']=sha256(candidate)
            atomic(STATE/'install.json',(json.dumps(record,indent=2)+'\n').encode())
            atomic(GRUB,candidate.read_bytes())
            record['state']='installed'
            atomic(STATE/'install.json',(json.dumps(record,indent=2)+'\n').encode())
        except BaseException:
            # atomic() can replace the file and then fail while syncing its parent.
            # Inspect the result itself before removing the image it may reference.
            if sha256(GRUB)==record.get('protected_grub_sha256'):
                atomic(GRUB,original)
            if HOOK.exists() and sha256(HOOK)==record['hook_sha256']:
                HOOK.unlink()
            if image.exists() and sha256(image)==record['image_sha256']:
                image.unlink()
            record['state']='failed_rolled_back'
            atomic(STATE/'install.json',(json.dumps(record,indent=2)+'\n').encode())
            raise
    print(json.dumps({'installed':True,'default_boot_changed':False,
        'entry':'Ubuntu USB root protection ('+release+')','rebooted':False,
        'normal_initrd_unchanged':sha256(Path('/boot')/('initrd.img-'+release))==baseline['initrd_sha256']}))


def rollback():
    if os.geteuid()!=0:
        raise RuntimeError('Rollback requires local administrator authentication')
    from trusted_paths import read_trusted, read_trusted_json, trusted_sha256
    with deployment_lock():
        record=read_trusted_json(STATE/'install.json', limit=1024**2)
        image=Path(record['image'])
        if (record['state']!='installed' or trusted_sha256(GRUB)!=record['protected_grub_sha256']
                or trusted_sha256(HOOK)!=record['hook_sha256'] or trusted_sha256(image)!=record['image_sha256']):
            raise RuntimeError('Files changed since installation; review before rollback')
        original=read_trusted(STATE/'grub.cfg.before', limit=16*1024**2)
        if hashlib.sha256(original).hexdigest()!=record['normal_grub_sha256']:
            raise RuntimeError('Normal menu backup checksum differs')
        atomic(GRUB,original)
        HOOK.unlink()
        image.unlink()
        record['state']='removed'
        atomic(STATE/'install.json',(json.dumps(record,indent=2)+'\n').encode())
    print(json.dumps({'removed':True,'normal_menu_restored':True,'rebooted':False}))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path)
    parser.add_argument('--enrollment',type=Path)
    parser.add_argument('--vm-report',type=Path)
    parser.add_argument('--install',action='store_true')
    parser.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    if args.rollback:
        if args.install: parser.error('Choose install or rollback')
        rollback()
        return
    if not all((args.build_dir,args.enrollment,args.vm_report)):
        parser.error('--build-dir, --enrollment and --vm-report are required')
    if args.install:
        install(args.build_dir,args.enrollment,args.vm_report)
    else:
        profile,build,image_name=validate(args.build_dir,args.enrollment,args.vm_report)
        print(menu(profile,image_name),end='')
        print('Validated candidate only; no host files changed.')


if __name__=='__main__':
    main()
