#!/usr/bin/env python3
"""Compare a465356 and current complete production packages on one Ubuntu seed.

Preparation is unprivileged and does not start QEMU. Add --run explicitly, or
resume the printed prepared.json later. The baseline is a complete pinned Git
archive used only for this A/B trial; clean current-version reproduction does
not depend on it. No live service, host block device, or network is exposed.
"""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import time
import traceback
from types import SimpleNamespace

import cpp_integration_probe as production
import cpp_guard_probe as cpp
from measure_guard import MEMORY_HELPERS

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
BASELINE = 'a465356d4363943af461d90c6199e0d31217ee10'
GUEST = BASE / 'guest/roadmap_performance.py'
WORK = cpp.WORK


def inventory(directory):
    return {str(path.relative_to(directory)): cpp.boot.sha256(path)
            for path in sorted(directory.rglob('*'))
            if path.is_file() and '__pycache__' not in path.parts}


def baseline_snapshot(folder):
    actual = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', '--verify',
                                      BASELINE + '^{commit}'], text=True).strip()
    if actual != BASELINE:
        raise RuntimeError('Pinned baseline commit is absent')
    content = subprocess.check_output(['git', '-C', str(REPO), 'archive', '--format=tar', BASELINE])
    destination = folder / 'baseline-source'
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        archive.extractall(destination, filter='data')
    return destination, hashlib.sha256(content).hexdigest()


class BaselineBuilder:
    """Relocate only the old hard-coded base-bundle input in an isolated process.

    The old controller, rescue/session code, native closure, manager and unit
    templates remain at BASELINE. Both variants get the same generic base so
    private developer enrollment/installed files are not benchmark inputs.
    """
    def __init__(self, repository, bundle):
        self.repository, self.bundle = repository, bundle

    def payload(self, profile, root, *, native_binary, base_rescue_dir=None):
        if Path(base_rescue_dir).resolve() != self.bundle.resolve():
            raise RuntimeError('The baseline must use the common explicit base bundle')
        enrollment = root.parent / 'baseline-profile.json'
        enrollment.write_text(json.dumps(profile) + '\n')
        code = (
            "import json,sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import build;"
            "original=build.Path;bundle=Path(sys.argv[2]);"
            "build.Path=lambda *p: bundle if p==('/usr/local/lib/ram-rescue-demo',) else original(*p);"
            "result=build.payload(json.loads(Path(sys.argv[3]).read_text()),Path(sys.argv[4]),"
            "native_binary=Path(sys.argv[5]));print(result)"
        )
        return subprocess.check_output([sys.executable, '-B', '-c', code,
            str(self.repository / 'guard'), str(self.bundle), str(enrollment),
            str(root), str(native_binary)], text=True).strip()


def controller_template(repository):
    return subprocess.check_output([sys.executable, '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);import manage;sys.stdout.write(manage.render_controller())',
        str(repository / 'guard')], text=True)


@contextmanager
def selected_package(repository, bundle):
    original = production.REPO, production.production_build, production.manage
    try:
        if repository != REPO:
            production.REPO = repository
            production.production_build = BaselineBuilder(repository, bundle)
            production.manage = SimpleNamespace(render_controller=lambda: controller_template(repository))
        yield
    finally:
        production.REPO, production.production_build, production.manage = original


def performance_overlay(folder, image):
    """Replace only the shared disposable observer; no controller drop-ins."""
    source = folder / 'overlay/opt/host-boot-probe/probe.py'
    text = source.read_text()
    old = (BASE / 'guest/mount_probe.py').read_text() + '\nSelectedProbe = MountProbe'
    new = (BASE / 'guest/performance_probe.py').read_text() + '\nSelectedProbe = PerformanceProbe'
    if text.count(old) != 1 or text.count('UnifiedProbe = ProductionIntegrationProbe') != 1:
        raise RuntimeError('Production observer composition contract changed')
    text = text.replace(old, new).replace('UnifiedProbe = ProductionIntegrationProbe',
        'UnifiedProbe = ProductionIntegrationProbe\n' + GUEST.read_text())
    compile(text, '<roadmap-performance-guest>', 'exec')
    staging = folder / 'performance-overlay'
    target = staging / 'opt/host-boot-probe/probe.py'
    target.parent.mkdir(parents=True)
    target.write_text(text)
    (staging / 'opt/performance-memory.py').write_text(MEMORY_HELPERS)
    hook = staging / 'scripts/init-bottom/zz-host-boot-probe'
    hook.parent.mkdir(parents=True)
    hook.write_text((folder / 'overlay/scripts/init-bottom/zz-host-boot-probe').read_text() +
                    '\ncp /opt/performance-memory.py "$TOOLS/opt/vmprobe/memory_helpers.py"\n')
    hook.chmod(0o755)
    cpp.append_archive(image, staging)
    return {'observer_sha256': cpp.boot.sha256(target),
            'memory_helpers_sha256': hashlib.sha256(MEMORY_HELPERS.encode()).hexdigest()}


