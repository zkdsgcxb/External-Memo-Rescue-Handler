"""Persistent enrollments never turn stale paths into recovery authority."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BASE / 'guard'), str(BASE / 'ram-rescue-demo/src')]
from admin import registry


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

    def test_root_tracking_cannot_represent_an_arbitrary_lvm_data_owner(self):
        for key, value in [('map_name', 'rr-data-test'), ('run_dir', '/run/other/state'),
                           ('root_lv', 'not-enrolled')]:
            profile = root_profile()
            profile['guard'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                registry.record_from_profile(profile)


if __name__ == '__main__':
    unittest.main()
