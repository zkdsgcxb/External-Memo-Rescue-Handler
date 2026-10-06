#!/usr/bin/python3 -I
"""Installed, root-owned administration entry. No workspace imports."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import sys

sys.dont_write_bytecode = True

PACKAGE_ROOT = Path('/usr/lib/ram-rescue-handler/PACKAGE_VERSION')


def trusted_bytes(path, limit):
    """Bootstrap verification uses stdlib only, before importing the package."""
    if not path.is_absolute() or '..' in path.parts:
        raise RuntimeError('Invalid installed package path')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Installed administration root directory is not protected')
        for index, part in enumerate(path.parts[1:]):
            directory = index < len(path.parts) - 2
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                            (os.O_DIRECTORY if directory else os.O_NONBLOCK), dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            kind = stat.S_ISDIR if directory else stat.S_ISREG
            if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('Installed administration path is not root-owned and protected')
            if not directory and (info.st_nlink != 1 or info.st_size > limit):
                raise RuntimeError('Invalid installed administration file')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError('Oversized installed administration file')
        return data
    finally:
        os.close(fd)


def main():
    commands = {'device': 'lifecycle.py', 'manager': 'manage.py', 'install-image': 'install.py',
                'upgrade-image': 'upgrade.py', 'enroll-root': 'enroll.py', 'image-inputs': 'stage_inputs.py'}
    if sys.argv[1:] == ['--version']:
        print('rescue-guard-admin 0.0.1-beta')
        return
    if sys.argv[1:] in (['--help'], ['-h']):
        print('Usage: rescue-guard-admin {device|manager|install-image|upgrade-image|enroll-root|image-inputs} ARGS...')
        return
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        raise SystemExit('Usage: rescue-guard-admin {device|manager|install-image|upgrade-image|enroll-root|image-inputs} ARGS...')
    if os.geteuid() != 0:
        raise SystemExit('Run the installed entry with local administrator authentication')
    manifest_bytes = trusted_bytes(PACKAGE_ROOT / 'administration.json', 1024 * 1024)
    manifest = json.loads(manifest_bytes)
    files = manifest.get('files', {})
    if (manifest.get('schema') != 1 or not isinstance(files, dict)
            or not 1 <= len(files) <= 4096
            or 'guard/' + commands[sys.argv[1]] not in files):
        raise RuntimeError('Invalid installed administration manifest')
    bootstrap = None
    for relative, expected in files.items():
        name = Path(relative)
        if (not name.parts or name.is_absolute() or '..' in name.parts
                or name.parts[0] not in ('guard', 'ram-rescue-demo')):
            raise RuntimeError('Invalid installed administration member')
        content = trusted_bytes(PACKAGE_ROOT / name, 8 * 1024 * 1024)
        if hashlib.sha256(content).hexdigest() != expected:
            raise RuntimeError('Installed administration code differs: ' + relative)
        if relative == 'guard/admin_entry.py':
            bootstrap = content
    if bootstrap is None:
        raise RuntimeError('Verified bootstrap is missing from the manifest')
    entry = trusted_bytes(Path('/usr/bin/rescue-guard-admin'), 65536)
    placeholder = b'/usr/lib/ram-rescue-handler/' + b'PACKAGE_VERSION'
    rendered = bootstrap.replace(placeholder, str(PACKAGE_ROOT).encode())
    if entry != rendered:
        raise RuntimeError('Installed administration entry differs from its verified source')
    # Only verified package paths enter the import search path. -I disables
    # caller PYTHONPATH, user site packages and the current working directory.
    sys.path[:0] = [str(PACKAGE_ROOT / 'guard'), str(PACKAGE_ROOT / 'ram-rescue-demo/src')]
    import current_support
    current_support.bind_entry({'package_root': str(PACKAGE_ROOT),
                               'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
                               'entrypoint_sha256': hashlib.sha256(entry).hexdigest()})
    program = PACKAGE_ROOT / 'guard' / commands[sys.argv[1]]
    sys.argv = [str(program), *sys.argv[2:]]
    runpy.run_path(str(program), run_name='__main__')


if __name__ == '__main__':
    main()
