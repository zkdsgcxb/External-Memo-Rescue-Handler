"""Current identity failures stay blocked; real success is separately tested in VM."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BASE / 'guard'), str(BASE / 'ram-rescue-demo/src')]
import admin_entry
import current_support as current
import diagnostics as doctor
import support
import trusted_paths


def note(identity=b'x' * 20):
    return struct.pack('<III', 4, len(identity), 3) + b'GNU\0' + identity


class CurrentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.reader = doctor.Reader(self.root, os.geteuid())
        self.release = '7.0.0-fixture'
        self.base = current.KERNELS + '/' + self.release
        self.write(current.KEYRING, b'fixture keyring')
        self.write(self.base + '/InRelease', b'fixture signed source')
        self.write(self.base + '/Packages', b'fixture packages')
        self.write('/boot/vmlinuz-' + self.release, b'fixture image')
        self.write('/sys/kernel/notes', note())
        modules = {}
        builtin = []
        for name in current.ROOT_MODULES:
            if name in ('dm_multipath', 'dm_round_robin', 'usb_storage', 'uas'):
                path = '/usr/lib/modules/' + self.release + '/kernel/' + name + '.ko.zst'
                self.write(path, name.encode())
                self.write('/sys/module/' + name + '/initstate', b'live\n')
                self.write('/sys/module/' + name + '/notes/.note.gnu.build-id', note())
                self.write('/sys/module/' + name + '/srcversion', b'src\n')
                modules[name] = {'builtin': False, 'path': path, 'sha256': current.sha(name.encode()),
                                 'build_id': (b'x' * 20).hex(), 'srcversion': 'src', 'depends': []}
            else:
                builtin.append('kernel/' + name + '.ko')
                modules[name] = {'builtin': True, 'path': None, 'sha256': None, 'build_id': None,
                                 'srcversion': None, 'depends': []}
        metadata = {'modules.builtin': ('\n'.join(builtin) + '\n').encode(), 'modules.builtin.modinfo': b'fixture metadata'}
        for name, data in metadata.items():
            self.write('/usr/lib/modules/' + self.release + '/' + name, data)
        self.source = {'schema': 1, 'release': self.release, 'image': {'sha256': current.sha(b'fixture image'), 'build_id': (b'x' * 20).hex()},
                       'modules': modules, 'metadata': {name: current.sha(data) for name, data in metadata.items()},
                       'packages': {}, 'origin': {'inrelease_sha256': current.sha(b'fixture signed source'),
                                                  'packages_sha256': current.sha(b'fixture packages'),
                                                  'keyring_sha256': current.sha(b'fixture keyring')}, 'generator_sha256': {}}
        self.save()
        self.signed = patch.object(current, 'authenticate_index', return_value=(self.source['origin']['inrelease_sha256'], self.source['origin']['packages_sha256']))
        self.rows = patch.object(current, 'package_rows', return_value={})
        self.signed.start(); self.rows.start()
        self.addCleanup(self.signed.stop); self.addCleanup(self.rows.stop)

    def write(self, name, value):
        path = self.root / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        for parent in path.parents:
            if parent == self.root:
                break
            parent.chmod(0o700)
        path.write_bytes(value)
        path.chmod(0o600)
        return path

    def save(self):
        self.write(self.base + '/source.json', json.dumps(self.source).encode())

    def kernel(self):
        return current.kernel_evidence(self.reader, self.release)

    def test_runtime_notes_and_disk_artifacts_both_match(self):
        kernel, observed = self.kernel()
        self.assertEqual(kernel['image_sha256'], self.source['image']['sha256'])
        self.assertEqual(observed['modules']['dm_mod'], 'builtin_in_verified_image')
        self.assertEqual(observed['modules']['dm_multipath'], 'loaded_identity_matches')

    def test_changed_running_kernel_is_rejected_despite_unchanged_disk_image(self):
        self.write('/sys/kernel/notes', note(b'y' * 20))
        with self.assertRaisesRegex(ValueError, 'running_kernel_build_id_mismatch'):
            self.kernel()

    def test_changed_image_or_module_on_disk_is_rejected(self):
        for name in ('/boot/vmlinuz-' + self.release, self.source['modules']['usb_storage']['path']):
            with self.subTest(name=name):
                path = self.root / name.lstrip('/')
                original = path.read_bytes()
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    self.kernel()
                path.write_bytes(original)

    def test_sysfs_page_size_hint_does_not_reject_short_live_values(self):
        original = os.fstat
        def sysfs_stat(fd):
            info = original(fd)
            path = os.readlink('/proc/self/fd/' + str(fd))
            if path.endswith(('/initstate', '/srcversion')):
                values = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
                values['st_size'] = 4096
                return SimpleNamespace(**values)
            return info
        with patch('os.fstat', side_effect=sysfs_stat):
            self.assertEqual(self.kernel()[1]['modules']['dm_multipath'], 'loaded_identity_matches')

    def test_loaded_module_build_id_and_liveness_are_checked(self):
        for suffix, data, reason in (('notes/.note.gnu.build-id', note(b'z' * 20), 'build_id'),
                                    ('initstate', b'coming', 'not_live'), ('srcversion', b'wrong', 'srcversion')):
            with self.subTest(suffix=suffix):
                path = self.root / ('sys/module/dm_multipath/' + suffix)
                original = path.read_bytes()
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, reason):
                    self.kernel()
                path.write_bytes(original)

    def test_missing_required_module_and_uncovered_livepatch_are_rejected(self):
        path = self.root / 'sys/module/dm_multipath'
        path.rename(path.with_name('hidden'))
        with self.assertRaisesRegex(ValueError, 'required_module_not_loaded'):
            self.kernel()
        path.with_name('hidden').rename(path)
        self.write('/sys/kernel/livepatch/patch/enabled', b'1')
        with self.assertRaisesRegex(ValueError, 'livepatch_not_covered'):
            self.kernel()

    def test_optional_unloaded_module_is_not_reported_as_loaded(self):
        path = self.root / 'sys/module/uas'
        path.rename(path.with_name('hidden_uas'))
        self.assertEqual(self.kernel()[1]['modules']['uas'], 'not_loaded')

    def test_builtin_claim_must_match_authenticated_metadata(self):
        record = self.source['modules']['dm_multipath']
        record.update(builtin=True, path=None, sha256=None, build_id=None)
        self.save()
        with self.assertRaisesRegex(ValueError, 'builtin_classification'):
            self.kernel()

    def test_reference_source_material_change_is_rejected(self):
        with patch.object(current, 'authenticate_index', return_value=('f' * 64, 'f' * 64)), \
                self.assertRaisesRegex(ValueError, 'source_material_changed'):
            self.kernel()
        self.write(current.KEYRING, b'changed keyring')
        with self.assertRaisesRegex(ValueError, 'keyring_changed'):
            self.kernel()

    def test_build_id_parser_rejects_truncated_duplicate_and_absent_notes(self):
        self.assertEqual(current.build_id(note()), (b'x' * 20).hex())
        for data in (note()[:-1], note() + note(), note(b'x' * 19), b'', b'bad'):
            with self.subTest(data=data[:12]), self.assertRaises(ValueError):
                current.build_id(data)

    def test_reference_elf_must_be_amd64(self):
        raw = bytearray(64)
        raw[:6] = b'\x7fELF\x02\x01'
        struct.pack_into('<H', raw, 18, 183)
        with self.assertRaisesRegex(ValueError, 'expected_amd64_elf'):
            current.elf_build_id(raw)

    def test_official_ubuntu_ga_source_is_not_hwe(self):
        self.rows.stop()
        content = ('Package: linux-image-' + self.release + '\nArchitecture: amd64\nSource: linux\n').encode()
        with self.assertRaisesRegex(ValueError, 'not_official_hwe_source'):
            current.package_rows(content, self.release)

    def test_subject_not_emitted_when_any_current_binding_is_missing(self):
        with patch.object(current, '_ENTRY', None):
            result = current.collect(self.reader)
        self.assertIsNone(result['subject'])
        self.assertEqual(set(result['bindings'].values()), {'unknown'})

    def test_origin_signature_failure_cannot_be_treated_as_valid(self):
        self.signed.stop()
        with patch.object(current.subprocess, 'run', return_value=Mock(returncode=1, stdout=b'', stderr=b'bad signature')), \
                self.assertRaisesRegex(ValueError, 'signature_failed'):
            current.authenticate_index(b'bad signed index', b'packages')

    def test_signed_index_must_cover_exact_packages_bytes(self):
        self.signed.stop()
        data = b'actual Packages'
        release = ('Origin: Ubuntu\nCodename: noble\nSHA256:\n ' + current.sha(data) + ' ' + str(len(data)) + ' main/binary-amd64/Packages\n').encode()
        with patch.object(current.subprocess, 'run', return_value=Mock(returncode=0, stdout=release)):
            self.assertEqual(current.authenticate_index(b'signed', data), (current.sha(b'signed'), current.sha(data)))
            with self.assertRaisesRegex(ValueError, 'digest_mismatch'):
                current.authenticate_index(b'signed', b'wrong')

    def test_actual_fixed_entry_is_rendered_verified_source_and_passed_in_process(self):
        package_root = Path('/usr/lib/ram-rescue-handler/test')
        bootstrap = b"PACKAGE_ROOT = '/usr/lib/ram-rescue-handler/PACKAGE_VERSION'\n"
        files = {'guard/admin_entry.py': bootstrap, 'guard/manage.py': b'pass\n'}
        manifest = json.dumps({'schema': 1, 'files': {name: current.sha(data) for name, data in files.items()}}).encode()
        rendered = bootstrap.replace(b'/usr/lib/ram-rescue-handler/PACKAGE_VERSION', str(package_root).encode())
        def read(path, limit):
            if str(path) == '/usr/bin/rescue-guard-admin':
                return rendered
            if path.name == 'administration.json':
                return manifest
            return files[str(path.relative_to(package_root))]
        original = list(sys.path)
        try:
            with patch.object(admin_entry, 'PACKAGE_ROOT', package_root), patch.object(admin_entry, 'trusted_bytes', side_effect=read), \
                    patch.object(admin_entry.os, 'geteuid', return_value=0), patch.object(sys, 'argv', ['rescue-guard-admin', 'manager', 'plan']), \
                    patch.object(current, '_ENTRY', None), patch.object(admin_entry.runpy, 'run_path') as execute:
                admin_entry.main()
                self.assertEqual(current._ENTRY['manifest_sha256'], current.sha(manifest))
                self.assertEqual(current._ENTRY['entrypoint_sha256'], current.sha(rendered))
                execute.assert_called_once()
        finally:
            sys.path[:] = original

    def test_runtime_archive_member_tampering_is_rejected_without_extraction(self):
        root = self.root / 'package'
        payload = root / 'runtime'
        payload.mkdir(parents=True)
        contents = {'/opt/guard-runtime/guard-runtime': b'elf', '/opt/guard-runtime/maintain': b'entry', '/lib/library.so': b'lib'}
        native = {'runtime': 'cpp', 'binary_path': '/opt/guard-runtime/guard-runtime', 'binary_sha256': current.sha(b'elf'),
                  'entrypoint_path': '/opt/guard-runtime/maintain', 'entrypoint_sha256': current.sha(b'entry'),
                  'library_sha256': {'/lib/library.so': current.sha(b'lib')}}
        manifest = {'native_runtime': native, 'binary_sha256': native['binary_sha256'], 'archive_sha256': 'a' * 64}
        def archive(tamper):
            with tarfile.open(payload / 'tools.tar.gz', 'w:gz') as stream:
                for name, content in contents.items():
                    content = b'changed' if tamper and name.endswith('library.so') else content
                    info = tarfile.TarInfo(name.lstrip('/')); info.size = len(content)
                    stream.addfile(info, io.BytesIO(content))
                data = json.dumps(native).encode()
                info = tarfile.TarInfo('opt/guard-runtime/runtime.json'); info.size = len(data)
                stream.addfile(info, io.BytesIO(data))
        fake_reader = Mock()
        fake_reader.read.return_value = json.dumps(manifest).encode()
        def trusted(path):
            return trusted_paths.open_trusted(path, uid=os.geteuid(), anchor=self.root)
        root.chmod(0o700); payload.chmod(0o700)
        for tamper in (False, True):
            archive(tamper); (payload / 'tools.tar.gz').chmod(0o600)
            with patch.object(current.doctor, 'Reader', return_value=fake_reader), \
                    patch.object(current.ram_environment, 'package_manifest', return_value=manifest), \
                    patch.object(current, 'open_trusted', side_effect=trusted):
                if tamper:
                    with self.assertRaisesRegex(ValueError, 'runtime_content_mismatch'):
                        current.runtime_evidence(root)
                else:
                    self.assertEqual(current.runtime_evidence(root)['binary_sha256'], native['binary_sha256'])
            self.assertEqual(set(payload.iterdir()), {payload / 'tools.tar.gz'})


if __name__ == '__main__':
    unittest.main()
