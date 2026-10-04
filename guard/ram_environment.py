"""Prepare one immutable RAM tool environment for registered data maps.

The existing root environment is reused when it matches the installed package.
Otherwise a separate tmpfs is prepared at boot. This module never starts a
controller, mounts a filesystem from a disk, or changes a DM mapping.
"""
import json
import os
from pathlib import Path
import subprocess
import tarfile

from host_files import atomic, sha256
from native_payload import verify_runtime
from trusted_paths import open_directory, open_trusted, read_trusted_json, trusted_sha256

BASE = Path(__file__).resolve().parent
STATE = Path('/run/ram-rescue-manager')
ALIAS = STATE / 'tools'
SHARED = Path('/run/ram-rescue-demo')
PRIVATE = STATE / 'rootfs'
PAYLOAD = BASE.parent / 'runtime'
RECEIPT = STATE / 'runtime-environment.json'
FSTAB = Path('/etc/fstab')


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=45).strip()


def mount_record(path):
    result = subprocess.run(['/usr/bin/findmnt', '-J', '-M', str(path), '-o', 'TARGET,FSTYPE,OPTIONS'],
                            text=True, capture_output=True, timeout=10)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError('Cannot inspect the RAM environment mount')
    rows = json.loads(result.stdout).get('filesystems', [])
    return rows[0] if len(rows) == 1 else None


def verify_mount(path):
    row = mount_record(path)
    if not row or row['fstype'] != 'tmpfs' or 'noswap' not in row['options'].split(','):
        raise RuntimeError('A private tmpfs,noswap tool environment is required')
    os.close(open_directory(path))
    for name in ('run', 'dev', 'sys', 'proc'):
        if not os.path.samefile(path / name, '/' + name):
            raise RuntimeError('RAM tools must share the host /' + name)
    return verify_runtime(path)


def active_root():
    flags = Path('/proc/cmdline').read_text().split()
    return ('ram_rescue_guard=1' in flags and 'nompath' in flags and
            run('/usr/bin/systemctl', 'show', 'ram-rescue-guard.service',
                '-p', 'ActiveState', '--value') == 'active')


def package_manifest(payload):
    manifest = read_trusted_json(payload / 'manifest.json')
    if (manifest.get('schema') != 1 or manifest.get('kind') != 'data-runtime'
            or manifest.get('kernel_release') != os.uname().release):
        raise RuntimeError('Installed data tools do not match the current kernel')
    if trusted_sha256(payload / 'tools.tar.gz') != manifest.get('archive_sha256'):
        raise RuntimeError('Installed data tools archive checksum differs')
    return manifest


def unpack(payload, destination):
    """Extract only a bounded, root-owned package into a fresh empty tmpfs."""
    manifest = package_manifest(payload)
    with tarfile.open(payload / 'tools.tar.gz', 'r:gz') as archive:
        members, size = [], 0
        for item in archive:
            size += item.size
            if len(members) >= 20000 or item.size < 0 or size > 240 * 1024**2:
                raise RuntimeError('RAM tools package exceeds its bounds')
            if not (item.isfile() or item.isdir() or item.issym()):
                raise RuntimeError('RAM tools package contains unsupported file types')
            members.append(item)
        archive.extractall(destination, members=members, filter='data')
    if sha256(destination / 'opt/guard-runtime/guard-runtime') != manifest['binary_sha256']:
        raise RuntimeError('Unpacked native runtime differs from its installed package')
    verify_runtime(destination)
    return manifest


def runtime_matches(root, desired):
    """Compare the entire frozen release, including same-ELF library updates."""
    try:
        native = read_trusted_json(root / 'opt/guard-runtime/runtime.json')
        base = read_trusted_json(root / 'etc/rescue/base-source.json')
    except FileNotFoundError:
        return False
    return (native == desired['native_runtime'] and base.get('schema') == 1
            and base.get('sha256') == desired['base_sha256'])


