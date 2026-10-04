import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('tested_trusted_paths', REPO / 'guard/trusted_paths.py')
trusted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trusted)


class TrustedPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'config'
        self.folder.mkdir(mode=0o700)
        self.path = self.folder / 'record.json'
        self.path.write_text('{"value": 1}')
        self.path.chmod(0o600)
        self.options = {'anchor': self.root, 'uid': os.getuid()}

    def test_read_and_streaming_hash(self):
        self.assertEqual(trusted.read_trusted_json(self.path, **self.options), {'value': 1})
        self.assertEqual(len(trusted.trusted_sha256(self.path, **self.options)), 64)

    def test_writable_parent_and_file_rejected(self):
        for path in (self.folder, self.path):
            original = path.stat().st_mode & 0o777
            path.chmod(original | 0o020)
            with self.assertRaises(RuntimeError):
                trusted.read_trusted(self.path, **self.options)
            path.chmod(original)

    def test_wrong_owner_policy_rejected(self):
        with self.assertRaises(RuntimeError):
            trusted.read_trusted(self.path, anchor=self.root, uid=os.getuid() + 1)

    def test_file_and_parent_symlinks_rejected(self):
        for target in (self.path, self.folder):
            link = self.root / ('link-' + target.name)
            link.symlink_to(target)
            path = link / self.path.name if target.is_dir() else link
            with self.assertRaises(OSError):
                trusted.read_trusted(path, **self.options)

    def test_hardlink_and_fifo_rejected(self):
        link = self.root / 'hardlink'
        os.link(self.path, link)
        with self.assertRaises(RuntimeError):
            trusted.read_trusted(self.path, **self.options)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(RuntimeError):
            trusted.read_trusted(fifo, **self.options)

    def test_oversize_and_escape_rejected(self):
        with self.assertRaises(ValueError):
            trusted.read_trusted(self.path, limit=3, **self.options)
        with self.assertRaises(ValueError):
            trusted.read_trusted(self.folder / '..' / 'config' / self.path.name, **self.options)

    def test_open_descriptor_survives_path_replacement(self):
        with trusted.open_trusted(self.path, **self.options) as descriptor:
            self.path.rename(self.folder / 'old.json')
            self.path.write_text('{"value": 999}')
            self.assertEqual(json.loads(os.read(descriptor, 100)), {'value': 1})
