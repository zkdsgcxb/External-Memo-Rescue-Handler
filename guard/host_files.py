"""Small file helpers shared by host enrollment, builds and installation."""
import hashlib
import os
from pathlib import Path
import tempfile


def sha256(path):
    """Hash a file without loading an entire image into memory."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic(path, data, mode=0o600):
    """Replace one file and sync both its contents and directory entry.

    The temporary file stays beside the destination so the rename is atomic.
    A directory sync failure can occur after replacement; callers that manage
    transactions must check the actual destination before rolling back.
    """
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)
