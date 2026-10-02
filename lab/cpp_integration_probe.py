#!/usr/bin/env python3
"""Exercise the shipped C++ image and manager integration in disposable Ubuntu.

Unlike the frozen language comparison, this fixture stages the current production
payload and boot/service templates unchanged. Python is only the cold manager,
test observer and workload; each resident controller must be the native ELF.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import traceback

import cpp_guard_probe as cpp

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
sys.path.insert(0, str(REPO / 'guard'))
import manage

spec = importlib.util.spec_from_file_location('production_guard_build', REPO / 'guard/build.py')
production_build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(production_build)


GUEST = r'''

class ProductionIntegrationProbe(CppExperimentProbe):
    def activate(self):
        # Call the shipped manager install. The comparison observer's activate
        # method injects a command override and is intentionally bypassed here.
        return ProductionUnifiedProbe.activate(self)

    def integration_integrity(self):
        import native_payload
        binary = native_payload.verify_runtime(Path('/'))
        if binary != Path(NATIVE):
            raise RuntimeError('Production payload verification selected another runtime')
        manifest = json.loads(Path('/opt/guard-runtime/runtime.json').read_text())
        if manifest != EXPERIMENT['native_manifest']:
            raise RuntimeError('Production payload manifest changed in the guest')
        unit_paths = {
            'root': self.ROOT / 'run/systemd/system/ram-rescue-guard.service',
            'manager': self.ROOT / 'etc/systemd/system/ram-rescue-maintain@.service',
        }
        units = {}
        for name, path in unit_paths.items():
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != EXPERIMENT['unit_sha256'][name]:
                raise RuntimeError('Shipped unit bytes differ: ' + name)
            units[name] = {'sha256': actual, 'text': path.read_text()}
        names = ['ram-rescue-guard.service'] + [
            'ram-rescue-maintain@' + item['name'] + '.service' for item in self.specs]
        dropins = {}
        for name in names:
            result = self.host('/usr/bin/systemctl', 'show', name, '-p', 'DropInPaths', '--value')
            if result['stdout'].strip():
                raise RuntimeError('Unexpected command/unit override: ' + name)
            dropins[name] = result['stdout'].strip()
        if Path('/opt/manager').resolve() != Path('/opt/guard-runtime'):
            raise RuntimeError('Manager alias does not select the production runtime')
        entrypoint = Path('/opt/manager/maintain')
        if entrypoint.read_text() != native_payload.ENTRYPOINT_SCRIPT:
            raise RuntimeError('Manager entrypoint is not the packaged exec wrapper')
        return {'verified': True, 'native_manifest': manifest, 'units': units,
                'unit_dropins': dropins, 'manager_alias': str(Path('/opt/manager').resolve()),
                'entrypoint': entrypoint.read_text(), 'actual_controllers': self.runtime_integrity(require_all=False)}

    def mount_registered(self):
        result = super().mount_registered()
        result['production_integration'] = self.integration_integrity()
        return result

    def logs(self):
        result = super().logs()
        result['production_integration'] = self.integration_integrity()
        return result

UnifiedProbe = ProductionIntegrationProbe
'''


def archive_payload(root, target):
    """Use the same archive ownership and file ordering as the image builder."""
    with tarfile.open(target, 'w:gz', compresslevel=3) as archive:
        for path in sorted(root.rglob('*')):
            item = archive.gettarinfo(str(path), arcname=str(path.relative_to(root)))
            item.uid = item.gid = 0
            item.uname = item.gname = 'root'
            if item.isfile():
                with path.open('rb') as stream:
                    archive.addfile(item, stream)
            else:
                archive.addfile(item)


def payload_inventory(root):
    files = {str(path.relative_to(root)): path.stat().st_size for path in root.rglob('*')
             if path.is_file() and not path.is_symlink()}
    allocated = int(subprocess.check_output(['du', '-s', '-B1', str(root)], text=True).split()[0])
    return {'regular_files': len(files), 'regular_file_bytes': sum(files.values()),
            'staging_filesystem_allocated_bytes': allocated}, files


def compare_payloads(folder, profile, native):
    """Account tmpfs input separately from process PSS or cgroup memory."""
    python = folder / 'python-comparison-payload'
    production_build.payload(profile, python, runtime='python')
    baseline = folder / 'base-rescue-payload'
    baseline.mkdir()
    source = Path('/usr/local/lib/ram-rescue-demo/rescue-root.tar.gz')
    with tarfile.open(source, 'r:gz') as archive:
        archive.extractall(baseline, filter='data')
    native_stats, native_files = payload_inventory(native)
    python_stats, python_files = payload_inventory(python)
    baseline_stats, _ = payload_inventory(baseline)
    added = {path: native_files[path] for path in native_files.keys() - python_files.keys()}
    removed = {path: python_files[path] for path in python_files.keys() - native_files.keys()}
    changed = {path: {'python_bytes': python_files[path], 'cpp_bytes': native_files[path]}
               for path in native_files.keys() & python_files.keys()
               if cpp.boot.sha256(native / path) != cpp.boot.sha256(python / path)}
    return {'base_rescue': baseline_stats, 'python_image': python_stats, 'cpp_image': native_stats,
            'cpp_minus_python_regular_bytes': native_stats['regular_file_bytes'] - python_stats['regular_file_bytes'],
            'added_regular_files': dict(sorted(added.items())), 'removed_regular_files': dict(sorted(removed.items())),
            'changed_regular_files': dict(sorted(changed.items())),
            'scope': 'image regular-file bytes; du describes the staging filesystem, not measured guest RAM; do not add to PSS/cgroup memory'}


def create_initrd(folder, original, enrollment, binary):
    combined = folder / 'production-integration-guest.py'
    combined.write_text('\n'.join([
        cpp.unified.GUEST.read_text(), 'ProductionUnifiedProbe = UnifiedProbe',
        (BASE / 'guest/mount_probe.py').read_text(), 'SelectedProbe = MountProbe',
        cpp.GUEST.read_text(), GUEST,
    ]) + '\n')
    # Only the observer shell and matching autofs module are added by this
    # hook. Controller commands come entirely from production unit templates.
    hook = ('\ncp /opt/cpp-experiment.json "$TOOLS/opt/vmprobe/cpp-experiment.json"\n'
            'insmod /opt/cpp-autofs.ko\n')
    with cpp.compose_sources(REPO, combined, hook):
        image = cpp.unified.create_initrd(folder, original)

    overlay = folder / 'production-overlay'
    archive_directory = overlay / 'opt/ram-rescue-guard'
    archive_directory.mkdir(parents=True)
    profile = json.loads(enrollment.read_text())
    payload = folder / 'production-payload'
    base_checksum = production_build.payload(profile, payload, runtime='cpp', native_binary=binary)
    archive = archive_directory / 'tools.tar.gz'
    archive_payload(payload, archive)
    checksum = cpp.boot.sha256(archive)
    (archive_directory / 'tools.tar.gz.sha256').write_text(checksum + '  tools.tar.gz\n')

    templates = REPO / 'guard/integration'
    destinations = {
        'local-top': overlay / 'scripts/local-top/ram-rescue-guard',
        'init-bottom': overlay / 'scripts/init-bottom/ram-rescue-guard',
        'lvmlocal.conf': overlay / 'etc/lvm/lvmlocal.conf',
        '58-ram-rescue-guard.rules': overlay / 'etc/udev/rules.d/58-ram-rescue-guard.rules',
    }
    for source, destination in destinations.items():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(templates / source, destination)
    staged_templates = overlay / 'etc/ram-rescue-guard'
    shutil.copytree(templates, staged_templates)
    for name in ('local-top', 'init-bottom'):
        if (templates / name).read_bytes() != destinations[name].read_bytes():
            raise RuntimeError('Production boot template was modified')

    native_manifest = json.loads((payload / 'opt/guard-runtime/runtime.json').read_text())
    import hashlib
    manifest = {
        'implementation': 'cpp', 'integration': 'current-production',
        'binary_sha256': cpp.boot.sha256(binary), 'native_manifest': native_manifest,
        'runtime_payload_sha256': {'guard-runtime': cpp.boot.sha256(binary)},
        'base_rescue_payload_sha256': base_checksum, 'tools_archive_sha256': checksum,
        'template_sha256': {path.name: cpp.boot.sha256(path) for path in sorted(templates.iterdir()) if path.is_file()},
        'unit_sha256': {'root': cpp.boot.sha256(templates / 'ram-rescue-guard.service'),
                       'manager': hashlib.sha256(manage.render_controller().encode()).hexdigest()},
        'native_boot_activation': True, 'command_overrides': False,
    }
    (folder / 'payload-comparison.json').write_text(json.dumps(compare_payloads(folder, profile, payload), indent=2) + '\n')
    (overlay / 'opt/cpp-experiment.json').write_text(json.dumps(manifest, indent=2) + '\n')
    module = Path(subprocess.check_output(
        ['modinfo', '-k', profile['guard']['kernel_release'], '-F', 'filename', 'autofs4'], text=True).strip())
    module_bytes = subprocess.check_output(['zstd', '-dc', str(module)]) if module.suffix == '.zst' else module.read_bytes()
    (overlay / 'opt/cpp-autofs.ko').write_bytes(module_bytes)
    cpp.append_archive(image, overlay)
    return image, manifest


def source_hashes(binary):
    sources = cpp.source_hashes(binary, 'cpp')
    sources.update({str(path.relative_to(REPO)): cpp.boot.sha256(path) for path in [
        Path(__file__).resolve(), REPO / 'ram-rescue-demo/src/rescue.py',
        *sorted((REPO / 'guard').glob('*.py')), *sorted((REPO / 'guard/runtime').glob('*.py')),
        *sorted(path for path in (REPO / 'guard/integration').rglob('*') if path.is_file()),
    ]})
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=cpp.WORK / 'cpp-runtime/guard-runtime')
    parser.add_argument('--build-dir', type=Path, default=cpp.WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=cpp.WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=cpp.WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    if not args.binary.is_file():
        parser.error('Build the native runtime before integration testing')
    build_dir, seed_path, build, seed, before_hash = cpp.validate_inputs(args)
    folder = cpp.WORK / ('cpp-int-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    sources = source_hashes(args.binary)
    report = {'schema': 1, 'scenario': 'production-integration-mounts', 'implementation': 'cpp',
              'scope': 'current shipped C++ payload, unchanged boot/service templates and manager exec wrapper',
              'source_sha256': sources, 'source_image_sha256_before': before_hash,
              'build': build, 'seed_report': str(seed_path), 'passed': False}
    print('Production integration VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = cpp.data.create_images(folder, seed)
            image, manifest = create_initrd(folder, build_dir / 'initrd.img', args.enrollment, args.binary)
            report['experiment_manifest'] = manifest
            report['payload_comparison'] = json.loads((folder / 'payload-comparison.json').read_text())
            report['runtime_payload_sha256'] = manifest['runtime_payload_sha256']
            report['initrd_sha256'] = cpp.boot.sha256(image)
            if source_hashes(args.binary) != sources:
                raise RuntimeError('Production source changed during fixture preparation')
            command = cpp.data.vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            cpp.wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = cpp.Channel(folder / 'qmp.sock', qlog, qmp=True)
            cpp.mounts.run_scenarios(folder, qmp, vm, report)
            report['checks']['shipped_payload_verified_before_recovery'] = report['mounted']['production_integration']['verified']
            report['checks']['shipped_payload_verified_after_terminal_case'] = report['logs']['production_integration']['verified']
            report['checks']['three_wrapper_started_native_controllers'] = len(
                report['mounted']['production_integration']['actual_controllers']['controllers']) == 3
            report['checks']['no_controller_command_overrides'] = not any(
                report['mounted']['production_integration']['unit_dropins'].values())
            report['passed'] = all(report['checks'].values())
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                for action in ('snapshot', 'logs'):
                    try:
                        report['failure_' + action] = cpp.data.ram_call(folder, action)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            cpp.stop_vm(vm)
            if qmp:
                qmp.close()
            report['source_image_unchanged'] = cpp.boot.sha256(seed) == before_hash
            report['sources_unchanged'] = source_hashes(args.binary) == sources
            try:
                report['filesystem_audits'] = cpp.data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged']
                and not report.get('filesystem_audit_error') and report.get('filesystem_audits')
                and all(audit['returncode'] == 0 for audit in report['filesystem_audits'].values()))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Production integration report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Production C++ integration acceptance failed')


if __name__ == '__main__':
    main()
