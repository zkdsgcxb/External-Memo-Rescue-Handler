#!/usr/bin/env python3
"""Compare read-only native/Python observers in a disposable full Ubuntu VM."""
import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

SOURCE = Path(__file__).resolve().parent
REPO = SOURCE.parents[1]
sys.path.insert(0, str(REPO / 'lab'))
import data_guard_probe as data
from efi_mount_probe import validate_inputs, wait_for_boot, stop_vm
from run import WORK, Channel
from auto_run import wait_for
import host_boot_probe as boot


GUEST = '''
_native_original_action = DataProbe.action
def _native_action(self, action):
    if action == 'native_diagnostics':
        return self.host('/usr/bin/journalctl', '-b', '--no-pager', '-u', 'ram-rescue-native-bench-*', check=False)
    if action == 'native_ready':
        self.gate()
        return self.host('/usr/bin/systemctl', 'is-active', 'dbus.service', check=False)['returncode'] == 0
    if action in ('native_compare', 'native_fault_start', 'native_fault_finish'):
        self.gate()
        sys.path.insert(0, '/opt/native')
        import benchmark
        config = self.read(self.directory(self.specs[0]['name']) / 'config.json')
        if action == 'native_fault_start':
            return benchmark.start_fault('/opt/native', config)
        if action == 'native_fault_finish':
            return benchmark.finish_fault()
        return benchmark.compare('/opt/native', config)
    return _native_original_action(self, action)
DataProbe.action = _native_action
'''


def initrd(folder, original):
    guest = folder / 'native-data-probe.py'
    guest.write_text(data.GUEST.read_text() + GUEST)
    old_guest, old_hook = data.GUEST, boot.HOOK
    data.GUEST = guest
    boot.HOOK += '\ncp -r /opt/native "$TOOLS/opt/native"\n'
    try:
        image = data.create_initrd(folder, original)
    finally:
        data.GUEST, boot.HOOK = old_guest, old_hook
    staging = folder / 'native-overlay'
    payload = staging / 'opt/native'
    payload.mkdir(parents=True)
    subprocess.run([sys.executable, str(SOURCE / 'build.py'), '--output', str(folder / 'build')], check=True)
    binary = folder / 'build/guard-observe'
    shutil.copy2(binary, payload / 'guard-observe')
    for name in ('benchmark.py', 'reference.py'):
        shutil.copy2(SOURCE / name, payload / name)
    runtime = payload / 'runtime'
    runtime.mkdir()
    for name in ('dm_monitor.py', 'linux_abi.py'):
        shutil.copy2(REPO / 'guard/runtime' / name, runtime / name)
    paths = [Path('.'), *sorted(p.relative_to(staging) for p in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=staging,
        input=b'\0'.join(str(p).encode() for p in paths) + b'\0',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    with image.open('ab') as output:
        output.write(gzip.compress(archive, mtime=0))
    hashes = {str(path.relative_to(payload)): boot.sha256(path) for path in payload.rglob('*') if path.is_file()}
    return image, hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    parser.add_argument('--fault-only', action='store_true', help='Read-only observer fault-detection correctness, no performance sampling')
    args = parser.parse_args()
    build_dir, seed_path, build_record, seed, digest = validate_inputs(args)
    folder = WORK / ('native-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    print('Native observer VM:', folder, flush=True)
    report = {'schema': 1, 'build': build_record, 'seed_report': str(seed_path),
              'source_image_sha256_before': digest, 'passed': False}
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            image, report['payload_sha256'] = initrd(folder, build_dir / 'initrd.img')
            command = data.vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            wait_for_boot(folder, vm)
            wait_for(lambda: data.ram_call(folder, 'native_ready'), 45, 'Ubuntu system bus')
            report['configure'] = data.ram_call(folder, 'configure')
            if args.fault_only:
                report['workers'] = data.ram_call(folder, 'start')
                report['before'] = data.ready(folder, {spec['name']: 0 for spec in data.SPECS})
                report['fault_start'] = data.ram_call(folder, 'native_fault_start')
                time.sleep(.4)
                removed = data.remove(qmp, data.SPECS[0])
                qmp.call('device_add', driver='usb-storage', bus='xhci.0', port='4',
                         id='native-decoy', drive='decoydisk', serial='NATIVE-UNREGISTERED')
                time.sleep(.2)
                attached = data.attach(qmp, data.SPECS[0])
                report['fault_gap_seconds'] = attached - removed
                report['recovered'] = data.ready(folder, {data.SPECS[0]['name']: 1, data.SPECS[1]['name']: 0})
                time.sleep(8)
                report['fault_result'] = data.ram_call(folder, 'native_fault_finish')
                report['audit'] = data.ram_call(folder, 'audit')
                if not all(row['ready_before_fault'] and row['detected_unavailable'] for row in report['fault_result'].values()):
                    raise RuntimeError('Observer did not detect the USB loss')
                rejected = report['fault_start']['rejected']
                if rejected['map_uuid']['returncode'] != 1 or rejected['initial_diskseq']['returncode'] != 2:
                    raise RuntimeError('Native observer accepted mismatched map/instance')
            else:
                report['comparison'] = data.ram_call(folder, 'native_compare', timeout=240)
            report['stop'] = data.ram_call(folder, 'stop')
            report['shutdown'] = boot.ram_call(folder, 'shutdown')
            vm.wait(timeout=150)
            report['passed'] = vm.returncode == 0
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                try:
                    report['diagnostic'] = data.ram_call(folder, 'native_diagnostics')
                except Exception:
                    report['diagnostic_error'] = traceback.format_exc()
        finally:
            stop_vm(vm)
            if qmp:
                qmp.close()
            report['source_image_sha256_after'] = boot.sha256(seed)
            report['source_image_unchanged'] = report['source_image_sha256_after'] == digest
            report['readonly_filesystem_audit'] = data.audit_images(folder)
            report['passed'] = (report['passed'] and report['source_image_unchanged'] and
                all(row['returncode'] == 0 for row in report['readonly_filesystem_audit'].values()))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Native report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Native observer benchmark failed')


if __name__ == '__main__':
    main()
