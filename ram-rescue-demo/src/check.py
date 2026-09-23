#!/usr/bin/python3
import json
import os
from pathlib import Path
import subprocess

def value(*args):
    return subprocess.check_output(args, text=True).strip()

services = ['ram-rescue-prepare.service', 'ram-rescue@tty9.service',
            'ram-rescue@tty10.service', 'ram-rescue-log.service']
for service in services:
    if value('systemctl', 'is-active', service) != 'active':
        raise SystemExit(service + ' is not active')
opts = value('findmnt', '-n', '-o', 'OPTIONS', '/run/ram-rescue-demo').split(',')
assert 'noswap' in opts, 'tmpfs is not protected from swap'
group = Path('/sys/fs/cgroup/ramrescue.slice')
assert (group / 'memory.swap.max').read_text().strip() == '0'
assert int((group / 'memory.max').read_text()) == 768 * 1024 * 1024
for service in services[1:]:
    pid = value('systemctl', 'show', '-p', 'MainPID', '--value', service)
    assert pid != '0'
    assert os.path.samefile('/proc/' + pid + '/root', '/run/ram-rescue-demo')
print(json.dumps({'services': 'active', 'tmpfs_noswap': True, 'process_swap_limit': 0,
                  'slice_memory_bytes': int((group / 'memory.current').read_text()),
                  'slice_limit_bytes': int((group / 'memory.max').read_text())}, indent=2))
