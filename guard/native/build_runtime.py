#!/usr/bin/env python3
"""Build the complete native controller and its non-destructive unit checks."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def verify_hardening(binary):
    """Inspect the delivered ELF, not the compiler's platform defaults."""
    def readelf(*args):
        return subprocess.check_output(['readelf', *args, str(binary)], text=True,
                                       env={**os.environ, 'LC_ALL': 'C'})
    header, program, dynamic, symbols = (readelf('-hW'), readelf('-lW'),
                                        readelf('-dW'), readelf('-sW'))
    stack = next((line for line in program.splitlines() if 'GNU_STACK' in line), '')
    checks = {
        'pie': bool(re.search(r'Type:\s+DYN', header)),
        'relro': 'GNU_RELRO' in program,
        'bind_now': 'BIND_NOW' in dynamic or bool(re.search(r'FLAGS_1.*\bNOW\b', dynamic)),
        'non_executable_stack': bool(stack) and 'E' not in stack.split()[-2],
        'stack_protector': '__stack_chk_fail' in symbols,
        'fortified_calls': bool(re.search(r'__(?:memcpy|memmove|memset|read|snprintf|printf|fprintf|strcpy|strcat)_chk', symbols)),
    }
    if not all(checks.values()):
        raise RuntimeError('Native ELF hardening verification failed: ' + str(checks))
    return checks


def build(output, compiler='g++', tests=True, sanitizers=False, fuzz_cases=0):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    flags = [compiler, '-std=c++17', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
             '-pedantic', '-pthread', '-fPIE', '-fstack-protector-strong',
             '-D_FORTIFY_SOURCE=3', '-I', str(BASE / 'vendor')]
    link_flags = ['-pie', '-Wl,-z,relro,-z,now,-z,noexecstack']
    if sanitizers:
        # GCC 13's instrumented std::regex emits a libstdc++ false positive.
        # Keep it visible without weakening the ordinary release build.
        flags += ['-O1', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                  '-Wno-error=maybe-uninitialized']
    sources = BASE / 'runtime'
    libraries = ['-ldl', '-lcrypto', '-pthread']
    objects = []
    for name in ('core', 'admission', 'controller', 'boot'):
        obj = output / (name + '.o')
        subprocess.run([*flags, '-c', str(sources / (name + '.cpp')), '-o', str(obj)], check=True)
        objects.append(str(obj))
    binary = output / 'guard-runtime'
    subprocess.run([*flags, str(sources / 'main.cpp'), *objects, *libraries, *link_flags, '-o', str(binary)], check=True)
    # Keep debug symbols beside the stripped executable for diagnosis, without
    # paying their tmpfs payload cost in the recovery environment.
    subprocess.run(['objcopy', '--only-keep-debug', str(binary), str(output / 'guard-runtime.debug')], check=True)
    subprocess.run(['strip', '--strip-unneeded', str(binary)], check=True)
    hardening = verify_hardening(binary)
    if tests:
        for source in sorted(sources.glob('*_test.cpp')):
            target = output / source.stem
            subprocess.run([*flags, str(source), *objects, *libraries, *link_flags, '-o', str(target)], check=True)
            subprocess.run([str(target)], check=True)
    if fuzz_cases:
        if not 1 <= fuzz_cases <= 1000000:
            raise ValueError('fuzz_cases must be between 1 and 1000000')
        target = output / 'input_fuzz'
        subprocess.run([*flags, str(sources / 'input_fuzz.cpp'), *objects,
                        *libraries, *link_flags, '-o', str(target)], check=True)
        subprocess.run([str(target), str(fuzz_cases)], check=True)
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(BASE.rglob('*')) if path.is_file() and '__pycache__' not in path.parts}
    details = {'schema': 1, 'compiler': subprocess.check_output([compiler, '--version'], text=True).splitlines()[0],
               'flags': flags[1:], 'link_flags': link_flags, 'hardening': hardening,
               'sanitizers': sanitizers, 'fuzz_cases': fuzz_cases, 'sources': hashes,
               'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'binary_bytes': binary.stat().st_size,
               'runtime': 'cpp', 'installed': False}
    (output / 'build.json').write_text(json.dumps(details, indent=2) + '\n')
    return binary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'lab/work/cpp-runtime')
    parser.add_argument('--compiler', default='g++')
    parser.add_argument('--no-tests', action='store_true')
    parser.add_argument('--sanitizers', action='store_true')
    parser.add_argument('--fuzz-cases', type=int, default=0)
    args = parser.parse_args()
    print(build(args.output, args.compiler, not args.no_tests, args.sanitizers, args.fuzz_cases))
