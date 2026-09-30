#!/usr/bin/env python3
"""Run the existing controller in an isolated ARM64 Linux 7 USB guest.

This is an Ubuntu kernel/userspace RAM image, not an installed Ubuntu desktop.
Only private image files are attached; no host devices, network or shares.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
import traceback

from guest.data_probe import DATA_WORKLOAD
from run import Channel


REPO = Path(__file__).resolve().parents[1]
RELEASE = '7.0.0-34-generic'
MODULES = ['usb_storage', 'uas', 'sd_mod', 'dm_multipath', 'dm_round_robin',
           'ext4', 'virtio_console', 'virtio_pci', 'xhci_pci']


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def kernel_image(source, target):
    """QEMU direct boot needs Image, while Ubuntu ships an EFI zboot image."""
    data = source.read_bytes()
    if data[0x38:0x3c] == b'ARMd':
        target.write_bytes(data)
    elif data[4:8] == b'zimg' and data[24:28] == b'zstd':
        offset, size = struct.unpack_from('<II', data, 8)
        if offset < 64 or size <= 0 or offset + size > len(data):
            raise ValueError('Invalid Ubuntu EFI zboot payload bounds')
        with target.open('wb') as stream:
            subprocess.run(['zstd', '-dc'], input=data[offset:offset + size],
                           stdout=stream, check=True)
    else:
        raise ValueError('Expected an ARM64 Image or Ubuntu zstd EFI zboot image')
    with target.open('rb') as stream:
        if stream.read(64)[0x38:0x3c] != b'ARMd':
            raise ValueError('Extracted kernel is not an ARM64 Image')


def prepare(tools, kernel_root, folder):
    if not folder.is_relative_to(REPO / 'lab/work') or folder.exists():
        raise ValueError('Use a new directory below lab/work')
    folder.mkdir(parents=True, mode=0o700)
    root = folder / 'root'
    root.mkdir()
    foreign = tools / 'arm64'
    for path in ['usr/lib/aarch64-linux-gnu', 'usr/lib/python3.12']:
        shutil.copytree(foreign / path, root / path, symlinks=True)
    for path in ['usr/bin/python3.12', 'usr/bin/busybox', 'usr/sbin/dmsetup', 'usr/sbin/blkid']:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(foreign / path, target)
        target.chmod(0o755)
    for name in ['lib', 'bin', 'sbin']:
        (root / name).symlink_to('usr/' + name)
    (root / 'usr/lib/ld-linux-aarch64.so.1').symlink_to('aarch64-linux-gnu/ld-linux-aarch64.so.1')
    (root / 'usr/bin/sh').symlink_to('busybox')
    (root / 'usr/bin/python3').symlink_to('python3.12')
    modules = root / 'usr/lib/modules' / RELEASE
    modules.mkdir(parents=True)
    original = kernel_root / 'lib/modules' / RELEASE
    for name in ['modules.builtin', 'modules.builtin.modinfo']:
        shutil.copyfile(original / name, modules / name)
    (modules / 'modules.order').write_text('')
    selected = set()
    for module in MODULES:
        output = subprocess.check_output(['modprobe', '-d', str(kernel_root),
            '-S', RELEASE, '--show-depends', module], text=True)
        selected.update(Path(line.split()[1]) for line in output.splitlines()
                        if line.startswith('insmod '))
    for source in sorted(selected):
        target = modules / source.relative_to(original)
        target = target.with_suffix('') if target.suffix == '.zst' else target
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as stream:
            subprocess.run(['zstd', '-dc', str(source)], stdout=stream, check=True)
    subprocess.run(['depmod', '-b', str(root), RELEASE], check=True)
    payload = root / 'opt/guard'
    payload.mkdir(parents=True)
    sources = [*sorted((REPO / 'guard/runtime').glob('*.py')),
               REPO / 'ram-rescue-demo/src/rescue.py']
    for source in sources:
        shutil.copyfile(source, payload / source.name)
    shutil.copyfile(REPO / 'lab/guest/arm64_probe.py', root / 'opt/probe.py')
    (root / 'opt/workload.py').write_text(DATA_WORKLOAD)
    for directory in ['proc', 'sys', 'dev', 'run', 'tmp', 'mnt', 'etc']:
        (root / directory).mkdir(exist_ok=True)
    (root / 'etc/fstab').write_text('')
    init = root / 'init'
    init.write_text('''#!/bin/sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
exec </dev/console >/dev/console 2>&1
set -e
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
''' + ''.join('/bin/busybox modprobe ' + name + '\n' for name in MODULES) + '''
echo 10 >/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs
/bin/busybox sleep 3
exec /usr/bin/python3.12 /opt/probe.py
''')
    init.chmod(0o755)
    paths = [Path('.'), *sorted(path.relative_to(root) for path in root.rglob('*'))]
    archive = subprocess.check_output(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'],
        cwd=root, input=b'\0'.join(str(path).encode() for path in paths) + b'\0',
        stderr=subprocess.DEVNULL)
    (folder / 'initramfs.cpio.gz').write_bytes(gzip.compress(archive, compresslevel=1, mtime=0))
    kernel = kernel_root / 'boot' / ('vmlinuz-' + RELEASE)
    kernel_image(kernel, folder / 'Image')
    disk = folder / 'usb.raw'
    with disk.open('xb') as stream:
        stream.truncate(80 * 1024**2)
    subprocess.run(['sfdisk', str(disk)], input='label: gpt\nstart=2048,size=131072,type=L\n',
                   text=True, stdout=subprocess.DEVNULL, check=True)
    partition = folder / 'partition.raw'
    with partition.open('xb') as stream:
        stream.truncate(64 * 1024**2)
    subprocess.run(['mkfs.ext4', '-q', '-F', str(partition)], check=True)
    with partition.open('rb') as source, disk.open('r+b') as target:
        target.seek(1024**2)
        shutil.copyfileobj(source, target)
    partition.unlink()
    command = [str(tools / 'root/usr/bin/qemu-system-aarch64'), '-machine', 'virt,gic-version=3',
        '-cpu', 'max', '-accel', 'tcg,thread=multi', '-m', '1024', '-smp', '2',
        '-nodefaults', '-display', 'none', '-nic', 'none', '-no-reboot',
        '-kernel', str(folder / 'Image'), '-initrd', str(folder / 'initramfs.cpio.gz'),
        '-append', 'console=ttyAMA0 rdinit=/init panic=-1',
        '-serial', 'file:' + str(folder / 'console.log'),
        '-qmp', 'unix:' + str(folder / 'qmp.sock') + ',server=on,wait=off',
        '-chardev', 'socket,id=agent,path=' + str(folder / 'agent.sock') + ',server=on,wait=off',
        '-device', 'virtio-serial-pci', '-device', 'virtserialport,chardev=agent,name=ram-rescue-agent',
        '-device', 'qemu-xhci,id=xhci', '-blockdev', json.dumps({'driver': 'raw',
        'node-name': 'usbdata', 'file': {'driver': 'file', 'filename': str(disk)}}),
        '-device', 'usb-storage,id=stick,bus=xhci.0,drive=usbdata,serial=RAMRESCUE-ARM64-USB']
    details = {'command': command, 'kernel_release': RELEASE,
        'kernel_sha256': sha256(kernel), 'initramfs_sha256': sha256(folder / 'initramfs.cpio.gz'),
        'boot_image_sha256': sha256(folder / 'Image'),
        'source_sha256': {str(path.relative_to(REPO)): sha256(path) for path in
            [*sources, REPO / 'lab/guest/arm64_probe.py', REPO / 'lab/guest/data_probe.py']}}
    (folder / 'build.json').write_text(json.dumps(details, indent=2) + '\n')
    return details


def run(folder):
    build = json.loads((folder / 'build.json').read_text())
    report = {'passed': False, 'build': build, 'scope': 'ARM64 Ubuntu kernel with RAM-only userspace'}
    channels = []
    with (folder / 'qemu.log').open('w') as output, (folder / 'qmp.jsonl').open('w') as qlog, \
            (folder / 'agent.jsonl').open('w') as glog:
        vm = subprocess.Popen(build['command'], stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            console = folder / 'console.log'
            while not console.exists() or 'ARM64_GUEST_READY' not in console.read_text(errors='replace'):
                if vm.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError('ARM64 guest boot failed; see console/qemu logs')
                time.sleep(.1)
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            guest = Channel(folder / 'agent.sock', glog)
            channels += [qmp, guest]

            def action(name):
                answer = guest.call(name, timeout=60)
                if not answer.get('ok'):
                    raise RuntimeError(answer)
                return answer['value']

            report['configured'] = action('configure')
            time.sleep(2)
            report['before'] = action('snapshot')
            qmp.call('device_del', id='stick')
            deadline = time.monotonic() + 15
            while not any(event['message'].get('event') == 'DEVICE_DELETED' for event in qmp.events):
                if time.monotonic() >= deadline:
                    raise RuntimeError('USB device removal was not acknowledged')
                time.sleep(.01)
            detached = time.monotonic()
            time.sleep(.2)
            qmp.call('device_add', driver='usb-storage', id='stick', bus='xhci.0',
                     drive='usbdata', serial='RAMRESCUE-ARM64-USB')
            report['requested_gap_seconds'] = .2
            report['host_reinsert_gap_seconds'] = time.monotonic() - detached
            deadline = time.monotonic() + 20
            while True:
                after = action('snapshot')
                report['last_recovery_snapshot'] = after
                if (after['state']['state'] == 'ready' and after['state']['recoveries'] == 1
                        and len(after['records']) > len(report['before']['records'])):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('ARM64 Guard did not recover in time: ' + repr(after['state']))
                time.sleep(.1)
            time.sleep(1)
            report['audit'] = action('audit')
            audit = report['audit']
            def data_mount(snapshot):
                return next(line for line in snapshot['mountinfo'].splitlines()
                            if line.split()[4] == '/mnt/data')
            report['checks'] = {
                'arm64_kernel': audit['abi']['machine'] == 'aarch64' and audit['kernel'] == RELEASE,
                'native_probe_ioctl': report['configured']['probe']['errno'] == 0,
                'original_guard': audit['guard_running'] and audit['guard_pid'] == report['before']['guard_pid'],
                'original_worker': audit['original_worker_alive_before_stop'] and audit['worker_pid'] == report['before']['worker_pid'],
                'all_io_successful': bool(audit['records']) and all(row['ok'] for row in audit['records']),
                'durable_data_matches': audit['direct_read_and_file_content_match'],
                'mount_preserved': data_mount(audit) == data_mount(report['before']),
                'cleanly_unmounted': audit['cleanly_unmounted'],
                'recovered': audit['state']['state'] == 'ready' and audit['state']['recoveries'] == 1,
                'sources_unchanged': all(sha256(REPO / path) == expected
                    for path, expected in build['source_sha256'].items()),
            }
            partition = folder / 'audit-partition.raw'
            with (folder / 'usb.raw').open('rb') as source, partition.open('xb') as target:
                source.seek(1024**2)
                remaining = 64 * 1024**2
                while remaining:
                    block = source.read(min(1024**2, remaining))
                    if not block:
                        raise RuntimeError('USB image is shorter than its partition')
                    target.write(block)
                    remaining -= len(block)
            checked = subprocess.run(['e2fsck', '-fn', str(partition)], text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            report['offline_fsck'] = {'returncode': checked.returncode, 'output': checked.stdout}
            report['checks']['offline_fsck_clean'] = checked.returncode == 0
            report['max_io_seconds'] = max(row['elapsed'] for row in audit['records'])
            report['passed'] = all(report['checks'].values())
        except BaseException:
            report['error'] = traceback.format_exc()
            raise
        finally:
            for channel in channels:
                channel.close()
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill()
                vm.wait()
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-root', type=Path)
    parser.add_argument('--kernel-root', type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--run-prepared', action='store_true')
    args = parser.parse_args()
    folder = args.work_dir.resolve()
    if not folder.is_relative_to(REPO / 'lab/work') or os.geteuid() == 0:
        parser.error('Run unprivileged with a directory below lab/work')
    if not args.run_prepared:
        if args.tools_root is None or args.kernel_root is None:
            parser.error('Preparing requires --tools-root and --kernel-root')
        prepare(args.tools_root.resolve(), args.kernel_root.resolve(), folder)
    if not args.prepare_only:
        report = run(folder)
        print(json.dumps({'passed': report['passed'], 'checks': report.get('checks')}))
        if not report['passed']:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
