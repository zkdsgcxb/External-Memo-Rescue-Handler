#!/usr/bin/python3
"""Isolated chroot smoke tests; no physical disk access, no unplug simulation."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import termios
import time

BASE = Path(__file__).resolve().parents[1]


def run(args, **kw):
    result = subprocess.run(args, text=True, capture_output=True, **kw)
    if result.returncode:
        raise RuntimeError(' '.join(args) + '\n' + result.stdout + result.stderr)
    return result


def login_test(root, password, correct):
    pid, fd = pty.fork()
    if pid == 0:
        os.chroot(root)
        os.chdir('/')
        os.execve('/bin/busybox', ['busybox', 'login', 'rescue'],
                  {'PATH': '/bin:/sbin:/usr/bin', 'TERM': 'linux', 'LOGIN_TIMEOUT': '12'})
    output = b''
    sent = marker = False
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if b'Password:' in output and not sent:
                os.write(fd, (password + '\n').encode())
                sent = True
            if not correct and b'Login incorrect' in output:
                return 'wrong password rejected'
            if correct and (b'# ' in output or b'~ #' in output) and not marker:
                os.write(fd, b'echo RAM_AUTH_OK; exit\n')
                marker = True
            if correct and b'\r\nRAM_AUTH_OK\r\n' in output:
                return 'correct password authenticated'
        raise RuntimeError('Login smoke test failed: ' + output.decode(errors='replace')[-1000:])
    finally:
        try: os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError: pass
        os.close(fd)
        try: os.waitpid(pid, 0)
        except ChildProcessError: pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--privileged', action='store_true')
    parser.add_argument('--inside', action='store_true')
    args = parser.parse_args()
    if not args.inside:
        prefix = ['unshare', '--mount', '--propagation', 'private']
        if not args.privileged:
            prefix.insert(1, '--user')
            prefix.insert(2, '--map-root-user')
        subprocess.run(prefix + [sys.executable, __file__, '--inside'] +
                       (['--privileged'] if args.privileged else []), check=True)
        return
    if os.geteuid() != 0:
        raise SystemExit('This test requires root inside an isolated user/mount namespace.')
    work = Path(tempfile.mkdtemp(prefix='smoke-', dir=BASE / 'work'))
    root = work / 'root'
    root.mkdir()
    mounts = []
    results = []
    try:
        options = 'size=128M,noswap' if args.privileged else 'size=128M'
        run(['mount', '-t', 'tmpfs', '-o', options, 'rescue-smoke', str(root)])
        mounts.append(root)
        results.append('PASS: payload is tested from a separate RAM filesystem')
        results.append('PASS: tmpfs noswap' if args.privileged else
                       'PENDING: noswap requires initial-user-namespace root; installer tests it.')
        with tarfile.open(BASE / 'rescue-root.tar.gz') as archive:
            archive.extractall(root, filter='data')
        # Static kernel tables suffice for config parsing; no live /proc or disks
        # are needed for these library/authentication tests.
        for name in ['mounts', 'devices', 'filesystems']:
            (root / 'proc' / name).write_text(Path('/proc/' + name).read_text())
        # Expose only terminals and null/random devices. No /dev/sd*, mapper or NVMe nodes.
        (root / 'dev/pts').mkdir(exist_ok=True)
        run(['mount', '--bind', '/dev/pts', str(root / 'dev/pts')])
        mounts.append(root / 'dev/pts')
        for name in ['null', 'urandom', 'random', 'tty']:
            dest = root / 'dev' / name
            dest.touch()
            run(['mount', '--bind', '/dev/' + name, str(dest)])
            mounts.append(dest)
        commands = [
            ['/bin/busybox', 'sh', '-c', 'echo standalone-shell-ok'],
            ['/usr/bin/python3', '-I', '-c', 'import json,subprocess,select,hashlib; print("python-ok")'],
            ['/sbin/lvm', 'dumpconfig', '--validate'],
            ['/sbin/e2fsck', '-V'],
            ['/sbin/blkid', '-V'],
            ['/lib64/ld-linux-x86-64.so.2', '--list', '/sbin/dmsetup'],
            ['/sbin/rescue', 'help'],
        ]
        for command in commands:
            run(['chroot', str(root)] + command, timeout=15)
            results.append('PASS: ' + ' '.join(command[:2]))
        # Known password is only in this disposable test image, never in the payload.
        secret = 'Test-only-ram-rescue-12345'
        hashed = run(['busybox', 'mkpasswd', '-m', 'sha512', '-P', '0'], input=secret + '\n').stdout.strip()
        (root / 'etc/shadow').write_text('root:!:20000:0:99999:7:::\nrescue:' + hashed + ':20000:0:99999:7:::\n')
        (root / 'etc/passwd').write_text('root:x:0:0:root:/root:/bin/sh\nrescue:x:0:0:rescue:/root:/bin/sh\n')
        # No securetty restrictions in the DISPOSABLE PTY test image.
        (root / 'etc/securetty').unlink()
        results.append('PASS: ' + login_test(str(root), 'wrong-test-password', False))
        if args.privileged:
            results.append('PASS: ' + login_test(str(root), secret, True))
        else:
            results.append('PENDING: successful login requires real root for setgroups; installer tests it.')
        print('\n'.join(results))
    finally:
        for mount in reversed(mounts):
            subprocess.run(['umount', str(mount)], check=False, capture_output=True)
        shutil.rmtree(work)


if __name__ == '__main__':
    main()
