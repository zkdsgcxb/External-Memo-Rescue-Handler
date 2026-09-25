#!/usr/bin/env python3
"""Exercise DM_MPATH_PROBE_PATHS on isolated mappings in a disposable QEMU guest.

The test disk is an empty, per-run regular file. A lower DM suspend models an
incomplete read; it does not claim to reproduce a USB driver hang. These tests
exercise the kernel ABI directly, independently of the Guard implementation.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import textwrap
import time

from auto_run import wait_for
from run import Channel, WORK, qemu_command, shell_probe


GUEST_COMMON = '''\
import fcntl, json, os, subprocess, time
from pathlib import Path
assert 'ram_rescue_lab=1' in Path('/proc/cmdline').read_text().split()
assert Path('/sys/class/dmi/id/product_name').read_text().strip() == 'RAMRescueLab'
DM_MPATH_PROBE_PATHS = 0xfd12  # _IO(0xfd, 0x12), linux/dm-ioctl.h
ROOT = Path('/run/kernel-probe')
ROOT.mkdir(exist_ok=True)
def dm(*args):
    try:
        return subprocess.check_output(['/sbin/dmsetup', '--noudevsync', *args],
                                       text=True, stderr=subprocess.STDOUT, timeout=10).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'{args}: {exc.output}') from exc
def status():
    return dm('status', 'probe-target')
def backend_reads():
    st = os.stat('/dev/mapper/probe-backend')
    return int(Path('/sys/dev/block/%d:%d/stat' % (os.major(st.st_rdev), os.minor(st.st_rdev))).read_text().split()[0])
def probe(start_marker=None):
    fd = os.open('/dev/mapper/probe-target', os.O_RDONLY | os.O_CLOEXEC)
    started = time.monotonic()
    if start_marker:
        Path(start_marker).write_text(json.dumps({'pid': os.getpid(), 'started': started}))
    try:
        value = fcntl.ioctl(fd, DM_MPATH_PROBE_PATHS)
        answer = {'return': value, 'errno': None}
    except OSError as exc:
        answer = {'return': None, 'errno': exc.errno, 'error': str(exc)}
    finally:
        os.close(fd)
    answer['elapsed_seconds'] = time.monotonic() - started
    return answer
'''


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def ram_action(folder, name, body, timeout=20):
    """Run recorded source in the guest RAM shell and retain the raw serial reply."""
    source = GUEST_COMMON + '\n' + textwrap.dedent(body)
    (folder / (name + '.py')).write_text(source)
    wrapper = (
        'import base64,json,traceback\n'
        'scope={}\n'
        'try:\n'
        ' exec(base64.b64decode(' + repr(base64.b64encode(source.encode()).decode()) + '),scope)\n'
        ' result={"ok":True,"result":scope["answer"]}\n'
        'except BaseException:\n'
        ' result={"ok":False,"error":traceback.format_exc()}\n'
        'print("KERNEL_PROBE_RESULT="+json.dumps(result),flush=True)\n'
    )
    encoded = base64.b64encode(wrapper.encode()).decode()
    output = b''
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(folder / 'rescue.sock'))
        sock.sendall(b'stty -echo\n')
        time.sleep(.1)
        sock.sendall(b': > /run/kernel-probe-action.b64\n')
        for offset in range(0, len(encoded), 256):
            sock.sendall(("printf '%s' '" + encoded[offset:offset + 256] +
                          "' >> /run/kernel-probe-action.b64\n").encode())
            time.sleep(.01)
        sock.sendall(b"python3 -c \"import base64;exec(base64.b64decode(open('/run/kernel-probe-action.b64','rb').read()))\"\n")
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    raise RuntimeError('RAM shell closed')
                output += chunk
                marker = b'KERNEL_PROBE_RESULT='
                if marker in output and b'\n' in output.split(marker, 1)[1]:
                    response = json.loads(output.split(marker, 1)[1].split(b'\n', 1)[0])
                    if not response['ok']:
                        raise RuntimeError(response['error'])
                    return response['result']
        finally:
            (folder / (name + '.serial.log')).write_bytes(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, default=WORK)
    parser.add_argument('--tcg', action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as a normal user; no host privileges are needed')
    for name in ['vmlinuz', 'initramfs.cpio.gz', 'build.json']:
        if not (args.build_dir / name).is_file():
            parser.error('Missing guest build file: ' + name)
    build = json.loads((args.build_dir / 'build.json').read_text())
    for name, key in [('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')]:
        if sha256(args.build_dir / name) != build[key]:
            parser.error('Guest build hash mismatch: ' + name)
    folder = WORK / (time.strftime('%Y%m%d-%H%M%S') + '-kernel-probe-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    for name, size in [('usb.raw', 2 * 1024**3), ('decoy.raw', 16 * 1024**2)]:
        with (folder / name).open('xb') as stream:
            stream.truncate(size)
    command = qemu_command(folder, tcg=args.tcg, kernel=args.build_dir / 'vmlinuz',
                           initramfs=args.build_dir / 'initramfs.cpio.gz')
    report = {
        'expected_capability': 'available',
        'build': build,
        'command': command,
        'qemu_version': subprocess.check_output(['qemu-system-x86_64', '--version'], text=True).splitlines()[0],
        'runner_sha256': sha256(Path(__file__)),
        'qemu_runner_sha256': sha256(Path(__file__).parent / 'run.py'),
        'passed': False,
        'scope': 'Direct kernel ABI on an isolated virtual disk; no Guard integration or physical USB equivalence claim',
    }
    print('Kernel probe experiment:', folder, flush=True)
    channels = []
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog:
        vm = subprocess.Popen(command, stdout=log, stderr=log)
        try:
            wait_for(lambda: (folder / 'qmp.sock').exists(), 10, 'QMP')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            channels.append(qmp)
            def booted():
                console = (folder / 'console.log').read_text(errors='replace')
                if 'Kernel panic' in console or vm.poll() is not None:
                    raise RuntimeError('Guest boot failed; see console.log')
                return 'LAB_ROOT_READY:' in console
            wait_for(booted, 120, 'guest root')
            qmp.call('device_add', driver='usb-storage', bus='xhci.0', id='probe-disk',
                     drive='decoydisk', serial='KERNEL-PROBE-ONLY')
            report['setup'] = ram_action(folder, '01-setup', '''
                deadline = time.monotonic() + 10
                while not Path('/sys/class/block/sdb/size').exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError('Probe disk did not appear')
                    time.sleep(.05)
                disk = Path('/sys/class/block/sdb').resolve()
                usb = next(p for p in disk.parents if (p/'idVendor').exists())
                assert (usb/'serial').read_text().strip() == 'KERNEL-PROBE-ONLY'
                size = int((disk/'size').read_text())
                assert size == 32768
                # sysfs appears before add_disk()/devtmpfs is necessarily ready.
                while True:
                    try:
                        disk_fd = os.open('/dev/sdb', os.O_RDONLY)
                        try:
                            assert len(os.pread(disk_fd, 512, 0)) == 512
                        finally:
                            os.close(disk_fd)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.05)
                dm('create', 'probe-backend', '--table', f'0 {size} linear /dev/sdb 0')
                dm('mknodes', 'probe-backend')
                dm('create', 'probe-target', '--table', f'0 {size} multipath 2 queue_mode bio 0 1 1 round-robin 0 1 1 /dev/mapper/probe-backend 1')
                dm('mknodes', 'probe-target')
                cold_reads_before = backend_reads()
                cold_probe = probe()
                cold_reads_after = backend_reads()
                # Select current_pg before probing. An unselected group may skip reads.
                fd = os.open('/dev/mapper/probe-target', os.O_RDONLY)
                try:
                    assert len(os.pread(fd, 4096, 0)) == 4096
                finally:
                    os.close(fd)
                answer = {'kernel': os.uname().release, 'size_sectors': size, 'status': status(),
                          'backend_table': dm('table', 'probe-backend'), 'probe_table': dm('table', 'probe-target'),
                          'cold_probe': cold_probe, 'cold_reads_before': cold_reads_before, 'cold_reads_after': cold_reads_after}
            ''')
            report['healthy'] = ram_action(folder, '02-healthy', '''
                before = backend_reads()
                outcome = probe()
                answer = {'probe': outcome, 'reads_before': before, 'reads_after': backend_reads(), 'status': status()}
            ''')
            report['blocked_start'] = ram_action(folder, '03-block-start', '''
                dm('suspend', '--noflush', '--nolockfs', 'probe-backend')
                pid = os.fork()
                if pid == 0:
                    os.setsid()
                    devnull = os.open('/dev/null', os.O_RDWR)
                    for descriptor in (0, 1, 2):
                        os.dup2(devnull, descriptor)
                    outcome = probe(str(ROOT/'started.json'))
                    (ROOT/'complete.tmp').write_text(json.dumps(outcome))
                    os.replace(ROOT/'complete.tmp', ROOT/'complete.json')
                    os._exit(0)
                (ROOT/'pid').write_text(str(pid))
                answer = {'pid': pid, 'lower_mapping_suspended': dm('info', '-c', '--noheadings', '-o', 'suspended', 'probe-backend')}
            ''')
            time.sleep(2)
            report['blocked_observed'] = ram_action(folder, '04-block-observe', '''
                pid = int((ROOT/'pid').read_text())
                started = json.loads((ROOT/'started.json').read_text())
                answer = {'started': started, 'result_exists': (ROOT/'complete.json').exists(),
                          'process_state': Path(f'/proc/{pid}/stat').read_text().split()[2],
                          'elapsed_seconds': time.monotonic() - started['started'], 'status': status()}
            ''')
            report['ram_shell_while_blocked'] = shell_probe(folder / 'rescue.sock')
            report['released'] = ram_action(folder, '05-block-release', '''
                dm('resume', 'probe-backend')
                deadline = time.monotonic() + 10
                while not (ROOT/'complete.json').exists():
                    if time.monotonic() > deadline:
                        raise RuntimeError('Original probe failed to complete after release')
                    time.sleep(.05)
                answer = {'probe': json.loads((ROOT/'complete.json').read_text()), 'status': status()}
            ''')
            report['read_error'] = ram_action(folder, '06-read-error', '''
                dm('suspend', '--noflush', '--nolockfs', 'probe-backend')
                dm('reload', 'probe-backend', '--table', '0 32768 error')
                dm('resume', 'probe-backend')
                before = status()
                outcome = probe()
                answer = {'status_before': before, 'probe': outcome, 'status_after': status(),
                          'backend_table': dm('table', 'probe-backend')}
            ''')
            report['failed_path_not_reinstated'] = ram_action(folder, '07-no-reinstate', '''
                dm('suspend', '--noflush', '--nolockfs', 'probe-backend')
                dm('reload', 'probe-backend', '--table', '0 32768 linear /dev/sdb 0')
                dm('resume', 'probe-backend')
                before = backend_reads()
                answer = {'probe': probe(), 'reads_before': before,
                          'reads_after': backend_reads(), 'status': status()}
            ''')
            healthy, blocked, released, error, failed = (report[key] for key in
                ['healthy', 'blocked_observed', 'released', 'read_error', 'failed_path_not_reinstated'])
            report['checks'] = {
                'cold_zero_can_skip_reads': report['setup']['cold_probe']['return'] == 0 and report['setup']['cold_reads_before'] == report['setup']['cold_reads_after'],
                'healthy_returns_zero': healthy['probe']['return'] == 0,
                'healthy_probe_performs_read': healthy['reads_after'] > healthy['reads_before'],
                'blocked_exceeds_two_seconds': blocked['elapsed_seconds'] >= 2,
                'blocked_probe_incomplete': not blocked['result_exists'],
                'blocked_child_alive': blocked['process_state'] in ['D', 'S', 'R'],
                'ram_shell_alive_during_block': report['ram_shell_while_blocked'],
                'original_probe_completes_after_release': released['probe']['return'] == 0 and released['probe']['elapsed_seconds'] >= 2,
                'error_path_was_active': ' A ' in error['status_before'] and ' F ' not in error['status_before'],
                'read_error_marks_path_failed': ' F ' in error['status_after'],
                'all_failed_is_enotconn': error['probe']['errno'] == 107,
                'probe_does_not_reinstate_failed_path': failed['probe']['errno'] == 107 and ' F ' in failed['status'],
                'failed_path_not_probed': failed['reads_before'] == failed['reads_after'],
            }
            report['passed'] = all(report['checks'].values())
        except BaseException as exc:
            report['error'] = repr(exc)
            raise
        finally:
            report['guest_action_sha256'] = {path.name: sha256(path) for path in sorted(folder.glob('*.py'))}
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill()
                vm.wait()
            for channel in channels:
                channel.close()
    print(json.dumps({key: report[key] for key in ['passed', 'checks']}, indent=2), flush=True)
    if not report['passed']:
        raise SystemExit('Kernel probe checks failed; see report.json')


if __name__ == '__main__':
    main()
