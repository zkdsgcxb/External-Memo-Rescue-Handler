#!/usr/bin/env python3
"""Build the optional C++ observer into disposable lab output, never install it."""
import argparse
from pathlib import Path
import subprocess


def build(output, compiler='g++', flags=()):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    common = [compiler, '-std=c++17', '-O2', '-Wall', '-Wextra', '-Werror', '-pedantic', *flags]
    for name, file in [('guard-observe', 'observe.cpp'), ('health-test', 'health_test.cpp')]:
        subprocess.run([*common, str(source / file), '-ldl', '-o', str(output / name)], check=True)
    subprocess.run([str(output / 'health-test')], check=True)
    return output / 'guard-observe'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compiler', default='g++')
    args = parser.parse_args()
    print(build(args.output, args.compiler))
