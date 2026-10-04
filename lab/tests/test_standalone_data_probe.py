"""The independent-data VM fixture must not reuse protected-root integration."""
import ast
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import standalone_data_probe as standalone


class StandaloneFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_plain_command_retains_isolation_and_removes_root_guard(self):
        inputs = {'kernel': self.root / 'vmlinuz', 'seed': self.root / 'seed.raw'}
        command = standalone.vm_command(self.root, inputs, self.root / 'plain.img', self.root / 'overlay.qcow2')
        flags = command[command.index('-append') + 1].split()
        self.assertNotIn('ram_rescue_guard=1', flags)
        self.assertNotIn('nompath', flags)
        self.assertIn('ram_rescue_standalone_test=1', flags)
        self.assertIn('ram_rescue_lab=1', flags)
        self.assertEqual(command[command.index('-nic') + 1], 'none')
        blocks = [json.loads(command[i + 1]) for i, value in enumerate(command) if value == '-blockdev']
        root = next(value for value in blocks if value['node-name'] == 'usbdisk')
        self.assertTrue(root['backing']['read-only'])
        self.assertEqual(root['file']['filename'], str(self.root / 'overlay.qcow2'))
        self.assertTrue(all(not value['file']['filename'].startswith('/dev/') for value in blocks))

    def test_guest_uses_current_observer_and_installed_entry(self):
        source = standalone.guest_source()
        compile(source, '<standalone guest>', 'exec')
        self.assertNotIn('from path_guard import', source)
        self.assertNotIn('from dm_monitor import', source)
        self.assertNotIn('/bin/chroot', source)
        self.assertNotIn('eval(', source)
        self.assertIn("'/usr/bin/rescue-guard-admin', 'manager'", source)
        self.assertIn("or 'ram_rescue_guard=1' in flags", source)
        self.assertIn("len(line) > 4096", source)

    def test_doctor_acceptance_requires_candidate_and_current_library_match(self):
        package = {'runtime': dict.fromkeys(('binary_sha256', 'base_sha256', 'archive_sha256'), 'a' * 64)}
        manager = {**package['runtime'], 'archive_integrity_checked': False}
        matches = {'native_manifest': True, 'base_source': True}
        report = {'state': 'ready', 'devices': [
            {'owner_matches': True, 'runtime_context': 'standalone_data'} for _ in range(2)],
            'versions': {'installed_candidates': {'manager': manager, 'root': None},
                         'runtimes': [{'context': 'standalone_data', 'installed_candidate_matches': {'manager': matches}}]}}
        self.assertTrue(standalone.doctor_matches_installed_runtime(report, package))
        matches['native_manifest'] = False
        self.assertFalse(standalone.doctor_matches_installed_runtime(report, package))
        matches['native_manifest'] = True
        manager['base_sha256'] = 'b' * 64
        self.assertFalse(standalone.doctor_matches_installed_runtime(report, package))
        report['versions'].pop('installed_candidates')
        self.assertFalse(standalone.doctor_matches_installed_runtime(report, package))

    def test_hook_is_double_gated_and_does_not_install_protection(self):
        self.assertIn('ram_rescue_standalone_test=1', standalone.HOOK)
        self.assertIn('RAMRescueLab', standalone.HOOK)
        self.assertNotIn('ram-rescue-guard', standalone.HOOK)
        self.assertNotIn('RootDirectory=', standalone.UNIT)
        self.assertNotIn('/bin/sh', standalone.UNIT)

    def test_namespace_verification_rejects_writable_root_and_missing_runtime_mounts(self):
        tree = ast.parse(standalone.GUEST.read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == 'controller_mount_policy')
        namespace = {}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<mount policy>', 'exec'), namespace)
        inspect = namespace['controller_mount_policy']
        root = '1 0 0:1 / / ro,nosuid - tmpfs tools rw\n'
        run = '2 1 0:2 / /run rw,nosuid - tmpfs run rw\n'
        dev = '3 1 0:3 / /dev rw,nosuid - devtmpfs dev rw\n'
        self.assertTrue(inspect(root + run + dev)['verified'])
        with self.assertRaisesRegex(RuntimeError, 'root is not read-only'):
            inspect(root.replace('ro,nosuid', 'rw,nosuid') + run + dev)
        with self.assertRaisesRegex(RuntimeError, 'writable /run or /dev'):
            inspect(root + run)
        with self.assertRaisesRegex(RuntimeError, 'writable /run or /dev'):
            inspect(root + run.replace('rw,nosuid', 'ro,nosuid') + dev)

    def test_protected_initrd_is_rejected_before_overlay_generation(self):
        order = self.root / 'unpacked/main/scripts/init-bottom/ORDER'
        order.parent.mkdir(parents=True)
        order.write_text('/scripts/init-bottom/ram-rescue-guard "$@"\n')
        with patch.object(standalone.subprocess, 'run'):
            with self.assertRaisesRegex(RuntimeError, 'protected root initrd'):
                standalone.create_initrd(self.root, self.root / 'original.img', self.root / 'handler.deb', {})
        self.assertFalse((self.root / 'overlay').exists())

    def test_rejects_host_path_symlink_and_directory_inputs(self):
        with self.assertRaises(ValueError):
            standalone.regular_lab_file('/etc/hostname')
        link = self.root / 'link'
        link.symlink_to('/etc/hostname')
        with self.assertRaises(ValueError):
            standalone.regular_lab_file(link)
        with self.assertRaises(ValueError):
            standalone.regular_lab_file(self.root)

    def test_validation_requires_matching_disposable_seed_and_package(self):
        (self.root / 'seed/s0').mkdir(parents=True)
        for name in ('seed/s0/usb.raw', 'vmlinuz', 'original-initrd.img', 'handler.deb'):
            (self.root / name).write_bytes(b'test-input-' + name.encode())
        seed = {'passed': True, 'creation': {'identity': {'usb_serial': 'RAMRESCUE-LAB-001', 'vg_name': 'labrescue'}},
                'source_image_sha256_after': standalone.sha256(self.root / 'seed/s0/usb.raw')}
        package = {'package': 'handler.deb', 'sha256': standalone.sha256(self.root / 'handler.deb'), 'maintainer_scripts': False}
        (self.root / 'seed/report.json').write_text(json.dumps(seed))
        (self.root / 'package.json').write_text(json.dumps(package))
        with patch.object(standalone.os, 'geteuid', return_value=1000):
            inputs, _ = standalone.validate_inputs(self.root, self.root)
            self.assertEqual(inputs['seed'], self.root / 'seed/s0/usb.raw')
            (self.root / 'handler.deb').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'unchanged package'):
                standalone.validate_inputs(self.root, self.root)
        with patch.object(standalone.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'host root'):
                standalone.validate_inputs(self.root, self.root)


if __name__ == '__main__':
    unittest.main()
