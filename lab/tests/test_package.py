"""Inspect offline .deb payloads without installing or requesting privilege."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO/'guard'))
import package
import admin_entry


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REPO/'lab/work')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory/'source'
        (self.source/'guard').mkdir(parents=True)
        (self.source/'ram-rescue-demo/src').mkdir(parents=True)
        (self.source/'guard/admin_entry.py').write_text((REPO/'guard/admin_entry.py').read_text())
        (self.source/'guard/manage.py').write_text('# test administration\n')
        (self.source/'ram-rescue-demo/src/rescue.py').write_text('# test manual tools\n')
        for name in ('build.py', 'session_payload.py', 'install.py'):
            (self.source/'ram-rescue-demo'/name).write_text('# source fixture\n')
        self.base = self.directory/'base'
        self.base.mkdir()
        with tarfile.open(self.base/'rescue-root.tar.gz', 'w:gz') as archive:
            for name in ('etc', 'etc/rescue', 'opt', 'tmp'):
                entry = tarfile.TarInfo(name)
                entry.type = tarfile.DIRTYPE
                entry.mode = 0o1777 if name == 'tmp' else 0o755
                archive.addfile(entry)
            for name in ('etc/shadow', 'etc/rescue/identity.json', 'etc/rescue/enrollment.json'):
                value = b'fixture identity or locked account\n'
                entry = tarfile.TarInfo(name)
                entry.mode = 0o600
                entry.size = len(value)
                archive.addfile(entry, io.BytesIO(value))
        (self.base/'manifest.json').write_text(json.dumps({'sha256': package.sha256(self.base/'rescue-root.tar.gz')}))
        self.library = b'library version one'

    def stage_runtime(self, root, binary):
        directory = root/'opt/guard-runtime'
        directory.mkdir(parents=True)
        (directory/'guard-runtime').write_bytes(b'ELF fixture')
        (directory/'guard-runtime').chmod(0o755)
        (directory/'library.so').write_bytes(self.library)
        result = {'schema': 1, 'binary_sha256': hashlib.sha256(b'ELF fixture').hexdigest(),
                  'library_sha256': hashlib.sha256(self.library).hexdigest()}
        (directory/'runtime.json').write_text(json.dumps(result))
        return result

    def build(self, name, *, timestamp=1000000000):
        check_output = subprocess.check_output
        def command(args, **kwargs):
            if args[:3] == ['git', 'rev-parse', 'HEAD']:
                return 'fixture-source-revision\n'
            if args[:2] == ['git', 'show']:
                return '1000000000\n'
            return check_output(args, **kwargs)
        with patch.object(package, 'REPO', self.source), \
                patch.object(package, 'stage_runtime', side_effect=self.stage_runtime), \
                patch.object(package.subprocess, 'check_output', side_effect=command), \
                patch('gzip.time.time', return_value=timestamp), patch('builtins.print'):
            return package.build(self.directory/name, base_rescue_dir=self.base,
                                 native_binary=self.directory/'native-binary')

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'requires dpkg-deb, no root')
    def test_deb_has_no_maintainer_scripts_and_protected_ownership_modes(self):
        previous = os.umask(0o002)
        try:
            result = self.build('first')
        finally:
            os.umask(previous)
        deb = self.directory/'first'/result['package']
        control = subprocess.check_output(['dpkg-deb', '--ctrl-tarfile', str(deb)])
        with tarfile.open(fileobj=io.BytesIO(control)) as archive:
            self.assertEqual({p.name.removeprefix('./') for p in archive if p.isfile()}, {'control'})
        data = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(deb)])
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            entries = list(archive)
            self.assertTrue(all(p.uid == 0 and p.gid == 0 for p in entries))
            self.assertTrue(all(not p.mode & 0o022 for p in entries if not p.issym()))
            runtime = next(p for p in entries if p.name.endswith('/runtime/tools.tar.gz'))
            content = archive.extractfile(runtime).read()
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
            entries = list(archive)
            self.assertNotIn('etc/rescue/identity.json', {p.name for p in entries})
            self.assertNotIn('etc/rescue/enrollment.json', {p.name for p in entries})
            self.assertTrue(all(p.uid == 0 and p.gid == 0 for p in entries))
            self.assertEqual([p.name for p in entries if p.name != 'tmp' and not p.issym() and p.mode & 0o022], [])

    @unittest.skipUnless(shutil.which('dpkg-deb'), 'requires dpkg-deb, no root')
    def test_version_tracks_content_not_gzip_wall_clock_and_changes_with_library(self):
        first = self.build('first', timestamp=1000000000)
        second = self.build('second', timestamp=1000000010)
        self.assertEqual(first['administration_version'], second['administration_version'])
        self.library = b'updated library, same controller source'
        third = self.build('third', timestamp=1000000010)
        self.assertNotEqual(first['administration_version'], third['administration_version'])


class AdministrationEntryTests(unittest.TestCase):
    def call(self, files, payloads):
        root = Path('/usr/lib/ram-rescue-handler/test-release')
        manifest = json.dumps({'schema': 1, 'files': files}).encode()
        def read(path, limit):
            if path == root/'administration.json':
                return manifest
            return payloads[str(path.relative_to(root))]
        return root, read

    def test_changed_member_stops_before_import_or_exec(self):
        root, read = self.call({'guard/manage.py': '0'*64}, {'guard/manage.py': b'unverified code'})
        original_path = list(sys.path)
        with patch.object(admin_entry, 'PACKAGE_ROOT', root), \
                patch.object(admin_entry, 'trusted_bytes', side_effect=read), \
                patch.object(admin_entry.os, 'geteuid', return_value=0), \
                patch.object(sys, 'argv', ['rescue-guard-admin', 'manager', 'status']), \
                patch.object(admin_entry.runpy, 'run_path') as execute:
            with self.assertRaisesRegex(RuntimeError, 'code differs'):
                admin_entry.main()
        execute.assert_not_called()
        self.assertEqual(sys.path, original_path)

    def test_path_escape_stops_before_loading_package_member(self):
        root, read = self.call({'../guard/manage.py': '0'*64, 'guard/manage.py': '0'*64}, {})
        with patch.object(admin_entry, 'PACKAGE_ROOT', root), \
                patch.object(admin_entry, 'trusted_bytes', side_effect=read) as reads, \
                patch.object(admin_entry.os, 'geteuid', return_value=0), \
                patch.object(sys, 'argv', ['rescue-guard-admin', 'manager']), \
                patch.object(admin_entry.runpy, 'run_path') as execute:
            with self.assertRaisesRegex(RuntimeError, 'member'):
                admin_entry.main()
        self.assertEqual(reads.call_count, 1)
        execute.assert_not_called()

    def test_selected_program_must_be_in_the_verified_manifest(self):
        content = b'reviewed helper, not the selected program'
        root, read = self.call({'guard/helper.py': hashlib.sha256(content).hexdigest()},
                               {'guard/helper.py': content})
        original = list(sys.path)
        try:
            with patch.object(admin_entry, 'PACKAGE_ROOT', root), \
                    patch.object(admin_entry, 'trusted_bytes', side_effect=read), \
                    patch.object(admin_entry.os, 'geteuid', return_value=0), \
                    patch.object(sys, 'argv', ['rescue-guard-admin', 'manager']), \
                    patch.object(admin_entry.runpy, 'run_path') as execute:
                with self.assertRaises(RuntimeError):
                    admin_entry.main()
                execute.assert_not_called()
        finally:
            sys.path[:] = original

    def test_untrusted_bootstrap_permissions_stop_before_package_import(self):
        with patch.object(admin_entry, 'trusted_bytes', side_effect=RuntimeError('untrusted path')), \
                patch.object(admin_entry.os, 'geteuid', return_value=0), \
                patch.object(sys, 'argv', ['rescue-guard-admin', 'manager']), \
                patch.object(admin_entry.runpy, 'run_path') as execute:
            with self.assertRaisesRegex(RuntimeError, 'untrusted path'):
                admin_entry.main()
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
