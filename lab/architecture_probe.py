#!/usr/bin/env python3
"""Compare native-header and Python ABIs in x86_64, ARM64 and RISC-V processes.

Uses an explicitly prepared, unprivileged tool directory. QEMU user mode shares
the host kernel; this probe does not establish foreign-kernel USB recovery.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
TESTS = ['test_linux_abi', 'test_admission', 'test_data_recovery',
         'test_guard_state', 'test_monitor', 'test_owned_operation']


def run(command, *, env=None, timeout=180):
    result = subprocess.run([str(arg) for arg in command], text=True,
                            capture_output=True, env=env, timeout=timeout,
                            cwd=REPO)
    if result.returncode:
        raise RuntimeError(f'{shlex.join(map(str, command))}\n{result.stdout}{result.stderr}')
    return result.stdout, result.stderr


def probe(tools, work):
    if not work.is_relative_to(REPO / 'lab/work') or work.exists():
        raise ValueError('Use a new directory below lab/work')
    work.mkdir(parents=True, mode=0o700)
    includes = ['-I' + str(tools / 'linux7'), '-I' + str(tools / 'root/usr/include')]
    paths = ['guard/runtime', 'ram-rescue-demo/src', 'lab/tests', 'lab/guest']
    python_env = {**os.environ, 'PYTHONPATH': ':'.join(str(REPO / p) for p in paths)}
    compiler_env = {**os.environ, 'LD_LIBRARY_PATH': str(tools / 'root/usr/lib/x86_64-linux-gnu')}
    sources = [Path(__file__), REPO / 'lab/abi_probe.c', REPO / 'ram-rescue-demo/src/rescue.py',
               *sorted((REPO / 'guard/runtime').glob('*.py')),
               *(REPO / 'lab/tests' / (name + '.py') for name in TESTS),
               tools / 'linux7/linux/fs.h', tools / 'linux7/linux/dm-ioctl.h',
               tools / 'root/usr/include/libdevmapper.h']
    before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    report = {'passed': False,
              'scope': 'header ABI and QEMU user-mode Python tests; no foreign kernel',
              'architectures': {}, 'sources': before}
    try:
        for machine, package_arch in [('x86_64', None), ('aarch64', 'arm64'),
                                       ('riscv64', 'riscv64')]:
            binary = work / ('abi-' + machine)
            if package_arch:
                compiler = tools / 'root/usr/bin' / (machine + '-linux-gnu-gcc-13')
                emulator = tools / 'root/usr/bin' / ('qemu-' + machine + '-static')
                prefix = tools / package_arch
                python = [emulator, '-L', prefix, prefix / 'usr/bin/python3.12']
                environment = {**python_env, 'PYTHONHOME': str(prefix / 'usr')}
                execute = [emulator, binary]
            else:
                compiler, python = 'cc', [sys.executable]
                environment, execute = python_env, [binary]
            command = [compiler, '-O2', '-static', '-std=c11', '-Wall', '-Wextra',
                       '-Werror', *includes, REPO / 'lab/abi_probe.c', '-o', binary]
            run(command, env=compiler_env)
            c_values = json.loads(run(execute)[0])
            python_values = json.loads(run([*python, '-S', '-c',
                'import json,linux_abi; print(json.dumps(linux_abi.abi_report()))'],
                env=environment)[0])
            if c_values != python_values:
                raise RuntimeError(f'C/Python ABI mismatch on {machine}')

            # A local wrapper lets subprocess tests re-enter QEMU without
            # registering a foreign ELF handler in the host's binfmt_misc.
            wrapper = work / (machine + '-python')
            wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(map(str, python)) + ' "$@"\n')
            wrapper.chmod(0o700)
            script = ('import sys,unittest; sys.executable=' + repr(str(wrapper)) + '; '
                      'unittest.main(module=None,argv=' + repr([str(wrapper), '-q', *TESTS]) + ')')
            output, error = run([*python, '-S', '-c', script], env=environment)
            (work / (machine + '-tests.log')).write_text(output + error)
            report['architectures'][machine] = {
                'abi': python_values, 'matches_native_headers': True,
                'test_modules': TESTS, 'tests_passed': True,
                'compiler_command': list(map(str, command)),
                'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            }
        report['sources_unchanged'] = all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
                                          for path, digest in before.items())
        if not report['sources_unchanged']:
            raise RuntimeError('Probe inputs changed during validation')
        report['passed'] = True
        return report
    finally:
        (work / 'report.json').write_text(json.dumps(report, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools-root', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    args = parser.parse_args()
    report = probe(args.tools_root.resolve(), args.work_dir.resolve())
    print(json.dumps({'passed': report['passed'],
                      'architectures': list(report['architectures'])}))


if __name__ == '__main__':
    main()
