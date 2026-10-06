"""Stage the local distribution's authenticated rescue-session dependencies."""
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

BASE = Path(__file__).resolve().parent
SOURCES = (BASE / 'session_payload.py', BASE / 'src/session.sh',
           BASE / 'src/supervisor.sh', BASE / 'src/session.conf', BASE / 'src/motd')


def stage_session(root):
    """Overlay current session code on a new/offline rescue image, never live RAM.

    Bash provides a tested prompt idle timeout. Only the trusted executable
    installed by the local distribution is passed to ldd.
    """
    root = Path(root).resolve(strict=True)
    binary = Path('/usr/bin/bash')
    result = subprocess.run(['ldd', str(binary)], text=True, capture_output=True,
                            check=True)
    if 'not found' in result.stdout + result.stderr:
        raise RuntimeError('A rescue shell dependency is missing')
    libraries = re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)', result.stdout, re.M)
    files = {str(binary): binary, **{path: Path(path).resolve(strict=True) for path in libraries}}
    files.update({'/bin/rescue-session': BASE / 'src/session.sh',
                  '/sbin/rescue-supervisor': BASE / 'src/supervisor.sh',
                  '/etc/rescue/session.conf': BASE / 'src/session.conf',
                  '/etc/motd': BASE / 'src/motd'})
    added_bytes = replaced_bytes = 0
    for absolute, source in files.items():
        target = root / absolute.lstrip('/')
        if not target.resolve().is_relative_to(root):
            raise RuntimeError('Rescue session path escapes the image: ' + absolute)
        if target.is_symlink():
            raise RuntimeError('Unexpected rescue session symlink: ' + absolute)
        if target.exists():
            replaced_bytes += target.stat().st_size
        added_bytes += source.stat().st_size
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644 if absolute.startswith('/etc/') else 0o755)
    return {'file_sha256': {path: hashlib.sha256(source.read_bytes()).hexdigest()
                            for path, source in sorted(files.items())},
            'staged_file_bytes': added_bytes, 'replaced_file_bytes': replaced_bytes,
            'net_added_file_bytes': added_bytes - replaced_bytes}
