#!/usr/bin/env python3
"""Cross-build native Guard and execute synthetic checks in QEMU user mode.

Uses unpacked GCC/QEMU and foreign OpenSSL development packages in the existing
user tool directory. It neither installs host packages nor registers binfmt.
No foreign kernel or real block-device behavior is claimed by this check.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
NATIVE = REPO / 'guard/native'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(tools, output):
    if not output.is_relative_to(REPO / 'lab/work') or output.exists():
        raise ValueError('Use a fresh directory below lab/work')
    output.mkdir(parents=True)
    sources = [*sorted((NATIVE / 'runtime').glob('*.cpp')),
               *sorted((NATIVE / 'runtime').glob('*.hpp')),
               NATIVE / 'vendor/nlohmann/json.hpp']
    before = {str(path.relative_to(REPO)): sha(path) for path in sources}
    report = {'schema': 1, 'passed': False,
              'scope': 'Full runtime cross-build, synthetic admission/controller tests, canonical JSON; QEMU user mode only',
              'foreign_kernel_tested': False, 'host_packages_installed': False,
              'source_sha256': before, 'architectures': {}}
    compiler_env = {**os.environ, 'LD_LIBRARY_PATH': str(tools / 'root/usr/lib/x86_64-linux-gnu')}

    def run(args, env=None, input=None, timeout=180):
        result = subprocess.run(list(map(str, args)), cwd=REPO, env=env, input=input,
                                text=True, capture_output=True, timeout=timeout)
        with (output / 'commands.jsonl').open('a') as log:
            log.write(json.dumps({'args': list(map(str, args)), 'returncode': result.returncode,
                                  'stdout': result.stdout, 'stderr': result.stderr}) + '\n')
        if result.returncode:
            raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr[-4000:]}')
        return result.stdout

    try:
        for machine, package_arch in [('aarch64', 'arm64'), ('riscv64', 'riscv64')]:
            work = output / machine
            work.mkdir()
            prefix = tools / package_arch
            triplet = machine + '-linux-gnu'
            compiler = tools / 'root/usr/bin' / (triplet + '-g++-13')
            emulator = tools / 'root/usr/bin' / ('qemu-' + machine + '-static')
            flags = [compiler, '--sysroot=' + str(tools / 'root'), '-std=c++17', '-O2',
                     '-Wall', '-Wextra', '-Werror', '-pedantic', '-pthread',
                     '-I' + str(NATIVE / 'vendor'), '-I' + str(prefix / 'usr/include'),
                     '-I' + str(prefix / 'usr/include' / triplet)]
            libraries = ['-static-libstdc++', '-static-libgcc',
                         '-L' + str(prefix / 'usr/lib' / triplet), '-lcrypto', '-ldl', '-pthread']
            item = {'passed': False, 'compiler': run([compiler, '--version'], env=compiler_env).splitlines()[0],
                    'flags': list(map(str, flags[1:])), 'libraries': libraries, 'binaries': {}, 'checks': {}}
            report['architectures'][machine] = item
            objects = []
            for name in ('core', 'admission', 'controller', 'boot'):
                target = work / (name + '.o')
                run([*flags, '-c', NATIVE / 'runtime' / (name + '.cpp'), '-o', target], env=compiler_env)
                objects.append(target)
            for name in ('main', 'admission_test', 'controller_test', 'core_test'):
                binary = work / ('guard-runtime' if name == 'main' else name)
                run([*flags, NATIVE / 'runtime' / (name + '.cpp'), *objects, *libraries, '-o', binary],
                    env=compiler_env)
                item['binaries'][binary.name] = {'sha256': sha(binary), 'bytes': binary.stat().st_size,
                                                'elf': run(['file', '-b', binary]).strip()}
            execute = [emulator, '-L', prefix,
                       '-E', 'LD_LIBRARY_PATH=' + str(prefix / 'usr/lib' / triplet)]
            item['checks']['runtime_version'] = run([*execute, work / 'guard-runtime', '--version']).strip()
            for name in ('admission_test', 'controller_test'):
                item['checks'][name] = run([*execute, work / name]).strip()
            cases = [{'unicode': '中文😀', 'ordered': [True, None, 1, -0.0]},
                     [1e-7, 1e-5, 1e-4, 1e6, 1e15, 1e16, 1.2345678901234567],
                     {'path': '/dev/sdb1', 'diskseq': 45, 'deadline': 106.0},
                     [5e-324, 1.7976931348623157e308], {}]
            expected = [json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
                        for value in cases]
            observed = run([*execute, work / 'core_test', '--canonical'], input='\n'.join(expected) + '\n').splitlines()
            if observed != expected:
                raise RuntimeError(f'{machine}: Python canonical JSON differs')
            item['checks']['canonical_json_cases'] = len(cases)
            item['passed'] = True
            print(machine, item['checks'], flush=True)
        report['source_unchanged'] = before == {str(path.relative_to(REPO)): sha(path) for path in sources}
        if not report['source_unchanged']:
            raise RuntimeError('Native sources changed during architecture validation')
        report['passed'] = True
    finally:
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tools', type=Path, default=Path.home() / '.local/share/ram-rescue-abi-tools')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    probe(args.tools.resolve(), args.output.resolve())
