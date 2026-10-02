#!/usr/bin/env python3
"""Compare full Python/C++ recovery controllers in isolated Ubuntu QEMU guests.

The pinned Python release supplies identical boot preparation, enrollment,
mount policy and RAM observation for both variants. C++ replaces the three
resident controllers and their systemd takeover commands before first start.
No host block device, installation, network or shared directory enters a VM.
"""
import argparse
from contextlib import contextmanager
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import traceback

from auto_run import wait_for
import data_guard_probe as data
import host_boot_probe as boot
import mount_guard_probe as mounts
import performance_probe as performance
import soak_guard_probe as soak
import unified_guard_probe as unified
from efi_mount_probe import validate_inputs, stop_vm
from measure_guard import MEMORY_HELPERS
from run import Channel, WORK
from historical import PYTHON_REVISION, snapshot

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
BASELINE = PYTHON_REVISION
NATIVE = '/opt/guard-runtime/guard-runtime'
GUEST = BASE / 'guest/cpp_probe.py'


def frozen_python(folder):
    """Use the complete released Python payload, including cold administration."""
    return snapshot(BASELINE, destination=folder / 'python-release'), BASELINE


def binary_closure(binary):
    """Resolve the trusted local build's loader and shared libraries for RAM."""
    if binary.read_bytes()[:4] != b'\x7fELF':
        raise ValueError('C++ runtime must be an ELF executable')
    result = subprocess.run(['ldd', str(binary)], text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, check=False)
    if result.returncode and 'not a dynamic executable' not in result.stdout:
        raise RuntimeError('Cannot resolve C++ runtime libraries: ' + result.stdout)
    if 'not found' in result.stdout:
        raise RuntimeError('C++ runtime dependency is absent: ' + result.stdout)
    paths = {Path(match.group(1)) for line in result.stdout.splitlines()
             if (match := re.search(r'(?:=>\s+)?(/\S+)\s+\(', line))}
    return {str(path): path.resolve(strict=True) for path in sorted(paths)}


