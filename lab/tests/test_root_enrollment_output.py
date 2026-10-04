"""Privileged enrollment writes only to a verified private store, never a workspace."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'guard'))
import enroll
import trusted_paths


class RootEnrollmentOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REPO / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = self.root / 'var/lib/ram-rescue-enrollments'
        self.store.parent.mkdir(mode=0o700, parents=True)
        (self.root / 'var').chmod(0o700)
        self.boot = self.root / 'boot'
        self.boot.mkdir(mode=0o700)
        self.release = '7.0.0-fixture'
        for name in ('vmlinuz-', 'initrd.img-'):
            path = self.boot / (name + self.release)
            path.write_bytes(('fixture ' + name).encode())
            path.chmod(0o600)
        self.local = self.root / 'lvmlocal.conf'
        self.identity = {'lvs': {'ubuntu': {'dm_uuid': 'LVM-fixture'}}}
        self.profile = {'schema': 1, 'identity': self.identity,
                        'guard': {'kernel_release': self.release}}
        self.enterContext(patch.multiple(enroll, ENROLLMENTS=self.store, BOOT=self.boot, LVM_CONFIG=self.local))
        self.collect = self.enterContext(patch.object(enroll, 'collect', side_effect=lambda identity: deepcopy(self.profile)))
        # Exercise the same owner/mode/link checks within an ordinary user's
        # namespace; only the required UID and absolute trust anchor differ.
        for name in ('open_directory', 'open_trusted', 'read_trusted_json', 'trusted_sha256'):
            original = getattr(trusted_paths, name)
            self.enterContext(patch.object(enroll, name, side_effect=lambda path, _original=original, **kwargs:
                _original(path, uid=os.getuid(), anchor=self.root, **kwargs)))
        self.chown = self.enterContext(patch.object(enroll.os, 'chown', side_effect=AssertionError('Never chown enrollment to a user')))

    def test_success_copies_only_three_private_files_and_never_changes_owner(self):
        result = enroll.enroll('host-2026', self.identity)
        destination = self.store / 'host-2026'
        self.assertEqual(result['output'], str(destination))
        self.assertEqual(set(result['export_files']), {'enrollment.json', 'vmlinuz', 'original-initrd.img'})
        self.assertEqual({p.name for p in destination.iterdir()}, set(result['export_files']))
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
        for path in destination.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        profile = json.loads((destination / 'enrollment.json').read_text())
        self.assertEqual(profile['baseline']['kernel_sha256'], hashlib.sha256((destination / 'vmlinuz').read_bytes()).hexdigest())
        self.assertEqual(profile['baseline']['initrd_sha256'], hashlib.sha256((destination / 'original-initrd.img').read_bytes()).hexdigest())
        self.assertIsNone(profile['baseline']['lvmlocal_sha256'])
        self.chown.assert_not_called()

    def test_existing_record_and_dangling_symlink_are_refused_before_collect(self):
        self.store.mkdir(mode=0o700)
        (self.store / 'old').mkdir(mode=0o700)
        (self.store / 'link').symlink_to(self.root / 'absent')
        for name in ('old', 'link'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'already exists'):
                enroll.enroll(name, self.identity)
        self.collect.assert_not_called()

    def test_nonprivate_store_and_symlinked_store_are_refused_before_collect(self):
        self.store.mkdir(mode=0o750)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            enroll.enroll('new', self.identity)
        self.store.rmdir()
        private = self.root / 'elsewhere'
        private.mkdir(mode=0o700)
        self.store.symlink_to(private)
        with self.assertRaises(OSError):
            enroll.enroll('new', self.identity)
        self.assertEqual(list(private.iterdir()), [])
        self.collect.assert_not_called()

    def test_writable_parent_and_wrong_owner_are_rejected_before_store_creation(self):
        self.store.parent.chmod(0o770)
        with self.assertRaisesRegex(RuntimeError, 'Untrusted'):
            enroll.enroll('new', self.identity)
        self.assertFalse(self.store.exists())
        self.store.parent.chmod(0o700)
        with patch.object(enroll, 'open_directory', side_effect=lambda path:
                          trusted_paths.open_directory(path, uid=os.getuid() + 1, anchor=self.root)):
            with self.assertRaisesRegex(RuntimeError, 'Untrusted'):
                enroll.enroll('new', self.identity)
        self.assertFalse(self.store.exists())
        self.collect.assert_not_called()

    def test_names_cannot_select_absolute_nested_or_parent_directories(self):
        for name in ('../outside', '/tmp/output', '.', '..', 'nested/output', 'a' * 65, ''):
            with self.subTest(name=name), self.assertRaises(enroll.argparse.ArgumentTypeError):
                enroll.enroll(name, self.identity)
        self.assertFalse(self.store.exists())
        self.collect.assert_not_called()

    def test_kernel_symlink_and_changed_copy_refuse_complete_enrollment(self):
        kernel = self.boot / ('vmlinuz-' + self.release)
        original = self.boot / 'original'
        kernel.rename(original)
        kernel.symlink_to(original)
        with self.assertRaises(OSError):
            enroll.enroll('symlink', self.identity)
        self.assertFalse((self.store / 'symlink/enrollment.json').exists())
        kernel.unlink()
        original.rename(kernel)
        read = os.read
        changed = False
        def changed_read(descriptor, size):
            nonlocal changed
            data = read(descriptor, size)
            if data and not changed:
                changed = True
                with kernel.open('ab') as stream:
                    stream.write(b'changed')
            return data
        with patch.object(enroll.os, 'read', side_effect=changed_read):
            with self.assertRaisesRegex(RuntimeError, 'changed while copying'):
                enroll.enroll('changed', self.identity)
        self.assertFalse((self.store / 'changed/enrollment.json').exists())

    def test_optional_lvm_config_is_hashed_but_symlinks_are_rejected(self):
        self.local.write_bytes(b'trusted lvm config')
        self.local.chmod(0o600)
        enroll.enroll('with-lvm', self.identity)
        profile = json.loads((self.store / 'with-lvm/enrollment.json').read_text())
        self.assertEqual(profile['baseline']['lvmlocal_sha256'], hashlib.sha256(self.local.read_bytes()).hexdigest())
        self.local.unlink()
        self.local.symlink_to(self.root / 'missing-config')
        with self.assertRaises(OSError):
            enroll.enroll('unsafe-lvm', self.identity)
        self.assertFalse((self.store / 'unsafe-lvm/enrollment.json').exists())

    def test_cli_requires_explicit_identity_and_rejects_user_workspace_json(self):
        identity = self.root / 'identity.json'
        identity.write_text(json.dumps(self.identity))
        identity.chmod(0o666)
        with patch.object(enroll.os, 'geteuid', return_value=0), \
                patch.object(sys, 'argv', ['enroll.py', '--name', 'new', '--identity', str(identity)]):
            with self.assertRaisesRegex(RuntimeError, 'Untrusted'):
                enroll.main()
        self.collect.assert_not_called()
        self.assertFalse(self.store.exists())

    def test_cli_partition_pair_and_private_output_work_from_installed_location(self):
        with patch.object(enroll.os, 'geteuid', return_value=0), \
                patch.object(enroll, 'BASE', Path('/usr/lib/ram-rescue-handler/release/guard')), \
                patch.object(enroll, 'identify', return_value=self.identity) as identify, \
                patch.object(sys, 'argv', ['enroll.py', '--name', 'installed', '--partition', '/dev/mock1', '--usb-serial', 'KNOWN']), \
                patch('builtins.print'):
            enroll.main()
        identify.assert_called_once_with(Path('/dev/mock1'), 'KNOWN')
        self.assertTrue((self.store / 'installed/enrollment.json').is_file())


if __name__ == '__main__':
    unittest.main()
