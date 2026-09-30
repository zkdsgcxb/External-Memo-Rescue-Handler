"""Guest-only EFI scenarios, appended to the RAM observer in the lab initrd.

Every public action first uses the protected-boot observer's VM gate. Commands
run in PID 1's root; the observer itself stays in the rescue RAM environment.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import time


class EfiProbe:
    MOUNT = 'boot-efi.mount'
    PATH = 'ram-rescue-efi.path'
    ROOT = Path('/proc/1/root')
    UNIT_PROPERTIES = (
        'ActiveState,SubState,Result,After,BindsTo,Requires,TimeoutUSec,'
        'TimeoutIdleUSec,JobTimeoutUSec,JobRunningTimeoutUSec'
    )

    def __init__(self, gate, systemctl, *, uuid, options, rule, path_unit,
                 fsck_unit, fsck_dropin):
        self.gate = gate
        self.systemctl = systemctl
        self.uuid = uuid
        self.options = options
        self.rule = rule
        self.path_unit = path_unit
        self.fsck_unit = fsck_unit
        self.fsck_dropin = fsck_dropin

    def host(self, *args, timeout=20, check=True):
        started = time.monotonic()
        result = subprocess.run(
            ['/bin/chroot', str(self.ROOT), *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
        response = {
            'returncode': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'elapsed': time.monotonic() - started,
        }
        if check and result.returncode:
            raise RuntimeError(f'Guest command {args!r} failed: {response!r}')
        return response

    @staticmethod
    def optional_text(path):
        path = Path(path)
        return path.read_text() if path.exists() else None

    @staticmethod
    def resolved_link(path):
        return str(path.resolve()) if path.exists() else None

    def snapshot(self):
        units = {}
        for name in (self.MOUNT, self.PATH):
            output = self.systemctl('show', name, '-p', self.UNIT_PROPERTIES)
            units[name] = dict(line.split('=', 1) for line in output.splitlines()
                               if '=' in line)
        link = Path('/dev/disk/by-uuid') / self.uuid
        return {
            'units': units,
            'device': self.resolved_link(link),
            'enrolled_link': self.resolved_link(Path('/dev/ram-rescue-efi')),
            # A device can disappear between the link check and udev query.
            'udev': self.host('/usr/bin/udevadm', 'info', '--query=property',
                              '--name=' + str(link), check=False) if link.exists() else None,
            'fsck': self.host(
                '/usr/bin/systemctl', 'show', self.fsck_unit, '-p',
                'Id,ActiveState,SubState,Result,ExecMainStartTimestampMonotonic,BindsTo',
            ),
            'fsck_journal': self.host('/usr/bin/journalctl', '-b', '--no-pager',
                                      '-o', 'short-monotonic', '-u', self.fsck_unit),
            'delayed_umount': self.optional_text('/run/efi-delayed-umount.log'),
            'udev_events': self.optional_text('/run/efi-udev-events.log'),
            'mounts': [line for line in Path('/proc/1/mountinfo').read_text().splitlines()
                       if line.split()[4] == '/boot/efi'],
        }

    def configure(self):
        (self.ROOT / 'boot/efi').mkdir(exist_ok=True)
        tools = self.host('/bin/sh', '-c',
                          'command -v fsck.vfat; systemd --version | head -n 1')
        if not any((self.ROOT / name).exists()
                   for name in ('usr/sbin/fsck.vfat', 'sbin/fsck.vfat')):
            raise RuntimeError(f'Ubuntu fixture needs dosfstools for pass=1: {tools!r}')
        self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/mount',
                  '-t', 'vfat', '/dev/disk/by-uuid/' + self.uuid, '/boot/efi')
        payload = b'EFI native mount reconnect sentinel\n' * 128
        with (self.ROOT / 'boot/efi/sentinel.bin').open('wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/umount', '/boot/efi')
        fstab_line = f'UUID={self.uuid} /boot/efi vfat {self.options} 0 1'
        with (self.ROOT / 'etc/fstab').open('a') as output:
            output.write('\n' + fstab_line + '\n')
        (self.ROOT / 'etc/udev/rules.d/90-ram-rescue-efi.rules').write_text(self.rule + '\n')
        (self.ROOT / 'etc/systemd/system/ram-rescue-efi.path').write_text(self.path_unit)
        if self.fsck_dropin:
            dropin = self.ROOT / 'etc/systemd/system' / (self.fsck_unit + '.d')
            dropin.mkdir(exist_ok=True)
            (dropin / '50-ram-rescue-efi.conf').write_text(self.fsck_dropin)
        self.host('/usr/bin/udevadm', 'control', '--reload-rules')
        self.systemctl('daemon-reload')
        self.systemctl('stop', self.MOUNT)
        self.systemctl('stop', self.fsck_unit)
        self.systemctl('start', self.MOUNT)
        self.systemctl('enable', '--now', self.PATH)
        device = (Path('/dev/disk/by-uuid') / self.uuid).resolve(strict=True).name
        self.host('/usr/bin/udevadm', 'trigger', '--action=change', '/sys/class/block/' + device)
        self.host('/usr/bin/udevadm', 'settle', '--timeout=10')
        return {
            'tools': tools,
            'expected_sha256': hashlib.sha256(payload).hexdigest(),
            'fstab_line': fstab_line,
            'units': self.systemctl('cat', self.MOUNT, self.PATH),
            'snapshot': self.snapshot(),
        }

    def delay_umount(self):
        binary = self.ROOT / 'usr/bin/umount'
        real = self.ROOT / 'usr/bin/umount.efi-probe-real'
        if real.exists():
            raise RuntimeError('VM umount fixture already installed')
        binary.rename(real)
        binary.write_text('''#!/bin/sh
case " $* " in
    *" /boot/efi "*)
        read now rest </proc/uptime
        printf 'start %s\\n' "$now" >>/run/efi-delayed-umount.log
        sleep 2
        read now rest </proc/uptime
        printf 'end %s\\n' "$now" >>/run/efi-delayed-umount.log
        ;;
esac
exec /usr/bin/umount.efi-probe-real "$@"
''')
        binary.chmod(0o755)
        self.host('/usr/bin/umount.efi-probe-real', '--version')
        self.host('/bin/sync')
        with open('/run/efi-udev-events.log', 'ab', buffering=0) as log:
            monitor = subprocess.Popen(
                ['/bin/chroot', str(self.ROOT), '/usr/bin/udevadm', 'monitor',
                 '--udev', '--property', '--subsystem-match=block'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        Path('/run/efi-udev-monitor.pid').write_text(str(monitor.pid))
        return {'vm_only_umount_delay_seconds': 2, 'monitor_pid': monitor.pid}

    def journal(self):
        return self.host('/usr/bin/journalctl', '-b', '--no-pager', '-o',
                         'short-monotonic', '-u', self.MOUNT, '-u', self.PATH)

    def read(self):
        # Failure while the virtual disk is absent is an expected observation.
        result = self.host(
            '/usr/bin/python3', '-c',
            'import hashlib;print(hashlib.sha256(open("/boot/efi/sentinel.bin","rb").read()).hexdigest())',
            check=False,
        )
        result['snapshot'] = self.snapshot()
        return result

    def repeat_start(self):
        before = self.snapshot()
        self.systemctl('start', self.MOUNT)
        return {'before': before, 'after': self.snapshot(), 'read': self.read()}

    def failure_limit(self):
        self.systemctl('stop', self.PATH)
        self.systemctl('stop', self.MOUNT)
        directory = self.ROOT / 'etc/systemd/system/boot-efi.mount.d'
        directory.mkdir(exist_ok=True)
        dropin = directory / '99-vm-only-failure.conf'
        dropin.write_text('[Mount]\nType=ram_rescue_vm_missing\n')
        self.systemctl('daemon-reload')
        self.systemctl('reset-failed', self.MOUNT, self.PATH)
        self.systemctl('start', self.PATH)
        deadline = time.monotonic() + 8
        while True:
            failed = self.snapshot()
            if failed['units'][self.PATH]['ActiveState'] == 'failed':
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(.05)
        time.sleep(1)
        quiet = self.snapshot()
        dropin.unlink()
        self.systemctl('daemon-reload')
        self.systemctl('reset-failed', self.MOUNT, self.PATH)
        self.systemctl('start', self.MOUNT)
        self.systemctl('start', self.PATH)
        return {'failed': failed, 'quiet': quiet, 'restored': self.snapshot(),
                'read': self.read()}

    def restart_path(self):
        # The deliberate failure consumed the path's 30-second budget. As in
        # production maintenance, restore the mount before enabling its watcher.
        self.systemctl('start', self.MOUNT)
        self.systemctl('start', self.PATH)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.snapshot()
            if result['units'][self.MOUNT]['ActiveState'] == 'active':
                return result
            time.sleep(.05)
        raise RuntimeError('Restarted native path did not remount EFI')

    def stop(self):
        self.systemctl('stop', self.PATH)
        self.systemctl('stop', self.MOUNT)
        return {'snapshot': self.snapshot(), 'journal': self.journal()}

    def action(self, name):
        self.gate()
        actions = {
            'configure': self.configure,
            'delay_umount': self.delay_umount,
            'snapshot': self.snapshot,
            'journal': self.journal,
            'read': self.read,
            'repeat_start': self.repeat_start,
            'failure_limit': self.failure_limit,
            'restart_path': self.restart_path,
            'stop': self.stop,
        }
        if name not in actions:
            raise ValueError(f'Unknown EFI action: {name}')
        return actions[name]()
