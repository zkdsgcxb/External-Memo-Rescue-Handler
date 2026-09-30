#!/usr/bin/env python3
"""Validate two additional filesystem maps in a disposable protected Ubuntu VM.

Host inputs are the previously verified lab seed and regular image files under
lab/work. Only fresh overlays and disposable data images are writable; neither
host block devices nor network access are given to QEMU.
"""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import traceback

from auto_run import wait_for
import host_boot_probe as boot
from efi_mount_probe import validate_inputs, wait_for_boot, wait_for_deleted, stop_vm
from run import Channel, WORK, qemu_command


BASE = Path(__file__).resolve().parent
REPO = BASE.parent
GUEST = BASE / 'guest/data_probe.py'
PARTITION_BYTES = 64 * 1024**2
PARTITION_OFFSET = 2048 * 512
SPECS = [
    {'name': 'rr-data-vm-ext4', 'fs_type': 'ext4', 'port': '2',
     'serial': 'DATA-VM-EXT4', 'fs_uuid': 'ec994cec-166b-4d1e-8577-d2c3cc88a8c3',
     'partuuid': '4dba02a2-e343-4e9b-855e-af41b9500268',
     'map_uuid': 'RAMRESCUE-DATA-VM-EXT4'},
    {'name': 'rr-data-vm-vfat', 'fs_type': 'vfat', 'port': '3',
     'serial': 'DATA-VM-VFAT', 'fs_uuid': 'DA7A-0002',
     'partuuid': 'eea3f73f-d5e1-4501-9968-919ae5fe89dc',
     'map_uuid': 'RAMRESCUE-DATA-VM-VFAT'},
]


def source_hashes():
    sources = [Path(__file__).resolve(), GUEST, REPO / 'guard/data.py', REPO / 'guard/host_files.py', REPO / 'ram-rescue-demo/src/rescue.py',
               *sorted((REPO / 'guard/runtime').glob('*.py'))]
    return {str(p.relative_to(REPO)): boot.sha256(p) for p in sources}


def ram_call(folder, action, timeout=45):
    source = ("import sys,json,traceback;sys.path.insert(0,'/opt/vmprobe');import probe\n"
              "try:\n"
              f" value=probe.data_action({action!r})\n"
              " response={'ok':True,'value':value}\n"
              "except BaseException:\n"
              " response={'ok':False,'error':traceback.format_exc()}\n"
              "print('DATA_REPLY='+json.dumps(response),flush=True)\n")
    encoded = base64.b64encode(source.encode()).decode()
    command = f'python3 -c "import base64;exec(base64.b64decode(\'{encoded}\'))"\n'
    deadline = time.monotonic() + timeout
    record = {'action': action}
    output = b''
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(folder / 'rescue.sock'))
            sock.sendall(b'stty -echo\n')
            time.sleep(.05)
            sock.sendall(command.encode())
            while True:
                sock.settimeout(max(.001, deadline - time.monotonic()))
                if time.monotonic() >= deadline:
                    raise TimeoutError('RAM data request timed out: ' + action)
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeError('RAM data shell closed')
                output += chunk
                if len(output) > 8 * 1024**2:
                    raise RuntimeError('RAM data request exceeded output limit')
                _, found, reply = output.partition(b'DATA_REPLY=')
                if found and b'\n' in reply:
                    response = json.loads(reply.split(b'\n', 1)[0])
                    record['response'] = response
                    if not response['ok']:
                        raise RuntimeError(response['error'])
                    return response['value']
    except BaseException:
        record['error'] = traceback.format_exc()
        record['output_tail'] = output[-4096:].decode(errors='replace')
        raise
    finally:
        record['host_time'] = time.monotonic()
        with (folder / 'actions.jsonl').open('a') as stream:
            stream.write(json.dumps(record) + '\n')


def create_images(folder, seed):
    overlay = folder / 'usb.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw',
                    '-b', str(seed), str(overlay)], check=True)
    with (folder / 'decoy.raw').open('xb') as stream:
        stream.truncate(16 * 1024**2)
    for spec in SPECS:
        disk = folder / (spec['name'] + '.raw')
        with disk.open('xb') as stream:
            stream.truncate(80 * 1024**2)
        subprocess.run(['sfdisk', str(disk)], text=True, check=True,
                       stdout=subprocess.DEVNULL,
                       input=f"label: gpt\nstart=2048,size=131072,type=L,uuid={spec['partuuid']}\n")
        if spec['fs_type'] == 'ext4':
            partition = folder / 'ext4-format.raw'
            with partition.open('xb') as stream:
                stream.truncate(PARTITION_BYTES)
            subprocess.run(['mkfs.ext4', '-q', '-F', '-U', spec['fs_uuid'],
                            '-L', 'DATA-VM-EXT4', str(partition)], check=True)
            with partition.open('rb') as source, disk.open('r+b') as target:
                target.seek(PARTITION_OFFSET)
                shutil.copyfileobj(source, target)
            partition.unlink()
        else:
            subprocess.run(['mkfs.fat', '--offset=2048', '-F', '32', '-i',
                            spec['fs_uuid'].replace('-', ''), '-n', 'DATAVM',
                            str(disk), '65536'], check=True)
            wrong = folder / 'wrong-vfat.raw'
            shutil.copyfile(disk, wrong)
            subprocess.run(['mkfs.fat', '--offset=2048', '-F', '32', '-i', 'BAD00002',
                            '-n', 'WRONGVM', str(wrong), '65536'], check=True)
    return overlay