def source_hashes(binary):
    result = production.source_hashes(binary)
    for path in (Path(__file__).resolve(), GUEST, BASE / 'guest/performance_probe.py',
                 BASE / 'performance_probe.py', BASE / 'measure_guard.py'):
        result[str(path.relative_to(REPO))] = cpp.boot.sha256(path)
    return result


def prepare(args):
    build_dir, seed_path, build, seed, before = cpp.validate_inputs(args)
    if not args.binary.is_file() or not args.base_rescue_dir:
        raise ValueError('Preparation requires --binary and --base-rescue-dir')
    bundle = args.base_rescue_dir.resolve(strict=True)
    if not bundle.is_relative_to(WORK.resolve()):
        raise ValueError('Use an explicit generated base-rescue below lab/work')
    folder = WORK / ('rperf-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    if len(str(folder / 'b0/rescue.sock').encode()) >= 108:
        raise ValueError('VM socket path exceeds Unix socket length')
    sources = source_hashes(args.binary)
    baseline, archive_hash = baseline_snapshot(folder)
    baseline_sources = inventory(baseline)
    baseline_binary = folder / 'baseline-native/guard-runtime'
    with (folder / 'baseline-build.log').open('w') as log:
        subprocess.run([sys.executable, '-B', str(baseline / 'guard/native/build_runtime.py'),
                        '--output', str(baseline_binary.parent)], stdout=log, stderr=subprocess.STDOUT, check=True)
    variants = {}
    for name, repository, binary in [('baseline', baseline, baseline_binary), ('current', REPO, args.binary.resolve())]:
        output = folder / ('b-image' if name == 'baseline' else 'c-image')
        output.mkdir()
        with selected_package(repository, bundle):
            image, manifest = production.create_initrd(output, build_dir / 'initrd.img',
                args.enrollment.resolve(), binary, base_rescue_dir=bundle, historical_comparison=False)
        observer = performance_overlay(output, image)
        variants[name] = {'image': str(image), 'image_sha256': cpp.boot.sha256(image),
                          'binary': str(binary), 'experiment_manifest': manifest, **observer}
    if source_hashes(args.binary) != sources or inventory(baseline) != baseline_sources:
        raise RuntimeError('Source changed while preparing the comparison')
    if variants['baseline']['observer_sha256'] != variants['current']['observer_sha256']:
        raise RuntimeError('The baseline and current observers must be byte-identical')
    report = {'schema': 1, 'kind': 'roadmap-production-performance', 'prepared': True,
              'folder': str(folder), 'baseline_ref': BASELINE, 'baseline_repository': str(baseline),
              'baseline_archive_sha256': archive_hash, 'baseline_source_sha256': baseline_sources,
              'source_sha256': sources, 'build_dir': str(build_dir), 'build': build,
              'enrollment': str(args.enrollment.resolve()), 'seed_report': str(seed_path),
              'seed': str(seed), 'seed_sha256': before, 'base_rescue_dir': str(bundle),
              'base_archive_sha256': cpp.boot.sha256(bundle / 'rescue-root.tar.gz'),
              'variants': variants,
              'scope': 'complete selected C++ payload, manager and production units; common kernel/seed/generic base/tools; no unit overrides',
              'baseline_adapter': 'relocate fixed installed-base input to the same explicit generic bundle; historical source/runtime/units unchanged',
              'sampling': {'cpu_seconds': .02, 'pss_seconds': .2, 'idle_and_storm_seconds': 20,
                           'recovery_seconds': 16, 'quiet_recovery_fetch_seconds': 17,
                           'event_rate_per_second': 100, 'quota_percent': 20,
                           'aggregation': 'root service plus data parent slice once; helpers included, sampler/workloads/kernel workers excluded'}}
    path = folder / 'prepared.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print('Prepared paired production trial:', path, flush=True)
    return path, report


def verify_prepared(path):
    path = path.resolve(strict=True)
    if not path.is_relative_to(WORK.resolve()):
        raise ValueError('Prepared trial must be below lab/work')
    report = json.loads(path.read_text())
    if report.get('kind') != 'roadmap-production-performance' or report.get('baseline_ref') != BASELINE:
        raise ValueError('Unsupported prepared performance trial')
    if Path(report['folder']).resolve() != path.parent:
        raise ValueError('Prepared report folder mismatch')
    # Reuse the original lab input gate on resume; hashes supplied in a JSON
    # report are not permission to pass arbitrary host devices to QEMU.
    _, _, _, checked_seed, checked_hash = cpp.validate_inputs(SimpleNamespace(
        build_dir=Path(report['build_dir']), enrollment=Path(report['enrollment']),
        seed_report=Path(report['seed_report'])))
    if checked_seed != Path(report['seed']) or checked_hash != report['seed_sha256']:
        raise RuntimeError('Prepared seed no longer matches the isolated lab input contract')
    if cpp.boot.sha256(Path(report['seed'])) != report['seed_sha256']:
        raise RuntimeError('Disposable seed differs from prepared report')
    if cpp.boot.sha256(Path(report['base_rescue_dir']) / 'rescue-root.tar.gz') != report['base_archive_sha256']:
        raise RuntimeError('Common base bundle changed')
    if inventory(Path(report['baseline_repository'])) != report['baseline_source_sha256']:
        raise RuntimeError('Historical baseline source changed')
    for relative, expected in report['source_sha256'].items():
        if not (REPO / relative).resolve().is_relative_to(REPO):
            raise ValueError('Prepared source path escapes the checkout')
        if cpp.boot.sha256(REPO / relative) != expected:
            raise RuntimeError('Current source/input changed: ' + relative)
    for entry in report['variants'].values():
        for field in ('image', 'binary'):
            location = Path(entry[field])
            if not location.is_file() or not location.resolve().is_relative_to(WORK.resolve()):
                raise ValueError('Prepared ELF/initrd must be regular files below lab/work')
        if cpp.boot.sha256(Path(entry['image'])) != entry['image_sha256']:
            raise RuntimeError('Prepared image changed')
        if cpp.boot.sha256(Path(entry['binary'])) != entry['experiment_manifest']['binary_sha256']:
            raise RuntimeError('Prepared native ELF changed')
    return report


def run_trial(prepared, variant, iteration):
    folder = Path(prepared['folder']) / (variant[0] + str(iteration))
    folder.mkdir(mode=0o700)
    entry = prepared['variants'][variant]
    seed = Path(prepared['seed'])
    report = {'schema': 1, 'scenario': 'roadmap-production-performance', 'variant': variant,
              'implementation': 'cpp', 'trial': iteration, 'baseline_ref': BASELINE,
              'quota_percent': 20, 'recovery_rpc_quiet': True, 'quiet_wait_seconds': 17,
              'build': prepared['build'], 'source_image_sha256_before': prepared['seed_sha256'],
              'experiment_manifest': entry['experiment_manifest'],
              'runtime_payload_sha256': entry['experiment_manifest']['runtime_payload_sha256'],
              'initrd_sha256': entry['image_sha256'], 'observer_sha256': entry['observer_sha256'],
              'scope': prepared['scope'], 'sampling': prepared['sampling'], 'passed': False}
    vm = qmp = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        try:
            overlay = cpp.data.create_images(folder, seed)
            command = cpp.data.vm_command(folder, Path(prepared['build_dir']), Path(entry['image']), overlay, seed)
            report['command'] = command
            print('Production performance VM:', variant, iteration, folder, flush=True)
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            cpp.wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP socket')
            qmp = cpp.Channel(folder / 'qmp.sock', qlog, qmp=True)
            cpp.performance.scenarios(folder, qmp, vm, report)
            report['checks']['production_policy_verified_before'] = report['budget']['integration']['verified']
            report['checks']['production_policy_verified_after'] = report['logs']['effective_policy']['integration']['verified']
            report['checks']['detailed_process_and_pss_samples'] = all(
                set(phase['process_cost']) == {'root', *[spec['name'] for spec in cpp.data.SPECS]} and
                phase['pss']['aggregate']['valid_samples'] > 0 for phase in report['phases'])
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
            report['source_image_unchanged'] = cpp.boot.sha256(seed) == prepared['seed_sha256']
            try:
                verify_prepared(Path(prepared['folder']) / 'prepared.json')
                report['sources_unchanged'] = True
            except Exception:
                report['sources_unchanged'] = False
                report['source_error'] = traceback.format_exc()
            try:
                report['filesystem_audits'] = cpp.data.audit_images(folder)
            except Exception:
                report['filesystem_audit_error'] = traceback.format_exc()
            report['passed'] = bool(report['passed'] and report['source_image_unchanged'] and report['sources_unchanged'] and
                report.get('filesystem_audits') and all(value['returncode'] == 0 for value in report['filesystem_audits'].values()))
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return folder / 'report.json', report


def summary(trials):
    result = {}
    for variant in ('baseline', 'current'):
        selected = [report for _, report in trials if report['variant'] == variant and report['passed']]
        phases = {}
        for label in ('idle', 'unrelated', 'relevant', 'recovery'):
            values = [next(phase for phase in report['phases'] if phase['label'] == label) for report in selected]
            def describe(numbers):
                return {'min': min(numbers), 'median': statistics.median(numbers), 'max': max(numbers)} if numbers else None
            phases[label] = {key: describe([phase['groups']['aggregate'][key] for phase in values]) for key in
                ('cpu_mean_percent', 'cpu_peak_20ms_percent', 'cpu_peak_100ms_percent', 'memory_sampled_peak_bytes')}
            phases[label]['pss_sampled_peak_bytes'] = describe([phase['pss']['aggregate']['sampled_peak_bytes'] for phase in values])
            phases[label]['helper_cpu_ticks'] = describe([sum(item['waited_child_cpu_ticks'] for item in phase['process_cost'].values())
                                                         for phase in values if all(item['stable_controller'] for item in phase['process_cost'].values())])
            phases[label]['sampled_helper_count_lower_bound'] = describe([sum(item['observed_helper_count_lower_bound'] for item in phase['process_cost'].values()) for phase in values])
            if label == 'recovery':
                phases[label]['active_recovery_seconds'] = describe([phase['recovery_windows']['aggregate']['journal_seconds'] for phase in values])
                phases[label]['active_recovery_cpu_usec'] = describe([phase['recovery_windows']['aggregate']['accounting']['cpu_total_usec'] for phase in values])
        result[variant] = {'successful_trials': len(selected), 'phases': phases}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path)
    parser.add_argument('--enrollment', type=Path)
    parser.add_argument('--seed-report', type=Path)
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--base-rescue-dir', type=Path)
    parser.add_argument('--prepared', type=Path, help='Resume a previously prepared paired trial')
    parser.add_argument('--run', action='store_true', help='Explicitly start disposable QEMU guests sequentially')
    parser.add_argument('--trials', type=int, default=2, help='Trials per variant; alternate AB/BA ordering')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as an ordinary user; no privileged host operations are needed')
    if not 1 <= args.trials <= 5:
        parser.error('--trials must be 1..5')
    if args.prepared:
        path = args.prepared.resolve()
        prepared = verify_prepared(path)
    else:
        if any(value is None for value in (args.build_dir, args.enrollment, args.seed_report, args.binary, args.base_rescue_dir)):
            parser.error('Preparation needs build-dir, enrollment, seed-report, binary, and base-rescue-dir')
        path, prepared = prepare(args)
    if not args.run:
        print(f'No VM started. Run: python3 lab/roadmap_performance_probe.py --prepared {path} --run --trials {args.trials}')
        return
    trials = []
    for iteration in range(args.trials):
        order = ('baseline', 'current') if iteration % 2 == 0 else ('current', 'baseline')
        for variant in order:
            trial = run_trial(prepared, variant, iteration)
            trials.append(trial)
            result = {'schema': 1, 'prepared_report': str(path), 'baseline_ref': BASELINE,
                      'requested_trials_per_variant': args.trials,
                      'trial_reports': [{'path': str(location), 'sha256': cpp.boot.sha256(location), 'passed': report['passed']}
                                        for location, report in trials],
                      'passed': len(trials) == args.trials * 2 and all(report['passed'] for _, report in trials),
                      'summary': summary(trials),
                      'limits': ['20ms accounting windows and 200ms PSS samples are not arbitrary instantaneous peaks.',
                                 'Observed helper count is a lower bound; very short helpers can finish between samples.',
                                 'Cgroup CPU includes helpers; proc child ticks are quantized and cannot exactly split short helper cost.',
                                 'The observer is excluded from Guard cgroups but still consumes guest CPU.',
                                 'Common generic base/host libraries are controlled; this is not a comparison of two different installed tool bundles.']}
            output = path.parent / 'comparison.json'
            output.write_text(json.dumps(result, indent=2) + '\n')
            print('Paired production comparison:', output, flush=True)
            if not trial[1]['passed']:
                raise SystemExit('Trial failed; preserved raw evidence; later trials were not run')


if __name__ == '__main__':
    main()
