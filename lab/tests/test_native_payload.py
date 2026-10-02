"""Native image dependency closure and mandatory runtime verification."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

GUARD = Path(__file__).resolve().parents[2] / 'guard'
sys.path.insert(0, str(GUARD))
import native_payload


class NativePayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / 'input-binary'
        self.binary.write_bytes(b'fixture runtime')
        self.library = self.root / 'input-library'
        self.library.write_bytes(b'fixture library')
        self.image = self.root / 'image'
        self.image.mkdir()

    def stage(self):
        with patch.object(native_payload, 'binary_closure', return_value={'/lib/fixture.so': self.library}):
            return native_payload.stage_runtime(self.image, self.binary)

    def test_complete_manifest_verifies_without_mutation(self):
        manifest = self.stage()
        target = self.image / str(native_payload.BINARY).lstrip('/')
        before = {path: path.read_bytes() for path in self.image.rglob('*') if path.is_file()}
        self.assertEqual(native_payload.verify_runtime(self.image), target)
        self.assertEqual(manifest['payload_file_bytes'], self.binary.stat().st_size + self.library.stat().st_size
                         + len(native_payload.ENTRYPOINT_SCRIPT.encode()))
        self.assertEqual({path: path.read_bytes() for path in self.image.rglob('*') if path.is_file()}, before)

    def test_missing_native_runtime_refuses_startup(self):
        with self.assertRaisesRegex(RuntimeError, 'Native Guard runtime is missing'):
            native_payload.verify_runtime(self.image)

    def test_partial_native_runtime_never_falls_back(self):
        self.stage()
        (self.image / str(native_payload.MANIFEST).lstrip('/')).unlink()
        with self.assertRaisesRegex(RuntimeError, 'partial'):
            native_payload.verify_runtime(self.image)

    def test_entrypoint_is_exec_only_and_authenticated(self):
        manifest = self.stage()
        path = self.image / str(native_payload.ENTRYPOINT).lstrip('/')
        self.assertEqual(path.read_text(), '#!/bin/sh\nexec /opt/guard-runtime/guard-runtime maintain "$@"\n')
        self.assertTrue(path.stat().st_mode & 0o111)
        self.assertEqual(native_payload.sha256(path), manifest['entrypoint_sha256'])
        path.write_text('#!/bin/sh\nexit 0\n')
        with self.assertRaisesRegex(RuntimeError, 'entrypoint'):
            native_payload.verify_runtime(self.image)

    def test_missing_entrypoint_is_partial(self):
        self.stage()
        (self.image / str(native_payload.ENTRYPOINT).lstrip('/')).unlink()
        with self.assertRaisesRegex(RuntimeError, 'partial'):
            native_payload.verify_runtime(self.image)

    def test_changed_library_rejected(self):
        self.stage()
        (self.image / 'lib/fixture.so').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'library checksum'):
            native_payload.verify_runtime(self.image)

    def test_changed_binary_rejected(self):
        self.stage()
        (self.image / str(native_payload.BINARY).lstrip('/')).write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'executable checksum'):
            native_payload.verify_runtime(self.image)

    def test_dangling_binary_is_partial(self):
        target = self.image / str(native_payload.BINARY).lstrip('/')
        target.parent.mkdir(parents=True)
        target.symlink_to('missing')
        with self.assertRaisesRegex(RuntimeError, 'partial'):
            native_payload.verify_runtime(self.image)

    def test_image_escape_rejected(self):
        (self.image / 'lib').symlink_to(self.root)
        with self.assertRaisesRegex(RuntimeError, 'escapes'):
            self.stage()

    def test_manifest_escape_rejected(self):
        self.stage()
        path = self.image / str(native_payload.MANIFEST).lstrip('/')
        value = json.loads(path.read_text())
        value['library_sha256']['/../input-library'] = native_payload.sha256(self.library)
        path.chmod(0o600)
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, 'normalized'):
            native_payload.verify_runtime(self.image)

    def test_real_elf_closure_contains_dlopen_library(self):
        closure = native_payload.binary_closure(Path('/usr/bin/true'))
        self.assertTrue(any(Path(name).name == 'libdevmapper.so.1.02.1' for name in closure))
        self.assertTrue(all(path.is_file() for path in closure.values()))


if __name__ == '__main__':
    unittest.main()
