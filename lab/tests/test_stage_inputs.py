"""Sealed image inputs and trusted paths use only disposable ordinary files."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO/'guard'))
import install
import stage_inputs
import trusted_paths


class ImageInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REPO/'lab/work')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.build = self.directory/'build'
        self.build.mkdir()
        self.project = self.directory/'project'
        (self.project/'guard').mkdir(parents=True)
        self.source = self.project/'guard/reviewed.py'
        self.source.write_text('# reviewed candidate source\n')
        self.enrollment = self.build/'enrollment.json'
        self.enrollment.write_text(json.dumps({'guard': {'kernel_release': '7.0.0-test'},
                                               'baseline': {'kernel_sha256': 'a'*64}}))
        (self.build/'initrd.img').write_bytes(b'candidate image bytes')
        self.record = {'source_sha256': {'guard/reviewed.py': install.sha256(self.source)},
                       'kernel_sha256': 'a'*64, 'base_rescue_payload_sha256': 'b'*64,
                       'enrollment_sha256': install.sha256(self.enrollment),
                       'initramfs_sha256': install.sha256(self.build/'initrd.img')}
        (self.build/'build.json').write_text(json.dumps(self.record))
        self.report = self.build/'vm_report.json'
        self.report.write_text(json.dumps({'passed': True, 'build': deepcopy(self.record)}))

    def bundle(self):
        output = self.directory/'bundle.tar'
        stage_inputs.bundle(self.build, self.enrollment, self.report, output)
        return output

    def extract(self, bundle):
        destination = self.directory/'extracted'
        destination.mkdir()
        with patch.object(install, 'PROJECT', self.project):
            stage_inputs.extract(bundle, destination)
        return destination

    def test_valid_bundle_rechecks_sources_and_writes_private_files(self):
        bundle = self.bundle()
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        output = self.extract(bundle)
        self.assertEqual({p.name for p in output.iterdir()}, set(stage_inputs.MEMBERS))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in output.iterdir()))
        self.assertEqual((output/'initrd.img').read_bytes(), b'candidate image bytes')

    def test_changed_source_release_is_rejected_after_copy(self):
        bundle = self.bundle()
        self.source.write_text('# installed different release\n')
        with self.assertRaisesRegex(RuntimeError, 'installed administration release'):
            self.extract(bundle)

    def test_failed_vm_or_different_build_sources_cannot_be_bundled(self):
        cases = [{'passed': False, 'build': self.record},
                 {'passed': True, 'build': {**self.record, 'source_sha256': {'other.py': 'c'*64}}}]
        for index, value in enumerate(cases):
            self.report.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                stage_inputs.bundle(self.build, self.enrollment, self.report,
                                    self.directory/f'rejected-{index}.tar')

    def test_privileged_install_checks_namespace_before_parsing_inputs(self):
        with patch.object(install.os, 'geteuid', return_value=0), \
                patch.object(trusted_paths, 'open_directory', side_effect=RuntimeError('untrusted candidate directory')), \
                patch.object(install, 'validate') as parse:
            with self.assertRaisesRegex(RuntimeError, 'untrusted candidate directory'):
                install.install(self.build, self.enrollment, self.report)
        parse.assert_not_called()

    def test_changed_initrd_digest_is_rejected(self):
        (self.build/'initrd.img').write_bytes(b'changed after test')
        with self.assertRaisesRegex(RuntimeError, 'initrd checksum'):
            self.bundle()

    def bad_archive(self, variant):
        path = self.directory/(variant+'.tar')
        names = list(stage_inputs.MEMBERS)
        if variant == 'duplicate':
            names[-1] = names[0]
        elif variant == 'escape':
            names[-1] = '../outside'
        elif variant == 'absolute':
            names[-1] = '/tmp/outside'
        with tarfile.open(path, 'w') as archive:
            for index, name in enumerate(names):
                member = tarfile.TarInfo(name)
                if variant == 'symlink' and index == 0:
                    member.type = tarfile.SYMTYPE
                    member.linkname = '/etc/passwd'
                elif variant == 'hardlink' and index == 0:
                    member.type = tarfile.LNKTYPE
                    member.linkname = 'enrollment.json'
                elif variant == 'oversized' and index == 0:
                    member.size = stage_inputs.MEMBERS[name]+1
                    archive.fileobj.write(member.tobuf())
                    break  # Header alone already declares an unacceptable member.
                archive.addfile(member)
        return path

    def test_duplicate_links_escape_and_oversized_members_are_rejected(self):
        for variant in ('duplicate', 'escape', 'absolute', 'symlink', 'hardlink', 'oversized'):
            destination = self.directory/('output-'+variant)
            destination.mkdir()
            with self.subTest(variant=variant), self.assertRaises((RuntimeError, tarfile.ReadError)):
                stage_inputs.extract(self.bad_archive(variant), destination)
        self.assertFalse((self.directory/'outside').exists())

    def test_header_count_is_bounded_before_accumulating_an_archive_catalog(self):
        bundle = self.directory/'many-members.tar'
        with tarfile.open(bundle, 'w') as archive:
            for index in range(200):
                archive.addfile(tarfile.TarInfo(f'excess-{index}'))
        output = self.directory/'many-output'
        output.mkdir()
        original = tarfile.TarFile.next
        observed = []
        def next_member(archive):
            value = original(archive)
            if value is not None:
                observed.append(value.name)
            return value
        with patch.object(tarfile.TarFile, 'next', next_member):
            with self.assertRaises(RuntimeError):
                stage_inputs.extract(bundle, output)
        self.assertLessEqual(len(observed), 6)

    def test_reviewed_digest_mismatch_never_extracts_and_cleans_temporary_copy(self):
        bundle = self.bundle()
        store = self.directory/'store'
        with patch.object(stage_inputs, 'STORE', store), \
                patch.object(stage_inputs.os, 'geteuid', return_value=0), \
                patch.object(stage_inputs, 'open_directory', side_effect=lambda path: os.open(path, os.O_DIRECTORY)), \
                patch.object(stage_inputs, 'extract') as extract:
            with self.assertRaisesRegex(RuntimeError, 'reviewed digest'):
                stage_inputs.stage(bundle, '0'*64)
        extract.assert_not_called()
        self.assertEqual(list(store.iterdir()), [])

    def test_source_symlink_is_not_followed_at_privileged_copy(self):
        bundle = self.bundle()
        link = self.directory/'alias.tar'
        link.symlink_to(bundle)
        with patch.object(stage_inputs, 'STORE', self.directory/'store'), \
                patch.object(stage_inputs.os, 'geteuid', return_value=0), \
                patch.object(stage_inputs, 'open_directory', side_effect=lambda path: os.open(path, os.O_DIRECTORY)), \
                patch.object(stage_inputs, 'extract') as extract:
            with self.assertRaises(OSError):
                stage_inputs.stage(link, install.sha256(bundle))
        extract.assert_not_called()


class TrustedPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REPO/'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root/'sealed'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory/'record.json'
        self.path.write_bytes(b'{"record":"original"}')
        self.path.chmod(0o600)
        self.options = {'uid': os.getuid(), 'anchor': self.root}

    def test_correct_owner_and_modes_allow_readonly_record_access(self):
        before = self.path.read_bytes()
        self.assertEqual(trusted_paths.read_trusted_json(self.path, **self.options), {'record': 'original'})
        self.assertEqual(self.path.read_bytes(), before)

    def test_group_writable_parent_is_rejected(self):
        self.directory.chmod(0o770)
        with self.assertRaisesRegex(RuntimeError, 'permissions'):
            trusted_paths.read_trusted(self.path, **self.options)

    def test_wrong_owner_policy_and_hardlinks_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'owner'):
            trusted_paths.read_trusted(self.path, uid=os.getuid()+1, anchor=self.root)
        os.link(self.path, self.directory/'alias')
        with self.assertRaisesRegex(RuntimeError, 'file type'):
            trusted_paths.read_trusted(self.path, **self.options)

    def test_symlink_component_and_record_are_rejected(self):
        alias = self.root/'alias'
        alias.symlink_to(self.directory)
        with self.assertRaises(OSError):
            trusted_paths.read_trusted(alias/self.path.name, **self.options)
        linked = self.directory/'linked'
        linked.symlink_to(self.path)
        with self.assertRaises(OSError):
            trusted_paths.read_trusted(linked, **self.options)

    def test_oversized_records_and_path_escape_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Oversized'):
            trusted_paths.read_trusted(self.path, limit=2, **self.options)
        with self.assertRaises(ValueError):
            trusted_paths.read_trusted(self.root/'../outside', **self.options)

    def test_open_descriptor_remains_bound_during_path_replacement(self):
        with trusted_paths.open_trusted(self.path, **self.options) as descriptor:
            self.path.rename(self.directory/'old')
            self.path.write_bytes(b'{"record":"replacement"}')
            self.path.chmod(0o600)
            self.assertEqual(os.read(descriptor, 4096), b'{"record":"original"}')
        self.assertEqual(trusted_paths.read_trusted_json(self.path, **self.options), {'record': 'replacement'})


if __name__ == '__main__':
    unittest.main()
