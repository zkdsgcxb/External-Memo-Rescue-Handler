#!/usr/bin/env python3
"""Accept independently installed data Guards in disposable ordinary Ubuntu.

No root Guard, protection initrd, historical checkout, network or host block
device enters this scenario. The observer creates only the VM's test maps;
the installed production package owns their automatic recovery.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
import traceback

import data_guard_probe as data
from auto_run import wait_for
from efi_mount_probe import stop_vm
from run import Channel, WORK

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
GUEST = BASE / 'guest/standalone_data_probe.py'

HOOK = '''#!/bin/sh
set -eu
case "${1:-}" in prereqs) exit 0 ;; esac
case " $(cat /proc/cmdline) " in *" ram_rescue_standalone_test=1 "*) ;; *) exit 0 ;; esac
[ "$(cat /sys/class/dmi/id/product_name)" = RAMRescueLab ] || exit 0
mkdir -p /run/standalone /run/systemd/system/multi-user.target.wants
cp /opt/standalone/* /run/standalone/
cp /opt/standalone/standalone-probe.service /run/systemd/system/
ln -s ../standalone-probe.service /run/systemd/system/multi-user.target.wants/standalone-probe.service
insmod /opt/standalone/autofs.ko
modprobe nls_iso8859-1
'''
UNIT = '''[Unit]
Description=Disposable standalone data acceptance observer
After=local-fs.target systemd-udevd.service
ConditionKernelCommandLine=ram_rescue_standalone_test=1
[Service]
Type=simple
ExecStart=/usr/bin/python3 /run/standalone/probe.py
StandardInput=tty
StandardOutput=tty
StandardError=journal
TTYPath=/dev/ttyS2
Restart=no
TimeoutStopSec=3
'''


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular_lab_file(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or not path.resolve().is_relative_to(WORK.resolve()):
        raise ValueError('Expected a regular disposable artifact below lab/work')
    return path.resolve()


def validate_inputs(folder, package_dir):
    if os.geteuid() == 0:
        raise ValueError('This VM test never needs host root')
    folder = Path(folder).resolve(strict=True)
    seed_report = regular_lab_file(folder / 'seed/report.json')
    seed = json.loads(seed_report.read_text())
    identity = seed.get('creation', {}).get('identity', {})
    if not seed.get('passed') or identity.get('usb_serial') != 'RAMRESCUE-LAB-001' or identity.get('vg_name') != 'labrescue':
        raise ValueError('Expected the freshly created disposable Ubuntu LVM seed')
    inputs = {name: regular_lab_file(folder / filename) for name, filename in (
        ('seed', 'seed/s0/usb.raw'), ('kernel', 'vmlinuz'), ('initrd', 'original-initrd.img'))}
    if sha256(inputs['seed']) != seed['source_image_sha256_after']:
        raise ValueError('Seed digest changed')
    package = json.loads(regular_lab_file(Path(package_dir) / 'package.json').read_text())
    inputs['package'] = regular_lab_file(Path(package_dir) / package['package'])
    if sha256(inputs['package']) != package['sha256'] or not (package.get('maintainer_scripts') is False or
            package.get('maintainer_scripts') == ['prerm'] and package.get('automatic_activation') is False):
        raise ValueError('Expected an unchanged package without automatic maintainer scripts')
    return inputs, package


def guest_source():
    sources = [BASE / 'guest/data_probe.py', BASE / 'guest/unified_probe.py',
               BASE / 'guest/mount_probe.py', GUEST]
    combined = '\n'.join(path.read_text() for path in sources)
    return combined.replace('from dm_monitor import DeviceMapper', 'from dm_observer import DeviceMapper').replace(
        'from path_guard import table', 'from dm_observer import table').replace(
        "['/bin/chroot', str(self.ROOT),", '[')


def create_initrd(folder, original, package, metadata, *, kernel=None):
    unpacked = folder / 'unpacked'
    subprocess.run(['unmkinitramfs', str(original), str(unpacked)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    candidates = list(unpacked.glob('*/scripts/init-bottom/ORDER'))
    if len(candidates) != 1:
        raise RuntimeError('Expected one ordinary initramfs init-bottom order')
    order = candidates[0].read_text()
    if 'ram-rescue-guard' in order:
        raise RuntimeError('Standalone acceptance refuses a protected root initrd')
    staging = folder / 'overlay'
    payload = staging / 'opt/standalone'
    payload.mkdir(parents=True)
    (payload / 'probe.py').write_text(guest_source())
    shutil.copyfile(BASE / 'guest/dm_observer.py', payload / 'dm_observer.py')
    shutil.copyfile(package, payload / 'handler.deb')
    # The minimal seed has no /boot payload. Supply the exact real boot inputs
    # solely for enrollment's read-only copy/hash test inside the disposable VM.
    shutil.copyfile(kernel, payload / 'enrollment-vmlinuz')
    shutil.copyfile(original, payload / 'enrollment-initrd.img')
    (payload / 'fixture.json').write_text(json.dumps({'specs': data.SPECS,
        'administration_version': metadata['administration_version'], 'native_sha256': metadata['runtime']['binary_sha256'],
        'boot_inputs': {'kernel_sha256': sha256(kernel), 'initrd_sha256': sha256(original)}}))
    (payload / 'standalone-probe.service').write_text(UNIT)
    module = Path(subprocess.check_output(['modinfo', '-k', metadata['runtime']['kernel_release'], '-F', 'filename', 'autofs4'], text=True).strip())
    content = subprocess.check_output(['zstd', '-dc', str(module)]) if module.suffix == '.zst' else module.read_bytes()
    (payload / 'autofs.ko').write_bytes(content)
    hook = staging / 'scripts/init-bottom/standalone-test'
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK)
    hook.chmod(0o755)
    (hook.parent / 'ORDER').write_text(order + '\n/scripts/init-bottom/standalone-test "$@"\n')
    paths = [Path('.'), *sorted(p.relative_to(staging) for p in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=staging,
        input=b'\0'.join(str(p).encode() for p in paths) + b'\0', stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True).stdout
    image = folder / 'initrd.img'
    shutil.copyfile(original, image)
    with image.open('ab') as stream:
        stream.write(gzip.compress(archive, mtime=0))
    return image


def vm_command(folder, inputs, image, overlay):
    # Keep existing, reviewed disk construction/QMP wiring; remove only the
    # protected-root command-line flags for this independent deployment case.
    command = data.vm_command(folder, inputs['kernel'].parent, image, overlay, inputs['seed'])
    index = command.index('-append') + 1
    flags = command[index].split()
    command[index] = ' '.join(flag for flag in flags if flag not in ('nompath', 'ram_rescue_guard=1')) + ' ram_rescue_standalone_test=1 dm_multipath.queue_if_no_path_timeout_secs=10'
    return command


def call(channel, action, timeout=60):
    value = channel.call(action, timeout=timeout)
    if not value.get('ok'):
        raise RuntimeError(value.get('error', 'VM action failed'))
    return value['value']


def ready(channel, recoveries, *, previous=None, workers=False):
    def inspect():
        value = call(channel, 'snapshot')
        for name, count in recoveries.items():
            item = value['devices'][name]
            state = item['state'] or {}
            if state.get('state') in ('expired', 'failed', 'blocked', 'interrupted'):
                raise RuntimeError('Data controller terminated: ' + json.dumps(state))
            if state.get('state') != 'ready' or state.get('recoveries', 0) < count:
                return None
            if workers and item['ack_count'] < (previous['devices'][name]['ack_count'] + 3 if previous else 5):
                return None
            if item['errors']:
                raise RuntimeError('Original data worker observed an I/O error')
        if workers and any(item['errors'] or item['acks'] < (previous['children'][name]['acks'] + 3 if previous else 5)
                           for name, item in value['children'].items()):
            return None
        return value
    return wait_for(inspect, 30, 'standalone controllers and original workers')


def run_scenarios(channel, qmp, report):
    report['preflight'] = call(channel, 'preflight')
    report['configured'] = call(channel, 'configure', timeout=150)
    names = [spec['name'] for spec in data.SPECS]
    report['ready'] = ready(channel, dict.fromkeys(names, 0))
    report['initial_diagnostics'] = call(channel, 'diagnostics')
    report['idle'] = call(channel, 'measured_idle')
    report['plan'] = call(channel, 'install_mount_plan')
    report['mounted'] = call(channel, 'mount_registered')
    call(channel, 'start')
    before = report['before'] = ready(channel, dict.fromkeys(names, 0), workers=True)
    ext4, vfat = data.SPECS
    removed = data.remove(qmp, ext4)
    qmp.call('device_add', driver='usb-storage', bus='xhci.0', port='4', id='unregistered-usb',
             drive='decoydisk', serial='UNREGISTERED-USB')
    time.sleep(max(0, .2 - (time.monotonic() - removed)))
    attached = data.attach(qmp, ext4)
    report['ext4_gap_seconds'] = attached - removed
    report['ext4_recovered'] = ready(channel, {names[0]: 1, names[1]: 0}, previous=before, workers=True)
    data.remove(qmp, vfat)
    time.sleep(.2)
    data.attach(qmp, vfat, wrong=True)

    def rejected():
        value = call(channel, 'snapshot')
        event = value['devices'][names[1]]['state'] or {}
        return value if event.get('state') == 'rejected' else None

    report['wrong_identity'] = wait_for(rejected, 5, 'wrong filesystem identity refusal')
    # Do not spend the finite admission window doing dependency hashes here.
    data.remove(qmp, vfat)
    data.attach(qmp, vfat)
    after = report['after'] = ready(channel, dict.fromkeys(names, 1), previous=before, workers=True)
    report['audit'] = call(channel, 'audit')
    report['manager'] = call(channel, 'manager_status')
    report['final_diagnostics'] = call(channel, 'diagnostics')
    report['readonly'] = call(channel, 'readonly_policy')
    data.remove(qmp, vfat)
    time.sleep(.2)
    data.attach(qmp, vfat)
    report['readonly_recovered'] = ready(channel, {names[0]: 1, names[1]: 2})
    report['logs'] = call(channel, 'logs')
    report['boot_fixture'] = call(channel, 'install_boot_fixture')
    report['finished'] = call(channel, 'finish')
    checked = report['checks'] = {
        'ordinary_ubuntu_without_root_guard': report['preflight']['root_config_absent'] and report['preflight']['protected_ram_absent'] and 'ram-rescue-path' not in report['preflight']['maps'],
        'installed_root_enrollment_identifies_real_usb_lvm': report['configured']['root_enrollment']['identity_verified'],
        'root_enrollment_private_files_match_boot_inputs': report['configured']['root_enrollment']['private_files_verified'],
        'root_enrollment_preserves_boot_and_maps': report['configured']['root_enrollment']['boot_and_maps_unchanged'],
        'root_enrollment_standard_tar_export_matches': report['configured']['root_enrollment']['tar_verified'],
        'independent_private_ram_created': report['configured']['environment']['root'] == '/run/ram-rescue-manager/rootfs',
        'two_shipped_native_controllers': len(report['manager']['controllers']) == 2 and all(c['binary_sha256'] == report['package']['runtime']['binary_sha256'] for c in report['manager']['controllers']),
        'two_actual_controller_namespaces_restricted': len(report['manager']['controllers']) == 2 and all(
            item['namespace']['verified'] for item in report['manager']['controllers']),
        'no_service_overrides': all(not c['properties']['DropInPaths'] for c in report['manager']['controllers']),
        'systemd_mount_units_verified': report['plan']['verify']['returncode'] == 0,
        'automount_deferred_until_access': not report['plan']['snapshot']['mount_graph'][MountPaths.EXT4],
        'mounts_and_submounts_present': all(report['mounted']['mount_graph'].values()),
        'original_mount_graph_continuous': before['mount_graph'] == after['mount_graph'],
        'wrong_identity_not_committed': report['wrong_identity']['devices'][names[1]]['state']['recoveries'] == 0,
        'wrong_identity_preserved_mounts': report['wrong_identity']['mount_graph'] == before['mount_graph'],
        'ext4_reenumerated': before['devices'][names[0]]['slaves'] != after['devices'][names[0]]['slaves'],
        'same_boot_and_pid1': before['boot_id'] == after['boot_id'] and before['pid1']['start_ticks'] == after['pid1']['start_ticks'],
        'root_mapping_unchanged': {name: report['manager']['preflight']['maps'][name] for name in report['preflight']['maps']} == report['preflight']['maps'],
        'unknown_usb_not_enrolled': set(report['manager']['preflight']['maps']) == set(report['preflight']['maps']) | set(names),
        'readonly_not_forced_writable': 'ro' in report['readonly_recovered']['mount_graph'][MountPaths.VFAT].split()[5].split(','),
        'orderly_unmounts': not any(report['finished']['unmounted']['mount_graph'].values()),
    }
    for stage in ('initial_diagnostics', 'final_diagnostics'):
        diagnostics = report[stage]
        checked[stage + '_ready'] = doctor_matches_installed_runtime(diagnostics['doctor'], report['package'])
        checked[stage + '_private_redacted_export'] = diagnostics['redaction_passed'] and diagnostics['file_mode'] == 0o600 and diagnostics['directory_mode'] == 0o700
    for name in names:
        old, new = before['devices'][name], after['devices'][name]
        checked[name + '_original_process'] = all(old['process'][key] == new['process'][key] for key in ('pid', 'start_ticks'))
        checked[name + '_no_io_errors'] = not new['errors']
        checked[name + '_durable_hash'] = report['audit']['audit'][name]['hash_matches']
    for name in before['children']:
        checked[name + '_original_process'] = all(before['children'][name]['process'][key] == after['children'][name]['process'][key] for key in ('pid', 'start_ticks'))
        checked[name + '_durable_hash'] = report['audit']['children'][name]['hash_matches']
    call(channel, 'shutdown')


def doctor_matches_installed_runtime(doctor, package):
    """Require actual owner health and the installed data candidate's provenance."""
    versions = doctor.get('versions', {})
    candidates = versions.get('installed_candidates', {})
    manager = candidates.get('manager') or {}
    runtimes = versions.get('runtimes', [])
    expected = package['runtime']
    return (doctor.get('state') == 'ready' and len(doctor.get('devices', [])) == 2
            and all(item['owner_matches'] and item['runtime_context'] == 'standalone_data' for item in doctor['devices'])
            and candidates.get('root') is None and versions.get('installed_image_sha256') is None
            and all(manager.get(key) == expected[key] for key in ('binary_sha256', 'base_sha256', 'archive_sha256'))
            and manager.get('archive_integrity_checked') is False
            and len(runtimes) == 1 and runtimes[0].get('context') == 'standalone_data'
            and runtimes[0].get('installed_candidate_matches', {}).get('manager') == {
                'native_manifest': True, 'base_source': True})


