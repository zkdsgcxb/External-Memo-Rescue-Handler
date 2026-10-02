"""Boot-image upgrades and crash-safe rollback, entirely in a temp directory."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'guard'))
import upgrade


class GuardUpgradeTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / 'boot'
        self.boot.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.hook = self.root / '42_ram_rescue_guard'
        self.grub = self.boot / 'grub.cfg'
        self.local = self.root / 'lvmlocal.conf'
        self.local.write_text('local {}\n')
        self.tools = self.root / 'rescue-root.tar.gz'
        self.tools.write_bytes(b'base rescue tools')
        self.project = self.root / 'source'
        source = self.project / 'guard/native/runtime/controller.cpp'
        source.parent.mkdir(parents=True)
        source.write_text('// tested source\n')
        self.release = '7.0.0-test'
        self.kernel = self.boot / ('vmlinuz-' + self.release)
        self.kernel.write_bytes(b'current kernel')
        self.normal = self.boot / ('initrd.img-' + self.release)
        self.normal.write_bytes(b'ordinary current initrd')
        self.image = self.boot / (self.normal.name + '-ram-rescue')
        self.image.write_bytes(b'previous protected image')
        self.profile = {
            'schema': 1, 'identity': {'vg_name': 'vg-usb'},
            'guard': {'kernel_release': self.release, 'root_lv': 'ubuntu-root',
                      'root_fs_uuid': '11111111-2222-3333-4444-555555555555'},
            'baseline': {'kernel_sha256': upgrade.sha256(self.kernel),
                         'initrd_sha256': upgrade.sha256(self.normal),
                         'lvmlocal_sha256': upgrade.sha256(self.local)},
        }
        ordinary = ("set default=\"0\"\nmenuentry 'Ubuntu' --id normal {\n"
                    f'    linux /boot/{self.kernel.name} root=/dev/mapper/vg--usb-ubuntu--root ro\n'
                    f'    initrd /boot/{self.normal.name}\n}}\n')
        menu = upgrade.installer.menu(self.profile, self.image.name)
        self.hook.write_text("#!/bin/sh\ncat <<'RAM_RESCUE_MENU'\n" + menu + 'RAM_RESCUE_MENU\n')
        self.hook.chmod(0o755)
        (self.state / 'grub.cfg.before').write_text(ordinary)
        original_protected = ordinary + menu
        # Kernel package updates may regenerate grub.cfg without touching the
        # protection entry. The old uninstall receipt must not be rewritten.
        self.grub.write_text(original_protected + '# regenerated after installation\n')
        self.receipt = {
            'state': 'installed', 'kernel_release': self.release, 'image': str(self.image),
            'image_sha256': upgrade.sha256(self.image), 'hook_sha256': upgrade.sha256(self.hook),
            'normal_grub_sha256': upgrade.sha256(self.state / 'grub.cfg.before'),
            'protected_grub_sha256': hashlib.sha256(original_protected.encode()).hexdigest(),
            'normal_initrd_sha256': 'historical-normal-initrd-hash',
            'vm_report_sha256': 'historical-vm-report-hash',
            'build': {'initramfs_sha256': upgrade.sha256(self.image)},
            'future_uninstall_metadata': {'preserve': True},
        }
        self.receipt_path = self.state / 'install.json'
        self.receipt_path.write_bytes(upgrade.encoded(self.receipt))
        self.original_receipt = self.receipt_path.read_bytes()
        self.enrollment = self.root / 'enrollment.json'
        self.enrollment.write_bytes(upgrade.encoded(self.profile))
        self.build_dir = self.root / 'build'
        self.build_dir.mkdir()
        self.candidate = self.build_dir / 'initrd.img'
        self.candidate.write_bytes(b'new complete C++ protected image')
        self.build = {
            'schema': 1, 'runtime': 'cpp', 'kernel_release': self.release,
            'source_sha256': {str(source.relative_to(self.project)): upgrade.sha256(source)},
            'kernel_sha256': upgrade.sha256(self.kernel),
            'base_rescue_payload_sha256': upgrade.sha256(self.tools),
            'enrollment_sha256': upgrade.sha256(self.enrollment),
            'initramfs_sha256': upgrade.sha256(self.candidate),
        }
        (self.build_dir / 'build.json').write_bytes(upgrade.encoded(self.build))
        self.vm = self.root / 'vm-report.json'
        self.vm.write_bytes(upgrade.encoded({'passed': True, 'build': self.build}))
        for key, value in {'STATE': self.state, 'HOOK': self.hook, 'GRUB': self.grub,
                           'BOOT': self.boot, 'LVM_CONFIG': self.local,
                           'BASE_TOOLS': self.tools, 'PROJECT': self.project}.items():
            self.patch(key, value)
        self.collect = self.patch('collect', return_value={
            key: self.profile[key] for key in ('schema', 'identity', 'guard')})
        self.patch('os.geteuid', return_value=0)
        self.patch('shutil.disk_usage', return_value=SimpleNamespace(free=2**40))
        self.original_image = self.image.read_bytes()
        self.untouched = {path: path.read_bytes() for path in
                          (self.grub, self.hook, self.normal, self.kernel, self.local,
                           self.state / 'grub.cfg.before')}

    def patch(self, name, *args, **kwargs):
        target = 'upgrade.' + name
        patcher = patch(target, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def perform(self, *, install=True):
        return upgrade.upgrade(self.build_dir, self.enrollment, self.vm, install=install)

    def assert_untouched(self):
        for path, content in self.untouched.items():
            self.assertEqual(path.read_bytes(), content, str(path))

    def assert_original(self):
        self.assertEqual(self.image.read_bytes(), self.original_image)
        self.assertEqual(self.receipt_path.read_bytes(), self.original_receipt)
        self.assert_untouched()

    def evidence(self):
        entries = list((self.state / 'upgrades').iterdir())
        self.assertEqual(len(entries), 1)
        return entries[0], json.loads((entries[0] / 'upgrade.json').read_text())

    def test_default_preflight_changes_no_files(self):
        before = {path: (path.read_bytes(), path.stat().st_mode)
                  for path in self.root.rglob('*') if path.is_file()}
        result = self.perform(install=False)
        after = {path: (path.read_bytes(), path.stat().st_mode)
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertTrue(result['validated'])
        self.assertFalse(result['installed'])
        self.assertEqual(before, after)
        self.assertFalse((self.state / 'upgrades').exists())

    def test_success_preserves_uninstall_metadata_and_regenerated_grub(self):
        result = self.perform()
        self.assertTrue(result['upgraded'])
        self.assertFalse(result['running_owner_changed'])
        self.assertTrue(result['reboot_required'])
        self.assertEqual(self.image.read_bytes(), self.candidate.read_bytes())
        updated = json.loads(self.receipt_path.read_text())
        for key in ('hook_sha256', 'normal_grub_sha256', 'protected_grub_sha256',
                    'normal_initrd_sha256', 'future_uninstall_metadata'):
            self.assertEqual(updated[key], self.receipt[key])
        self.assertEqual(updated['build'], self.build)
        self.assertEqual(updated['vm_report_sha256'], upgrade.sha256(self.vm))
        directory, record = self.evidence()
        self.assertEqual(record['state'], 'installed')
        self.assertEqual((directory / 'initrd.img.before').read_bytes(), self.original_image)
        self.assertEqual((directory / 'install.json.before').read_bytes(), self.original_receipt)
        for path in (directory, *directory.iterdir()):
            self.assertFalse(stat.S_IMODE(path.stat().st_mode) & 0o077)
        self.assert_untouched()

    def test_vm_mismatch_is_rejected_before_live_inspection_or_writes(self):
        tested = copy.deepcopy(self.build)
        tested['kernel_sha256'] = 'different'
        self.vm.write_bytes(upgrade.encoded({'passed': True, 'build': tested}))
        with self.assertRaisesRegex(RuntimeError, 'VM did not validate'):
            self.perform()
        self.collect.assert_not_called()
        self.assertFalse((self.state / 'upgrades').exists())
        self.assert_original()

    def test_live_layout_mismatch_is_rejected_without_writes(self):
        self.collect.return_value = {'different': 'root'}
        with self.assertRaisesRegex(RuntimeError, 'no longer matches'):
            self.perform()
        self.assertFalse((self.state / 'upgrades').exists())
        self.assert_original()

    def test_changed_protection_image_is_rejected(self):
        self.image.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'image changed'):
            self.perform()
        self.assertFalse((self.state / 'upgrades').exists())

    def test_changed_hook_is_rejected(self):
        self.hook.write_text('changed hook')
        with self.assertRaisesRegex(RuntimeError, 'hook changed'):
            self.perform()

    def test_inconsistent_receipt_is_rejected(self):
        self.receipt['image_sha256'] = 'changed'
        self.receipt_path.write_bytes(upgrade.encoded(self.receipt))
        with self.assertRaisesRegex(RuntimeError, 'receipt is incomplete or inconsistent'):
            self.perform()

    def test_changed_current_kernel_normal_initrd_base_and_source_are_rejected(self):
        cases = [(self.kernel, 'kernel differs'), (self.normal, 'initrd changed'),
                 (self.tools, 'base rescue tools differ'),
                 (self.project / next(iter(self.build['source_sha256'])), 'build source differs')]
        for path, message in cases:
            with self.subTest(path=path):
                old = path.read_bytes()
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(RuntimeError, message):
                    self.perform()
                path.write_bytes(old)
        self.assertFalse((self.state / 'upgrades').exists())

    def test_protected_entry_must_reference_exact_candidate_name(self):
        self.grub.write_text(self.grub.read_text().replace(self.image.name, 'other-protected-image'))
        with self.assertRaisesRegex(RuntimeError, 'expected protected entry'):
            self.perform()

    def test_ordinary_entry_must_not_select_protection(self):
        text = self.grub.read_text()
        self.grub.write_text(text.replace(' root=/dev/mapper/', ' ram_rescue_guard=1 root=/dev/mapper/', 1))
        with self.assertRaisesRegex(RuntimeError, 'first GRUB entry'):
            self.perform()

    def test_commented_protected_entry_is_not_an_active_menu_entry(self):
        self.grub.write_text(self.grub.read_text().replace(
            "menuentry 'Ubuntu USB root protection", "# menuentry 'Ubuntu USB root protection"))
        with self.assertRaisesRegex(RuntimeError, 'expected protected entry'):
            self.perform()

    def test_receipt_changed_during_live_validation_is_rejected(self):
        def changed(identity):
            altered = {**self.receipt, 'future_uninstall_metadata': {'changed': True}}
            self.receipt_path.write_bytes(upgrade.encoded(altered))
            return {key: self.profile[key] for key in ('schema', 'identity', 'guard')}
        self.collect.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, 'prerequisite changed'):
            self.perform()
        self.assertFalse((self.state / 'upgrades').exists())
        self.assertEqual(self.image.read_bytes(), self.original_image)

    def test_another_deployment_holds_the_same_lock(self):
        with upgrade.deployment_lock():
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                self.perform()
        self.assert_original()

    def test_previous_preparing_evidence_prevents_a_second_upgrade(self):
        old = self.state / 'upgrades/unfinished'
        old.mkdir(parents=True)
        (old / 'upgrade.json').write_bytes(upgrade.encoded({'state': 'preparing'}))
        with self.assertRaisesRegex(RuntimeError, 'earlier protection upgrade is incomplete'):
            self.perform()
        self.assert_original()

    def test_failure_after_image_rename_restores_image_and_exact_receipt(self):
        real_copy = upgrade.atomic_copy
        failed = False

        def interrupted(source, target, digest):
            nonlocal failed
            real_copy(source, target, digest)
            if target == self.image and source == self.candidate and not failed:
                failed = True
                raise OSError('directory fsync failed after image rename')

        with patch.object(upgrade, 'atomic_copy', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'directory fsync'):
                self.perform()
        self.assert_original()
        self.assertEqual(self.evidence()[1]['state'], 'failed_rolled_back')

    def test_failure_after_receipt_commit_also_restores_both_files(self):
        real_atomic = upgrade.atomic
        failed = False

        def interrupted(path, data, mode=0o600):
            nonlocal failed
            real_atomic(path, data, mode)
            if path == self.receipt_path and json.loads(data)['state'] == 'installed' and not failed:
                failed = True
                raise OSError('receipt commit interrupted')

        with patch.object(upgrade, 'atomic', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'receipt commit interrupted'):
                self.perform()
        self.assert_original()
        self.assertEqual(self.evidence()[1]['state'], 'failed_rolled_back')

    def test_rollback_failure_is_retained_and_blocks_a_later_upgrade(self):
        real_copy = upgrade.atomic_copy

        def interrupted(source, target, digest):
            if source.name == 'initrd.img.before':
                raise OSError('backup read failed')
            real_copy(source, target, digest)
            if source == self.candidate and target == self.image:
                raise OSError('image rename interrupted')

        with patch.object(upgrade, 'atomic_copy', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'rollback failed'):
                self.perform()
        self.assertEqual(self.evidence()[1]['state'], 'rollback_failed')
        with self.assertRaisesRegex(RuntimeError, 'earlier protection upgrade is incomplete'):
            self.perform()
        self.assert_untouched()

    def test_candidate_changed_after_preflight_is_never_committed(self):
        real_apply = upgrade.apply_upgrade

        def changed(context, vm_report):
            self.candidate.write_bytes(b'mutated candidate')
            return real_apply(context, vm_report)

        with patch.object(upgrade, 'apply_upgrade', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'Image changed while staging'):
                self.perform()
        self.assert_original()
        self.assertEqual(self.evidence()[1]['state'], 'failed_rolled_back')

    def test_install_requires_administrator_but_default_never_installs(self):
        with patch.object(upgrade.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(RuntimeError, 'administrator'):
                self.perform()
            self.assertFalse(self.perform(install=False)['installed'])
        self.assert_original()


if __name__ == '__main__':
    unittest.main()
