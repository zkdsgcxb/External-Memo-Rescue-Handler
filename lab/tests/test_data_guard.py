"""Data-map adoption must not broaden the root controller's authority."""
import copy
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BASE / 'guard/runtime'), str(BASE / 'ram-rescue-demo/src')]
import data_guard
import guard_state
import path_guard


def config():
    return {'profile': 'host-data', 'map_name': 'rr-data-test',
            'map_uuid': 'RAMRESCUE-DATA-TEST', 'kernel_release': '7.0.0-test',
            'run_dir': '/run/ram-rescue-data/rr-data-test/state',
            'identity_path': '/run/ram-rescue-data/rr-data-test/identity.json',
            'queue_seconds': 8, 'partition_sectors': 2048, 'partition_start': 2048}


class ProfileTests(unittest.TestCase):
    def tearDown(self):
        path_guard.configure({})

    def test_data_profile_has_no_root_boot_flag_or_lab_hooks(self):
        path_guard.configure(config())
        self.assertFalse(guard_state.ENABLE_LAB_HOOKS)
        with patch('path_guard.os.uname', return_value=SimpleNamespace(release='7.0.0-test')), \
                patch('path_guard.Path.read_text', side_effect=AssertionError('must not read boot flags')):
            path_guard.validate_environment(config())

    def test_cannot_adopt_root_namespace_or_split_owner_directory(self):
        for key, value in [('map_name', 'ram-rescue-path'), ('map_name', 'rr-data-../x'),
                           ('map_uuid', 'RAMRESCUE-HOST-test'),
                           ('run_dir', '/run/another-owner'),
                           ('identity_path', '/etc/rescue/identity.json'),
                           ('queue_seconds', True), ('queue_seconds', 60)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                path_guard.configure({**config(), key: value})

    def test_data_budget_checks_global_policy_without_writing_it(self):
        timeout = Mock()
        timeout.read_text.return_value = '9'
        with patch('data_guard.TIMEOUT', timeout), \
                patch('data_guard.os.uname', return_value=SimpleNamespace(release='7.0.0-test')):
            with self.assertRaisesRegex(RuntimeError, 'shorter'):
                data_guard.check_environment(config())
        timeout.write_text.assert_not_called()


class MapAdoptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.partition = self.root / 'devices/usb/disk/sdb1'
        self.map = self.root / 'devices/virtual/block/dm-4'
        self.partition.mkdir(parents=True)
        (self.map / 'slaves').mkdir(parents=True)
        (self.map / 'slaves/sdb1').symlink_to(self.partition)
        (self.root / 'class/block').mkdir(parents=True)
        (self.root / 'class/block/sdb1').symlink_to(self.partition)
        (self.root / 'dev/block').mkdir(parents=True)
        (self.root / 'dev/block/252:4').symlink_to(self.map)
        self.snapshot = {'uuid': config()['map_uuid'], 'inactive': [],
                         'active': path_guard.table_targets(path_guard.table(2048, '8:17')),
                         'info': {'major': 252, 'minor': 4, 'suspended': 0,
                                  'internal_suspend': 0, 'deferred_remove': 0, 'read_only': 0}}

    def check(self, snapshot):
        def path(value):
            return self.root / value[5:] if str(value).startswith('/sys/') else Path(value)
        original_stat = os.stat
        def stat_node(value, *args, **kwargs):
            if str(value) == '/dev/sdb1':
                return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 17))
            return original_stat(value, *args, **kwargs)
        with patch('data_guard.Path', side_effect=path), patch('data_guard.os.stat', side_effect=stat_node):
            return data_guard.check_map(config(), '/dev/sdb1', Mock(snapshot=Mock(return_value=snapshot)))

    def test_ready_single_path_is_adoptable(self):
        self.assertEqual(self.check(self.snapshot), (self.partition, self.map))

    def test_rejects_wrong_backend_geometry_selector_and_missing_queue(self):
        tables = [path_guard.table(2048, '8:33'), path_guard.table(2047, '8:17'),
                  path_guard.table(2048, '8:17').replace('round-robin', 'service-time'),
                  path_guard.table(2048, '8:17').replace('3 queue_if_no_path', '2')]
        for table in tables:
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['active'] = path_guard.table_targets(table)
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                self.check(snapshot)

    def test_rejects_incomplete_or_foreign_transaction(self):
        snapshots = []
        for key in ('suspended', 'internal_suspend', 'deferred_remove', 'read_only'):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot['info'][key] = 1
            snapshots.append(snapshot)
        snapshots += [{**self.snapshot, 'inactive': self.snapshot['active']},
                      {**self.snapshot, 'uuid': 'mpath-foreign'}]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot), self.assertRaises(RuntimeError):
                self.check(snapshot)


class IsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.partition = self.root / 'devices/disk/sdb1'
        self.partition.mkdir(parents=True)
        (self.partition / 'partition').write_text('1')
        (self.partition / 'dev').write_text('8:17')
        (self.root / 'dev/block').mkdir(parents=True)
        (self.root / 'dev/block/8:17').symlink_to(self.partition)

    def isolation_fixture(self, fstab='', swaps='', mountpoint='/mnt/data'):
        mapping = self.root / 'dm'
        mapping.mkdir()
        (mapping / 'dev').write_text('252:4')
        (mapping / 'holders').mkdir()
        (self.partition / 'holders').mkdir()
        (self.partition / 'holders/dm').symlink_to(mapping)
        files = {'/proc/swaps': 'Filename Type Size Used Priority\n' + swaps,
                 '/proc/1/root/etc/fstab': fstab,
                 '/proc/1/mountinfo': f'1 0 252:4 / {mountpoint} rw - ext4 /dev/dm-4 rw\n'}
        redirected = {}
        for index, (name, text) in enumerate(files.items()):
            target = self.root / str(index)
            target.write_text(text)
            redirected[name] = target
        def path(value):
            return redirected.get(str(value), Path(value))
        identity = {'fs_uuid': 'ABCD-1234', 'partuuid': 'part-test'}
        return mapping, identity, patch('data_guard.Path', side_effect=path)

    def test_active_swapfile_on_mount_with_spaces_is_refused(self):
        mapping, identity, paths = self.isolation_fixture(
            swaps='/media/My\\040Disk/swap file 1024 0 -2\n', mountpoint='/media/My\\040Disk')
        with paths, self.assertRaisesRegex(RuntimeError, 'active swap'):
            data_guard.check_isolation('/dev/sdb1', self.partition, mapping, identity)

    def test_quoted_case_variant_uuid_is_still_a_raw_mount_conflict(self):
        mapping, identity, paths = self.isolation_fixture('UUID="abcd-1234" /mnt/data vfat defaults 0 0\n')
        with paths, self.assertRaisesRegex(RuntimeError, 'ambiguous UUID'):
            data_guard.check_isolation('/dev/sdb1', self.partition, mapping, identity)

    def test_label_conflict_uses_only_selected_partition_probe(self):
        mapping, identity, paths = self.isolation_fixture('LABEL="DATA" /mnt/data ext4 defaults 0 0\n')
        runner = Mock(return_value='LABEL=DATA\n')
        with paths, self.assertRaisesRegex(RuntimeError, 'ambiguous label'):
            data_guard.check_isolation('/dev/sdb1', self.partition, mapping, identity, runner)
        runner.assert_called_once_with(['/sbin/blkid', '-p', '-o', 'export', '/dev/sdb1'])

    def test_partition_is_leaf_without_a_slaves_directory(self):
        def path(value):
            return self.root / value[5:] if str(value).startswith('/sys/') else Path(value)
        with patch('data_guard.Path', side_effect=path):
            self.assertEqual(data_guard.physical_disks('8:17'), {self.partition.parent})

    def test_even_another_partition_of_system_disk_is_not_data_mode(self):
        entries = [['1', '0', '252:3', '/', '/workspace']]
        with patch('data_guard.mounts', return_value=entries), \
                patch('data_guard.Path.exists', return_value=True), \
                patch('data_guard.physical_disks', return_value={self.partition.parent}):
            with self.assertRaisesRegex(RuntimeError, 'system mount'):
                data_guard.check_isolation('/dev/sdb1', self.partition, self.root / 'dm', {})

    def test_raw_mounted_partition_is_not_adopted(self):
        entries = [['1', '0', '8:17', '/', '/media/test']]
        with patch('data_guard.mounts', return_value=entries):
            with self.assertRaisesRegex(RuntimeError, 'Raw partition is mounted'):
                data_guard.check_isolation('/dev/sdb1', self.partition, self.root / 'dm', {})


if __name__ == '__main__':
    unittest.main()