def pin_host_fstab(root):
    """Pin only this cold-admission input, outside every recovery/stop path.

    In-place edits remain visible. An atomic replacement requires a new boot
    instead of silently reusing an old configuration or swapping live mounts.
    """
    target = root / 'etc/rescue/host-fstab'
    with open_trusted(FSTAB) as descriptor:
        info = os.fstat(descriptor)
        if info.st_size > 4 * 1024**2:
            raise RuntimeError('Oversized host fstab')
        expected = info.st_dev, info.st_ino
        current = mount_record(target)
        if current:
            actual = target.stat()
            if (actual.st_dev, actual.st_ino) != expected or 'ro' not in current['options'].split(','):
                raise RuntimeError('Host fstab was replaced; finish data use and reboot before new admission')
            return False
        with target.open('xb') as placeholder:
            os.fchmod(placeholder.fileno(), 0o600)
        mounted = False
        try:
            run('/bin/mount', '--bind', str(FSTAB), str(target))
            mounted = True
            actual = target.stat()
            if (actual.st_dev, actual.st_ino) != expected:
                raise RuntimeError('Host fstab changed while preparing admission')
            run('/bin/mount', '-o', 'remount,bind,ro', str(target))
        except BaseException:
            if mounted:
                run('/bin/umount', str(target))
            target.unlink()
            raise
    return True


def prepare(payload=PAYLOAD):
    if os.geteuid() != 0:
        raise RuntimeError('RAM preparation needs local administrator authentication')
    payload = Path(payload)
    desired = package_manifest(payload) if payload.exists() else None
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.close(open_directory(STATE))
    if ALIAS.is_symlink():
        target = ALIAS.readlink()
        if target != PRIVATE:
            raise RuntimeError('Unexpected manager tools target')
        verify_mount(target)
        if desired and not runtime_matches(target, desired):
            raise RuntimeError('This boot already uses another runtime; restart only after orderly shutdown')
        pin_host_fstab(target)
        return {'root': str(target), 'reused': True}
    if os.path.lexists(ALIAS):
        raise RuntimeError('Manager tools alias is not an original symlink')
    shared = False
    if active_root():
        verify_mount(SHARED)
        if not desired or runtime_matches(SHARED, desired):
            shared = True
    if not shared and desired is None:
        raise RuntimeError('Install a package containing data RAM tools before independent maintenance')
    if os.path.lexists(PRIVATE):
        raise RuntimeError('Incomplete RAM preparation exists; inspect it without replacing live tools')
    PRIVATE.mkdir(mode=0o700)
    mounted = []
    try:
        if shared:
            # systemd 255 cannot recursively remount a RootDirectory symlink
            # under ProtectSystem=strict. A real bind mount keeps one shared
            # backing tmpfs and gives both startup modes the same actual path.
            run('/bin/mount', '--bind', str(SHARED), str(PRIVATE))
            mounted.append(PRIVATE)
        else:
            run('/bin/mount', '-t', 'tmpfs', '-o', 'size=256M,noswap,nosuid,mode=0700',
                'ram-rescue-data-tools', str(PRIVATE))
            mounted.append(PRIVATE)
            unpack(payload, PRIVATE)
        # Only these four existing host trees are shared; recursive bind of
        # /run would include the new mount beneath itself.
        for name in ('dev', 'proc', 'sys', 'run'):
            (PRIVATE / name).mkdir(exist_ok=True)
            run('/bin/mount', '--bind', '/' + name, str(PRIVATE / name))
            mounted.append(PRIVATE / name)
        verify_mount(PRIVATE)
        if pin_host_fstab(PRIVATE):
            mounted.append(PRIVATE / 'etc/rescue/host-fstab')
    except BaseException:
        # Do not recursively remove a mount if cleanup cannot finish.
        for mount in reversed(mounted):
            run('/bin/umount', str(mount))
        PRIVATE.rmdir()
        raise
    ALIAS.symlink_to(PRIVATE)
    result = {'schema': 1, 'root': str(PRIVATE), 'reused': shared,
              'binary_sha256': sha256(verify_runtime(PRIVATE))}
    atomic(RECEIPT, (json.dumps(result, indent=2) + '\n').encode())
    return result
