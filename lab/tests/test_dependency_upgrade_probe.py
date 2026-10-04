"""Dependency experiments must use real versioned bytes and actual mapped evidence."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import dependency_upgrade_probe as probe


class DependencyUpgradeFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.status = ('Package: libssl3t64\nStatus: install ok installed\n'
                       'Version: 3.0.13-0ubuntu3.15\nArchitecture: amd64\n')
        self.library = '/usr/lib/x86_64-linux-gnu/libcrypto.so.3'

    def archive(self, members):
        path = self.root / 'root.tar.xz'
        with tarfile.open(path, 'w:xz') as archive:
            for name, content, kind in members:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        return path

    def test_extracts_regular_library_and_records_real_package_status(self):
        content = b'actual old library bytes'
        archive = self.archive([(self.library.lstrip('/'), content, tarfile.REGTYPE),
                                ('var/lib/dpkg/status', self.status.encode(), tarfile.REGTYPE)])
        output = self.root / 'old.so'
        metadata = probe.extract_old_library(archive, output, Path(self.library))
        self.assertEqual(output.read_bytes(), content)
        self.assertEqual(metadata['sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(metadata['version'], '3.0.13-0ubuntu3.15')
        self.assertEqual(metadata['package'], 'libssl3t64:amd64')

    def test_duplicate_or_nonregular_source_member_is_rejected(self):
        for members in (
            [(self.library.lstrip('/'), b'x', tarfile.REGTYPE)] * 2,
            [(self.library.lstrip('/'), b'', tarfile.SYMTYPE)],
        ):
            with self.subTest(members=members), self.assertRaisesRegex(ValueError, 'Unexpected'):
                probe.extract_old_library(self.archive(members), self.root / 'old.so', Path(self.library))
        self.assertFalse((self.root / 'old.so').exists())

    def test_uninstalled_or_duplicate_package_status_cannot_claim_provenance(self):
        for text in (self.status.replace('install ok installed', 'deinstall ok config-files'),
                     self.status + '\n' + self.status):
            with self.assertRaises(ValueError):
                probe.package_status(text, probe.PACKAGE)

    def test_offline_resolver_restores_production_functions_after_failure(self):
        old = self.root / 'old.so'
        closure = {self.library: Path(self.library)}
        native = SimpleNamespace(binary_closure=lambda _: closure,
            dependency_packages=lambda paths: {str(p): {'version': 'current'} for p in paths})
        original_closure, original_packages = native.binary_closure, native.dependency_packages
        metadata = {'package': 'libssl3t64:amd64', 'version': 'old', 'architecture': 'amd64'}
        with self.assertRaisesRegex(RuntimeError, 'fixture error'):
            with probe.old_resolver(native, closure, old, metadata):
                self.assertEqual(native.binary_closure(None)[self.library], old)
                self.assertEqual(native.dependency_packages([old])[str(old)], metadata)
                raise RuntimeError('fixture error')
        self.assertIs(native.binary_closure, original_closure)
        self.assertIs(native.dependency_packages, original_packages)

    def test_actual_loaded_library_must_match_not_just_resident_path(self):
        fixture = {'a': {'sha256': 'old'}, 'binary_sha256': 'binary'}
        snapshot = {'resident': {'library_sha256': 'old', 'binary_sha256': 'binary'},
                    'controllers': [{'binary_sha256': 'binary', 'loaded_library_sha256': 'old'} for _ in range(2)]}
        self.assertTrue(probe.runtime_matches(snapshot, fixture, 'a'))
        snapshot['controllers'][1]['loaded_library_sha256'] = 'new'
        self.assertFalse(probe.runtime_matches(snapshot, fixture, 'a'))

    def test_guest_protocol_has_one_entrypoint_and_reads_actual_map_files(self):
        source = probe.guest_source()
        compile(source, '<dependency guest>', 'exec')
        self.assertEqual(source.count("if __name__ == '__main__':"), 1)
        self.assertIn("proc / 'map_files'", source)
        self.assertIn("self.manager('upgrade')", source)
        self.assertNotIn("'--force'", source)


if __name__ == '__main__':
    unittest.main()
