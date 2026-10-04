"""Optional boot-entry installation and rollback, entirely in a temp directory."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import select
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
import trusted_paths

REAL_OPEN_DIRECTORY = trusted_paths.open_directory
REAL_OPEN_TRUSTED = trusted_paths.open_trusted


def lock_process(directory, *, hold=False):
    """Use a separate interpreter so lock contention crosses process boundaries."""
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import os
import trusted_paths
# This child uses only an ordinary-user fixture, while production always
# requires the root-owned absolute namespace. Retain real directory checks.
verified_directory = trusted_paths.open_directory
trusted_paths.open_directory = lambda path: verified_directory(
    path, uid=os.getuid(), anchor=Path(sys.argv[2]).parent)
from install import deployment_lock
try:
    with deployment_lock(Path(sys.argv[2])):
        print('locked', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except RuntimeError as error:
    if 'already running' not in str(error):
        raise
    print('busy', flush=True)
"""
    return subprocess.Popen(
        [sys.executable, '-I', '-c', script, str(BASE.parent / 'guard'),
         str(directory), 'hold' if hold else 'probe'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@contextmanager
def foreign_deployment_lock(directory):
    process = lock_process(directory, hold=True)
    try:
        if not select.select([process.stdout], [], [], 5)[0]:
            raise RuntimeError('Child did not acquire the deployment lock in time')
        if process.stdout.readline().strip() != 'locked':
            raise RuntimeError('Child could not acquire the deployment lock')
        yield
    finally:
        try:
            process.communicate('\n', timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


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
        self.project = self.root / 'source'
        source = self.project / 'guard/native/runtime/controller.cpp'
        source.parent.mkdir(parents=True)
        source.write_text('// tested source\n')
        self.build = {
            'schema': 1, 'kernel_release': self.release,
            'source_sha256': {str(source.relative_to(self.project)): installer.sha256(source)},
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
        self.patch('PROJECT', self.project)
        self.patch('HOOK', self.hook)
        self.patch('GRUB', self.grub)
        self.patch('Path', side_effect=self.mapped_path)
        self.patch('os.geteuid', return_value=0)
        # Transaction fixtures belong to the ordinary test user. Trust-policy
        # checks have their own tests; retain actual reads and checksums here.
        @contextmanager
        def fixture_file(path, **_):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                yield descriptor
            finally:
                os.close(descriptor)
        for target, implementation in (
                ('open_trusted', fixture_file),
                ('open_directory', lambda path: os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))):
            boundary = patch('trusted_paths.' + target, implementation)
            boundary.start()
            self.addCleanup(boundary.stop)
        self.patch('shutil.disk_usage', return_value=SimpleNamespace(free=2**40))
        self.collect = self.patch('collect', return_value={
            key: self.profile[key] for key in ('schema', 'identity', 'guard')})
        self.commands = self.patch('subprocess.run', side_effect=self.run_command)
        self.patch('print', create=True)
        # Shared-workspace umasks may default to group-writable directories.
        # These transaction fixtures represent an administrator's private tree.
        for directory in self.root.rglob('*'):
            if directory.is_dir():
                directory.chmod(0o700)

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

    def test_foreign_deployment_blocks_rollback_and_upgrade_before_receipt_reads(self):
        import upgrade
        self.perform_install()
        receipt = self.state / 'install.json'
        valid_receipt = receipt.read_bytes()
        receipt.write_bytes(b'incomplete receipt must not be read before locking')
        before = {path: path.read_bytes() for path in
                  (receipt, self.grub, self.hook, self.image, self.normal_initrd)}
        with foreign_deployment_lock(self.state):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                installer.rollback()
            with patch.object(upgrade, 'STATE', self.state):
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    upgrade.upgrade(self.build_dir, self.enrollment, self.vm_report, install=True)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        receipt.write_bytes(valid_receipt)
        installer.rollback()
        self.assert_original_boot()

    def test_first_install_holds_lock_through_late_commit_failure_and_cleanup(self):
        real_atomic = installer.atomic
        observed = []

        def interrupted(path, data, mode=0o600):
            real_atomic(path, data, mode)
            phase = None
            if path == self.state / 'grub.cfg.before':
                phase = 'backup'
            elif path == self.state / 'install.json':
                phase = json.loads(data)['state']
            if phase not in ('backup', 'installed', 'failed_rolled_back'):
                return
            process = lock_process(self.state)
            try:
                output, error = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail('Child lock probe timed out')
            self.assertEqual(process.returncode, 0, error)
            self.assertEqual(output.strip(), 'busy')
            observed.append(phase)
            if phase == 'installed':
                raise OSError('simulated receipt fsync failure after replace')

        with patch.object(installer, 'atomic', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'receipt fsync'):
                self.perform_install()
        self.assertEqual(observed, ['backup', 'installed', 'failed_rolled_back'])
        self.assert_original_boot()
        with foreign_deployment_lock(self.state):
            pass  # The transaction released its lock even after rollback.

    def test_deployment_lock_rejects_untrusted_parent_and_directory_symlink(self):
        self.state.mkdir(mode=0o700)
        def fixture_directory(path):
            return REAL_OPEN_DIRECTORY(path, uid=os.getuid(), anchor=self.root)
        with patch('trusted_paths.open_directory', fixture_directory):
            self.state.parent.chmod(0o777)
            with self.assertRaisesRegex(RuntimeError, 'Untrusted'):
                with installer.deployment_lock():
                    self.fail('Untrusted lock directory was accepted')
            self.state.parent.chmod(0o755)
            link = self.state.parent / 'alias'
            link.symlink_to(self.state, target_is_directory=True)
            with self.assertRaises(OSError):
                with installer.deployment_lock(link):
                    self.fail('Symlink lock directory was accepted')

    def test_rollback_rejects_linked_or_writable_inputs_without_modifying_boot(self):
        self.perform_install()
        before = {path: path.read_bytes() for path in
                  (self.grub, self.hook, self.image, self.state / 'install.json',
                   self.state / 'grub.cfg.before')}
        def fixture_directory(path, **_):
            return REAL_OPEN_DIRECTORY(path, uid=os.getuid(), anchor=self.root)
        def fixture_file(path, **_):
            return REAL_OPEN_TRUSTED(path, uid=os.getuid(), anchor=self.root)
        with patch('trusted_paths.open_directory', fixture_directory), \
                patch('trusted_paths.open_trusted', fixture_file):
            for path in before:
                with self.subTest(path=path):
                    mode = path.stat().st_mode & 0o777
                    path.chmod(mode | 0o020)
                    with self.assertRaisesRegex(RuntimeError, 'Untrusted'):
                        installer.rollback()
                    path.chmod(mode)
                    saved = path.with_name(path.name + '.saved')
                    path.rename(saved)
                    path.symlink_to(saved)
                    with self.assertRaises(OSError):
                        installer.rollback()
                    path.unlink()
                    saved.rename(path)
                    self.assertEqual(before, {item: item.read_bytes() for item in before})


if __name__ == '__main__':
    unittest.main()
