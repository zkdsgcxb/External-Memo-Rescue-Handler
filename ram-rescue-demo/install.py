#!/usr/bin/python3
"""Install only after explicit local sudo authentication; does not touch disks/GRUB."""
import getpass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BASE = Path(__file__).resolve().parent
DEST = Path('/usr/local/lib/ram-rescue-demo')
ETC = Path('/etc/ram-rescue-demo')
UNITS = ['ramrescue.slice', 'ram-rescue-prepare.service', 'ram-rescue@.service', 'ram-rescue-log.service']
SERVICES = ['ram-rescue@tty9.service', 'ram-rescue@tty10.service', 'ram-rescue-log.service']


def run(*args, **kw):
    return subprocess.run(args, check=True, **kw)


def main():
    if os.geteuid() != 0:
        sys.exit('Run with sudo: sudo python3 ' + str(BASE / 'install.py'))
    if not sys.stdin.isatty():
        sys.exit('Run this installer in an interactive local terminal for password entry.')
    if DEST.exists() or ETC.exists():
        sys.exit('An installation already exists. Use its uninstall command before reinstalling.')
    if Path('/run/ram-rescue-demo').exists():
        sys.exit('The reserved RAM path already exists. Inspect it before installing.')
    for unit in UNITS:
        if (Path('/etc/systemd/system') / unit).exists():
            sys.exit('Conflicting systemd unit: ' + unit)
    active_tty = Path('/sys/class/tty/tty0/active').read_text().strip()
    if active_tty in ('tty9', 'tty10'):
        sys.exit('Switch away from tty9/tty10 before installation.')
    for tty in ('tty9', 'tty10'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', f'getty@{tty}.service']).returncode == 0:
            sys.exit(tty + ' already has an active getty; refusing to replace it.')
        if shutil.which('fuser') and subprocess.run(['fuser', f'/dev/{tty}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            sys.exit(tty + ' is already in use.')
    manifest = json.loads((BASE / 'manifest.json').read_text())
    digest = hashlib.sha256((BASE / 'rescue-root.tar.gz').read_bytes()).hexdigest()
    if digest != manifest['sha256']:
        sys.exit('Payload checksum mismatch. Rebuild before installing.')
    print('安装两个独立救援终端：Ctrl+Alt+F9 / Ctrl+Alt+F10。')
    print('用户名 rescue。请设置独立救援密码；不使用或修改 Ubuntu 登录密码。')
    while True:
        password = getpass.getpass('救援密码（不能为空）：')
        again = getpass.getpass('再次输入：')
        if password == again and password and '\n' not in password:
            break
        print('密码不一致、为空或包含换行，请重试。')
    hashed = subprocess.check_output(['/usr/bin/busybox', 'mkpasswd', '-m', 'sha512', '-P', '0'],
                                     input=password + '\n', text=True).strip()
    del password, again
    if not hashed.startswith('$6$'):
        sys.exit('Unexpected password hash format.')
    # Root-capable smoke tests run before any host service or mount is changed.
    run('/usr/bin/python3', str(BASE / 'tests/smoke.py'), '--privileged')
    DEST.mkdir(mode=0o755)
    ETC.mkdir(mode=0o700)
    try:
        for name in ['rescue-root.tar.gz', 'rescue-root.sha256', 'manifest.json']:
            shutil.copyfile(BASE / name, DEST / name)
            (DEST / name).chmod(0o644)
        for name in ['prepare.sh', 'uninstall.py', 'check.py']:
            shutil.copyfile(BASE / 'src' / name, DEST / name)
            (DEST / name).chmod(0o755)
        shadow = ETC / 'shadow'
        fd = os.open(shadow, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write('root:!:20000:0:99999:7:::\nrescue:' + hashed + ':20000:0:99999:7:::\n')
        for name in UNITS:
            shutil.copyfile(BASE / 'src' / name, Path('/etc/systemd/system') / name)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'start', *SERVICES)
        run('/usr/bin/python3', str(DEST / 'check.py'))
        run('systemctl', 'enable', *SERVICES)
    except Exception:
        print('Installation did not complete. Rolling back the demo only.', file=sys.stderr)
        if (DEST / 'uninstall.py').exists():
            subprocess.run(['/usr/bin/python3', str(DEST / 'uninstall.py')])
        raise
    print('\n已安装并启用开机准备。现在请测试 Ctrl+Alt+F9，用户名 rescue。')
    print('运行 rescue status 和 rescue verify；正常状态下不要模拟拔盘。')
    print('验证后输入 exit 锁定终端；Ctrl+Alt+F2 通常返回当前桌面。')
    print('撤销：sudo python3 /usr/local/lib/ram-rescue-demo/uninstall.py')


if __name__ == '__main__':
    main()
