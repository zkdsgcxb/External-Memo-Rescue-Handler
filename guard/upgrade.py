#!/usr/bin/python3
"""Upgrade one already installed protection initrd without changing live owners.

The default is a read-only preflight. --install replaces the same boot image
and its receipt, keeping private rollback evidence. GRUB, hooks, ordinary
initrds and running services are never written by this command.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import tempfile
import time
import uuid

import install as installer
from install import deployment_lock
from enroll import collect
from host_files import atomic, sha256

PROJECT = Path(__file__).resolve().parent.parent
STATE = installer.STATE
HOOK = installer.HOOK
GRUB = installer.GRUB
BOOT = Path('/boot')
LVM_CONFIG = Path('/etc/lvm/lvmlocal.conf')


def encoded(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def regular(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise RuntimeError('Expected an ordinary file: ' + str(path))
    if os.geteuid() == 0:
        from trusted_paths import open_trusted
        with open_trusted(path):
            pass
    return path


def incomplete_upgrade():
    directory = STATE / 'upgrades'
    if directory.is_symlink():
        raise RuntimeError('Upgrade evidence directory must not be a symlink')
    if directory.exists():
        for item in directory.iterdir():
            record = item / 'upgrade.json'
            if item.is_symlink() or not item.is_dir() or not record.is_file():
                raise RuntimeError('Incomplete upgrade evidence requires review: ' + str(item))
            state = json.loads(regular(record).read_text()).get('state')
            if state not in ('installed', 'failed_rolled_back', 'failed_no_changes'):
                raise RuntimeError('An earlier protection upgrade is incomplete: ' + str(item))


def check_menu(profile, image_name):
    text = regular(GRUB).read_text()
    expected = installer.menu(profile, image_name)
    entry_count = text.count('\n' + expected) + int(text.startswith(expected))
    if text.count('--id ram-rescue-guard') != 1 or entry_count != 1:
        raise RuntimeError('Current GRUB must contain exactly the expected protected entry')
    entry = installer.first_entry(text)
    if any('ram_rescue_guard=1' in line or '--id ram-rescue-guard' in line for line in entry):
        raise RuntimeError('The first GRUB entry must remain the ordinary Ubuntu entry')
    for line in entry:
        words = shlex.split(line)
        if words[0] not in ('linux', 'initrd'):
            continue
        paths = words[1:2] if words[0] == 'linux' else words[1:]
        for value in paths:
            path = Path(value)
            if not path.is_relative_to('/boot') or '..' in path.parts or path.name == image_name:
                raise RuntimeError('Normal GRUB entry has an unexpected kernel/initrd path')
            regular(BOOT / path.relative_to('/boot'))
    return entry


def preflight(build_dir, enrollment, vm_report):
    """Read every prerequisite; callers hold the directory deployment lock."""
    inputs = (enrollment, vm_report, build_dir / 'build.json')
    watched = {path: sha256(regular(path)) for path in inputs}
    profile, build, image_name = installer.validate(build_dir, enrollment, vm_report, trusted=os.geteuid() == 0)
    if build.get('runtime') != 'cpp':
        raise RuntimeError('Only the current C++ protection image may be upgraded')
    incomplete_upgrade()
    receipt_path = regular(STATE / 'install.json')
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    release = profile['guard']['kernel_release']
    image = BOOT / image_name
    kernel = BOOT / ('vmlinuz-' + release)
    normal = BOOT / ('initrd.img-' + release)
    watched.update({path: sha256(regular(path)) for path in
                    (image, HOOK, GRUB, kernel, normal, STATE / 'grub.cfg.before')})
    watched[LVM_CONFIG] = installer.optional_hash(LVM_CONFIG)
    watched[receipt_path] = hashlib.sha256(receipt_bytes).hexdigest()
    if (receipt.get('state') != 'installed' or receipt.get('kernel_release') != release
            or receipt.get('image') != str(image)
            or receipt.get('build', {}).get('initramfs_sha256') != receipt.get('image_sha256')):
        raise RuntimeError('Installed protection receipt is incomplete or inconsistent')
    if sha256(regular(image)) != receipt['image_sha256']:
        raise RuntimeError('Installed protection image changed since its receipt')
    expected_hook = ("#!/bin/sh\ncat <<'RAM_RESCUE_MENU'\n" +
                     installer.menu(profile, image_name) + 'RAM_RESCUE_MENU\n').encode()
    if (regular(HOOK).read_bytes() != expected_hook or
            sha256(HOOK) != receipt['hook_sha256']):
        raise RuntimeError('Installed protection hook changed or belongs to another profile')
    if sha256(regular(STATE / 'grub.cfg.before')) != receipt['normal_grub_sha256']:
        raise RuntimeError('Original uninstall menu backup changed')
    normal_entry = check_menu(profile, image_name)
    baseline = profile['baseline']
    if sha256(kernel) != build['kernel_sha256']:
        raise RuntimeError('Current installed kernel differs from the candidate')
    if sha256(normal) != baseline['initrd_sha256']:
        raise RuntimeError('Normal initrd changed since enrollment')
    if installer.optional_hash(LVM_CONFIG) != baseline['lvmlocal_sha256']:
        raise RuntimeError('Host LVM configuration changed since enrollment')
    if not build['source_sha256']:
        raise RuntimeError('Candidate build source manifest must not be empty')
    for relative, digest in build['source_sha256'].items():
        path = PROJECT / relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts
                or not path.resolve().is_relative_to(PROJECT.resolve())
                or sha256(regular(path)) != digest):
            raise RuntimeError('Current build source differs: ' + relative)
        watched[path] = digest
    current = collect(profile['identity'])
    if current != {key: profile[key] for key in ('schema', 'identity', 'guard')}:
        raise RuntimeError('Live root layout/configuration no longer matches enrollment')
    candidate = regular(build_dir / 'initrd.img')
    required = candidate.stat().st_size + 512 * 1024**2
    if shutil.disk_usage(BOOT).free < required:
        raise RuntimeError('Insufficient boot space to stage the replacement safely')
    if shutil.disk_usage(STATE).free < image.stat().st_size + required:
        raise RuntimeError('Insufficient state space for the private image backup')
    context = {'profile': profile, 'build': build, 'image': image, 'candidate': candidate,
               'receipt': receipt, 'receipt_bytes': receipt_bytes,
               'normal_first_entry': normal_entry, 'watched_sha256': watched}
    unchanged(context)
    return context


def unchanged(context, *, written=()):
    for path, digest in context['watched_sha256'].items():
        if path in written:
            continue
        if digest is None:
            if os.path.lexists(path):
                raise RuntimeError('Previously absent upgrade input appeared: ' + str(path))
            continue
        if sha256(regular(path)) != digest:
            raise RuntimeError('Upgrade prerequisite changed during preparation: ' + str(path))


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_copy(source, destination, expected):
    """Stream an image into a same-directory temporary file, verify, then rename."""
    descriptor, temporary = tempfile.mkstemp(prefix='.' + destination.name + '-',
                                             dir=destination.parent)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, 'wb') as output, regular(source).open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                output.write(block)
                digest.update(block)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise RuntimeError('Image changed while staging: ' + str(source))
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_upgrade(context, vm_report):
    image, old = context['image'], context['receipt']
    build = context['build']
    evidence_root = STATE / 'upgrades'
    evidence_root.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(evidence_root.stat().st_mode) & 0o077:
        raise RuntimeError('Upgrade evidence directory must be private')
    sync_directory(STATE)
    identifier = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:12]
    evidence = evidence_root / identifier
    evidence.mkdir(mode=0o700)
    sync_directory(evidence_root)
    record_path = evidence / 'upgrade.json'
    record = {'schema': 1, 'state': 'preparing', 'upgrade_id': identifier,
              'image': str(image), 'old_image_sha256': old['image_sha256'],
              'new_image_sha256': build['initramfs_sha256'],
              'old_receipt_sha256': hashlib.sha256(context['receipt_bytes']).hexdigest(),
              'observed_grub_sha256': context['watched_sha256'][GRUB],
              'normal_first_entry': context['normal_first_entry'],
              'vm_report_sha256': sha256(vm_report),
              'backup_image': 'initrd.img.before', 'backup_receipt': 'install.json.before',
              'grub_written': False, 'services_restarted': False}
    receipt_path = STATE / 'install.json'
    preparing = {**old, 'state': 'upgrading', 'upgrade_id': identifier}
    updated = {**old, 'state': 'installed', 'build': build,
               'image_sha256': build['initramfs_sha256'],
               'vm_report_sha256': record['vm_report_sha256'],
               'upgrade_id': identifier, 'upgrade_evidence': str(evidence)}
    mutated = False
    try:
        atomic(record_path, encoded(record))
        atomic_copy(image, evidence / record['backup_image'], old['image_sha256'])
        atomic(evidence / record['backup_receipt'], context['receipt_bytes'])
        unchanged(context)
        mutated = True  # atomic() can replace the receipt before raising on fsync.
        atomic(receipt_path, encoded(preparing))
        atomic_copy(context['candidate'], image, build['initramfs_sha256'])
        if sha256(image) != build['initramfs_sha256']:
            raise RuntimeError('Installed replacement image checksum differs')
        unchanged(context, written=(image, receipt_path))
        atomic(receipt_path, encoded(updated))
        record['state'] = 'installed'
        atomic(record_path, encoded(record))
    except BaseException as error:
        record['error'] = str(error)
        record['state'] = 'failed_no_changes'
        if mutated:
            try:
                if sha256(regular(image)) not in (old['image_sha256'], build['initramfs_sha256']):
                    raise RuntimeError('Image was independently changed; refusing to overwrite it')
                expected_receipts = (context['receipt_bytes'], encoded(preparing), encoded(updated))
                if regular(receipt_path).read_bytes() not in expected_receipts:
                    raise RuntimeError('Receipt was independently changed; refusing to overwrite it')
                atomic_copy(evidence / record['backup_image'], image, old['image_sha256'])
                atomic(receipt_path, context['receipt_bytes'])
                record['state'] = 'failed_rolled_back'
            except BaseException as rollback_error:
                record['state'] = 'rollback_failed'
                record['rollback_error'] = str(rollback_error)
                atomic(record_path, encoded(record))
                raise RuntimeError('Upgrade rollback failed; inspect ' + str(evidence)) from error
        atomic(record_path, encoded(record))
        raise
    return {'upgraded': True, 'image': str(image), 'image_sha256': updated['image_sha256'],
            'evidence': str(evidence), 'reboot_required': True,
            'running_owner_changed': False, 'grub_changed': False,
            'uninstall_metadata_preserved': True}


def upgrade(build_dir, enrollment, vm_report, *, install=False):
    if install and os.geteuid() != 0:
        raise RuntimeError('Upgrade requires local administrator authentication')
    with deployment_lock(STATE):
        context = preflight(build_dir, enrollment, vm_report)
        if not install:
            return {'validated': True, 'installed': False, 'image': str(context['image']),
                    'candidate_sha256': context['build']['initramfs_sha256'],
                    'normal_first_entry': context['normal_first_entry'],
                    'running_owner_changed': False, 'grub_changed': False}
        return apply_upgrade(context, vm_report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', required=True, type=Path)
    parser.add_argument('--enrollment', required=True, type=Path)
    parser.add_argument('--vm-report', required=True, type=Path)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args()
    print(json.dumps(upgrade(args.build_dir.resolve(), args.enrollment.resolve(),
                             args.vm_report.resolve(), install=args.install),
                     indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
