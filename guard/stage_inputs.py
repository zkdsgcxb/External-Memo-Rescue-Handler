#!/usr/bin/env python3
"""Bundle reviewed image inputs, then copy them into a private root-owned store.

The expected bundle digest must come from the operator's trusted build/review.
It pins input bytes during the privileged operation, not publisher identity.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

from host_files import sha256
from trusted_paths import open_directory

STORE = Path('/var/lib/ram-rescue-candidates')
MEMBERS = {'build.json': 1024**2, 'enrollment.json': 65536,
           'vm_report.json': 32 * 1024**2, 'initrd.img': 512 * 1024**2}


def bundle(build, enrollment, report, output):
    from install import validate
    validate(build, enrollment, report)
    with output.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        with tarfile.open(fileobj=stream, mode='w') as archive:
            for name, path in {'build.json': build / 'build.json', 'initrd.img': build / 'initrd.img',
                               'enrollment.json': enrollment, 'vm_report.json': report}.items():
                if path.stat().st_size > MEMBERS[name] or not path.is_file() or path.is_symlink():
                    raise RuntimeError('Invalid image input: ' + name)
                archive.add(path, arcname=name, recursive=False)
    return {'bundle': str(output), 'sha256': sha256(output), 'installed': False}


def extract(archive_path, destination):
    with tarfile.open(archive_path, 'r:') as archive:
        seen = set()
        for member in archive:
            if member.name not in MEMBERS or member.name in seen or len(seen) >= len(MEMBERS):
                raise RuntimeError('Unexpected image bundle members')
            if not member.isfile() or not 0 <= member.size <= MEMBERS[member.name]:
                raise RuntimeError('Invalid image bundle member')
            seen.add(member.name)
            with archive.extractfile(member) as source, (destination / member.name).open('xb') as target:
                os.fchmod(target.fileno(), 0o600)
                shutil.copyfileobj(source, target, 1024**2)
        if seen != set(MEMBERS):
            raise RuntimeError('Missing image bundle members')
    from install import validate, verify_sources
    _, build, _ = validate(destination, destination / 'enrollment.json', destination / 'vm_report.json')
    verify_sources(build)


def stage(source, expected):
    if os.geteuid() != 0:
        raise RuntimeError('Use the installed administration entry with local authentication')
    if not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('An independently reviewed bundle SHA256 is required')
    STORE.mkdir(mode=0o700, exist_ok=True)
    os.close(open_directory(STORE))
    destination = STORE / expected
    if destination.exists():
        raise RuntimeError('This candidate already exists; inspect or use its sealed inputs')
    temporary = Path(tempfile.mkdtemp(prefix='.staging-', dir=STORE))
    try:
        archive = temporary / 'bundle.tar'
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        import stat
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise RuntimeError('A regular image bundle is required')
        digest, total = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'rb') as incoming, archive.open('xb') as saved:
            os.fchmod(saved.fileno(), 0o600)
            while data := incoming.read(1024**2):
                total += len(data)
                if total > 550 * 1024**2:
                    raise RuntimeError('Image bundle exceeds its limit')
                digest.update(data)
                saved.write(data)
            saved.flush()
            os.fsync(saved.fileno())
        if digest.hexdigest() != expected:
            raise RuntimeError('Image bundle differs from the reviewed digest')
        extract(archive, temporary)
        archive.unlink()
        os.rename(temporary, destination)
        return {'staged': str(destination), 'sha256': expected, 'boot_changed': False,
                'instruction': 'Use this directory with install-image or upgrade-image'}
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    make = commands.add_parser('bundle')
    for argument in ('build-dir', 'enrollment', 'vm-report', 'output'):
        make.add_argument('--' + argument, type=Path, required=True)
    copy = commands.add_parser('stage')
    copy.add_argument('--bundle', type=Path, required=True)
    copy.add_argument('--sha256', required=True)
    args = parser.parse_args()
    result = (bundle(args.build_dir, args.enrollment, args.vm_report, args.output)
              if args.action == 'bundle' else stage(args.bundle, args.sha256))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
