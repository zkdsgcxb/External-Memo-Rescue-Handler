"""Read-only static qualification fixtures, not kernel/package acceptance."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
sys.path.insert(0, str(BASE / 'ram-rescue-demo/src'))
import diagnostics as doctor
import manage
import support
import support_fixtures as fixtures
from admin.admission import digest


class SupportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = doctor.Reader(self.root, os.geteuid())
        self.current = fixtures.current()
        self.path = fixtures.write(self.root, self.current)

    def snapshot(self):
        return support.sample(current=lambda: deepcopy(self.current), reader=self.reader)

    def policy(self, **scope):
        snapshot = self.snapshot()
        return support.evaluate(snapshot, snapshot, scope.pop('filesystem', 'ext4'), **scope)['combination']

    def replace(self, record):
        self.path.write_text(json.dumps(record))

    def test_valid_record_only_authorizes_candidate_preparation(self):
        policy = self.policy()
        self.assertEqual(policy['state'], support.ELIGIBLE)
        self.assertEqual(policy['authorization'], 'save_candidate_only')
        self.assertEqual(policy['release_acceptance'], 'not_evaluated')
        self.assertEqual(policy['recovery_acceptance'], 'not_evaluated')
        self.assertEqual(policy['publisher_authentication'], 'not_established_by_local_ownership')
        self.assertEqual(policy['qualification']['sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_missing_record_never_falls_back_to_uname_or_hwe(self):
        self.path.unlink()
        policy = self.policy()
        self.assertEqual(policy['qualification']['state'], 'absent')
        self.assertEqual(policy['state'], 'unvalidated')
        self.assertIsNone(policy['authorization'])

    def test_every_current_binding_remains_required_with_matching_qualification(self):
        for name in support.BINDINGS:
            for state in ('unknown', 'fail'):
                with self.subTest(binding=name, state=state):
                    self.current['bindings'][name] = state
                    policy = self.policy()
                    self.assertEqual(policy['qualification']['state'], 'valid')
                    self.assertEqual(policy['state'], 'unvalidated')
                    self.current['bindings'][name] = 'pass'

    def test_default_collector_requires_verified_in_process_entry(self):
        import current_support
        with patch.object(current_support, '_ENTRY', None):
            current = support.current_evidence(self.reader)
        self.assertIsNone(current['subject'])
        self.assertEqual(set(current['bindings'].values()), {'unknown'})
        self.assertEqual(current['observations']['executing_management_error'], 'not_called_by_verified_entry')

    def test_external_marker_cannot_supply_bootstrap_binding(self):
        import current_support
        with patch.object(current_support, '_ENTRY', None), patch.dict(os.environ, {'RESCUE_VERIFIED': '1'}):
            current = support.current_evidence(self.reader)
        self.assertIsNone(current['subject'])
        self.assertEqual(current['bindings']['executing_management'], 'unknown')

    def test_architecture_aliases_have_identical_subject_hash(self):
        before = self.snapshot()
        self.current['subject']['platform']['architecture'] = 'x86_64'
        after = self.snapshot()
        self.assertEqual(before, after)
        self.assertEqual(after['subject']['platform']['architecture'], 'amd64')

    def test_other_platforms_and_architectures_are_rejected(self):
        for key, value in (('architecture', 'aarch64'), ('architecture', 'arm64'), ('id', 'debian'),
                           ('version_id', '26.04')):
            with self.subTest(key=key, value=value):
                original = self.current['subject']['platform'][key]
                self.current['subject']['platform'][key] = value
                self.assertIn('current_evidence_invalid', self.snapshot()['issues'])
                self.assertEqual(self.policy()['state'], 'unvalidated')
                self.current['subject']['platform'][key] = original

    def test_dynamic_fields_are_rejected_from_static_subject(self):
        for name in ('identity', 'boot_id', 'diskseq', 'mounts', 'fstab', 'swap', 'holders', 'configuration',
                     'registry', 'receipt', 'package_sha256', 'qualification_sha256', 'report_sha256'):
            with self.subTest(name=name):
                self.current['subject'][name] = 'local or circular input'
                self.assertIn('current_evidence_invalid', self.snapshot()['issues'])
                del self.current['subject'][name]
        self.current['subject']['kernel']['boot_id'] = 'nested-dynamic-input'
        self.assertIn('current_evidence_invalid', self.snapshot()['issues'])

    def test_all_kernel_admin_runtime_dependency_identities_change_subject(self):
        before = self.snapshot()['subject_sha256']
        for part in ('kernel', 'administration', 'runtime', 'host_dependencies'):
            for field in self.current['subject'][part]:
                with self.subTest(part=part, field=field):
                    original = self.current['subject'][part][field]
                    self.current['subject'][part][field] = '7.0.0-other' if field == 'release' else 'f' * 64
                    snapshot = self.snapshot()
                    self.assertNotEqual(before, snapshot['subject_sha256'])
                    self.assertEqual(snapshot['qualification']['state'], 'absent')
                    self.assertEqual(self.policy()['state'], 'unvalidated')
                    self.current['subject'][part][field] = original

    def test_filesystem_profile_and_topology_must_match_explicit_scope(self):
        for scope in ({'filesystem': 'exfat'}, {'filesystem': 'btrfs'}, {'filesystem': None},
                      {'profile': 'host-root'}, {'topology': 'raw_partition'}, {'topology': 'lvm'}):
            with self.subTest(scope=scope):
                self.assertIn('qualification_scope_mismatch', self.policy(**scope)['reasons'])
        record = fixtures.qualification(self.current, filesystems=('ext4', 'exfat'))
        self.replace(record)
        self.assertEqual(self.policy(filesystem='exfat')['state'], support.ELIGIBLE)

    def test_unknown_fields_at_every_qualification_level_are_rejected(self):
        for level in ('root', 'scope', 'evidence'):
            with self.subTest(level=level):
                record = fixtures.qualification(self.current)
                (record if level == 'root' else record[level])['unexpected'] = True
                self.replace(record)
                self.assertEqual(self.snapshot()['qualification']['state'], 'invalid')

    def test_scope_wildcards_root_activation_and_unpassed_results_are_invalid(self):
        mutations = [('purpose', 'activation'), ('purpose', 'root_protection'), ('purpose', 'recovery'),
                     ('purpose', 'release'), ('result', 'pending'), ('result', 'failed'), ('schema', True)]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                record = fixtures.qualification(self.current)
                record[key] = value
                self.replace(record)
                self.assertEqual(self.snapshot()['qualification']['state'], 'invalid')
        for key, value in (('profile', 'host-root'), ('topology', '*'), ('filesystems', ['*']),
                           ('filesystems', []), ('filesystems', ['ext4', 'ext4']), ('filesystems', [None])):
            with self.subTest(key=key, value=value):
                record = fixtures.qualification(self.current)
                record['scope'][key] = value
                self.replace(record)
                self.assertEqual(self.snapshot()['qualification']['state'], 'invalid')
        record = fixtures.qualification(self.current)
        del record['result']
        self.replace(record)
        self.assertEqual(self.snapshot()['qualification']['state'], 'invalid')

    def test_subject_mismatch_even_in_correct_filename_is_invalid(self):
        record = fixtures.qualification(self.current)
        record['subject_sha256'] = 'f' * 64
        self.replace(record)
        self.assertEqual(self.snapshot()['qualification']['state'], 'invalid')

    def test_evidence_references_are_required_and_valid_sha256(self):
        for name in fixtures.qualification(self.current)['evidence']:
            for value in (None, '../report.json', 'A' * 64, ''):
                with self.subTest(name=name, value=value):
                    record = fixtures.qualification(self.current)
                    record['evidence'][name] = value
                    self.replace(record)
                    self.assertEqual(self.snapshot()['qualification']['state'], 'invalid')

    def test_invalid_json_duplicate_fields_and_nonfinite_values_are_rejected(self):
        for raw in (b'{', b'[]', b'null', b'\xff', b'{"schema":1,"schema":1}', b'{"schema":NaN}',
                    b'{"schema":Infinity}', b'[' * 1100 + b']' * 1100):
            with self.subTest(raw=raw[:40]):
                self.path.write_bytes(raw)
                snapshot = self.snapshot()
                self.assertEqual(snapshot['qualification']['state'], 'invalid')
                self.assertEqual(snapshot['qualification']['sha256'], hashlib.sha256(raw).hexdigest())

    def test_size_limit_rejects_before_unbounded_parsing(self):
        self.path.write_bytes(b' ' * (support.QUALIFICATION_LIMIT + 1))
        self.assertEqual(self.snapshot()['qualification']['state'], 'unreadable')

    def test_leaf_symlink_and_hardlink_are_rejected(self):
        original = self.path.read_bytes()
        target = self.root / 'external.json'
        target.write_bytes(original)
        for kind in ('symbolic', 'hard'):
            with self.subTest(kind=kind):
                self.path.unlink()
                if kind == 'symbolic':
                    self.path.symlink_to(target)
                else:
                    os.link(target, self.path)
                self.assertEqual(self.snapshot()['qualification']['state'], 'unreadable')
                self.path.unlink()
                self.path.write_bytes(original)

    def test_parent_symlink_is_rejected(self):
        parent = self.path.parent
        relocated = self.root / 'redirected'
        parent.rename(relocated)
        parent.symlink_to(relocated, target_is_directory=True)
        self.assertEqual(self.snapshot()['qualification']['state'], 'unreadable')

    def test_wrong_owner_and_writable_leaf_or_parent_are_rejected(self):
        wrong = doctor.Reader(self.root, os.geteuid() + 1)
        self.assertEqual(support.load_qualification(digest(support.project(self.current['subject'])), wrong)['state'], 'unreadable')
        for path in (self.path, self.path.parent):
            old_mode = path.stat().st_mode & 0o777
            for mode in (0o666, 0o777):
                with self.subTest(path=path.name, mode=mode):
                    path.chmod(mode)
                    self.assertEqual(self.snapshot()['qualification']['state'], 'unreadable')
            path.chmod(old_mode)

    def test_nonregular_file_is_rejected_without_blocking(self):
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual(self.snapshot()['qualification']['state'], 'unreadable')

    def test_malformed_current_evidence_fails_closed(self):
        original = deepcopy(self.current)
        for mutate in (lambda: self.current.update(unexpected=True),
                       lambda: self.current['bindings'].update(unexpected='pass'),
                       lambda: self.current['bindings'].update(running_kernel=True),
                       lambda: self.current.update(observations='not-an-object'),
                       lambda: self.current['observations'].update(too_big='x' * support.CURRENT_LIMIT),
                       lambda: self.current['subject']['kernel'].update(image_sha256='not-a-hash')):
            self.current = deepcopy(original)
            mutate()
            self.assertIn('current_evidence_invalid', self.snapshot()['issues'])
            self.assertEqual(self.policy()['state'], 'unvalidated')

    def test_loader_only_reads_fixed_trusted_files_and_never_calls_subprocess(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        original = os.open
        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC))
            self.assertFalse(str(path).startswith('/dev/'))
            return original(path, flags, *args, **kwargs)
        with patch('os.open', side_effect=readonly), \
                patch.object(subprocess, 'Popen', side_effect=AssertionError('process/service/device mutation')):
            self.assertEqual(self.policy()['state'], support.ELIGIBLE)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_traversal_subject_hash_is_never_a_path_option(self):
        for value in ('../external', '/tmp/file', 'a' * 63, 'A' * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                support.load_qualification(value, self.reader)

    def test_cli_has_no_qualification_path_or_force_override(self):
        from contextlib import redirect_stderr
        import io
        for extra in (['--qualification', '/tmp/fake.json'], ['--force'], ['--current-support', '/tmp/fake.json']):
            with self.subTest(extra=extra), patch.object(sys, 'argv', ['manage.py', 'plan', '--device', '/data', *extra]), \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                manage.main()
            self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
