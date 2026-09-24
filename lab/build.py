#!/usr/bin/env python3
"""Build a disposable guest from installed Ubuntu tools, without host disk access."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent
WORK = BASE / 'work'
MODULES = ['xhci_pci', 'usb_storage', 'uas', 'sd_mod', 'dm_mod', 'dm_multipath', 'dm_round_robin', 'ext4', 'virtio_pci', 'virtio_blk']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kernel', type=Path, required=True, help='Readable guest vmlinuz')
    parser.add_argument('--release', default=os.uname().release)
    parser.add_argument('--module-root', type=Path, default=Path('/'),
                        help='Root containing lib/modules, including privately extracted kernel packages')
    parser.add_argument('--work-dir', type=Path, default=WORK,
                        help='Build directory under lab/work; leaves other builds unchanged')
    args = parser.parse_args()
    if not args.kernel.is_file():
        parser.error('kernel must be a regular file')
    work = args.work_dir.resolve()
    if not work.is_relative_to(WORK.resolve()):
        parser.error('work-dir must be inside lab/work')
    module_root = args.module_root.resolve()
    module_dir = module_root / 'lib/modules' / args.release
    if not module_dir.is_dir():
        parser.error('module-root must contain lib/modules for the requested release')
    work.mkdir(parents=True, exist_ok=True)
    root = work / 'rootfs'
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    spec = importlib.util.spec_from_file_location('payload', PROJECT / 'ram-rescue-demo/build.py')
    payload = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(payload)
    # Only use file/library copying helpers; NEVER call the host enrollment builder.
    payload.ROOT = root
    payload.with_libs('/usr/bin/busybox', '/bin/busybox')
    for applet in subprocess.check_output(['busybox', '--list'], text=True).split():
        if applet != 'busybox':
            (root / 'bin' / applet).symlink_to('busybox')
    for name in ['lvm', 'dmsetup', 'blkid', 'mkfs.ext4', 'e2fsck', 'sfdisk']:
        payload.with_libs('/usr/sbin/' + name, '/sbin/' + name)
    payload.with_libs('/usr/bin/python3.12')
    payload.with_libs('/usr/bin/kmod')
    payload.with_libs('/usr/bin/tar')
    payload.with_libs('/usr/bin/xz')
    (root / 'sbin/modprobe').symlink_to('/usr/bin/kmod')
    (root / 'usr/bin/python3').symlink_to('python3.12')
    shutil.copytree('/usr/lib/python3.12', root / 'usr/lib/python3.12',
                    ignore=shutil.ignore_patterns('__pycache__', 'test', 'tests', 'idlelib', 'tkinter'))
    for so in (root / 'usr/lib/python3.12').rglob('*.so'):
        payload.with_libs('/' + str(so.relative_to(root)))
    for module in MODULES:
        deps = subprocess.check_output(['modprobe', '-d', str(module_root), '-S', args.release,
                                        '--show-depends', module], text=True)
        for line in deps.splitlines():
            if line.startswith('insmod '):
                source = Path(line.split()[1]).resolve()
                relative = source.relative_to(module_dir.resolve())
                payload.copy_file(source, Path('/lib/modules') / args.release / relative)
    for name in ['modules.builtin', 'modules.builtin.modinfo', 'modules.order']:
        if (module_dir / name).exists():
            payload.copy_file(module_dir / name, Path('/lib/modules') / args.release / name)
    subprocess.run(['depmod', '-b', str(root), args.release], check=True)
    for directory in ['dev', 'proc', 'sys', 'run', 'tmp', 'newroot', 'etc/lvm', 'etc/rescue', 'opt/lab', 'var/log', 'root', 'shared']:
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / 'tmp').chmod(0o1777)
    payload.copy_file(PROJECT / 'ram-rescue-demo/src/lvm.conf', '/etc/lvm/lvm.conf')
    payload.copy_file(PROJECT / 'ram-rescue-demo/src/rescue.py', '/opt/lab/rescue.py')
    for source in (BASE / 'guest').iterdir():
        if source.is_file():
            payload.copy_file(source, '/opt/lab/' + source.name)
    payload.copy_file(BASE / 'guest/init.sh', '/init')
    (root / 'init').chmod(0o755)
    (root / 'opt/lab/root-init.sh').chmod(0o755)
    (root / 'etc/passwd').write_text('root:x:0:0:VM lab:/root:/bin/sh\n')
    (root / 'etc/group').write_text('root:x:0:\n')
    (root / 'etc/shadow').write_text('root:!:20000:0:99999:7:::\n')
    (root / 'etc/shadow').chmod(0o600)
    with subprocess.Popen(['find', '.', '-print0'], cwd=root, stdout=subprocess.PIPE) as find:
        with subprocess.Popen(['cpio', '--null', '-o', '-H', 'newc', '--owner=0:0'], cwd=root,
                              stdin=find.stdout, stdout=subprocess.PIPE) as cpio:
            find.stdout.close()
            with gzip.open(work / 'initramfs.cpio.gz', 'wb', compresslevel=3) as out:
                shutil.copyfileobj(cpio.stdout, out)
            if cpio.wait() or find.wait():
                raise RuntimeError('initramfs creation failed')
    shutil.copyfile(args.kernel, work / 'vmlinuz')
    (work / 'build.json').write_text(json.dumps({'kernel_release': args.release,
        'module_root': str(module_root),
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=PROJECT, text=True).strip(),
        'source_sha256': {str(p.relative_to(PROJECT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [BASE/'build.py', PROJECT/'ram-rescue-demo/src/rescue.py', PROJECT/'ram-rescue-demo/src/lvm.conf',
                      *sorted(p for p in (BASE/'guest').iterdir() if p.is_file())]},
        'kernel_sha256': hashlib.sha256((work/'vmlinuz').read_bytes()).hexdigest(),
        'initramfs_sha256': hashlib.sha256((work/'initramfs.cpio.gz').read_bytes()).hexdigest(),
        'note': 'guest payload copied from working tree; no host identities or passwords'}, indent=2)+'\n')
    print('Guest ready:', work / 'initramfs.cpio.gz')


if __name__ == '__main__':
    main()
