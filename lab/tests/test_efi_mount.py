"""EFI integration safety boundaries, with all devices and host paths isolated."""
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'guard'))
spec = importlib.util.spec_from_file_location('efi_mount_under_test', BASE.parent / 'guard/efi_mount.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class EFIMountTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.rule = self.root / 'etc/udev/rules.d/90-ram-rescue-efi.rules'
        self.rule.parent.mkdir(parents=True)
        self.path_unit = self.root / 'etc/systemd/system/ram-rescue-efi.path'
        self.path_unit.parent.mkdir(parents=True)
        self.enabled = self.path_unit.parent / 'local-fs.target.wants/ram-rescue-efi.path'
        self.state = self.root / 'var/lib/ram-rescue-efi'
        self.state.parent.mkdir(parents=True)
        self.fstab = self.root / 'etc/fstab'
        self.original = (b'# Preserve every existing mount and comment.\n'
                         b'/dev/mapper/root / ext4 defaults 0 1\n'
                         b'/dev/disk/by-uuid/1234-ABCD /boot/efi vfat defaults 0 1\n'
                         b'UUID=other /workspace ext4 defaults,nofail 0 2\n')
        self.fstab.write_bytes(self.original)
        self.identity = {'ID_FS_UUID': '1234-ABCD',
                         'ID_PART_ENTRY_UUID': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                         'ID_USB_SERIAL_SHORT': 'Serial-123_ABC'}
        self.fsck_unit = r'systemd-fsck@dev-disk-by\x2duuid-1234\x2dABCD.service'
        self.dropin = self.path_unit.parent / (self.fsck_unit + '.d') / '50-ram-rescue-efi.conf'
        self.props = {**self.identity, 'ID_FS_TYPE': 'vfat', 'ID_PART_ENTRY_NUMBER': '1'}
        self.dev = self.root / 'dev/sda1'
        self.dev.parent.mkdir()
        self.dev.touch()
        self.alias = self.root / 'dev/ram-rescue-efi'
        byuuid = self.root / 'dev/disk/by-uuid/1234-ABCD'
        byuuid.parent.mkdir(parents=True)
        byuuid.symlink_to(self.dev)
        self.sys = self.root / 'sys/class/block'
        self.sys.mkdir(parents=True)
        self.disk = self.root / 'sys/devices/usb/sda'
        (self.disk / 'sda1').mkdir(parents=True)
        (self.disk / 'sda3').mkdir()
        (self.disk / 'diskseq').write_text('123\n')
        (self.sys / 'sda1').symlink_to(self.disk / 'sda1')
        dm = self.sys / 'dm-0'
        (dm / 'dm').mkdir(parents=True)
        (dm / 'dm/name').write_text('ram-rescue-path\n')
        (dm / 'slaves').mkdir()
        self.slave = dm / 'slaves/sda3'
        self.slave.symlink_to(self.disk / 'sda3')
        self.backup = self.root / 'efi-partition.img.zst'
        self.backup.write_bytes(b'verified compressed snapshot fixture')
        self.preparation = self.root / 'preparation.json'
        self.prepared = {'clean': True, 'backup_verified': True,
                         'fsck_readonly': {'returncode': 0},
                         'mount_restored': {'returncode': 0},
                         'backup_compressed_sha256': installer.sha256(self.backup),
                         'identity': {'UUID': self.identity['ID_FS_UUID'],
                                      'PART_ENTRY_UUID': self.identity['ID_PART_ENTRY_UUID']},
                         'diskseq': '123'}
        self.preparation.write_text(json.dumps(self.prepared))
        self.vm_report = self.root / 'vm-report.json'
        self.vm = {'passed': True, 'scope': 'native EFI path-triggered mount',
                   'rule_renderer_sha256': installer.sha256(Path(installer.__file__)),
                   'source_sha256': installer.source_hashes()}
        self.vm_report.write_text(json.dumps(self.vm))
        self.failure = None
        self.mount_result = '1234-ABCD vfat rw,relatime\n'
        self.remain_after_exit = 'no'
        self.patch('RULE', self.rule)
        self.patch('PATH_UNIT', self.path_unit)
        self.patch('ENABLED', self.enabled)
        self.patch('STATE', self.state)
        self.patch('FSTAB', self.fstab)
        self.patch('Path', side_effect=self.mapped_path)
        self.patch('print', create=True)
        self.lock = self.patch('maintenance_lock', side_effect=nullcontext)
        self.euid = patch.object(installer.os, 'geteuid', return_value=0)
        self.addCleanup(self.euid.stop)
        self.euid.start()
        self.commands = self.patch('command', side_effect=self.command)

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(installer, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def mapped_path(self, value):
        path = Path(value)
        if path.is_relative_to('/dev') or path.is_relative_to('/sys'):
            return self.root / path.relative_to('/')
        return path

    def command(self, args):
        if self.failure:
            self.failure(args)
        if args[:3] == ['udevadm', 'info', '--query=property']:
            return ''.join(key + '=' + value + '\n' for key, value in self.props.items())
        if args == ['systemd-escape', '--path', '--template=systemd-fsck@.service',
                    '/dev/disk/by-uuid/1234-ABCD']:
            return self.fsck_unit + '\n'
        if args[:2] == ['udevadm', 'verify'] or args == ['udevadm', 'control', '--reload-rules']:
            return ''
        if args == ['systemd-analyze', 'verify', str(self.path_unit)]:
            return ''
        if args == ['systemctl', 'daemon-reload'] or args == ['systemctl', 'stop', self.path_unit.name]:
            return ''
        if args == ['systemctl', 'enable', '--now', self.path_unit.name]:
            self.enabled.parent.mkdir(exist_ok=True)
            self.enabled.symlink_to(self.path_unit)
            return ''
        if args == ['systemctl', 'is-active', self.path_unit.name]:
            return 'active\n'
        if args == ['udevadm', 'trigger', '--action=change', '--settle', str(self.sys / 'sda1')]:
            self.alias.unlink(missing_ok=True)
            if self.rule.exists():
                self.alias.symlink_to(self.dev)
            return ''
        if args == ['systemctl', 'start', 'boot-efi.mount']:
            return ''
        if args in (['systemctl', 'stop', 'boot-efi.mount'], ['systemctl', 'stop', self.fsck_unit]):
            return ''
        if args == ['systemctl', 'show', self.fsck_unit, '--value', '-p', 'RemainAfterExit']:
            return self.remain_after_exit + '\n'
        if args == ['findmnt', '-rn', '-M', '/boot/efi', '-o', 'UUID,FSTYPE,OPTIONS']:
            return self.mount_result
        self.fail('Unexpected external command: ' + repr(args))

    def install(self):
        installer.install(self.preparation, self.vm_report)

    def record(self):
        return json.loads((self.state / 'install.json').read_text())

    def assert_configuration_absent(self):
        for path in (self.rule, self.path_unit, self.enabled, self.dropin):
            with self.subTest(path=path.relative_to(self.root)):
                self.assertFalse(path.exists() or path.is_symlink())
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def assert_cleanup_complete(self, state='failed_removed'):
        self.assert_configuration_absent()
        self.assertEqual(self.record()['state'], state)

    def assert_no_mutation(self):
        self.assert_configuration_absent()
        self.assertFalse(self.state.exists())
        self.assert_no_mutating_commands()

    def assert_no_mutating_commands(self):
        self.assertTrue(all(call.args[0][:3] == ['udevadm', 'info', '--query=property'] or
                            call.args[0][0] == 'systemd-escape'
                            for call in self.commands.call_args_list))

    def test_rule_matches_three_exact_identities_without_helpers(self):
        rule = installer.render_rule(self.identity)
        for key, value in self.identity.items():
            self.assertIn('ENV{' + key + '}=="' + value + '"', rule)
        self.assertIn('SYMLINK+="ram-rescue-efi"', rule)
        self.assertNotIn('SYSTEMD_WANTS', rule)
        self.assertNotIn('RUN+=', rule)
        self.assertNotIn('PROGRAM=', rule)

    def test_path_monitor_rechecks_presence_and_bounds_repeated_triggers(self):
        unit = installer.render_path()
        for setting in ('DefaultDependencies=no', 'After=local-fs-pre.target',
                        'Before=umount.target', 'Conflicts=umount.target',
                        'PathExists=/dev/ram-rescue-efi', 'Unit=boot-efi.mount',
                        'TriggerLimitIntervalSec=30s', 'TriggerLimitBurst=5',
                        'WantedBy=local-fs.target'):
            self.assertIn(setting + '\n', unit)
        self.assertNotIn('ExecStart=', unit)
        self.assertNotIn('OnCalendar=', unit)

    def test_rule_rejects_udev_globs_quotes_and_substitutions(self):
        for key in installer.KEYS:
            for value in ('', '*', '?', '[ab]', 'a|b', 'a"', 'a\\b', 'a\nb',
                          '$env{KEY}', '%k', 'a b', 'a;b', None, 123):
                with self.subTest(key=key, value=value):
                    identity = {**self.identity, key: value}
                    with self.assertRaises((ValueError, TypeError)):
                        installer.render_rule(identity)

    def test_fstab_rejects_nonordinary_or_duplicate_mounts(self):
        ordinary = '/dev/disk/by-uuid/1234-ABCD /boot/efi vfat defaults 0 1\n'
        for text in ('', ordinary + ordinary, ordinary.replace('vfat', 'ext4'),
                     ordinary.replace('defaults', 'defaults,noauto'),
                     ordinary.replace('defaults', 'defaults,x-systemd.automount'),
                     ordinary.replace('0 1', '0 0'), ordinary.replace('0 1', '0 00'),
                     ordinary.replace('0 1', '0 garbage')):
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                installer.fstab_entry(text)
        self.assertEqual(installer.fstab_entry(self.original.decode())[1], '/boot/efi')

    def test_backup_changed_since_readonly_check_is_rejected_before_any_command(self):
        self.backup.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            self.install()
        self.commands.assert_not_called()
        self.assert_no_mutation()

    def test_preparation_requires_clean_fsck_and_restored_mount(self):
        cases = ({'clean': False}, {'backup_verified': False},
                 {'fsck_readonly': {'returncode': 1}}, {'mount_restored': {'returncode': 1}})
        for change in cases:
            with self.subTest(change=change):
                self.preparation.write_text(json.dumps({**self.prepared, **change}))
                with self.assertRaises(RuntimeError):
                    self.install()
                self.assert_no_mutation()

    def test_changed_partition_or_filesystem_is_rejected(self):
        original = self.props.copy()
        for key, value in (('ID_FS_UUID', 'ABCD-1234'), ('ID_PART_ENTRY_UUID', 'other'),
                           ('ID_FS_TYPE', 'ext4'), ('ID_PART_ENTRY_NUMBER', '2')):
            with self.subTest(key=key):
                self.props = {**original, key: value}
                with self.assertRaisesRegex(RuntimeError, 'identity differs'):
                    self.install()
                self.assert_no_mutation()

    def test_same_uuid_on_another_disk_cannot_replace_the_checked_efi(self):
        (self.disk / 'diskseq').write_text('124\n')
        with self.assertRaisesRegex(RuntimeError, 'disk instance changed'):
            self.install()
        self.assert_no_mutation()

    def test_efi_must_share_the_protected_root_disk(self):
        other = self.root / 'sys/devices/usb/sdb/sdb3'
        other.mkdir(parents=True)
        self.slave.unlink()
        self.slave.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'not on the current protected root disk'):
            self.install()
        self.assert_no_mutation()

    def test_failed_wrong_scope_or_stale_vm_report_precedes_installation(self):
        for change in ({'passed': False}, {'passed': 1}, {'scope': 'other experiment'},
                       {'rule_renderer_sha256': 'old renderer hash'},
                       {'source_sha256': {'guard/efi_mount.py': self.vm['rule_renderer_sha256']}}):
            with self.subTest(change=change):
                self.vm_report.write_text(json.dumps({**self.vm, **change}))
                with self.assertRaises(RuntimeError):
                    self.install()
                self.assert_no_mutation()

    def test_installation_preserves_fstab_and_existing_native_mount(self):
        self.install()
        record = self.record()
        self.assertEqual(record['state'], 'installed')
        self.assertEqual(self.rule.read_text(), installer.render_rule(self.identity))
        self.assertEqual(self.path_unit.read_text(), installer.render_path())
        self.assertEqual(self.dropin.read_text(), '[Service]\nRemainAfterExit=no\n')
        self.assertEqual(self.enabled.readlink(), self.path_unit)
        self.assertEqual(self.alias.resolve(strict=True), self.dev)
        self.assertEqual(self.fstab.read_bytes(), self.original)
        self.assertEqual((self.state / 'fstab.before').read_bytes(), self.original)
        self.commands.assert_any_call(['udevadm', 'verify', str(self.rule)])
        self.commands.assert_any_call(['systemd-analyze', 'verify', str(self.path_unit)])
        self.commands.assert_any_call(['systemctl', 'start', 'boot-efi.mount'])
        self.commands.assert_any_call(['systemctl', 'enable', '--now', self.path_unit.name])
        self.commands.assert_any_call(['udevadm', 'trigger', '--action=change', '--settle',
                                      str(self.sys / 'sda1')])
        commands = [call.args[0] for call in self.commands.call_args_list]
        stop_mount = commands.index(['systemctl', 'stop', 'boot-efi.mount'])
        stop_fsck = commands.index(['systemctl', 'stop', self.fsck_unit])
        start_mount = commands.index(['systemctl', 'start', 'boot-efi.mount'])
        enable_path = commands.index(['systemctl', 'enable', '--now', self.path_unit.name])
        self.assertLess(stop_mount, stop_fsck)
        self.assertLess(stop_fsck, start_mount)
        self.assertLess(start_mount, enable_path)

    def test_existing_rule_is_not_overwritten(self):
        self.rule.write_text('# existing independent rule\n')
        with self.assertRaisesRegex(RuntimeError, 'refusing to overwrite'):
            self.install()
        self.assertEqual(self.rule.read_text(), '# existing independent rule\n')
        self.assertFalse(self.state.exists())
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def test_existing_dangling_path_unit_is_not_overwritten(self):
        self.path_unit.symlink_to(self.root / 'missing-independent-unit')
        with self.assertRaisesRegex(RuntimeError, 'refusing to overwrite'):
            self.install()
        self.assertTrue(self.path_unit.is_symlink())
        self.assertFalse(self.rule.exists())
        self.assertFalse(self.state.exists())
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def test_ordinary_user_cannot_reach_live_checks_or_installation(self):
        with patch.object(installer.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'administrator'):
                self.install()
        self.commands.assert_not_called()
        self.assert_no_mutation()

    def test_busy_package_maintenance_lock_precedes_all_live_checks_and_mutations(self):
        self.lock.side_effect = BlockingIOError('another package operation owns the lock')
        with self.assertRaises(BlockingIOError):
            self.install()
        self.commands.assert_not_called()
        self.assert_no_mutation()

    def test_failed_mount_stop_restores_mount_without_stopping_fsck(self):
        def fail_mount_stop(args):
            if args == ['systemctl', 'stop', 'boot-efi.mount']:
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_mount_stop
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertNotIn(['systemctl', 'stop', self.fsck_unit], commands)
        self.assertLess(commands.index(['systemctl', 'stop', 'boot-efi.mount']),
                        commands.index(['systemctl', 'start', 'boot-efi.mount']))
        self.assert_cleanup_complete()

    def test_failed_fsck_cache_reset_restores_mount_before_withdrawing_configuration(self):
        def fail_fsck_stop(args):
            if args == ['systemctl', 'stop', self.fsck_unit]:
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_fsck_stop
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertLess(commands.index(['systemctl', 'stop', self.fsck_unit]),
                        commands.index(['systemctl', 'start', 'boot-efi.mount']))
        self.assertLess(commands.index(['systemctl', 'start', 'boot-efi.mount']),
                        commands.index(['systemctl', 'stop', self.path_unit.name]))
        self.assert_cleanup_complete()

    def test_remaining_fsck_cache_is_rejected_before_path_monitor_enable(self):
        self.remain_after_exit = 'yes'
        with self.assertRaisesRegex(RuntimeError, 'still caches'):
            self.install()
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertNotIn(['systemctl', 'enable', '--now', self.path_unit.name], commands)
        self.assert_cleanup_complete()

    def test_existing_fsck_dropin_is_not_overwritten(self):
        self.dropin.parent.mkdir()
        self.dropin.write_text('# independent local fsck setting\n')
        with self.assertRaisesRegex(RuntimeError, 'refusing to overwrite'):
            self.install()
        self.assertEqual(self.dropin.read_text(), '# independent local fsck setting\n')
        self.assertFalse(self.rule.exists())
        self.assertFalse(self.path_unit.exists())
        self.assertFalse(self.state.exists())
        self.assert_no_mutating_commands()

    def test_rule_verification_failure_removes_only_the_new_rule(self):
        def fail_verify(args):
            if args[:2] == ['udevadm', 'verify']:
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_verify
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assert_cleanup_complete()
        self.commands.assert_any_call(['udevadm', 'control', '--reload-rules'])

    def test_wrong_mount_after_start_removes_rule_without_editing_fstab(self):
        self.mount_result = '1234-ABCD vfat ro,relatime\n'
        with self.assertRaisesRegex(RuntimeError, 'not mounted read-write'):
            self.install()
        self.assert_cleanup_complete()

    def test_failure_after_enable_removes_unit_enable_link_and_rule(self):
        failed = False
        def fail_first_trigger(args):
            nonlocal failed
            if args[:2] == ['udevadm', 'trigger'] and not failed:
                failed = True
                self.assertTrue(self.enabled.is_symlink())
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_first_trigger
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertTrue(failed)
        self.assert_cleanup_complete()

    def test_remove_preserves_the_native_mount_and_fstab(self):
        self.install()
        self.commands.reset_mock()
        installer.remove()
        self.assert_cleanup_complete('removed')
        self.assertFalse(self.alias.exists())
        self.commands.assert_any_call(['udevadm', 'control', '--reload-rules'])
        self.commands.assert_any_call(['systemctl', 'stop', self.path_unit.name])
        self.assertFalse(any(call.args[0] == ['systemctl', 'stop', 'boot-efi.mount']
                             for call in self.commands.call_args_list))

    def test_remove_refuses_to_delete_an_edited_rule(self):
        self.install()
        self.rule.write_text('# independently edited\n')
        original_record = (self.state / 'install.json').read_bytes()
        self.commands.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            installer.remove()
        self.assertEqual(self.rule.read_text(), '# independently edited\n')
        self.assertEqual((self.state / 'install.json').read_bytes(), original_record)
        self.assert_no_mutating_commands()

    def test_remove_checks_edited_path_unit_before_stopping_or_removing_anything(self):
        self.install()
        self.path_unit.write_text('# independent unit change\n')
        self.commands.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            installer.remove()
        self.assertEqual(self.path_unit.read_text(), '# independent unit change\n')
        self.assertTrue(self.rule.exists())
        self.assertTrue(self.enabled.is_symlink())
        self.assert_no_mutating_commands()

    def test_remove_checks_edited_fsck_dropin_before_stopping_or_removing_anything(self):
        self.install()
        self.dropin.write_text('# independently changed fsck setting\n')
        self.commands.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            installer.remove()
        self.assertEqual(self.dropin.read_text(), '# independently changed fsck setting\n')
        self.assertTrue(self.rule.exists())
        self.assertTrue(self.path_unit.exists())
        self.assertTrue(self.enabled.is_symlink())
        self.assert_no_mutating_commands()

    def test_remove_preserves_other_fsck_dropins(self):
        self.dropin.parent.mkdir()
        other = self.dropin.parent / '10-independent.conf'
        other.write_text('[Unit]\nDescription=Independent administrator setting\n')
        self.install()
        installer.remove()
        self.assertFalse(self.dropin.exists())
        self.assertEqual(other.read_text(), '[Unit]\nDescription=Independent administrator setting\n')
        self.assertFalse(self.path_unit.exists())
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def test_remove_checks_changed_enable_link_before_stopping_or_removing_anything(self):
        self.install()
        self.enabled.unlink()
        self.enabled.symlink_to(self.root / 'independent.path')
        self.commands.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            installer.remove()
        self.assertEqual(self.enabled.readlink(), self.root / 'independent.path')
        self.assertTrue(self.rule.exists())
        self.assertTrue(self.path_unit.exists())
        self.assert_no_mutating_commands()

    def test_remove_recovers_an_interrupted_partial_installation(self):
        self.install()
        record = self.record()
        record['state'] = 'preparing'
        (self.state / 'install.json').write_text(json.dumps(record))
        # Model process death after writing the first managed file. The record
        # identifies its ownership even though installation never completed.
        self.enabled.unlink()
        self.path_unit.unlink()
        self.dropin.unlink()
        self.commands.reset_mock()

        installer.remove()

        self.assert_cleanup_complete('removed')
        self.assertFalse(self.alias.exists())
        self.commands.assert_any_call(['udevadm', 'control', '--reload-rules'])

    def test_remove_stop_failure_leaves_files_available_for_retry(self):
        self.install()
        def fail_stop(args):
            if args == ['systemctl', 'stop', self.path_unit.name]:
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_stop
        with self.assertRaises(subprocess.CalledProcessError):
            installer.remove()
        self.assertTrue(self.rule.exists())
        self.assertTrue(self.path_unit.exists())
        self.assertTrue(self.enabled.is_symlink())
        self.failure = None
        installer.remove()
        self.assertFalse(self.rule.exists())
        self.assertFalse(self.path_unit.exists())
        self.assertFalse(self.enabled.is_symlink())
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def test_remove_can_retry_after_reload_failure(self):
        self.install()
        def fail_reload(args):
            if args == ['udevadm', 'control', '--reload-rules']:
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_reload
        with self.assertRaises(subprocess.CalledProcessError):
            installer.remove()
        self.failure = None
        installer.remove()
        self.assertFalse(self.rule.exists())
        self.assertEqual(self.record()['state'], 'removed')
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def test_failed_install_cleanup_can_retry_when_reload_is_unavailable(self):
        def fail_reload(args):
            if args == ['udevadm', 'control', '--reload-rules']:
                raise subprocess.CalledProcessError(1, args)
        self.failure = fail_reload
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertFalse(self.rule.exists())
        self.assertEqual(self.record()['state'],
                         'failed_pending_reload')
        self.failure = None
        installer.remove()
        self.assertFalse(self.rule.exists())
        self.assertEqual(self.record()['state'], 'removed')
        self.assertEqual(self.fstab.read_bytes(), self.original)

    def test_cleanup_failure_preserves_the_install_error_and_allows_retry(self):
        install_error = subprocess.CalledProcessError(1, ['udevadm', 'verify', str(self.rule)])
        cleanup_error = subprocess.CalledProcessError(2, ['systemctl', 'stop', self.path_unit.name])

        def fail_install_and_cleanup(args):
            if args == install_error.cmd:
                raise install_error
            if args == cleanup_error.cmd:
                raise cleanup_error

        self.failure = fail_install_and_cleanup
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.install()

        self.assertIs(caught.exception, install_error)
        self.assertIn(repr(cleanup_error), '\n'.join(caught.exception.__notes__))
        self.assertEqual(self.record()['state'], 'failed_pending_reload')
        self.assertTrue(self.path_unit.exists())

        self.failure = None
        installer.remove()
        self.assert_cleanup_complete('removed')


if __name__ == '__main__':
    unittest.main()
