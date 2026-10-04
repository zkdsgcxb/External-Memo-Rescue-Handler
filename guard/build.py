#!/usr/bin/python3
"""Build an isolated Ubuntu protection initrd; never installs or changes disks."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from host_files import sha256
from native_payload import build_runtime, stage_runtime

BASE=Path(__file__).resolve().parent
PROJECT=BASE.parent
WORK=PROJECT/'lab/work'


def session_builder():
    source=PROJECT/'ram-rescue-demo/session_payload.py'
    spec=importlib.util.spec_from_file_location('rescue_session_payload',source)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def base_builder():
    # Load only this checkout's generic tool builder. It never discovers disks.
    import sys
    directory = PROJECT / 'ram-rescue-demo'
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location('generic_rescue_build', directory / 'build.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def payload(profile,root,*,native_binary,base_rescue_dir=None):
    installed=Path(base_rescue_dir) if base_rescue_dir else root.parent/'base-rescue'
    if base_rescue_dir is None:
        base_builder().build(installed)
    manifest=json.loads((installed/'manifest.json').read_text())
    archive=installed/'rescue-root.tar.gz'
    if sha256(archive)!=manifest['sha256']:
        raise RuntimeError('Base rescue payload checksum mismatch')
    root.mkdir()
    with tarfile.open(archive,'r:gz') as source:
        source.extractall(root,filter='data')
    (root/'etc/rescue/base-source.json').write_text(json.dumps(
        {'schema':1,'sha256':manifest['sha256']})+'\n')
    modules=root/'opt/guard'
    modules.mkdir(parents=True)
    rescue=PROJECT/'ram-rescue-demo/src/rescue.py'
    shutil.copyfile(rescue,modules/'rescue.py')
    shutil.copyfile(rescue,root/'sbin/rescue')
    (root/'sbin/rescue').chmod(0o755)
    # No host password or live shadow enters this image. The existing rescue
    # preparation service supplies it locally after the real root is mounted.
    (root/'etc/shadow').write_text('root:!:20000:0:99999:7:::\nrescue:!:20000:0:99999:7:::\n')
    (root/'etc/shadow').chmod(0o600)
    (root/'etc/rescue/identity.json').write_text(json.dumps(profile['identity'],indent=2)+'\n')
    (root/'etc/rescue/enrollment.json').write_text(json.dumps(profile,indent=2)+'\n')
    (root/'etc/rescue/enrollment.json').chmod(0o600)
    session=session_builder().stage_session(root)
    (root/'etc/rescue/session-build.json').write_text(json.dumps(session,indent=2)+'\n')
    stage_runtime(root,native_binary)
    base_builder().normalize_payload(root)
    return manifest['sha256']


def build(enrollment,work,kernel,*,base_rescue_dir=None):
    profile=json.loads(enrollment.read_text())
    config=profile['guard']
    release=config['kernel_release']
    if profile.get('schema')!=1 or config.get('profile')!='host':
        raise ValueError('A verified host-style enrollment is required')
    if release!=os.uname().release or not (Path('/lib/modules')/release).is_dir():
        raise ValueError('Build only against the current installed kernel and modules')
    if config['map_name']!='ram-rescue-path' or config['run_dir']!='/run/ram-rescue-guard/state':
        raise ValueError('Integration templates require the reserved map and RAM state paths')
    if sha256(kernel)!=profile['baseline']['kernel_sha256']:
        raise ValueError('Kernel differs from the enrolled baseline')
    if work.exists():
        raise ValueError('Use a new work directory to preserve prior artifacts')
    work.mkdir(mode=0o700,parents=True)
    conf=work/'conf'
    for name in ['hooks','conf.d','scripts/local-top','scripts/init-bottom','ram-rescue-guard']:
        (conf/name).mkdir(parents=True,exist_ok=True)
    shutil.copyfile('/etc/initramfs-tools/initramfs.conf',conf/'initramfs.conf')
    with (conf/'initramfs.conf').open('a') as stream:
        stream.write('\n# Isolated protection image\nMODULES=most\nBUSYBOX=y\nCOMPRESS=zstd\n')
    (conf/'conf.d/resume').write_text('RESUME=none\n')
    (conf/'modules').write_text('dm_multipath\ndm_round_robin\nusb_storage\nuas\nxhci_pci\n')
    source_templates=BASE/'integration'
    templates=conf/'ram-rescue-guard/integration'
    shutil.copytree(source_templates,templates)
    for src,dest in [('initramfs-hook','hooks/ram-rescue-guard'),
                     ('local-top','scripts/local-top/ram-rescue-guard'),
                     ('init-bottom','scripts/init-bottom/ram-rescue-guard')]:
        shutil.copyfile(templates/src,conf/dest)
        (conf/dest).chmod(0o755)
    payload_root=work/'payload'
    native_binary=build_runtime(work/'native-runtime')
    base_payload=payload(profile,payload_root,native_binary=native_binary,base_rescue_dir=base_rescue_dir)
    archive=conf/'ram-rescue-guard/tools.tar.gz'
    with tarfile.open(archive,'w:gz',compresslevel=3) as output:
        for path in sorted(payload_root.rglob('*')):
            item=output.gettarinfo(str(path),arcname=str(path.relative_to(payload_root)))
            item.uid=item.gid=0
            item.uname=item.gname='root'
            if item.isfile():
                with path.open('rb') as data:
                    output.addfile(item,data)
            else:
                output.addfile(item)
    sources=[PROJECT/'ram-rescue-demo/build.py',PROJECT/'ram-rescue-demo/src/lvm.conf',BASE/'build.py',BASE/'host_files.py',BASE/'native_payload.py',PROJECT/'ram-rescue-demo/src/rescue.py',
             *session_builder().SOURCES,*sorted(p for p in source_templates.iterdir() if p.is_file())]
    sources.extend(p for p in sorted((BASE/'native').rglob('*'))
                   if p.is_file() and '__pycache__' not in p.parts)
    source_hashes={str(p.relative_to(PROJECT)):sha256(p) for p in sources}
    temporary=work/'tmp'
    temporary.mkdir()
    image=work/'initrd.img'
    args=['/usr/sbin/mkinitramfs','-d',str(conf),'-o',str(image),release]
    (work/'command.json').write_text(json.dumps(args,indent=2)+'\n')
    with (work/'build.log').open('w') as log:
        subprocess.run(args,check=True,stdout=log,stderr=subprocess.STDOUT,
                       env={**os.environ,'TMPDIR':str(temporary)})
    shutil.copyfile(kernel,work/'vmlinuz')
    details={'schema':1,'kernel_release':release,'kernel_sha256':sha256(work/'vmlinuz'),
        'initramfs_sha256':sha256(image),'enrollment_sha256':sha256(enrollment),
        'source_sha256':source_hashes,'base_rescue_payload_sha256':base_payload,
        'tools_archive_sha256':sha256(archive),'installed':False,'runtime':'cpp',
        'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=PROJECT,text=True).strip()}
    details['native_runtime']=json.loads((payload_root/'opt/guard-runtime/runtime.json').read_text())
    details['rescue_session']=json.loads((payload_root/'etc/rescue/session-build.json').read_text())
    (work/'build.json').write_text(json.dumps(details,indent=2)+'\n')
    image.chmod(0o600)
    print(json.dumps({'built':str(image),'sha256':details['initramfs_sha256'],
                      'installed':False},ensure_ascii=False))
    return details


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enrollment',required=True,type=Path)
    parser.add_argument('--kernel',type=Path)
    parser.add_argument('--work-dir',required=True,type=Path)
    parser.add_argument('--base-rescue-dir',type=Path,
                        help='Explicit generic rescue artifacts; default builds tools from this checkout')
    args=parser.parse_args()
    work=args.work_dir.resolve()
    if not work.is_relative_to(WORK.resolve()):
        parser.error('work-dir must be below lab/work')
    build(args.enrollment.resolve(),work,(args.kernel or args.enrollment.parent/'vmlinuz').resolve(),
          base_rescue_dir=args.base_rescue_dir.resolve() if args.base_rescue_dir else None)


if __name__=='__main__':
    main()