def second_boot(folder, command, log, qlog, actions, report):
    """Reuse the installed package/registry after an orderly VM power cycle."""
    for name in ('qmp.sock', 'agent.sock', 'rescue.sock'):
        (folder / name).unlink(missing_ok=True)
    (folder / 'console.log').rename(folder / 'first-boot-console.log')
    vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    qmp = channel = None
    try:
        wait_for(lambda: (folder / 'qmp.sock').exists(), 15, 'second boot QMP')
        qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
        channel = Channel(folder / 'rescue.sock', actions)
        wait_for(lambda: any(row['message'].get('ready') for row in channel.events), 180, 'second ordinary Ubuntu boot')
        names = [spec['name'] for spec in data.SPECS]
        report['second_boot_ready'] = ready(channel, dict.fromkeys(names, 0))
        manager = report['second_boot_manager'] = call(channel, 'manager_status')
        report['checks'].update({
            'second_boot_has_new_boot_id': manager['preflight']['boot_id'] != report['preflight']['boot_id'],
            'second_boot_still_without_root_guard': manager['preflight']['root_config_absent'] and manager['preflight']['protected_ram_absent'],
            'persistent_registration_auto_starts_native_owners': len(manager['controllers']) == 2 and all(
                item['binary_sha256'] == report['package']['runtime']['binary_sha256'] for item in manager['controllers']),
            'second_boot_actual_controller_namespaces_restricted': len(manager['controllers']) == 2 and all(
                item['namespace']['verified'] for item in manager['controllers']),
            'second_boot_doctor_ready': doctor_matches_installed_runtime(manager['doctor'], report['package']),
        })
        call(channel, 'finish')
        call(channel, 'shutdown')
        vm.wait(timeout=150)
        report['checks']['second_clean_shutdown'] = vm.returncode == 0
    except BaseException:
        if channel is not None and vm.poll() is None:
            for action in ('failure_details', 'preflight', 'logs', 'manager_status'):
                try:
                    report['second_failure_' + action] = call(channel, action, timeout=90 if action == 'failure_details' else 15)
                except Exception:
                    report.setdefault('diagnostic_errors', {})['second_' + action] = traceback.format_exc()
        raise
    finally:
        stop_vm(vm)
        for connection in (qmp, channel):
            if connection is not None:
                connection.close()