def append_archive(image, staging):
    paths = [Path('.'), *sorted(path.relative_to(staging) for path in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=staging,
                             input=b'\0'.join(str(path).encode() for path in paths) + b'\0',
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    with image.open('ab') as stream:
        stream.write(gzip.compress(archive, mtime=0))


@contextmanager
def compose_sources(release, guest, hook, *, manager=None):
    originals = data.REPO, unified.REPO, unified.GUEST, boot.GUEST, boot.HOOK
    try:
        data.REPO = release
        unified.REPO = release if manager is None else manager
        unified.GUEST = guest
        marker = "'/opt/guard/path_guard.py' in owner['process']['cmdline']"
        if boot.GUEST.count(marker) != 1:
            raise RuntimeError('Root observer readiness contract changed')
        boot.GUEST = boot.GUEST.replace(marker, "selected_controller(owner['process'])")
        boot.HOOK += hook
        yield
    finally:
        data.REPO, unified.REPO, unified.GUEST, boot.GUEST, boot.HOOK = originals


def create_initrd(folder, original, release, implementation, scenario, binary, kernel_release):
    combined = folder / 'cpp-composed-guest.py'
    sources = [unified.GUEST.read_text()]
    if scenario == 'mounts':
        sources.extend([(BASE / 'guest/mount_probe.py').read_text(), 'SelectedProbe = MountProbe'])
    else:
        sources.append((BASE / 'guest/performance_probe.py').read_text())
        if scenario == 'soak':
            sources.extend([(BASE / 'guest/soak_probe.py').read_text(), 'SelectedProbe = SoakProbe'])
        else:
            sources.append('SelectedProbe = PerformanceProbe')
    sources.append(GUEST.read_text())
    combined.write_text('\n'.join(sources) + '\n')
    hook = ('\ncp /opt/data-guard/*.py "$TOOLS/opt/guard/"\n'
            'cp /opt/cpp-experiment.json "$TOOLS/opt/vmprobe/cpp-experiment.json"\n'
            'cp /opt/cpp-memory.py "$TOOLS/opt/vmprobe/memory_helpers.py"\n')
    if implementation == 'cpp':
        hook += ('cp -a /opt/cpp-closure/. "$TOOLS/"\n'
                 'mkdir -p /run/systemd/system/ram-rescue-guard.service.d\n'
                 'cp /opt/cpp-root.conf /run/systemd/system/ram-rescue-guard.service.d/cpp-experiment.conf\n')
    if scenario == 'mounts':
        hook += 'insmod /opt/cpp-autofs.ko\n'
    with compose_sources(release, combined, hook):
        image = unified.create_initrd(folder, original)
    staging = folder / 'cpp-overlay'
    (staging / 'opt').mkdir(parents=True)
    manifest = {'implementation': implementation, 'baseline_ref': BASELINE}
    payload = folder / 'data-overlay/opt/data-guard'
    manifest['runtime_payload_sha256'] = {path.name: boot.sha256(path) for path in sorted(payload.glob('*.py'))}
    if implementation == 'cpp':
        closure = staging / 'opt/cpp-closure'
        target = closure / NATIVE.lstrip('/')
        target.parent.mkdir(parents=True)
        shutil.copyfile(binary, target)
        target.chmod(0o755)
        manifest['binary_sha256'] = boot.sha256(binary)
        manifest['library_sha256'] = {}
        for relative, source in binary_closure(binary).items():
            target = closure / relative.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(0o755)
            manifest['library_sha256'][relative] = boot.sha256(target)
        manifest['runtime_payload_sha256'] = {'guard-runtime': manifest['binary_sha256']}
        (staging / 'opt/cpp-root.conf').write_text('[Service]\nExecStart=\n'
            f'ExecStart={NATIVE} run --config /run/ram-rescue-guard/config.json\n'
            'ExecStopPost=\n'
            f'ExecStopPost={NATIVE} takeover --config /run/ram-rescue-guard/config.json\n')
        manifest['native_boot_activation'] = True
    # Keep the released shell's fail-closed trap, RAM mounts and ordering.
    # Pin the Python admission code before first use, not only the resident
    # controller which starts later. C++ boot admission replaces that command.
    local_top = (release / 'guard/integration/local-top').read_text()
    old_command = 'chroot "$TOOLS" /usr/bin/python3 /opt/guard/boot.py --config /etc/rescue/enrollment.json'
    if local_top.count(old_command) != 1:
        raise RuntimeError('Released protected-boot activation command changed')
    activation = 'cp /opt/data-guard/*.py "$TOOLS/opt/guard/"\n'
    activation += ('cp -a /opt/cpp-closure/. "$TOOLS/"\n'
        f'chroot "$TOOLS" {NATIVE} activate --config /etc/rescue/enrollment.json'
        if implementation == 'cpp' else old_command)
    boot_script = staging / 'scripts/local-top/ram-rescue-guard'
    boot_script.parent.mkdir(parents=True)
    boot_script.write_text(local_top.replace(old_command, activation))
    boot_script.chmod(0o755)
    (staging / 'opt/cpp-experiment.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (staging / 'opt/cpp-memory.py').write_text(MEMORY_HELPERS)
    if scenario == 'mounts':
        module = Path(subprocess.check_output(['modinfo', '-k', kernel_release, '-F', 'filename', 'autofs4'], text=True).strip())
        content = subprocess.check_output(['zstd', '-dc', str(module)]) if module.suffix == '.zst' else module.read_bytes()
        (staging / 'opt/cpp-autofs.ko').write_bytes(content)
    append_archive(image, staging)
    return image, manifest


def source_hashes(binary, implementation):
    paths = [Path(__file__), GUEST, BASE / 'guest/performance_probe.py', BASE / 'guest/mount_probe.py',
             BASE / 'guest/soak_probe.py', Path(performance.__file__), Path(mounts.__file__),
             Path(soak.__file__), Path(unified.__file__), Path(data.__file__), Path(boot.__file__),
             BASE / 'measure_guard.py', BASE / 'historical.py']
    if implementation == 'cpp':
        paths += [path for path in (REPO / 'guard/native').rglob('*') if path.is_file()
                  and path.suffix in ('.cpp', '.hpp', '.h', '.py', '.txt')]
        paths.append(binary)
    return {str(path.resolve().relative_to(REPO)): boot.sha256(path) for path in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation', choices=('python', 'cpp'), required=True)
    parser.add_argument('--scenario', choices=('performance', 'mounts', 'soak'), required=True)
    parser.add_argument('--binary', type=Path, default=WORK / 'cpp-runtime/guard-runtime')
    parser.add_argument('--cycles', type=int, default=10)
    parser.add_argument('--quota-percent', type=int, choices=(5, 10, 20), default=20)
    parser.add_argument('--build-dir', type=Path, default=WORK / 'hb-build-v4')
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    if not 2 <= args.cycles <= 50:
        parser.error('--cycles must be 2..50')
    if args.implementation == 'cpp' and not args.binary.is_file():
        parser.error('Build the C++ runtime before running this experiment')
    build_dir, seed_path, build, seed, before_hash = validate_inputs(args)
    folder = WORK / ('cpp-' + args.implementation[0] + args.scenario[0] + '-' +
                     time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    release, revision = frozen_python(folder)
    sources = source_hashes(args.binary, args.implementation)
    report = {'schema': 1, 'scenario': args.scenario, 'implementation': args.implementation,
              'variant': 'baseline' if args.implementation == 'python' else 'current',
              'baseline_ref': revision, 'baseline_override_files': ['complete guard and rescue Python release'],
              'quota_percent': args.quota_percent, 'requested_cycles': args.cycles, 'settle_seconds': .75,
              'recovery_rpc_quiet': True, 'quiet_wait_seconds': 17,
              'source_sha256': sources, 'source_image_sha256_before': before_hash,
              'build': build, 'seed_report': str(seed_path), 'passed': False,
              'scope': 'three full recovery controllers; root service + data slice descendants once; one core = 100%; observer/workloads excluded'}
    print('Full-runtime comparison VM:', folder, flush=True)
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = data.create_images(folder, seed)
            release_name = json.loads(args.enrollment.read_text())['guard']['kernel_release']
            image, manifest = create_initrd(folder, build_dir / 'initrd.img', release,
                                             args.implementation, args.scenario, args.binary, release_name)
            report['experiment_manifest'] = manifest
            report['runtime_payload_sha256'] = manifest['runtime_payload_sha256']
            report['initrd_sha256'] = boot.sha256(image)
            command = data.vm_command(folder, build_dir, image, overlay, seed)
            report['command'] = command
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            scenario = {'performance': performance.scenarios, 'mounts': mounts.run_scenarios,
                        'soak': soak.scenarios}[args.scenario]
            scenario(folder, qmp, vm, report)
        except BaseException:
            report['error'] = traceback.format_exc()
            if vm is not None and vm.poll() is None:
                for action in ('snapshot', 'logs'):
                    try:
                        report['failure_' + action] = data.ram_call(folder, action)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            stop_vm(vm)
            if qmp:
                qmp.close()
            report['source_image_unchanged'] = boot.sha256(seed) == before_hash
            report['sources_unchanged'] = all((REPO / path).is_file() and boot.sha256(REPO / path) == digest
                                             for path, digest in sources.items())
            try:
                report['filesystem_audits'] = data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged']
                and not report.get('filesystem_audit_error') and report.get('filesystem_audits')
                and all(audit['returncode'] == 0 for audit in report['filesystem_audits'].values()))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Full-runtime comparison report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Full-runtime comparison acceptance failed')


if __name__ == '__main__':
    main()
