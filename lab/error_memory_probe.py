#!/usr/bin/env python3
"""Isolate first-error formatting allocations without opening a block device.

Each variant runs in a fresh process. The candidate changes only the local
module object in this disposable diagnostic process, never repository sources.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent.parent
BASELINE = '779869cf50842e6e8f5c8fae265be8629410ea49'


def child(variant):
    sys.path[:0] = [str(REPO / 'guard/runtime'), str(REPO / 'ram-rescue-demo/src')]
    import path_guard  # Same import graph as the actual controller, no startup.
    import owned_operation as production
    original = subprocess.check_output(
        ['git', 'show', BASELINE + ':guard/runtime/owned_operation.py'], cwd=REPO)
    digest = hashlib.sha256(original).hexdigest()
    source = REPO / 'lab/work' / ('error-format-baseline-' + digest[:16] + '.py')
    try:
        with source.open('xb') as output:
            output.write(original)
    except FileExistsError:
        if source.read_bytes() != original:
            raise RuntimeError('Existing baseline diagnostic source differs')
    specification = importlib.util.spec_from_file_location('owned_operation_reference', source)
    owned_operation = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(owned_operation)
    if variant == 'locations':
        owned_operation._error = production._error
    def memory():
        rows = {}
        for line in Path('/proc/self/smaps_rollup').read_text().splitlines():
            words = line.split()
            if len(words) == 3 and words[2] == 'kB':
                rows[words[0][:-1]] = int(words[1]) * 1024
        return rows
    before = memory()
    modules = set(sys.modules)
    owner_fd = os.open(__file__, os.O_RDONLY | os.O_CLOEXEC)
    operation = owned_operation.OwnedOperation(owner_fd)
    def missing_path():
        raise RuntimeError('Expected ONE matching USB disk; found 0. No changes made.')
    started = time.monotonic()
    operation.start('verify', missing_path)
    outcome = None
    while outcome is None:
        outcome = operation.poll()
        if outcome is None:
            time.sleep(.001)
    elapsed = time.monotonic() - started
    time.sleep(.2)
    after = memory()
    operation.close()
    os.close(owner_fd)
    print(json.dumps({'variant': variant, 'before_bytes': before, 'after_bytes': after,
                      'delta_bytes': {key: after[key] - before[key] for key in before},
                      'first_error_seconds': elapsed, 'new_modules': sorted(set(sys.modules) - modules),
                      'error': outcome['error'], 'python': sys.version, 'libc': os.confstr('CS_GNU_LIBC_VERSION'),
                      'baseline_ref': BASELINE, 'baseline_source_sha256': digest,
                      'candidate_source_sha256': hashlib.sha256((REPO / 'guard/runtime/owned_operation.py').read_bytes()).hexdigest(),
                      'probe_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}))


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        child(sys.argv[2])
        return
    results = []
    for _ in range(3):
        for variant in ('current', 'locations'):
            results.append(json.loads(subprocess.check_output(
                [sys.executable, str(Path(__file__).resolve()), '--child', variant], text=True)))
    print(json.dumps({'schema': 1, 'scope': 'isolated error formatter; no block access; not a full Guard benchmark',
                      'results': results}, indent=2))


if __name__ == '__main__':
    main()
