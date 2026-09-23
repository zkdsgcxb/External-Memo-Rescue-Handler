#!/usr/bin/python3
"""Bounded RAM-only kernel log, independent of journald and the root disk."""
import errno
import os
from pathlib import Path
import select
import subprocess
import time

path = Path('/var/log/kernel-live.log')
limit = 4 * 1024 * 1024
try:
    snapshot = subprocess.check_output(['/bin/busybox', 'dmesg'])
    path.write_bytes(snapshot[-limit:])
except subprocess.SubprocessError:
    path.write_bytes(b'Initial dmesg snapshot unavailable.\n')
fd = os.open('/dev/kmsg', os.O_RDONLY | os.O_NONBLOCK)
os.lseek(fd, 0, os.SEEK_END)
poll = select.poll()
poll.register(fd, select.POLLIN)
while True:
    poll.poll(2000)
    try:
        data = os.read(fd, 65536)
    except BlockingIOError:
        continue
    except OSError as exc:
        if exc.errno == errno.EPIPE:
            continue
        raise
    if path.exists() and path.stat().st_size + len(data) > limit:
        old = path.with_suffix('.log.1')
        path.replace(old)
    with path.open('ab') as stream:
        stream.write(data)
