#!/usr/bin/env python3
"""Rebuild disposable Ubuntu/C++ acceptance inputs from this checkout.

No installed rescue package, author enrollment, full Git history, host block
access or elevated privilege is used. Full VM execution is explicit --run.
"""
import argparse
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

import fetch_ubuntu
from run import qemu_command

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
WORK = BASE/'work'
MODULES = ('xhci_pci', 'usb_storage', 'uas', 'sd_mod', 'dm_mod', 'dm_multipath',
           'dm_round_robin', 'ext4', 'virtio_pci', 'virtio_blk')
KEYRING = Path('/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg')


def load_builder():
    sys.path.insert(0, str(REPO/'guard'))
    spec = importlib.util.spec_from_file_location('reproduce_guard_build', REPO/'guard/build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_directory(path, *, existing=False):
    resolved = Path(path).resolve()
    if resolved == WORK.resolve() or not resolved.is_relative_to(WORK.resolve()):
        raise ValueError('Artifacts must remain in a child directory of lab/work')
    if Path(path).is_symlink() or (resolved.exists() and not existing):
        raise ValueError('Use a new real work directory to preserve earlier evidence')
    return resolved


def verify_ubuntu(directory, keyring=KEYRING):
    """Do not accept source.json as authentication; reverify signed bytes."""
    directory = safe_directory(directory, existing=True)
    subprocess.run(['gpgv', '--keyring', str(keyring), str(directory/'SHA256SUMS.gpg'),
                    str(directory/'SHA256SUMS')], check=True)
    matches = [line.split()[0] for line in (directory/'SHA256SUMS').read_text().splitlines()
               if line.split() and line.split()[-1].lstrip('*') == fetch_ubuntu.NAME]
    if len(matches) != 1 or fetch_ubuntu.sha256(directory/'root.tar.xz') != matches[0]:
        raise ValueError('Signed Ubuntu root archive digest differs')
    # Check the QEMU-facing padded copy too; it is not independently signed.
    archive, disk = directory/'root.tar.xz', directory/'rootfs.raw'
    if disk.is_symlink() or not disk.is_file() or disk.stat().st_size != ((archive.stat().st_size+511)//512)*512:
        raise ValueError('Ubuntu seed must be a regular, correctly padded archive')
    with archive.open('rb') as source, disk.open('rb') as target:
        while block := source.read(1024**2):
            if target.read(len(block)) != block:
                raise ValueError('Padded Ubuntu archive differs from signed content')
        if any(target.read()):
            raise ValueError('Ubuntu padding is not zero')
    return {'archive_sha256': matches[0], 'seed_sha256': fetch_ubuntu.sha256(disk),
            'signed_checksums_sha256': fetch_ubuntu.sha256(directory/'SHA256SUMS'),
            'signature_verified': True, 'keyring_sha256': fetch_ubuntu.sha256(keyring)}


def acquire_kernel(folder, explicit, release):
    if explicit:
        source = Path(explicit).resolve(strict=True)
        if not source.is_file():
            raise ValueError('--kernel must be a readable regular file')
    else:
        source = Path('/boot')/('vmlinuz-'+release)
        if not os.access(source, os.R_OK):
            package = folder/'kernel-package'
            package.mkdir()
            subprocess.run(['apt-get', 'download', 'linux-image-'+release], cwd=package, check=True)
            archives = list(package.glob('*.deb'))
            if len(archives) != 1:
                raise ValueError('Expected exactly one authenticated distribution kernel package')
            subprocess.run(['dpkg-deb', '-x', str(archives[0]), str(package/'root')], check=True)
            source = package/'root/boot'/('vmlinuz-'+release)
    shutil.copyfile(source, folder/'vmlinuz')
    return folder/'vmlinuz'


def build_seed_initrd(folder, base_directory, release, builder):
    root = folder/'seed-root'
    shutil.copytree(base_directory/'rootfs', root, symlinks=True)
    payload = builder.base_builder()
    payload.ROOT = root
    for name in ('sfdisk', 'mkfs.ext4'):
        payload.with_libs('/usr/sbin/'+name, '/sbin/'+name)
    for name in ('kmod', 'tar', 'xz'):
        payload.with_libs('/usr/bin/'+name)
    (root/'sbin/modprobe').symlink_to('/usr/bin/kmod')
    module_dir = Path('/lib/modules')/release
    for module in MODULES:
        output = subprocess.check_output(['modprobe', '-S', release, '--show-depends', module], text=True)
        for line in output.splitlines():
            if line.startswith('insmod '):
                source = Path(line.split()[1]).resolve(strict=True)
                payload.copy_file(source, Path('/lib/modules')/release/source.relative_to(module_dir.resolve()))
    for name in ('modules.builtin', 'modules.builtin.modinfo', 'modules.order'):
        source = module_dir/name
        if source.exists():
            payload.copy_file(source, Path('/lib/modules')/release/name)
    subprocess.run(['depmod', '-b', str(root), release], check=True)
    seed = root/'opt/seed'
    seed.mkdir(parents=True)
    shutil.copytree(REPO/'guard/admin', seed/'guard/admin', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copyfile(REPO/'ram-rescue-demo/src/rescue.py', seed/'rescue.py')
    shutil.copyfile(BASE/'guest/seed_prepare.py', seed/'prepare.py')
    (root/'newroot').mkdir()
    (root/'init').write_text('#!/bin/sh\nset -eu\nexport PATH=/bin:/sbin:/usr/bin:/usr/sbin\n'
        'mount -t proc proc /proc\nmount -t sysfs sysfs /sys\nmount -t devtmpfs devtmpfs /dev\n'
        'mount -t tmpfs -o noswap tmpfs /run\nmkdir -p /run/lock/lvm /run/lvm\n'
        'for module in '+ ' '.join(MODULES) + '; do /sbin/modprobe "$module"; done\n'
        'exec python3 /opt/seed/prepare.py\n')
    (root/'init').chmod(0o755)
    paths = [Path('.'), *sorted(p.relative_to(root) for p in root.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=root,
        input=b'\0'.join(str(p).encode() for p in paths)+b'\0',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    image = folder/'seed-initrd.img'
    image.write_bytes(gzip.compress(archive, compresslevel=3, mtime=0))
    return image


def seed_vm(folder, kernel, image, ubuntu):
    seed_folder = folder/'seed'
    runtime = seed_folder/'s0'
    runtime.mkdir(parents=True)
    for name, size in (('usb.raw', 8*1024**3), ('decoy.raw', 16*1024**2)):
        with (runtime/name).open('xb') as stream:
            stream.truncate(size)
    # UNIX socket paths have a 108-byte limit. Use a relative cwd in QEMU only;
    # block inputs below are absolute regular files and never host devices.
    command = qemu_command(Path('.'), same_port=True, kernel=kernel, initramfs=image,
                           extra_kernel_args='ram_rescue_seed_create=1')
    command[command.index('-m')+1] = '3072'
    command += ['-blockdev', json.dumps({'driver': 'raw', 'node-name': 'ubuntu-seed',
        'read-only': True, 'file': {'driver': 'file', 'filename': str(ubuntu/'rootfs.raw')}}),
        '-device', 'virtio-blk-pci,drive=ubuntu-seed,serial=UBUNTU-ROOTFS-SEED']
    (runtime/'command.json').write_text(json.dumps(command, indent=2)+'\n')
    with (runtime/'qemu.log').open('w') as log:
        process = subprocess.Popen(command, cwd=runtime, stdout=log, stderr=subprocess.STDOUT)
        try:
            process.wait(timeout=480)
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            raise
    console = (runtime/'console.log').read_text(errors='replace')
    messages = [json.loads(line.split('RAM_RESCUE_SEED=', 1)[1]) for line in console.splitlines()
                if 'RAM_RESCUE_SEED=' in line]
    if process.returncode or len(messages) != 1 or not messages[0].get('ok'):
        raise RuntimeError('Seed creation failed; inspect '+str(runtime/'console.log'))
    profile = messages[0]['value']
    digest = fetch_ubuntu.sha256(runtime/'usb.raw')
    report = {'schema': 1, 'passed': True, 'source_image_sha256_after': digest,
              'cases': {'seed': {'observation': {'gate': {'identity': profile['identity']}}}},
              'creation': profile, 'scope': 'fresh disposable Ubuntu seed only; no recovery acceptance yet'}
    (seed_folder/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    return profile, seed_folder/'report.json'


def plain_initrd(folder, release):
    """A normal VM boot image for independent data-map acceptance, no root Guard."""
    conf = folder/'plain-conf'
    (conf/'hooks').mkdir(parents=True)
    (conf/'conf.d').mkdir()
    shutil.copyfile('/etc/initramfs-tools/initramfs.conf', conf/'initramfs.conf')
    with (conf/'initramfs.conf').open('a') as output:
        output.write('\nMODULES=most\nBUSYBOX=y\nCOMPRESS=zstd\n')
    (conf/'conf.d/resume').write_text('RESUME=none\n')
    (conf/'modules').write_text('\n'.join(MODULES)+'\n')
    hook = conf/'hooks/zz-lab-unrestricted-lvm'
    hook.write_text('#!/bin/sh\nset -eu\ncase "${1:-}" in prereqs) exit 0 ;; esac\n'
                   'mkdir -p "$DESTDIR/etc/lvm"\n'
                   'printf "%s\\n" "devices { use_devicesfile = 0 }" > "$DESTDIR/etc/lvm/lvmlocal.conf"\n')
    hook.chmod(0o755)
    image = folder/'original-initrd.img'
    with (folder/'plain-build.log').open('w') as log:
        subprocess.run(['mkinitramfs', '-d', str(conf), '-o', str(image), release],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir', type=Path, help='New short directory under lab/work')
    parser.add_argument('--kernel', type=Path, help='Current readable kernel; otherwise use /boot or authenticated apt download')
    parser.add_argument('--ubuntu-dir', type=Path, help='Previously downloaded signed source directory; always reverified')
    parser.add_argument('--ubuntu-build', default='20260911')
    parser.add_argument('--run', action='store_true', help='Explicitly create seed in QEMU, build and run complete acceptance')
    parser.add_argument('--prepare-only', action='store_true', help='With --run: create seed/build but leave full VM acceptance to caller')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as an ordinary user; never grant this experiment host root')
    if not args.run:
        print('No VM started. Run ordinary checks: python3 -m unittest discover -s lab/tests\n'
              'Full acceptance: python3 lab/reproduce.py --run\n'
              'Requires current Linux 7.0/modules, Ubuntu tools, cloud keyring and KVM access.')
        return
    release = os.uname().release
    if not release.startswith('7.0.') or not (Path('/lib/modules')/release).is_dir():
        parser.error('Current Linux 7.0 with installed modules is required')
    folder = safe_directory(args.work_dir or WORK/('repro-'+time.strftime('%m%d-%H%M%S')))
    folder.mkdir(mode=0o700, parents=True)
    builder = load_builder()
    sources = [Path(__file__), BASE/'fetch_ubuntu.py', BASE/'run.py', BASE/'guest/seed_prepare.py',
               REPO/'ram-rescue-demo/build.py', REPO/'ram-rescue-demo/src/rescue.py',
               *sorted((REPO/'guard/admin').glob('*.py'))]
    record = {'schema': 1, 'source_sha256': {str(p.relative_to(REPO)): fetch_ubuntu.sha256(p) for p in sources},
              'scope': 'ordinary-user disposable files and QEMU only', 'passed': False,
              'kernel_release': release, 'source_commit': subprocess.check_output(
                  ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()}
    try:
        ubuntu = safe_directory(args.ubuntu_dir, existing=True) if args.ubuntu_dir else folder/'ubuntu'
        if not args.ubuntu_dir:
            fetch_ubuntu.fetch(ubuntu, args.ubuntu_build)
        record['ubuntu'] = verify_ubuntu(ubuntu)
        kernel = acquire_kernel(folder, args.kernel, release)
        base = folder/'base-rescue'
        builder.base_builder().build(base)
        image = build_seed_initrd(folder, base, release, builder)
        original = plain_initrd(folder, release)
        profile, seed_report = seed_vm(folder, kernel, image, ubuntu)
        enrollment = {key: profile[key] for key in ('schema', 'identity', 'guard')}
        enrollment['baseline'] = {'kernel_sha256': fetch_ubuntu.sha256(kernel),
            'initrd_sha256': fetch_ubuntu.sha256(original), 'lvmlocal_sha256': None,
            'grub_default_path': '/boot/grub/grub.cfg'}
        path = folder/'enrollment.json'
        path.write_text(json.dumps(enrollment, indent=2)+'\n'); path.chmod(0o600)
        build_dir = folder/'protected'
        record['build'] = builder.build(path, build_dir, kernel, base_rescue_dir=base)
        common = ['--build-dir', str(build_dir), '--enrollment', str(path), '--seed-report', str(seed_report)]
        commands = [
            [sys.executable, str(BASE/'cpp_integration_probe.py'), *common,
             '--binary', str(build_dir/'native-runtime/guard-runtime'), '--base-rescue-dir', str(base)],
            [sys.executable, str(BASE/'boot_failure_probe.py'), *common],
        ]
        record['commands'] = commands
        record['seed_report'] = str(seed_report)
        record['ordinary_initrd'] = str(original)
        if not args.prepare_only:
            for index, command in enumerate(commands):
                with (folder/f'acceptance-{index}.log').open('w') as log:
                    subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, cwd=REPO)
            record['passed'] = True
        record['prepared'] = True
    except BaseException:
        record['error'] = traceback.format_exc()
        raise
    finally:
        (folder/'reproduction.json').write_text(json.dumps(record, indent=2)+'\n')
        print('Reproduction report:', folder/'reproduction.json', flush=True)


if __name__ == '__main__':
    main()
