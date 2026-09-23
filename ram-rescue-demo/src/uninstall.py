#!/usr/bin/python3
import os
from pathlib import Path
import shutil
import subprocess

if os.geteuid() != 0:
    raise SystemExit('Run with sudo.')
services = ['ram-rescue@tty9.service', 'ram-rescue@tty10.service', 'ram-rescue-log.service']
subprocess.run(['systemctl', 'disable', '--now', *services], check=False)
subprocess.run(['systemctl', 'stop', 'ram-rescue-prepare.service'], check=False)
if subprocess.run(['mountpoint', '-q', '/run/ram-rescue-demo']).returncode == 0:
    raise SystemExit('Rescue mount remains busy; files retained. Exit rescue sessions and retry.')
for name in ['ramrescue.slice', 'ram-rescue-prepare.service', 'ram-rescue@.service', 'ram-rescue-log.service']:
    (Path('/etc/systemd/system') / name).unlink(missing_ok=True)
subprocess.run(['systemctl', 'stop', 'ramrescue.slice'], check=False)
subprocess.run(['systemctl', 'daemon-reload'], check=True)
shutil.rmtree('/etc/ram-rescue-demo', ignore_errors=True)
shutil.rmtree('/usr/local/lib/ram-rescue-demo', ignore_errors=True)
print('Rescue demo uninstalled. Workspace sources and test results retained.')
