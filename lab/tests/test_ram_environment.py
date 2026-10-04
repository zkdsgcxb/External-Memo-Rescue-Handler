import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'guard'))
import ram_environment as environment


class RamEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = self.root / 'run'
        state.mkdir(mode=0o700)
        self.payload = self.root / 'payload'
        self.payload.mkdir()
        self.shared = self.root / 'shared'
        self.shared.mkdir()
        self.private = state / 'rootfs'
        for name, value in {'STATE': state, 'ALIAS': state / 'tools', 'SHARED': self.shared,
                            'PRIVATE': self.private, 'RECEIPT': state / 'environment.json'}.items():
            self.enterContext(patch.object(environment, name, value))
        self.enterContext(patch.object(environment.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(environment, 'open_directory', side_effect=lambda p: os.open(p, os.O_RDONLY)))
        self.active = self.enterContext(patch.object(environment, 'active_root', return_value=False))
        self.manifest = self.enterContext(patch.object(environment, 'package_manifest', return_value={'binary_sha256': 'new'}))
        self.verify = self.enterContext(patch.object(environment, 'verify_runtime', side_effect=lambda p: p / 'binary'))
        self.mounts = self.enterContext(patch.object(environment, 'verify_mount', side_effect=lambda p: p / 'binary'))
        self.hash = self.enterContext(patch.object(environment, 'sha256', return_value='new'))
        self.match = self.enterContext(patch.object(environment, 'runtime_matches', return_value=True))
        self.unpack = self.enterContext(patch.object(environment, 'unpack'))
        self.enterContext(patch.object(environment, 'pin_host_fstab', return_value=False))
        self.commands = self.enterContext(patch.object(environment, 'run', return_value=''))

    def test_ordinary_boot_prepares_independent_tools(self):
        result = environment.prepare(self.payload)
        self.assertEqual(result['root'], str(self.private))
        self.assertEqual(environment.ALIAS.readlink(), self.private)
        self.assertFalse(result['reused'])
        self.unpack.assert_called_once_with(self.payload, self.private)
        self.assertEqual(self.commands.call_count, 5)
        calls = [call.args for call in self.commands.call_args_list]
        self.assertTrue(any('size=256M,noswap,nosuid,mode=0700' in call for call in calls))
        self.assertFalse(any('/dev/sd' in value for call in calls for value in call))

    def test_matching_root_reuses_one_tools_environment(self):
        self.active.return_value = True
        self.assertTrue(environment.prepare(self.payload)['reused'])
        self.assertEqual(self.commands.call_args_list[0].args,
                         ('/bin/mount', '--bind', str(self.shared), str(self.private)))
        self.assertEqual(self.commands.call_count, 5)
        self.unpack.assert_not_called()
        self.assertEqual(environment.ALIAS.readlink(), self.private)

    def test_new_data_runtime_does_not_replace_old_root_runtime(self):
        self.active.return_value = True
        self.match.return_value = False
        self.assertEqual(environment.prepare(self.payload)['root'], str(self.private))
        self.assertTrue(self.shared.is_dir())

    def test_live_runtime_mismatch_refuses_hot_replacement(self):
        environment.ALIAS.symlink_to(self.private)
        self.match.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'already uses another runtime'):
            environment.prepare(self.payload)
        self.commands.assert_not_called()

        self.unpack.assert_not_called()

    def test_foreign_alias_refused(self):
        environment.ALIAS.symlink_to(self.root)
        with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
            environment.prepare(self.payload)
        self.commands.assert_not_called()

    def test_no_root_and_no_installed_package_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'Install a package'):
            environment.prepare(self.root / 'missing')
        self.commands.assert_not_called()

    def test_failed_extraction_unmounts_only_new_tmpfs(self):
        self.unpack.side_effect = RuntimeError('invalid package')
        with self.assertRaisesRegex(RuntimeError, 'invalid package'):
            environment.prepare(self.payload)
        self.assertEqual(self.commands.call_args.args, ('/bin/umount', str(self.private)))
        self.assertFalse(environment.ALIAS.exists())
        self.assertFalse(self.private.exists())

    def test_existing_incomplete_private_root_is_not_erased(self):
        self.private.mkdir()
        witness = self.private / 'witness'
        witness.write_text('keep')
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            environment.prepare(self.payload)
        self.assertEqual(witness.read_text(), 'keep')
        self.commands.assert_not_called()


class RuntimeVersionTests(unittest.TestCase):
    def test_same_elf_with_different_library_or_base_never_matches(self):
        root = Path('/trusted/runtime')
        native = {'binary_sha256': 'same', 'library_sha256': {'libcrypto': 'old'}}
        desired = {'native_runtime': native, 'base_sha256': 'base-old'}
        def read(path):
            return native if path.name == 'runtime.json' else {'schema': 1, 'sha256': 'base-old'}
        with patch.object(environment, 'read_trusted_json', side_effect=read):
            self.assertTrue(environment.runtime_matches(root, desired))
            self.assertFalse(environment.runtime_matches(root, {**desired, 'base_sha256': 'base-new'}))
            self.assertFalse(environment.runtime_matches(root, {**desired, 'native_runtime': {
                'binary_sha256': 'same', 'library_sha256': {'libcrypto': 'new'}}}))
