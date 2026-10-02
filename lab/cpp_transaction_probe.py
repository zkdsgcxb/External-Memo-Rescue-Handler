#!/usr/bin/env python3
"""Run the existing fault transaction matrix against the actual native owner.

Only entrypoint checks and launch commands change. The original phase gates,
candidate substitution, held NBD requests and terminal invariants are retained.
All generated guest sources and the adapted runner are saved with hashes.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import cpp_guard_probe as full
from run import WORK

BASE = Path(__file__).resolve().parent
NATIVE = full.NATIVE
CONFIG = '/etc/rescue/path-guard.json'


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise RuntimeError('Transaction fixture contract changed: ' + before)
    return source.replace(before, after)


def build_fixture(folder, original, binary):
    build = json.loads((original / 'build.json').read_text())
    for name, field in [('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')]:
        if full.boot.sha256(original / name) != build[field]:
            raise RuntimeError('Transaction base image hash mismatch: ' + name)
    release, revision = full.frozen_python(folder)
    image = folder / 'initramfs.cpio.gz'
    shutil.copyfile(original / 'initramfs.cpio.gz', image)
    shutil.copyfile(original / 'vmlinuz', folder / 'vmlinuz')
    staging = folder / 'overlay'
    lab = staging / 'opt/lab'
    lab.mkdir(parents=True)
    # All observations/setup use one fixed Python release; none run as owner.
    for path in (release / 'guard/runtime').glob('*.py'):
        shutil.copyfile(path, lab / path.name)
    shutil.copyfile(release / 'ram-rescue-demo/src/rescue.py', lab / 'rescue.py')
    for path in (BASE / 'guest').iterdir():
        if path.is_file():
            shutil.copyfile(path, lab / path.name)
    (lab / 'root-init.sh').chmod(0o755)
    init = (BASE / 'guest/init.sh').read_text()
    init = replace_once(init, 'python3 /opt/lab/path_guard.py; exec python3 /opt/lab/path_guard.py --takeover',
                        f'{NATIVE} run --config {CONFIG}; exec {NATIVE} takeover --config {CONFIG}')
    (staging / 'init').write_text(init)
    (staging / 'init').chmod(0o755)
    ubuntu = (lab / 'ubuntu.py').read_text()
    ubuntu = ubuntu.replace('/usr/bin/python3 /opt/lab/path_guard.py --takeover',
                            f'{NATIVE} takeover --config {CONFIG}')
    ubuntu = ubuntu.replace('/usr/bin/python3 /opt/lab/path_guard.py', f'{NATIVE} run --config {CONFIG}')
    (lab / 'ubuntu.py').write_text(ubuntu)
    agent = (lab / 'agent.py').read_text().replace("b'/opt/lab/path_guard.py'", repr(NATIVE.encode()))
    (lab / 'agent.py').write_text(agent)
    target = staging / NATIVE.lstrip('/')
    target.parent.mkdir(parents=True)
    shutil.copyfile(binary, target)
    target.chmod(0o755)
    for name, source in full.binary_closure(binary).items():
        target = staging / name.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755)
    full.append_archive(image, staging)
    build.update(implementation='cpp', python_observer_ref=revision,
                 native_binary_sha256=full.boot.sha256(binary),
                 initramfs_sha256=full.boot.sha256(image),
                 overlay_sha256={str(path.relative_to(staging)): full.boot.sha256(path)
                                 for path in staging.rglob('*') if path.is_file()})
    (folder / 'build.json').write_text(json.dumps(build, indent=2) + '\n')
    return build


def adapt_runner(binary_digest):
    source = (BASE / 'transaction_probe.py').read_text()
    source = replace_once(source,
        "    # The Python runtime was retired; replay this historical experiment intact.\n"
        "    from historical import run_legacy\n"
        "    raise SystemExit(run_legacy(__file__))",
        '    main()')
    source = replace_once(source,
        'p = subprocess.run(["/usr/bin/python3", "/opt/lab/path_guard.py"], text=True,',
        f'p = subprocess.run(["{NATIVE}", "run", "--config", "{CONFIG}"], text=True,')
    source = source.replace("b'/opt/lab/path_guard.py'", repr(NATIVE.encode()))
    source = replace_once(source, "and '--takeover' in service['stdout']",
                          "and ' takeover --config ' in service['stdout']")
    source = replace_once(source,
        "    assert b'/opt/guard-runtime/guard-runtime' in Path(f'/proc/{pid}/cmdline').read_bytes()",
        "    assert b'/opt/guard-runtime/guard-runtime' in Path(f'/proc/{pid}/cmdline').read_bytes()\n"
        f"    assert hashlib.sha256(Path(f'/proc/{{pid}}/exe').read_bytes()).hexdigest() == {binary_digest!r}")
    source = replace_once(source, "from auto_run import result, wait_for",
                          f"import sys\nsys.path.insert(0, {str(BASE)!r})\nfrom auto_run import result, wait_for")
    # The generated runner lives in its evidence directory, so retain the
    # original qemu runner location without changing its behavior or hash.
    source = source.replace("Path(__file__).parent / 'run.py'", repr(str(BASE / 'run.py')))
    source = source.replace("sha256(" + repr(str(BASE / 'run.py')) + ")",
                            "sha256(Path(" + repr(str(BASE / 'run.py')) + "))")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=('matrix', 'kill', 'manager-absent', 'deadline',
                                            'candidate-replaced', 'blocked-probe', 'random-kill'))
    parser.add_argument('--binary', type=Path, default=WORK / 'cpp-runtime/guard-runtime')
    parser.add_argument('--base-build', type=Path, default=WORK / 'route-refactor-v4')
    parser.add_argument('--guest', choices=('minimal', 'ubuntu'), default='minimal')
    parser.add_argument('--stage', default='after_load')
    parser.add_argument('--queue-seconds', type=int, default=12)
    args = parser.parse_args()
    folder = WORK / ('cpp-txb-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    build = build_fixture(folder, args.base_build, args.binary)
    runner = folder / 'transaction-native.py'
    runner.write_text(adapt_runner(build['native_binary_sha256']))
    build['adapted_runner_sha256'] = full.boot.sha256(runner)
    build['adapter_sha256'] = full.boot.sha256(Path(__file__))
    build['original_runner_sha256'] = full.boot.sha256(BASE / 'transaction_probe.py')
    (folder / 'build.json').write_text(json.dumps(build, indent=2) + '\n')
    subprocess.run([sys.executable, str(runner), args.scenario, '--build-dir', str(folder),
                    '--guest', args.guest, '--stage', args.stage,
                    '--queue-seconds', str(args.queue_seconds)], check=True)


if __name__ == '__main__':
    main()