class MountPaths:
    EXT4 = '/mnt/rr-data-vm-ext4'
    VFAT = EXT4 + '/nested-vfat'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reproduction-dir', type=Path, required=True)
    parser.add_argument('--package-dir', type=Path, required=True)
    args = parser.parse_args()
    inputs, package = validate_inputs(args.reproduction_dir, args.package_dir)
    folder = WORK / ('sd-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    report = {'schema': 1, 'scenario': 'ordinary-ubuntu-independent-data', 'passed': False,
              'package': package, 'input_sha256': {key: sha256(path) for key, path in inputs.items()},
              'observer_sha256': {str(path.relative_to(REPO)): sha256(path) for path in (
                  Path(__file__), GUEST, BASE / 'guest/data_probe.py', BASE / 'guest/unified_probe.py',
                  BASE / 'guest/mount_probe.py', BASE / 'guest/dm_observer.py')}}
    print('Standalone data VM:', folder, flush=True)
    vm = qmp = channel = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog, (folder / 'actions.jsonl').open('w') as actions:
        try:
            overlay = data.create_images(folder, inputs['seed'])
            image = create_initrd(folder, inputs['initrd'], inputs['package'], package, kernel=inputs['kernel'])
            command = vm_command(folder, inputs, image, overlay)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 15, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            channel = Channel(folder / 'rescue.sock', actions)
            wait_for(lambda: any(item['message'].get('ready') for item in channel.events), 180, 'ordinary Ubuntu observer')
            run_scenarios(channel, qmp, report)
            vm.wait(timeout=150)
            report['checks']['clean_shutdown'] = vm.returncode == 0
            qmp.close()
            channel.close()
            qmp = channel = None
            second_boot(folder, command, log, qlog, actions, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if channel is not None and vm is not None and vm.poll() is None:
                for action in ('failure_details', 'preflight', 'logs', 'manager_status'):
                    try:
                        report['failure_' + action] = call(channel, action, timeout=90 if action == 'failure_details' else 15)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            stop_vm(vm)
            for connection in (qmp, channel):
                if connection is not None:
                    connection.close()
            report['inputs_unchanged'] = all(sha256(path) == report['input_sha256'][key] for key, path in inputs.items())
            if all((folder / (spec['name'] + '.raw')).exists() for spec in data.SPECS):
                report['readonly_fsck'] = data.audit_images(folder)
            report['passed'] = (bool(report.get('checks')) and all(report['checks'].values()) and report['inputs_unchanged']
                                and all(result['returncode'] == 0 for result in report.get('readonly_fsck', {}).values())
                                and not report.get('error'))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Standalone data report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Standalone data acceptance failed')


if __name__ == '__main__':
    main()
