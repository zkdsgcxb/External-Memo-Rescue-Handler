#!/usr/bin/env python3
"""Build the complete native controller and its non-destructive unit checks."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent


def build(output, compiler='g++', tests=True):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    flags = [compiler, '-std=c++17', '-O2', '-g', '-Wall', '-Wextra', '-Werror',
             '-pedantic', '-pthread', '-I', str(BASE / 'vendor')]
    sources = BASE / 'runtime'
    libraries = ['-ldl', '-lcrypto', '-pthread']
    objects = []
    for name in ('core', 'admission', 'controller', 'boot'):
        obj = output / (name + '.o')
        subprocess.run([*flags, '-c', str(sources / (name + '.cpp')), '-o', str(obj)], check=True)
        objects.append(str(obj))
    binary = output / 'guard-runtime'
    subprocess.run([*flags, str(sources / 'main.cpp'), *objects, *libraries, '-o', str(binary)], check=True)
    # Keep debug symbols beside the stripped executable for diagnosis, without
    # paying their tmpfs payload cost in the recovery environment.
    subprocess.run(['objcopy', '--only-keep-debug', str(binary), str(output / 'guard-runtime.debug')], check=True)
    subprocess.run(['strip', '--strip-unneeded', str(binary)], check=True)
    if tests:
        for source in sorted(sources.glob('*_test.cpp')):
            target = output / source.stem
            subprocess.run([*flags, str(source), *objects, *libraries, '-o', str(target)], check=True)
            subprocess.run([str(target)], check=True)
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(BASE.rglob('*')) if path.is_file() and '__pycache__' not in path.parts}
    details = {'schema': 1, 'compiler': subprocess.check_output([compiler, '--version'], text=True).splitlines()[0],
               'flags': flags[1:], 'sources': hashes,
               'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'binary_bytes': binary.stat().st_size,
               'runtime': 'cpp', 'installed': False}
    (output / 'build.json').write_text(json.dumps(details, indent=2) + '\n')
    return binary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'lab/work/cpp-runtime')
    parser.add_argument('--compiler', default='g++')
    parser.add_argument('--no-tests', action='store_true')
    args = parser.parse_args()
    print(build(args.output, args.compiler, not args.no_tests))
