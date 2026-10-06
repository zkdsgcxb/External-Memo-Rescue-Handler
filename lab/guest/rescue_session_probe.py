#!/usr/bin/python3
"""VM-only real authentication tests using a separate, disposable RAM root."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile


def gate():
    if ('ram_rescue_lab=1' not in Path('/proc/cmdline').read_text().split() or
            Path('/sys/class/dmi/id/product_name').read_text().strip() != 'RAMRescueLab'):
        raise RuntimeError('Refusing authentication fixture outside the disposable QEMU lab')
    if os.geteuid() != 0:
        raise RuntimeError('Authentication fixture needs real root inside QEMU')


def command(*args, **kwargs):
    result = subprocess.run(args, check=False, text=True, capture_output=True,
                            timeout=15, **kwargs)
    if result.returncode:
        raise RuntimeError(f'Fixture command {args!r} exited {result.returncode}: '
                           + result.stderr[-4096:])
    return result


def inside_namespace(parent_namespace):
    gate()
    if os.stat('/proc/self/ns/mnt').st_ino == parent_namespace:
        raise RuntimeError('Authentication fixture needs a new mount namespace')
    # Unshare has already isolated this helper's mount namespace. Do not let
    # its terminal-only mounts propagate into the running guest's namespaces.
    command('/bin/busybox', 'mount', '--make-rprivate', '/')
    if command('/bin/busybox', 'stat', '-f', '-c', '%T', '/run').stdout.strip() != 'tmpfs':
        raise RuntimeError('Authentication fixture must stay in RAM')
    # The production rescue /dev bind deliberately does not import later
    # devpts submounts. Real VT logins need none; this PTY-only fixture does.
    command('/bin/busybox', 'mount', '-t', 'devpts', '-o',
            'newinstance,ptmxmode=0666,mode=0620', 'devpts', '/dev/pts')
    spec = importlib.util.spec_from_file_location(
        'rescue_session_smoke', Path(__file__).with_name('rescue_session_smoke.py'))
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    manifest = json.loads(Path('/etc/rescue/session-build.json').read_text())
    files = manifest['file_sha256']
    results = {}
    root = Path(tempfile.mkdtemp(prefix='rescue-auth-test-', dir='/run'))
    mounts = []
    try:
        # Ubuntu /run may be noexec. Only this private fixture mount needs to
        # execute the copied tools; the guest's /run mount policy is unchanged.
        command('/bin/busybox', 'mount', '-t', 'tmpfs', '-o',
                'size=32M,noswap,mode=0700,nosuid,nodev,exec', 'rescue-auth-test', str(root))
        mounts.append(root)
        for absolute, expected in files.items():
            source = Path(absolute)
            if not source.is_absolute() or '..' in source.parts or len(source.parts) < 2:
                raise RuntimeError('Unexpected session payload path')
            allowed = (absolute in ('/bin/rescue-session', '/sbin/rescue-supervisor',
                                   '/usr/bin/bash', '/etc/rescue/session.conf', '/etc/motd') or
                       source.parts[1] in ('lib', 'lib64'))
            if not allowed or source.is_symlink() or not source.is_file():
                raise RuntimeError('Unexpected session payload path')
            if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise RuntimeError('Session payload differs from its staged manifest: ' + absolute)
            target = root / absolute.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        shutil.copy2('/bin/busybox', root / 'bin/busybox')
        (root / 'bin/sh').symlink_to('busybox')
        for directory in ('root', 'dev/pts', 'run', 'var/log'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        for name in ('lastlog', 'wtmp'):
            (root / 'var/log' / name).touch()
        (root / 'run/utmp').touch()
        (root / 'etc/issue').write_text('Disposable RAM rescue authentication test\n')
        (root / 'etc/motd').write_text('Disposable test account, no production credentials\n')
        (root / 'etc/passwd').write_text(
            'root:x:0:0:Disabled root:/root:/bin/sh\n'
            'rescue:x:0:0:Disposable test:/root:/bin/rescue-session\n')
        (root / 'etc/group').write_text('root:x:0:\n')
        (root / 'etc/nsswitch.conf').write_text('passwd: files\ngroup: files\nshadow: files\n')
        (root / 'etc/rescue/session.conf').write_text('IDLE_TIMEOUT=2\n')
        # No securetty file in this temporary root: login is tested via a PTY.
        # Neither existing guest shadow database is read or copied.
        shadow = root / 'etc/shadow'
        shadow.write_text('root:!:20000:0:99999:7:::\nrescue:!:20000:0:99999:7:::\n')
        shadow.chmod(0o600)
        for device in ('pts', 'null', 'random', 'urandom', 'tty'):
            target = root / 'dev' / device
            if device != 'pts':
                target.touch()
            command('/bin/busybox', 'mount', '--bind', '/dev/' + device, str(target))
            mounts.append(target)
        secret = secrets.token_urlsafe(24)
        results['locked_account_rejected'] = smoke.login_test(str(root), secret, False, invalid_hash=True)
        hashed = command('/bin/busybox', 'mkpasswd', '-m', 'sha512', '-P', '0',
                         input=secret + '\n').stdout.strip()
        if not hashed.startswith('$6$'):
            raise RuntimeError('Unexpected test password hash format')
        shadow.write_text('root:!:20000:0:99999:7:::\nrescue:' + hashed + ':20000:0:99999:7:::\n')
        results['wrong_password_rejected'] = smoke.login_test(str(root), 'wrong-test-password', False)
        results['correct_password_authenticated'] = smoke.login_test(str(root), secret, True)
        results['explicit_exit_requires_reauthentication'] = smoke.supervisor_test(str(root), secret)
        results['idle_exit_requires_reauthentication'] = smoke.supervisor_test(str(root), secret, idle=True)
        shadow.unlink()
        results['missing_shadow_rejected'] = smoke.login_test(str(root), secret, False, invalid_hash=True)
        del secret, hashed
    except BaseException as error:
        raise RuntimeError('Completed authentication checks: ' + json.dumps(results) + '\n'
                           + str(error)) from error
    finally:
        # Never recursively delete through a mount after a failed unmount.
        # Namespace teardown releases any remaining mounts when this child exits.
        while mounts:
            target = mounts[-1]
            command('/bin/busybox', 'umount', str(target))
            mounts.pop()
        if not mounts:
            shutil.rmtree(root)
    return {'passed': len(results) == 6, 'checks': results,
            'scope': 'real BusyBox authentication in a temporary RAM root; fresh test password only',
            'temporary_root_removed': not root.exists(),
            'production_credentials_accessed': False}


def run():
    gate()
    result = subprocess.run(
        ['/bin/busybox', 'unshare', '-m', '/usr/bin/python3', '-I', __file__, '--inside',
         str(os.stat('/proc/self/ns/mnt').st_ino)],
        text=True, capture_output=True, check=False, timeout=100)
    if result.returncode:
        raise RuntimeError(f'Authentication fixture exited {result.returncode}: '
                           + result.stderr[-8192:])
    return json.loads(result.stdout)


if __name__ == '__main__':
    gate()
    if len(sys.argv) != 3 or sys.argv[1] != '--inside' or not sys.argv[2].isdigit():
        raise SystemExit('Invoke run() through the VM integration observer')
    print(json.dumps(inside_namespace(int(sys.argv[2]))))
