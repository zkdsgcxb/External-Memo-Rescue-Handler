"""Keep VM composition explicit and prevent a false native acceptance result."""
from pathlib import Path
import hashlib
import json
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_guard_probe as probe
import cpp_transaction_probe as transactions


class RuntimeFixtureTests(unittest.TestCase):
    def test_non_elf_cannot_be_benchmarked_as_native(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'wrapper'
            path.write_text('#!/bin/sh\nexec python3 controller.py\n')
            with self.assertRaisesRegex(ValueError, 'ELF'):
                probe.binary_closure(path)

    def test_dynamic_elf_closure_includes_loader_and_libc(self):
        libraries = probe.binary_closure(Path('/usr/bin/true'))
        self.assertTrue(any('ld-linux' in name for name in libraries))
        self.assertTrue(any('libc.so' in name for name in libraries))
        self.assertTrue(all(path.is_file() for path in libraries.values()))

    def test_composition_always_restores_shared_fixture_modules(self):
        original = (probe.data.REPO, probe.unified.REPO, probe.unified.GUEST,
                    probe.boot.GUEST, probe.boot.HOOK)
        with self.assertRaisesRegex(RuntimeError, 'deliberate'):
            with probe.compose_sources(Path('/pinned'), Path('/guest'), '\n# isolated hook\n'):
                self.assertEqual(probe.data.REPO, Path('/pinned'))
                self.assertIn("selected_controller(owner['process'])", probe.boot.GUEST)
                raise RuntimeError('deliberate composition failure')
        self.assertEqual((probe.data.REPO, probe.unified.REPO, probe.unified.GUEST,
                          probe.boot.GUEST, probe.boot.HOOK), original)

    def test_transaction_overlay_preserves_executable_init_and_native_supervisor(self):
        probe.WORK.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=probe.WORK) as temporary:
            root = Path(temporary)
            original, output = root / 'original', root / 'output'
            original.mkdir()
            output.mkdir()
            metadata = {}
            for name, key in [('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')]:
                (original / name).write_bytes(b'unused-test-fixture')
                metadata[key] = hashlib.sha256(b'unused-test-fixture').hexdigest()
            (original / 'build.json').write_text(json.dumps(metadata))
            # No VM or generated archive is executed by this filesystem test.
            with patch.object(probe, 'append_archive'):
                transactions.build_fixture(output, original, Path('/usr/bin/true'))
            for name in ('init', 'opt/lab/root-init.sh'):
                self.assertTrue((output / 'overlay' / name).stat().st_mode & stat.S_IXUSR)
            init = (output / 'overlay/init').read_text()
            self.assertIn('guard-runtime run --config', init)
            self.assertIn('guard-runtime takeover --config', init)
            self.assertNotIn('python3 /opt/lab/path_guard.py', init)

    def test_native_transaction_runner_cannot_dispatch_back_to_python(self):
        source = transactions.adapt_runner('a' * 64)
        self.assertNotIn('run_legacy', source)
        self.assertTrue(source.endswith("if __name__ == '__main__':\n    main()\n"))
        compile(source, '<native-transaction>', 'exec')


if __name__ == '__main__':
    unittest.main()
