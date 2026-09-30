"""Persistent enrollments never turn stale paths into recovery authority."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BASE / 'guard/runtime'), str(BASE / 'ram-rescue-demo/src')]
import registry


def data_profile():
    identity = {'kind': 'filesystem', 'vid': '1234', 'pid': '5678',
                'usb_serial': 'test-serial', 'sectors': 32768,
                'partition_number': 1, 'partuuid': 'test-partition',
                'fs_type': 'ext4', 'fs_uuid': 'test-filesystem'}
    return {'schema': 1, 'identity': identity, 'guard': {
        'schema': 1, 'profile': 'host-data', 'map_name': 'rr-data-test',
        'map_uuid': 'RAMRESCUE-DATA-test', 'kernel_release': '7.0.0-test',
        'run_dir': '/run/ram-rescue-data/rr-data-test/state',
        'identity_path': '/run/ram-rescue-data/rr-data-test/identity.json',
        'queue_seconds': 8, 'partition_sectors': 16384, 'partition_start': 2048,
        'logical_block_size': 512,
        'layout': {key: identity[key] for key in ('kind', 'fs_type', 'fs_uuid', 'partuuid')},
        'initial_node': '/dev/sdb1', 'initial_sys_path': '/sys/devices/old/sdb/sdb1',
        'initial_diskseq': 12,
    }}


def root_profile():
    profile = data_profile()
    profile['identity'].pop('kind')
    profile['identity'].update(pv_uuid='test-pv', vg_uuid='test-vg', vg_name='portable',
                               lvs={'ubuntu': {'dm_uuid': 'LVM-test-root'}})
    profile['guard'].update(profile='host', map_name='ram-rescue-path',
                            map_uuid='RAMRESCUE-HOST-test',
                            run_dir='/run/ram-rescue-guard/state',
                            identity_path='/etc/rescue/identity.json',
                            root_lv='ubuntu', root_fs_uuid='root-fs',
                            layout=[{'segtype': 'linear', 'lv_name': 'ubuntu'}])
    return profile


class RegistryPolicyTests(unittest.TestCase):
    def test_registration_discards_boot_instances_without_mutating_original(self):
        profile = data_profile()
        original = deepcopy(profile)
        record = registry.record_from_profile(profile)
        self.assertEqual(profile, original)
        self.assertFalse(registry.INSTANCE_FIELDS.intersection(record['guard']))
        record['identity']['usb_serial'] = 'changed by caller'
        self.assertEqual(profile, original)

    def test_persistent_record_refuses_replayed_path_diskseq_and_other_runtime_fields(self):
        record = registry.record_from_profile(data_profile())
        for key, value in [('initial_node', '/dev/sdb1'), ('initial_diskseq', 12),
                           ('initial_sys_path', '/sys/devices/old'), ('owner_epoch', 'old')]:
            changed = deepcopy(record)
            changed['guard'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.validate_record(changed)

    def test_tampered_filesystem_identity_cannot_keep_old_layout(self):
        for key, value in [('fs_uuid', 'another-fs'), ('partuuid', 'another-partition'),
                           ('fs_type', 'exfat')]:
            record = registry.record_from_profile(data_profile())
            record['identity'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'layout differs'):
                registry.validate_record(record)

    def test_unsupported_filesystem_or_mismatched_backend_is_refused(self):
        for section, key, value in [('identity', 'fs_type', 'ntfs'),
                                    ('identity', 'kind', 'lvm'),
                                    ('identity', 'kind', 'automatic'),
                                    ('guard', 'profile', 'host')]:
            record = registry.record_from_profile(data_profile())
            record[section][key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, RuntimeError)):
                registry.validate_record(record)

    def test_canonical_owner_and_plausible_geometry_are_required(self):
        for key, value in [('map_name', 'ram-rescue-path'), ('map_uuid', 'mpath-vendor'),
                           ('run_dir', '/run/another-owner'), ('partition_start', -1),
                           ('partition_sectors', 32768), ('partition_sectors', True),
                           ('logical_block_size', 513), ('queue_seconds', 9),
                           ('kernel_release', '7.0\nExecStart=bad')]:
            record = registry.record_from_profile(data_profile())
            record['guard'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.validate_record(record)

    def test_root_registration_is_tracking_only_and_cannot_spawn_another_owner(self):
        record = registry.record_from_profile(root_profile())
        with patch.object(registry, 'DeviceMapper') as mapper, \
                patch.object(registry, 'FilesystemRecovery') as recovery:
            with self.assertRaisesRegex(ValueError, 'already owned'):
                registry.current_profile(record)
        recovery.assert_not_called()
        mapper.assert_not_called()

    def test_root_tracking_cannot_represent_an_arbitrary_lvm_data_owner(self):
        for key, value in [('map_name', 'rr-data-test'), ('run_dir', '/run/other/state'),
                           ('root_lv', 'not-enrolled')]:
            profile = root_profile()
            profile['guard'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.record_from_profile(profile)


class CurrentProfileTests(unittest.TestCase):
    def setUp(self):
        self.record = registry.record_from_profile(data_profile())
        self.recovery = Mock()
        self.recovery.candidate_node.return_value = '/dev/sdz1'
        self.recovery_class = patch.object(registry, 'FilesystemRecovery', return_value=self.recovery).start()
        self.environment = patch.object(registry, 'check_environment').start()
        self.mapper = patch.object(registry, 'DeviceMapper').start().return_value
        self.mapper.target_version.return_value = (1, 15, 0)
        self.check_map = patch.object(registry, 'check_map', return_value=('partition-sys', 'map-sys')).start()
        self.isolation = patch.object(registry, 'check_isolation').start()
        self.admission = patch.object(registry, 'Admission').start()
        self.credential = MagicMock()
        self.candidate = self.credential.__enter__.return_value
        self.candidate.node = '/dev/sdz1'
        self.candidate.sys_path = '/sys/devices/new/sdz/sdz1'
        self.candidate.diskseq = 99
        self.admission.return_value.verify.return_value = self.credential
        self.addCleanup(patch.stopall)

    def test_reenumeration_gets_fresh_instance_after_identity_verification(self):
        result = registry.current_profile(self.record)
        self.recovery.candidate_node.assert_called_once_with()
        self.admission.assert_called_once_with(result['guard'], self.recovery)
        self.candidate.revalidate.assert_called_once_with('registered-start')
        self.assertEqual(result['guard']['initial_diskseq'], 99)
        self.assertEqual(result['guard']['initial_node'], '/dev/sdz1')
        self.assertFalse(registry.INSTANCE_FIELDS.intersection(self.record['guard']))

    def test_wrong_disk_never_produces_runtime_profile(self):
        self.admission.return_value.verify.side_effect = RuntimeError('UUID does not match')
        with self.assertRaisesRegex(RuntimeError, 'UUID does not match'):
            registry.current_profile(self.record)
        self.credential.__enter__.assert_not_called()
        self.assertFalse(registry.INSTANCE_FIELDS.intersection(self.record['guard']))

    def test_foreign_map_is_refused_before_content_admission(self):
        self.check_map.side_effect = RuntimeError('Existing map is not the exclusive ready single-path data table')
        with self.assertRaisesRegex(RuntimeError, 'Existing map'):
            registry.current_profile(self.record)
        self.admission.assert_not_called()

    def test_original_geometry_and_layout_are_used_for_admission(self):
        original = deepcopy(self.record)
        self.admission.return_value.verify.side_effect = RuntimeError('Partition start differs from enrollment')
        with self.assertRaisesRegex(RuntimeError, 'Partition start differs'):
            registry.current_profile(self.record)
        config = self.admission.call_args.args[0]
        self.assertEqual(config, original['guard'])
        self.assertEqual(self.record, original)

    def test_absent_map_is_reported_without_provisioning_a_replacement(self):
        self.check_map.side_effect = RuntimeError('Map does not exist')
        with self.assertRaisesRegex(RuntimeError, 'does not exist'):
            registry.current_profile(self.record)

    def test_persistent_budget_is_kept(self):
        self.record['guard']['queue_seconds'] = 5
        result = registry.current_profile(self.record)
        self.assertEqual(result['guard']['queue_seconds'], 5)

    def test_fenced_runner_is_used_for_media_probes_and_isolation_checks(self):
        runner = Mock(name='fenced-runner')
        registry.current_profile(self.record, runner=runner)
        self.assertIs(self.recovery_class.call_args.kwargs['runner'], runner)
        self.assertIs(self.isolation.call_args.kwargs['runner'], runner)

    def test_mount_isolation_refusal_prevents_admission(self):
        self.isolation.side_effect = RuntimeError('Raw partition is mounted')
        with self.assertRaisesRegex(RuntimeError, 'Raw partition'):
            registry.current_profile(self.record)
        self.admission.assert_not_called()

    def test_map_change_during_verification_discards_current_profile(self):
        self.check_map.side_effect = [('partition-sys', 'map-sys'), RuntimeError('Map changed')]
        with self.assertRaisesRegex(RuntimeError, 'Map changed'):
            registry.current_profile(self.record)
        self.credential.__exit__.assert_called_once()
        self.assertFalse(registry.INSTANCE_FIELDS.intersection(self.record['guard']))

    def test_invalid_record_fails_before_candidate_discovery(self):
        self.record['guard']['initial_node'] = '/dev/old'
        with self.assertRaises(ValueError):
            registry.current_profile(self.record)
        self.recovery.candidate_node.assert_not_called()
        self.check_map.assert_not_called()


if __name__ == '__main__':
    unittest.main()
