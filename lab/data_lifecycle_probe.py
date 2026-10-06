#!/usr/bin/env python3
"""Bounded isolated VM acceptance of native host-data safe exit/rearm (A)."""
import argparse
import ast
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import traceback

import candidate_probe as candidate
import data_guard_probe as data
import standalone_data_probe as standalone
from auto_run import wait_for
from efi_mount_probe import stop_vm
from run import Channel, WORK

REPO = Path(__file__).resolve().parents[1]
GUEST = REPO / 'lab/guest/data_lifecycle_probe.py'
UNIT = candidate.UNIT.replace('candidate', 'data-lifecycle').replace('ram_rescue_data-lifecycle_test', 'ram_rescue_data_lifecycle_test')
HOOK = candidate.HOOK.replace('candidate', 'data-lifecycle').replace('ram_rescue_data-lifecycle_test', 'ram_rescue_data_lifecycle_test')


def create_initrd(folder, inputs, fixture):
    unpacked = folder / 'unpacked'
    subprocess.run(['unmkinitramfs', str(inputs['initrd']), str(unpacked)], check=True, capture_output=True)
    orders = list(unpacked.glob('*/scripts/init-bottom/ORDER'))
    if len(orders) != 1 or 'ram-rescue-guard' in orders[0].read_text():
        raise RuntimeError('Need ordinary unprotected initramfs')
    stage = folder / 'initrd-overlay'
    payload = stage / 'opt/data-lifecycle'; payload.mkdir(parents=True)
    shutil.copyfile(GUEST, payload / 'probe.py')
    shutil.copyfile(inputs['package'], payload / 'handler.deb')
    workload_source = REPO / 'lab/guest/data_probe.py'
    assignment = next(node for node in ast.parse(workload_source.read_text()).body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'DATA_WORKLOAD' for target in node.targets))
    (payload / 'workload.py').write_text(ast.literal_eval(assignment.value))
    (payload / 'fixture.json').write_text(json.dumps(fixture, indent=2) + '\n')
    (payload / 'data-lifecycle-probe.service').write_text(UNIT)
    hook = stage / 'scripts/init-bottom/data-lifecycle-test'; hook.parent.mkdir(parents=True)
    hook.write_text(HOOK); hook.chmod(0o755)
    (hook.parent / 'ORDER').write_text(orders[0].read_text() + '\n/scripts/init-bottom/data-lifecycle-test "$@"\n')
    for path in stage.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)
        elif path != hook:
            path.chmod(0o644)
    paths = [Path('.'), *sorted(path.relative_to(stage) for path in stage.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=stage,
        input=b'\0'.join(str(path).encode() for path in paths) + b'\0', check=True, capture_output=True).stdout
    image = folder / 'initrd.img'; shutil.copyfile(inputs['initrd'], image)
    with image.open('ab') as stream:
        stream.write(gzip.compress(archive, mtime=0))
    return image


def run(reproduction, package_dir, folder):
    inputs, package = standalone.validate_inputs(reproduction, package_dir)
    folder.mkdir(mode=0o700)
    scope_root = folder.parent
    sources = [Path(__file__), GUEST, REPO / 'lab/guest/data_probe.py', REPO / 'lab/candidate_probe.py',
               REPO / 'lab/standalone_data_probe.py', REPO / 'lab/data_guard_probe.py']
    report = {'schema': 1, 'passed': False, 'scope': 'native_host_data_exit_rearm_engineering_A',
        'package': package, 'source_sha256': {str(path.relative_to(REPO)): candidate.sha(path) for path in sources},
        'input_sha256': {key: candidate.sha(path) for key, path in inputs.items() if key != 'seed'},
        'seed_sha256': json.loads((reproduction / 'seed/report.json').read_text())['source_image_sha256_after'],
        'resource_limits': {'vcpus': 2, 'ram_mib': 3072, 'additional_disk_max_bytes': 8 * 1024**3, 'concurrent_vms': 1},
        'host_deployed': False, 'first_enable_cli_tested': False, 'qualification_consumed': False,
        'recovery_evidence_inherited_from_previous_elf': False}
    info = inputs['seed'].stat(); seed_before = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    vm = qmp = guest = None; streams = []; errors = []; monitor_stop = threading.Event()
    def monitor():
        while not monitor_stop.wait(2):
            try:
                candidate.budget(scope_root)
            except Exception as error:
                errors.append(str(error))
                if vm is not None and vm.poll() is None:
                    vm.kill()
                return
    watcher = threading.Thread(target=monitor, daemon=True)
    def call(action, timeout=90):
        print('A VM action:', action, flush=True)
        value = standalone.call(guest, action, timeout=timeout)
        report.setdefault('actions', []).append({'action': action, 'value': value})
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        return value
    try:
        overlay = data.create_images(folder, inputs['seed'])
        image = create_initrd(folder, inputs, {'package': package, 'spec': data.SPECS[0]})
        command = standalone.vm_command(folder, inputs, image, overlay)
        flag = command.index('-append') + 1
        command[flag] = command[flag].replace('ram_rescue_standalone_test=1', 'ram_rescue_data_lifecycle_test=1')
        report['regular_block_inputs'] = candidate.validate_command(command, folder, inputs['seed'])
        report['command'] = command; report['initrd_sha256'] = candidate.sha(image)
        report['allocated_before_vm'] = candidate.budget(scope_root)
        watcher.start()
        log, qlog, actions = ((folder / name).open('w') for name in ('qemu.log', 'qmp.jsonl', 'actions.jsonl'))
        streams.extend((log, qlog, actions))
        vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        wait_for(lambda: (folder / 'qmp.sock').exists(), 15, 'A QMP socket')
        qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
        guest = Channel(folder / 'rescue.sock', actions)
        wait_for(lambda: any(row['message'].get('ready') for row in guest.events), 180, 'A observer')
        call('prepare', 180); call('busy_cases'); call('safe_stop'); call('fence_busy_reentry'); call('rearm')
        before = call('start_workload')
        removed_at = time.monotonic()
        data.remove(qmp, data.SPECS[0])
        call('gap_stop', 5)
        data.attach(qmp, data.SPECS[0])
        report['usb_gap_command_seconds'] = time.monotonic() - removed_at
        def recovered():
            value = standalone.call(guest, 'snapshot')
            if value['state'].get('state') in ('expired', 'failed', 'interrupted') or value['errors']:
                raise RuntimeError('Recovery failed: ' + json.dumps(value))
            if value['state'].get('recoveries', 0) >= 1 and value['acks'] >= before['acks'] + 3:
                assert value['worker_pid'] == before['worker_pid'] and value['worker_poll'] is None
                return value
            return None
        report['recovery'] = wait_for(recovered, 25, 'original worker recovery')
        call('finish_workload'); call('safe_stop'); call('rearm'); call('kill_rearm_reject'); call('diagnostics')
        call('shutdown', 30); vm.wait(timeout=90)
        if vm.returncode != 0:
            raise RuntimeError('VM did not shut down cleanly')
        report['passed'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
        if guest is not None and vm is not None and vm.poll() is None:
            try:
                report['failure_diagnostics'] = standalone.call(guest, 'diagnostics', timeout=60)
            except Exception:
                report['diagnostics_error'] = traceback.format_exc()
    finally:
        stop_vm(vm); monitor_stop.set()
        if watcher.ident is not None:
            watcher.join(timeout=10)
        for channel in (qmp, guest):
            if channel is not None:
                channel.close()
        for stream in streams:
            stream.close()
        info = inputs['seed'].stat()
        report['seed_readonly_backing_stat_unchanged'] = seed_before == (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        report['allocated_after_vm'] = candidate.allocated(scope_root); report['resource_monitor_errors'] = errors
        report['passed'] = (report['passed'] and report['seed_readonly_backing_stat_unchanged'] and not errors and
            not watcher.is_alive() and report['allocated_after_vm'] <= report['resource_limits']['additional_disk_max_bytes'])
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('A VM report:', folder / 'report.json', 'passed:', report['passed'], flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reproduction-dir', type=Path, required=True)
    parser.add_argument('--package-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for path in (args.reproduction_dir, args.package_dir, args.output):
        if path.is_symlink() or not path.resolve().is_relative_to(WORK.resolve()):
            raise SystemExit('Use only lab/work artifacts')
    run(args.reproduction_dir.resolve(), args.package_dir.resolve(), args.output.resolve())
