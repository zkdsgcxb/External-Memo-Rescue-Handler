"""Plain filesystem admission without mounting or accessing a real disk."""
import unittest

try:
    from .test_admission import AdmissionFixture
except ImportError:
    from test_admission import AdmissionFixture
from admin import admission
from admin.identity import FILESYSTEM_TYPES, FilesystemIdentity, LVMIdentity
from rescue import Refuse


class FilesystemAdmissionTests(AdmissionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.identity = {key: self.identity[key] for key in
                         ('vid', 'pid', 'usb_serial', 'sectors', 'partition_number', 'partuuid')}
        self.identity.update(kind='filesystem', fs_type='ext4', fs_uuid='filesystem-id')
        self.props = {'TYPE': 'ext4', 'UUID': 'filesystem-id',
                      'PART_ENTRY_UUID': 'partition-id', 'LABEL': 'mutable label'}
        self.recovery = FilesystemIdentity(self.identity, self.sys, self.dev, self.runner)
        self.config['layout'] = {'kind': 'filesystem', 'fs_type': 'ext4',
                                 'fs_uuid': 'filesystem-id', 'partuuid': 'partition-id'}
        self.config['partition_start'] = 2048
        self.policy = admission.Admission(self.config, self.recovery,
                                         clock=lambda: self.clock)

    def test_success_and_commit_only_probe_the_enrolled_partition(self):
        with self.verify() as candidate:
            self.assertEqual(candidate.diskseq, 45)
            self.assertIs(candidate.revalidate(), candidate)
        self.assertTrue(self.calls)
        self.assertTrue(all(call == ['/sbin/blkid', '-p', '-o', 'export', self.node]
                            for call in self.calls))
        self.assertEqual(len(self.closed), 1)

    def test_other_supported_filesystems_use_the_same_identity_policy(self):
        for filesystem in FILESYSTEM_TYPES:
            with self.subTest(filesystem=filesystem):
                identity = {**self.identity, 'fs_type': filesystem}
                self.props['TYPE'] = filesystem
                recovery = FilesystemIdentity(identity, self.sys, self.dev, self.runner)
                observed = admission.identity_layout(recovery, self.node)
                self.assertEqual(observed['fs_type'], filesystem)
                self.assertEqual(recovery.verify(), self.node)

    def test_filesystem_partition_and_type_mismatches_fail_closed(self):
        for key in ('TYPE', 'UUID', 'PART_ENTRY_UUID'):
            with self.subTest(key=key):
                original = self.props[key]
                self.props[key] = 'foreign-disk'
                with self.assertRaisesRegex(Refuse, key):
                    self.verify()
                self.props[key] = original
        self.assertEqual(len(self.closed), 3)

    def test_final_revalidation_detects_reformatted_filesystem(self):
        with self.verify() as candidate:
            self.props['UUID'] = 'replacement-filesystem'
            with self.assertRaisesRegex(Refuse, 'UUID'):
                candidate.revalidate()

    def test_label_changes_do_not_invalidate_filesystem_identity(self):
        with self.verify() as candidate:
            self.props['LABEL'] = 'new user label'
            candidate.revalidate()

    def test_reused_device_number_cannot_override_held_fd(self):
        with self.verify() as candidate:
            (self.disk_path / 'diskseq').write_text('46')
            before = list(self.calls)
            with self.assertRaisesRegex(admission.AdmissionError, 'disk instance'):
                candidate.revalidate()
            self.assertEqual(self.calls, before)

    def test_duplicate_serial_prevents_all_media_probes(self):
        self.disk('sdc')
        with self.assertRaisesRegex(Refuse, 'ONE'):
            self.verify()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.open_flags, [])

    def test_moved_partition_is_rejected_before_reading_filesystem(self):
        (self.block / 'start').write_text('4096')
        with self.assertRaisesRegex(admission.AdmissionError, 'start differs'):
            self.verify()
        self.assertEqual(self.calls, [])

    def test_partition_start_is_bound_to_enrollment_credential(self):
        with self.verify() as candidate:
            self.policy.partition_start = 4096
            before = list(self.calls)
            with self.assertRaisesRegex(admission.AdmissionError, 'Enrollment changed'):
                candidate.revalidate()
            self.assertEqual(self.calls, before)

    def test_changed_enrolled_layout_is_rejected(self):
        self.config['layout']['partuuid'] = 'stale-enrollment'
        policy = admission.Admission(self.config, self.recovery,
                                     clock=lambda: self.clock)
        with self.assertRaisesRegex(admission.AdmissionError, 'layout'):
            policy.verify(106.)

    def test_readonly_identity_facades_do_not_expose_manual_refresh(self):
        for identity in (self.recovery, LVMIdentity({'pv_uuid': 'legacy'})):
            self.assertFalse(hasattr(identity, 'refresh'))

    def test_unsupported_or_incomplete_policy_refuses_without_commands(self):
        for changes in ({'kind': 'anything'}, {'fs_type': 'crypto_LUKS'},
                        {'fs_type': 'ntfs'}, {'fs_type': []}, {'fs_uuid': ''}, {'partuuid': None},
                        {'usb_serial': ' '}, {'partition_number': 0},
                        {'partition_number': True}, {'sectors': -1}, {'vid': '12345'}):
            with self.subTest(changes=changes), self.assertRaises(Refuse):
                FilesystemIdentity({**self.identity, **changes}, self.sys, self.dev, self.runner)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
