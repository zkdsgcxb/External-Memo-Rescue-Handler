#!/usr/bin/env python3
"""Replay a pinned Python experiment from a complete local Git snapshot.

Historical code is materialized only under lab/work, never installed or used as
a production fallback. A local shared clone preserves Git provenance for old
experiments that record their own commit. No network is accessed.
"""
import argparse
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]
WORK = REPO / 'lab/work'
PYTHON_REVISION = 'e745e5e4b9cde4ffd21d03f6e45a491ca8400083'
PACKAGING_REVISION = '1f887fe3eca3a6089f3fec28cd30ceeb9ce20048'
REVISIONS = (PYTHON_REVISION, PACKAGING_REVISION)


def git(directory, *args):
    return subprocess.check_output(['git', '-C', str(directory), *args], text=True).strip()


def snapshot(revision=PYTHON_REVISION, *, destination=None):
    """Return a verified full historical checkout, refusing changed sources."""
    if revision not in REVISIONS:
        raise ValueError('Only the documented full historical commit IDs are allowed')
    if git(REPO, 'rev-parse', '--verify', revision + '^{commit}') != revision:
        raise RuntimeError('Pinned historical commit is unavailable locally')
    WORK.mkdir(parents=True, exist_ok=True)
    root = (destination or WORK / 'history' / revision).resolve()
    if root == WORK.resolve() or not root.is_relative_to(WORK.resolve()):
        raise ValueError('Historical checkout must be below lab/work')
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout',
                        str(REPO), str(root)], check=True)
        subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
                        'checkout', '--quiet', '--detach', revision], check=True)
    if Path(git(root, 'rev-parse', '--show-toplevel')).resolve() != root:
        raise RuntimeError('Historical directory is not its own Git checkout')
    if git(root, 'rev-parse', 'HEAD') != revision:
        raise RuntimeError('Historical checkout revision differs from its pin')
    subprocess.run(['git', '-C', str(root), 'diff', '--quiet', '--no-ext-diff',
                    'HEAD', '--'], check=True)
    outputs = root / 'lab/work'
    if not outputs.exists() and not outputs.is_symlink():
        outputs.symlink_to(WORK.resolve(), target_is_directory=True)
    if not outputs.is_symlink() or outputs.resolve() != WORK.resolve():
        raise RuntimeError('Historical lab/work must point to the current lab/work')
    untracked = git(root, 'ls-files', '--others', '--exclude-standard', '--',
                    'guard', 'ram-rescue-demo', 'lab').splitlines()
    # The old directory-only ignore rule does not match this symlink. Permit
    # exactly the output link whose destination was verified above.
    if any(path != 'lab/work' for path in untracked):
        raise RuntimeError('Historical checkout contains untracked source files')
    return root


def run_legacy(script, arguments=None, *, revision=PYTHON_REVISION):
    """Run an entire old experiment, keeping all of its imports at one commit."""
    path = Path(script)
    relative = path.resolve().relative_to(REPO) if path.is_absolute() else path
    if (relative.is_absolute() or '..' in relative.parts or relative.suffix != '.py'
            or relative.parts[0] not in ('lab', 'guard')):
        raise ValueError('Expected a repository-relative Python experiment path')
    root = snapshot(revision)
    tracked = git(root, 'ls-files', '--error-unmatch', '--', str(relative))
    if tracked != str(relative) or not (root / relative).is_file():
        raise RuntimeError('Experiment is absent from the selected historical release')
    print(f'Historical Python experiment: {revision} ({relative})', file=sys.stderr)
    result = subprocess.run([sys.executable, '-B', str(root / relative),
                             *(sys.argv[1:] if arguments is None else arguments)])
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', choices=REVISIONS, default=PYTHON_REVISION)
    parser.add_argument('script', type=Path)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    return run_legacy(args.script, args.arguments, revision=args.revision)


if __name__ == '__main__':
    raise SystemExit(main())
