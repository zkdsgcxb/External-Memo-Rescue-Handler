#!/usr/bin/env python3
"""One isolated ordinary Ubuntu VM, real fixed CLI, inert candidate lifecycle."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import traceback

import data_guard_probe as data
import standalone_data_probe as standalone
from auto_run import wait_for
from efi_mount_probe import stop_vm
from run import Channel, WORK

REPO = Path(__file__).resolve().parents[1]
GUEST = REPO / 'lab/guest/candidate_probe.py'
UNIT = '''[Unit]
Description=Disposable candidate acceptance observer
After=local-fs.target systemd-udevd.service
ConditionKernelCommandLine=ram_rescue_candidate_test=1
[Service]
Type=simple
ExecStart=/usr/bin/python3 /run/candidate/probe.py
StandardInput=tty
StandardOutput=tty
StandardError=journal
TTYPath=/dev/ttyS2
Restart=no
TimeoutStopSec=3
'''
HOOK = '''#!/bin/sh
set -eu
case "${1:-}" in prereqs) exit 0 ;; esac
case " $(cat /proc/cmdline) " in *" ram_rescue_candidate_test=1 "*) ;; *) exit 0 ;; esac
[ "$(cat /sys/class/dmi/id/product_name)" = RAMRescueLab ] || exit 0
mkdir -p /run/candidate /run/systemd/system/multi-user.target.wants
cp -a /opt/candidate/. /run/candidate/
cp /opt/candidate/candidate-probe.service /run/systemd/system/
ln -s ../candidate-probe.service /run/systemd/system/multi-user.target.wants/candidate-probe.service
'''


def sha(path):
    return standalone.sha256(path)


def allocated(root):
    size = 0
    for path in [root, *root.rglob('*')]:
        try:
            size += path.lstat().st_blocks * 512
        except FileNotFoundError:
            # QEMU removes sockets at shutdown; downloads atomically rename
            # their temporary files. A vanished entry consumes no allocation.
            continue
    return size


def budget(root):
    used = allocated(root)
    if used > 6 * 1024**3:
        raise RuntimeError('Stop with 2 GiB margin before the authorized 8 GiB allocation limit')
    return used


def create_initrd(folder, inputs, package, reference, source, fixture):
    unpacked = folder / 'unpacked'
    subprocess.run(['unmkinitramfs', str(inputs['initrd']), str(unpacked)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    orders = list(unpacked.glob('*/scripts/init-bottom/ORDER'))
    if len(orders) != 1 or 'ram-rescue-guard' in orders[0].read_text():
        raise RuntimeError('Need ordinary nonprotected initramfs')
    stage = folder / 'initrd-overlay'
    payload = stage / 'opt/candidate'
    payload.mkdir(parents=True)
    shutil.copyfile(GUEST, payload / 'probe.py')
    shutil.copyfile(inputs['package'], payload / 'handler.deb')
    shutil.copytree(reference / 'reference', payload / 'reference')
    (payload / 'fixture.json').write_text(json.dumps(fixture, indent=2) + '\n')
    (payload / 'candidate-probe.service').write_text(UNIT)
    files = payload / 'kernel-files'
    image = files / 'boot' / ('vmlinuz-' + source['release'])
    image.parent.mkdir(parents=True)
    shutil.copyfile(inputs['kernel'], image)
    record = json.loads((reference / 'report.json').read_text())
    module_root = Path(record['module_root'])
    target_root = files / 'usr/lib/modules' / source['release']
    target_root.mkdir(parents=True)
    for name in source['metadata']:
        shutil.copyfile(module_root / name, target_root / name)
    for entry in source['modules'].values():
        if entry['builtin']:
            continue
        relative = Path(entry['path']).relative_to('/usr/lib/modules/' + source['release'])
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(module_root / relative, target)
    hook = stage / 'scripts/init-bottom/candidate-test'
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK); hook.chmod(0o755)
    (hook.parent / 'ORDER').write_text(orders[0].read_text() + '\n/scripts/init-bottom/candidate-test "$@"\n')
    for path in stage.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)
        elif path != hook:
            path.chmod(0o644)
    paths = [Path('.'), *sorted(path.relative_to(stage) for path in stage.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=stage,
        input=b'\0'.join(str(path).encode() for path in paths) + b'\0', capture_output=True, check=True).stdout
    image = folder / 'initrd.img'
    shutil.copyfile(inputs['initrd'], image)
    with image.open('ab') as stream:
        stream.write(gzip.compress(archive, mtime=0))
    return image


def validate_command(command, folder, seed):
    if command[command.index('-m') + 1] != '3072' or command[command.index('-smp') + 1] != '2':
        raise RuntimeError('VM resource configuration differs')
    if command[command.index('-nic') + 1] != 'none':
        raise RuntimeError('VM network must remain disabled')
    seen = []
    def inspect(value):
        if isinstance(value, dict):
            if value.get('driver') == 'file':
                path = Path(value['filename'])
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(WORK.resolve()):
                    raise RuntimeError('QEMU input is not a regular lab artifact')
                seen.append(str(path.resolve()))
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)
    for index, arg in enumerate(command):
        if arg == '-blockdev':
            inspect(json.loads(command[index + 1]))
    return seen


def run(reproduction, package_dir, reference, folder):
    # Existing validation performs the one necessary full seed hash. QEMU opens
    # its backing read-only; final checks compare backing inode/size/timestamps.
    inputs, package = standalone.validate_inputs(reproduction, package_dir)
    source = json.loads((reference / 'reference/source.json').read_text())
    if sha(inputs['kernel']) != source['image']['sha256']:
        raise RuntimeError('Boot kernel differs from authenticated reference')
    folder.mkdir(mode=0o700)
    scope_root = folder.parent
    native_previous = json.loads((REPO / 'lab/work/standalone-package-v9-20261005/package.json').read_text())['runtime']['native_runtime']
    sources = [Path(__file__), GUEST, REPO / 'lab/kernel_reference.py', REPO / 'lab/tests/test_current_support.py']
    source_hashes = {str(path.relative_to(REPO)): sha(path) for path in sources}
    fixture = {'package': package, 'kernel': {'release': source['release']}, 'spec': data.SPECS[0],
               'test_source_sha256': hashlib.sha256(json.dumps(source_hashes, sort_keys=True).encode()).hexdigest()}
    report = {'schema': 1, 'passed': False, 'scope': 'inert_candidate_fixed_cli_on_ordinary_ubuntu',
              'package': package, 'reference_sha256': sha(reference / 'reference/source.json'),
              'source_sha256': source_hashes, 'input_sha256': {name: sha(path) for name, path in inputs.items() if name != 'seed'},
              'seed_sha256': json.loads((reproduction / 'seed/report.json').read_text())['source_image_sha256_after'],
              'resource_limits': {'vcpus': 2, 'ram_mib': 3072, 'additional_disk_max_bytes': 8 * 1024**3, 'concurrent_vms': 1},
              'reused_native_binary_and_libraries': {key: package['runtime']['native_runtime'][key] == native_previous[key]
                                                    for key in ('binary_sha256', 'library_sha256', 'entrypoint_sha256')}}
    info = inputs['seed'].stat()
    seed_before = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    vm = qmp = channel = None
    logs = []
    monitor_stop = threading.Event()
    resource_errors = []
    def monitor():
        while not monitor_stop.wait(2):
            try:
                budget(scope_root)
            except Exception as exc:
                resource_errors.append(str(exc))
                if vm is not None and vm.poll() is None:
                    vm.kill()
                return
    watcher = threading.Thread(target=monitor, daemon=True)
    try:
        overlay = data.create_images(folder, inputs['seed'])
        image = create_initrd(folder, inputs, package, reference, source, fixture)
        command = standalone.vm_command(folder, inputs, image, overlay)
        flag = command.index('-append') + 1
        command[flag] = command[flag].replace('ram_rescue_standalone_test=1', 'ram_rescue_candidate_test=1')
        report['regular_block_inputs'] = validate_command(command, folder, inputs['seed'])
        report['command'] = command
        report['initrd_sha256'] = sha(image)
        report['allocated_before_vm'] = budget(scope_root)
        watcher.start()
        for boot in (1, 2):
            log = (folder / ('qemu-%s.log' % boot)).open('w')
            qlog = (folder / ('qmp-%s.jsonl' % boot)).open('w')
            actions = (folder / ('actions-%s.jsonl' % boot)).open('w')
            logs.extend((log, qlog, actions))
            print('Starting candidate VM boot', boot, flush=True)
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 15, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            channel = Channel(folder / 'rescue.sock', actions)
            wait_for(lambda: any(row['message'].get('ready') for row in channel.events), 180, 'candidate observer')
            sequence = ('prepare', 'prerequisites', 'qualify', 'lifecycle') if boot == 1 else ('reboot_check',)
            for action in sequence:
                print('Candidate VM action:', action, flush=True)
                budget(scope_root)
                value = standalone.call(channel, action, timeout=600)
                report[action] = value
                (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
                if action == 'prerequisites' and not value['passed']:
                    raise RuntimeError('Actual CLI prerequisite checks did not pass; raw plan preserved')
            standalone.call(channel, 'shutdown', timeout=30)
            vm.wait(timeout=90)
            if vm.returncode != 0:
                raise RuntimeError('VM did not shut down cleanly')
            channel.close(); qmp.close(); channel = qmp = None
            for name in ('qmp.sock', 'rescue.sock', 'agent.sock'):
                (folder / name).unlink(missing_ok=True)
        report['passed'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
        if channel is not None and vm is not None and vm.poll() is None:
            try:
                report['failure_diagnostics'] = standalone.call(channel, 'diagnostics', timeout=240)
            except Exception:
                report['diagnostics_error'] = traceback.format_exc()
    finally:
        stop_vm(vm)
        monitor_stop.set()
        if watcher.ident is not None:
            watcher.join(timeout=10)
        for connection in (qmp, channel):
            if connection is not None:
                connection.close()
        for stream in logs:
            stream.close()
        final = inputs['seed'].stat()
        report['seed_readonly_backing_stat_unchanged'] = seed_before == (final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
        report['allocated_after_vm'] = allocated(scope_root)
        report['resource_monitor_errors'] = resource_errors
        report['passed'] = (report['passed'] and report['seed_readonly_backing_stat_unchanged']
                            and not resource_errors and not watcher.is_alive()
                            and report['allocated_after_vm'] <= report['resource_limits']['additional_disk_max_bytes'])
        report['host_deployed'] = False
        report['active_protection_enabled'] = False if report['passed'] else None
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('Candidate VM report:', folder / 'report.json', 'passed:', report['passed'], flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reproduction-dir', type=Path, required=True)
    parser.add_argument('--package-dir', type=Path, required=True)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for path in (args.reproduction_dir, args.package_dir, args.reference_dir, args.output):
        if not path.resolve().is_relative_to(WORK.resolve()) or path.is_symlink():
            raise SystemExit('Use only lab/work artifacts')
    run(args.reproduction_dir.resolve(), args.package_dir.resolve(), args.reference_dir.resolve(), args.output.resolve())