def create_initrd(folder, original):
    original_guest, original_hook = boot.GUEST, boot.HOOK
    try:
        boot.GUEST += '\n' + GUEST.read_text()
        boot.GUEST += f'\ndata_probe=DataProbe(gate,systemctl,process,{SPECS!r})\ndata_action=data_probe.action\n'
        boot.HOOK += ('\nmkdir -p "$TOOLS/opt/data-guard"\n'
                      'cp /opt/data-guard/* "$TOOLS/opt/data-guard/"\n'
                      'cp -r /opt/data-launcher-src /run/data-launcher-src\n'
                      'modprobe nls_iso8859-1\n')
        image = boot.overlay_initrd(folder, original)
    finally:
        boot.GUEST, boot.HOOK = original_guest, original_hook
    staging = folder / 'data-overlay'
    payload = staging / 'opt/data-guard'
    payload.mkdir(parents=True)
    for source in (REPO / 'guard/runtime').glob('*.py'):
        shutil.copyfile(source, payload / source.name)
    shutil.copyfile(REPO / 'ram-rescue-demo/src/rescue.py', payload / 'rescue.py')
    launcher = staging / 'opt/data-launcher-src'
    for relative in ['guard/data.py', 'guard/host_files.py', 'ram-rescue-demo/src/rescue.py',
                     *[str(p.relative_to(REPO)) for p in (REPO / 'guard/runtime').glob('*.py')]]:
        target = launcher / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / relative, target)
    paths = [Path('.'), *sorted(p.relative_to(staging) for p in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'],
                             cwd=staging, input=b'\0'.join(str(p).encode() for p in paths) + b'\0',
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    with image.open('ab') as target:
        target.write(gzip.compress(archive, mtime=0))
    return image


def vm_command(folder, build_dir, image, overlay, seed):
    masks = ' '.join('systemd.mask=' + name + '.service' for name in
                     ('lab-agent', 'lab-guard', 'lab-shell', 'lab-ready', 'lab-workload'))
    command = qemu_command(folder, same_port=True, kernel=build_dir / 'vmlinuz', initramfs=image,
                           extra_kernel_args='root=/dev/mapper/labrescue-ubuntu ro nompath ram_rescue_guard=1 ' + masks)
    command[command.index('-m') + 1] = '3072'
    index = command.index('-append') + 1
    command[index] = command[index].replace('rdinit=/init ', '').replace('panic=-1 ', '')
    for index, word in enumerate(command):
        if word == '-blockdev':
            value = json.loads(command[index + 1])
            if value['node-name'] == 'usbdisk':
                value.update(driver='qcow2', file={'driver': 'file', 'filename': str(overlay)},
                             backing={'driver': 'raw', 'read-only': True,
                                      'file': {'driver': 'file', 'filename': str(seed)}})
                command[index + 1] = json.dumps(value)
    for spec in SPECS:
        command += ['-blockdev', json.dumps({'driver': 'raw', 'node-name': spec['name'],
                    'file': {'driver': 'file', 'filename': str(folder / (spec['name'] + '.raw'))}}),
                    '-device', f"usb-storage,bus=xhci.0,port={spec['port']},id={spec['name']},drive={spec['name']},serial={spec['serial']}"]
    command += ['-blockdev', json.dumps({'driver': 'raw', 'node-name': 'wrong-vfat',
                'file': {'driver': 'file', 'filename': str(folder / 'wrong-vfat.raw')}})]
    return command


def remove(qmp, spec):
    started = time.monotonic()
    qmp.call('device_del', id=spec['name'])
    wait_for_deleted(qmp, spec['name'], started)
    return time.monotonic()


def attach(qmp, spec, *, wrong=False):
    qmp.call('device_add', driver='usb-storage', bus='xhci.0', port=spec['port'],
             id=spec['name'], drive='wrong-vfat' if wrong else spec['name'], serial=spec['serial'])
    return time.monotonic()


def ready(folder, expected, *, minimum_acks=None):
    def check():
        snapshot = ram_call(folder, 'snapshot')
        for name, count in expected.items():
            device = snapshot['devices'][name]
            state = device['state'] or {}
            if state.get('state') in ('expired', 'failed', 'interrupted', 'blocked'):
                raise RuntimeError('Data Guard reached terminal outcome: ' + json.dumps(device))
            if (state.get('state') != 'ready' or state.get('recoveries', 0) < count or
                    device['ack_count'] < (minimum_acks or {}).get(name, 1)):
                return None
            if device['errors']:
                raise RuntimeError('Data workload observed an error: ' + json.dumps(device))
        return snapshot
    return wait_for(check, 25, 'data maps ready and original workloads progressing')


def run_scenarios(folder, qmp, vm, report):
    wait_for_boot(folder, vm)
    report['root_before'] = boot.ram_call(folder, 'snapshot')
    report['configure'] = ram_call(folder, 'configure')
    report['healthy_resources'] = ram_call(folder, 'measure_idle')
    report['root_worker'] = boot.ram_call(folder, 'start_workload')
    report['workers_started'] = ram_call(folder, 'start')
    zero = {spec['name']: 0 for spec in SPECS}
    report['before'] = ready(folder, zero, minimum_acks={name: 5 for name in zero})
    ext4, vfat = SPECS
    deleted = remove(qmp, ext4)
    qmp.call('device_add', driver='usb-storage', bus='xhci.0', port='4',
             id='data-decoy', drive='decoydisk', serial='DATA-DECOY')
    time.sleep(max(0, .2 - (time.monotonic() - deleted)))
    added = attach(qmp, ext4)
    report['ext4_gap'] = {'deleted': deleted, 'added': added, 'seconds': added - deleted}
    one = {ext4['name']: 1, vfat['name']: 0}
    report['ext4_recovered'] = ready(folder, one, minimum_acks={name: report['before']['devices'][name]['ack_count'] + 2 for name in zero})
    # Same USB identity and GPT PARTUUID, deliberately different FAT UUID.
    # Guard must reject it without ever loading it into the stable map.
    deleted = remove(qmp, vfat)
    time.sleep(.2)
    wrong_added = attach(qmp, vfat, wrong=True)
    def rejected():
        snapshot = ram_call(folder, 'snapshot')
        state = snapshot['devices'][vfat['name']]['state'] or {}
        if state.get('state') in ('expired', 'failed', 'blocked'):
            raise RuntimeError('Wrong candidate exhausted recovery budget')
        return snapshot if state.get('state') == 'rejected' and 'filesystem' in state.get('reason', '').lower() else None
    report['wrong_identity'] = wait_for(rejected, 5, 'wrong filesystem UUID refusal')
    wrong_deleted = remove(qmp, vfat)
    time.sleep(.2)
    added = attach(qmp, vfat)
    report['vfat_wrong_gap'] = {'deleted': deleted, 'wrong_added': wrong_added,
                                'wrong_deleted': wrong_deleted, 'correct_added': added,
                                'seconds': added - deleted}
    both = {spec['name']: 1 for spec in SPECS}
    report['vfat_recovered'] = ready(folder, both)
    first_deleted = remove(qmp, ext4)
    second_deleted = remove(qmp, vfat)
    time.sleep(.2)
    first_added = attach(qmp, ext4)
    second_added = attach(qmp, vfat)
    report['joint_gap'] = {'ext4_seconds': first_added - first_deleted,
                            'vfat_seconds': second_added - second_deleted}
    twice = {spec['name']: 2 for spec in SPECS}
    report['joint_recovered'] = ready(folder, twice, minimum_acks={name: report['vfat_recovered']['devices'][name]['ack_count'] + 2 for name in zero})
    report['root_after'] = boot.ram_call(folder, 'snapshot')
    report['audit'] = ram_call(folder, 'audit')
    report['root_audit'] = boot.ram_call(folder, 'stop_and_audit')
    report['logs'] = ram_call(folder, 'logs')
    report['stop'] = ram_call(folder, 'stop')
    before, after = report['before'], report['joint_recovered']
    checks = {
        'root_guard_stayed_ready': report['root_before']['guard_ready'] and report['root_after']['guard_ready'],
        'root_guard_did_not_recover_unrelated_disks': report['root_after']['state']['recoveries'] == report['root_before']['state']['recoveries'],
        'same_boot_and_pid1': before['boot_id'] == after['boot_id'] and before['pid1']['start_ticks'] == after['pid1']['start_ticks'],
        'ext4_device_name_changed': before['devices'][ext4['name']]['slaves'] != report['ext4_recovered']['devices'][ext4['name']]['slaves'],
        'wrong_identity_never_loaded': (
            report['wrong_identity']['devices'][vfat['name']]['state']['recoveries'] == 0 and
            report['wrong_identity']['devices'][vfat['name']]['dm_active'] == before['devices'][vfat['name']]['dm_active'] and
            'UUID does not match' in report['wrong_identity']['devices'][vfat['name']]['state'].get('reason', '')),
        'unrelated_map_kept_progressing': report['ext4_recovered']['devices'][vfat['name']]['ack_count'] > before['devices'][vfat['name']]['ack_count'],
        'root_acknowledged_data_matches': report['root_audit']['prefix_matches'],
        'root_remains_writable': report['root_audit']['root_write_fsync'],
        'launcher_stop_retains_maps_and_exclusions': all(
            result['map']['uuid'] == spec['map_uuid'] and result['rule_retained']
            for spec in SPECS for result in [report['stop']['after_launcher_stop'][spec['name']]]),
        'data_maps_removed_cleanly': all(not device['dm'] and not device['mount']
                                       for device in report['stop']['snapshot']['devices'].values()),
    }
    for name in zero:
        initial, final = before['devices'][name], after['devices'][name]
        checks[name + '_same_open_fd_process'] = initial['process']['pid'] == final['process']['pid'] and initial['process']['start_ticks'] == final['process']['start_ticks']
        checks[name + '_mount_survived'] = initial['mount'] == final['mount']
        checks[name + '_two_recoveries'] = final['state']['recoveries'] == 2
        checks[name + '_no_io_errors'] = not final['errors']
        checks[name + '_durable_hash_matches'] = report['audit']['audit'][name]['hash_matches']
    report['checks'] = checks
    report['shutdown'] = boot.ram_call(folder, 'shutdown')
    try:
        vm.wait(timeout=150)
        checks['graceful_shutdown'] = vm.returncode == 0
    except subprocess.TimeoutExpired:
        checks['graceful_shutdown'] = False
    report['passed'] = all(checks.values())


def audit_images(folder):
    audits = {}
    for spec in SPECS:
        partition = folder / (spec['name'] + '-audit.raw')
        with (folder / (spec['name'] + '.raw')).open('rb') as source, partition.open('xb') as target:
            source.seek(PARTITION_OFFSET)
            content = source.read(PARTITION_BYTES)
            if len(content) != PARTITION_BYTES:
                raise RuntimeError('Disposable image has truncated partition')
            target.write(content)
        command = ['e2fsck', '-fn'] if spec['fs_type'] == 'ext4' else ['fsck.fat', '-n']
        result = subprocess.run([*command, str(partition)], text=True, timeout=30,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        audits[spec['name']] = {'returncode': result.returncode, 'output': result.stdout}
    return audits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    try:
        build_dir, seed_path, build, seed, before_hash = validate_inputs(args)
    except ValueError as error:
        parser.error(str(error))
    folder = WORK / ('data-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = source_hashes()
    report = {'schema': 1, 'scope': 'two precreated plain-filesystem maps, shared Guard controller',
              'build': build, 'seed_report': str(seed_path), 'source_sha256': sources,
              'source_image_sha256_before': before_hash, 'specs': SPECS, 'passed': False}
    print('Additional data-map VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = create_images(folder, seed)
            report['wrong_image_sha256_before'] = boot.sha256(folder / 'wrong-vfat.raw')
            image = create_initrd(folder, build_dir / 'initrd.img')
            if source_hashes() != sources:
                raise RuntimeError('Implementation changed during VM preparation')
            command = vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            (folder / 'command.json').write_text(json.dumps(command, indent=2) + '\n')
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            run_scenarios(folder, qmp, vm, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                for action in ('snapshot', 'logs'):
                    try:
                        report['failure_' + action] = ram_call(folder, action)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            stop_vm(vm)
            if qmp:
                qmp.close()
            report['source_image_sha256_after'] = boot.sha256(seed)
            report['source_image_unchanged'] = report['source_image_sha256_after'] == before_hash
            report['sources_unchanged'] = source_hashes() == sources
            report['wrong_image_unchanged'] = (boot.sha256(folder / 'wrong-vfat.raw') ==
                                               report.get('wrong_image_sha256_before'))
            try:
                report['readonly_filesystem_audit'] = audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged']
                                    and report['sources_unchanged'] and report['wrong_image_unchanged']
                                    and all(audit['returncode'] == 0 for audit in report.get('readonly_filesystem_audit', {}).values())
                                    and not report.get('filesystem_audit_error'))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Data-map report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Additional data-map acceptance failed')


if __name__ == '__main__':
    main()
