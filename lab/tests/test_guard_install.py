"""Optional boot-entry installation and rollback, entirely in a temp directory."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'guard'))
spec = importlib.util.spec_from_file_location('host_guard_install', BASE.parent / 'guard/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class GuardInstallTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.boot = self.root / 'boot'
        self.boot.mkdir()
        self.grub = self.boot / 'grub/grub.cfg'
        self.grub.parent.mkdir()
        self.hook = self.root / 'etc/grub.d/42_ram_rescue_guard'
        self.hook.parent.mkdir(parents=True)
        self.state = self.root / 'var/lib/ram-rescue-guard'
        self.state.parent.mkdir(parents=True)
        local = self.root / 'etc/lvm/lvmlocal.conf'
        local.parent.mkdir()
        local.write_text('local {}\n')
        self.release = '7.0.0-test'
        (self.boot / ('vmlinuz-' + self.release)).write_bytes(b'enrolled kernel')
        self.normal_initrd = self.boot / ('initrd.img-' + self.release)
        self.normal_initrd.write_bytes(b'normal initrd must remain untouched')
        self.original = (
            'set default="0"\n'
            "menuentry 'Ubuntu' --id ubuntu-normal {\n"
            '    linux /boot/vmlinuz-7.0.0-test root=/dev/mapper/vg--usb-ubuntu--root ro\n'
            '    initrd /boot/initrd.img-7.0.0-test\n}\n'
        ).encode()
        self.grub.write_bytes(self.original)
        self.profile = {
            'schema': 1, 'identity': {'vg_name': 'vg-usb'},
            'guard': {'kernel_release': self.release, 'root_lv': 'ubuntu-root',
                      'root_fs_uuid': '11111111-2222-3333-4444-555555555555'},
            'baseline': {
                'kernel_sha256': installer.sha256(self.boot / ('vmlinuz-' + self.release)),
                'initrd_sha256': installer.sha256(self.normal_initrd),
                'lvmlocal_sha256': installer.sha256(local),
            },
        }
        self.enrollment = self.root / 'enrollment.json'
        self.enrollment.write_text(json.dumps(self.profile))
        self.build_dir = self.root / 'build'
        self.build_dir.mkdir()
        (self.build_dir / 'initrd.img').write_bytes(b'candidate protected image')
        self.build = {
            'schema': 1, 'kernel_release': self.release,
            'source_sha256': {'guard/native/runtime/controller.cpp': 'source-hash'},
            'kernel_sha256': self.profile['baseline']['kernel_sha256'],
            'base_rescue_payload_sha256': 'base-tools-hash',
            'enrollment_sha256': installer.sha256(self.enrollment),
            'initramfs_sha256': installer.sha256(self.build_dir / 'initrd.img'),
        }
        (self.build_dir / 'build.json').write_text(json.dumps(self.build))
        self.vm_report = self.root / 'vm-report.json'
        self.vm = {'schema': 1, 'passed': True, 'build': copy.deepcopy(self.build)}
        self.vm_report.write_text(json.dumps(self.vm))
        self.image = self.boot / ('initrd.img-' + self.release + '-ram-rescue')
        self.generated = None
        self.generation_error = None
        self.patch('STATE', self.state)
        self.patch('HOOK', self.hook)
        self.patch('GRUB', self.grub)
        self.patch('Path', side_effect=self.mapped_path)
        self.patch('os.geteuid', return_value=0)
        self.patch('shutil.disk_usage', return_value=SimpleNamespace(free=2**40))
        self.collect = self.patch('collect', return_value={
            key: self.profile[key] for key in ('schema', 'identity', 'guard')})
        self.commands = self.patch('subprocess.run', side_effect=self.run_command)
        self.patch('print', create=True)

    def patch(self, name, *args, **kwargs):
        patcher = patch.object(installer, name, *args, **kwargs) if '.' not in name else \
            patch('host_guard_install.' + name, *args, **kwargs)
        # Dotted patches need the isolated module to be importable.
        sys.modules['host_guard_install'] = installer
        self.addCleanup(patcher.stop)
        return patcher.start()

    def mapped_path(self, value):
        path = Path(value)
        if path == Path('/boot') or path.is_relative_to('/boot') or path == Path('/etc/lvm/lvmlocal.conf'):
            return self.root / path.relative_to('/')
        return path

    def run_command(self, args, **kwargs):
        if args[0] == 'grub-mkconfig':
            if self.generation_error:
                raise self.generation_error
            generated = self.generated if self.generated is not None else self.original.decode()
            Path(args[2]).write_text(generated + installer.menu(self.profile, self.image.name))
        elif args[0] != 'grub-script-check':
            self.fail('Unexpected external command: ' + repr(args))
        return SimpleNamespace(returncode=0)

    def perform_install(self):
        installer.install(self.build_dir, self.enrollment, self.vm_report)

    def assert_original_boot(self):
        self.assertEqual(self.grub.read_bytes(), self.original)
        self.assertEqual(self.normal_initrd.read_bytes(), b'normal initrd must remain untouched')
        self.assertFalse(self.hook.exists())
        self.assertFalse(self.image.exists())

    def test_menu_escapes_lvm_names_and_only_adds_protected_entry(self):
        text = installer.menu(self.profile, self.image.name)
        self.assertIn('root=/dev/mapper/vg--usb-ubuntu--root ro ram_rescue_guard=1 nompath noresume', text)
        self.assertIn('initrd /boot/' + self.image.name, text)
        self.assertNotIn('set default', text)
        malicious = copy.deepcopy(self.profile)
        malicious['identity']['vg_name'] = "vg'; reboot; '"
        with self.assertRaises(ValueError):
            installer.menu(malicious, self.image.name)

    def test_mismatched_vm_inputs_are_rejected_before_live_or_boot_changes(self):
        for key in ('source_sha256', 'kernel_sha256', 'base_rescue_payload_sha256'):
            with self.subTest(key=key):
                self.vm['build'] = copy.deepcopy(self.build)
                self.vm['build'][key] = 'different-input'
                self.vm_report.write_text(json.dumps(self.vm))
                with self.assertRaisesRegex(RuntimeError, 'VM did not validate'):
                    self.perform_install()
                self.collect.assert_not_called()
                self.commands.assert_not_called()
                self.assertFalse(self.state.exists())
                self.assert_original_boot()

    def test_modified_candidate_image_is_rejected_before_install(self):
        (self.build_dir / 'initrd.img').write_bytes(b'modified after validation')
        with self.assertRaisesRegex(RuntimeError, 'checksum differs'):
            self.perform_install()
        self.collect.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assert_original_boot()

    def test_live_root_mismatch_is_rejected_before_install(self):
        self.collect.return_value = {'different': 'root'}
        with self.assertRaisesRegex(RuntimeError, 'no longer matches'):
            self.perform_install()
        self.commands.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assert_original_boot()

    def test_menu_generation_failure_removes_only_staged_protection_files(self):
        self.generation_error = subprocess.CalledProcessError(1, ['grub-mkconfig'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.perform_install()
        self.assert_original_boot()
        self.assertEqual(json.loads((self.state / 'install.json').read_text())['state'], 'failed_rolled_back')
        self.assertEqual((self.state / 'grub.cfg.before').read_bytes(), self.original)

    def test_failure_after_grub_replace_restores_original_menu(self):
        real_atomic = installer.atomic
        failed = False
        def fail_after_replace(path, data, mode=0o600):
            nonlocal failed
            real_atomic(path, data, mode)
            if path == self.grub and data != self.original and not failed:
                failed = True
                raise OSError('simulated directory fsync failure after replace')
        with patch.object(installer, 'atomic', side_effect=fail_after_replace):
            with self.assertRaisesRegex(OSError, 'directory fsync'):
                self.perform_install()
        self.assertTrue(failed)
        self.assert_original_boot()

    def test_changed_default_entry_is_rejected_even_with_same_initrd_line(self):
        self.generated = self.original.decode().replace(
            'root=/dev/mapper/vg--usb-ubuntu--root', 'root=/dev/wrong-root')
        with self.assertRaisesRegex(RuntimeError, 'normal|default'):
            self.perform_install()
        self.assert_original_boot()

    def test_success_and_explicit_rollback_preserve_normal_image(self):
        self.perform_install()
        self.assertTrue(self.grub.read_bytes().startswith(self.original))
        self.assertTrue(self.image.exists())
        self.assertTrue(self.hook.exists())
        self.assertEqual(self.normal_initrd.read_bytes(), b'normal initrd must remain untouched')
        installer.rollback()
        self.assert_original_boot()
        self.assertEqual(json.loads((self.state / 'install.json').read_text())['state'], 'removed')


if __name__ == '__main__':
    unittest.main()
