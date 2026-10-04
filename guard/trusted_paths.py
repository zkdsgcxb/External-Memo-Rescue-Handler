"""Read privileged records through verified directory descriptors.

Only configuration and installed code use this policy, never sysfs/device
discovery. The optional anchor/uid arguments support isolated test trees;
production callers use the root-owned filesystem namespace.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat


def _check(info, uid, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022
            or (not directory and info.st_nlink != 1)):
        raise RuntimeError('Untrusted owner, permissions or file type')


def open_directory(path, *, uid=0, anchor=Path('/')):
    path, anchor = Path(path), Path(anchor)
    if not path.is_absolute() or '..' in path.parts or not path.is_relative_to(anchor):
        raise ValueError('Expected an absolute path within the trusted namespace')
    parts = path.relative_to(anchor).parts
    directory = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _check(os.fstat(directory), uid, True)
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
            _check(os.fstat(directory), uid, True)
        return directory
    except BaseException:
        os.close(directory)
        raise


@contextmanager
def open_trusted(path, *, uid=0, anchor=Path('/')):
    path = Path(path)
    directory = open_directory(path.parent, uid=uid, anchor=anchor)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=directory)
        _check(os.fstat(descriptor), uid)
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def read_trusted(path, *, limit=65536, uid=0, anchor=Path('/')):
    with open_trusted(path, uid=uid, anchor=anchor) as fd:
        before = os.fstat(fd)
        if before.st_size > limit:
            raise ValueError('Oversized trusted record')
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RuntimeError('Trusted record changed or exceeded its size limit')
        return bytes(data)


def read_trusted_json(path, **kwargs):
    return json.loads(read_trusted(path, **kwargs))


def trusted_sha256(path, **kwargs):
    with open_trusted(path, **kwargs) as fd, os.fdopen(os.dup(fd), 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
