"""Cold fstab pinning with real trusted input reads and simulated mount syscalls."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'guard'))
import ram_environment as environment
from trusted_paths import open_trusted


class HostFstabPinTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REPO / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fstab = self.directory / 'fstab'
        self.fstab.write_text('# host mount policy\n')
        self.fstab.chmod(0o600)
        self.root = self.directory / 'ram'
        self.target = self.root / 'etc/rescue/host-fstab'
        self.target.parent.mkdir(parents=True)
        self.real_stat = Path.stat
        self.mounted = None
        self.options = None
        self.failure_stage = None
        self.replace_on_bind = False
        self.enterContext(patch.object(environment, 'FSTAB', self.fstab))
        self.enterContext(patch.object(environment, 'open_trusted', side_effect=lambda path:
            open_trusted(path, uid=os.getuid(), anchor=self.directory)))
        self.query = self.enterContext(patch.object(environment, 'mount_record', side_effect=self.mount_record))
        self.commands = self.enterContext(patch.object(environment, 'run', side_effect=self.mount_command))
        original = self.real_stat
        def mounted_stat(path, *args, **kwargs):
            if path == self.target and self.mounted is not None:
                return self.mounted
            return original(path, *args, **kwargs)
        self.enterContext(patch.object(Path, 'stat', mounted_stat))

    def mount_record(self, target):
        self.assertEqual(target, self.target)
        return {'options': self.options} if self.mounted is not None else None

    def replace_source(self):
        replacement = self.directory / 'new-fstab'
        replacement.write_text('# atomically replaced host policy\n')
        replacement.chmod(0o600)
        replacement.replace(self.fstab)

    def mount_command(self, *args):
        if self.failure_stage == args[1]:
            raise RuntimeError('simulated ' + args[1] + ' failure')
        if args[:2] == ('/bin/mount', '--bind'):
            if self.replace_on_bind:
                self.replace_source()
            self.mounted = self.real_stat(self.fstab)
            self.options = 'rw,relatime'
        elif args[:2] == ('/bin/mount', '-o'):
            self.options = 'ro,relatime'
        elif args[0] == '/bin/umount':
            self.mounted = None
            self.options = None
        else:
            self.fail('Unexpected mount syscall: ' + repr(args))
        return ''

    def existing_pin(self, *, options='ro,relatime'):
        self.target.write_text('mount point placeholder')
        self.mounted = self.real_stat(self.fstab)
        self.options = options

    def test_new_pin_is_readonly_and_reused_without_another_mount(self):
        self.assertTrue(environment.pin_host_fstab(self.root))
        self.assertEqual(self.options, 'ro,relatime')
        self.assertEqual(self.commands.call_args_list[0].args,
            ('/bin/mount', '--bind', str(self.fstab), str(self.target)))
        self.assertEqual(self.commands.call_args_list[1].args,
            ('/bin/mount', '-o', 'remount,bind,ro', str(self.target)))
        self.commands.reset_mock()
        self.assertFalse(environment.pin_host_fstab(self.root))
        self.commands.assert_not_called()

    def test_existing_old_inode_after_atomic_replace_requires_new_boot(self):
        self.existing_pin()
        old_inode = self.mounted.st_ino
        self.replace_source()
        with self.assertRaisesRegex(RuntimeError, 'reboot'):
            environment.pin_host_fstab(self.root)
        self.assertEqual(self.mounted.st_ino, old_inode)
        self.commands.assert_not_called()

    def test_inplace_edit_retains_same_inode_without_rebinding(self):
        self.existing_pin()
        inode = self.mounted.st_ino
        self.fstab.write_text('# edited in place\n')
        self.assertFalse(environment.pin_host_fstab(self.root))
        self.assertEqual(self.real_stat(self.fstab).st_ino, inode)
        self.commands.assert_not_called()

    def test_existing_rw_pin_is_rejected_without_changing_live_mount(self):
        self.existing_pin(options='rw,relatime')
        with self.assertRaisesRegex(RuntimeError, 'reboot'):
            environment.pin_host_fstab(self.root)
        self.assertEqual(self.options, 'rw,relatime')
        self.commands.assert_not_called()

    def test_failed_bind_removes_only_unmounted_placeholder(self):
        self.failure_stage = '--bind'
        with self.assertRaisesRegex(RuntimeError, 'simulated --bind'):
            environment.pin_host_fstab(self.root)
        self.assertIsNone(self.mounted)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.commands.call_count, 1)
        self.assertEqual(self.fstab.read_text(), '# host mount policy\n')

    def test_failed_readonly_remount_unmounts_before_removing_placeholder(self):
        self.failure_stage = '-o'
        with self.assertRaisesRegex(RuntimeError, 'simulated -o'):
            environment.pin_host_fstab(self.root)
        self.assertEqual(self.commands.call_args.args, ('/bin/umount', str(self.target)))
        self.assertIsNone(self.mounted)
        self.assertFalse(self.target.exists())

    def test_source_replacement_during_bind_is_detected_and_unmounted(self):
        self.replace_on_bind = True
        with self.assertRaisesRegex(RuntimeError, 'changed while preparing'):
            environment.pin_host_fstab(self.root)
        self.assertIsNone(self.mounted)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.commands.call_args.args, ('/bin/umount', str(self.target)))

    def test_untrusted_source_rejected_before_mount_query(self):
        self.fstab.chmod(0o666)
        with self.assertRaisesRegex(RuntimeError, 'Untrusted'):
            environment.pin_host_fstab(self.root)
        self.query.assert_not_called()
        self.commands.assert_not_called()

    def test_oversized_source_rejected_before_mount_query(self):
        with self.fstab.open('wb') as stream:
            stream.truncate(4 * 1024**2 + 1)
        with self.assertRaisesRegex(RuntimeError, 'Oversized'):
            environment.pin_host_fstab(self.root)
        self.query.assert_not_called()
        self.commands.assert_not_called()


if __name__ == '__main__':
    unittest.main()
