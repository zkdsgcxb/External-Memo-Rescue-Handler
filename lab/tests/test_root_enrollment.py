"""Root enrollment topology checks use synthetic sysfs and no real devices."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
import enroll
from admin.dm import expected_table


class RootEnrollmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root / 'sys'
        (self.sys / 'class/block').mkdir(parents=True)
        (self.sys / 'dev/block').mkdir(parents=True)
        self.partition = self.sys / 'devices/usb/sdb/sdb1'
        self.partition.mkdir(parents=True)
        (self.partition / 'size').write_text('2048')
        (self.partition.parent / 'queue').mkdir()
        (self.partition.parent / 'queue/logical_block_size').write_text('512')
        (self.sys / 'class/block/sdb1').symlink_to(self.partition)
        self.identity = {'partuuid': 'enrolled-partition', 'lvs': {
            'ubuntu': {'dm_uuid': 'LVM-root'}, 'shared': {'dm_uuid': 'LVM-shared'},
        }}
        self.map_uuid = 'RAMRESCUE-HOST-' + hashlib.sha256(b'enrolled-partition').hexdigest()[:24]
        self.stable = self.mapping('dm-0', 'ram-rescue-path', self.map_uuid, self.partition)
        self.lvs = {
            'ubuntu': self.mapping('dm-1', 'portable-ubuntu', 'LVM-root', self.stable),
            'shared': self.mapping('dm-2', 'portable-shared', 'LVM-shared', self.stable),
        }
        (self.sys / 'dev/block/252:0').symlink_to(self.stable)
        (self.sys / 'dev/block/252:1').symlink_to(self.lvs['ubuntu'])
        self.cmdline = self.root / 'cmdline'
        self.cmdline.write_text('root=/dev/mapper/portable-ubuntu ram_rescue_guard=1 nompath')
        self.snapshot = {
            'uuid': self.map_uuid, 'inactive': [], 'active': expected_table(2048, '8:17'),
            'info': {'major': 252, 'minor': 0, 'suspended': 0, 'internal_suspend': 0,
                     'deferred_remove': 0, 'read_only': 0},
        }
        self.recovery = Mock(verify=Mock(return_value='/dev/sdb1'),
                             mapping=Mock(side_effect=lambda name: self.lvs[name]))
        self.admission = MagicMock()
        self.candidate = self.admission.verify.return_value.__enter__.return_value
        self.candidate.node = '/dev/sdb1'
        self.candidate.sys_path = str(self.partition)
        self.mapper = Mock(snapshot=Mock(side_effect=lambda _: deepcopy(self.snapshot)))
        original_stat = os.stat

        def block_stat(path, *args, **kwargs):
            if str(path) == '/dev/sdb1':
                return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 17))
            return original_stat(path, *args, **kwargs)

        def local_path(path):
            path = str(path)
            if path.startswith('/sys/'):
                return self.sys / path[5:]
            return self.cmdline if path == '/proc/cmdline' else Path(path)

        patches = [
            patch.object(enroll, 'Path', side_effect=local_path),
            patch.object(enroll.os, 'stat', side_effect=block_stat),
            patch.object(enroll, 'LVMIdentity', return_value=self.recovery),
            patch.object(enroll, 'Admission', return_value=self.admission),
            patch.object(enroll, 'layout', return_value=[{'segtype': 'linear'}]),
            patch.object(enroll, 'DeviceMapper', return_value=self.mapper),
            patch.object(enroll.subprocess, 'check_output', side_effect=self.findmnt),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def mapping(self, device, name, uuid, slave):
        result = self.sys / 'devices/virtual/block' / device
        (result / 'dm').mkdir(parents=True)
        (result / 'dm/name').write_text(name)
        (result / 'dm/uuid').write_text(uuid)
        (result / 'slaves').mkdir()
        (result / 'slaves' / slave.name).symlink_to(slave)
        (self.sys / 'class/block' / device).symlink_to(result)
        return result

    def replace_slave(self, mapping, slave):
        for path in (mapping / 'slaves').iterdir():
            path.unlink()
        (mapping / 'slaves' / slave.name).symlink_to(slave)

    def findmnt(self, command, **_):
        self.assertIn(command, [
            ['findmnt', '-nro', 'MAJ:MIN', '-T', '/'],
            ['findmnt', '-nro', 'UUID', '-T', '/'],
        ])
        return '252:1\n' if command[2] == 'MAJ:MIN' else 'root-filesystem\n'

    def test_direct_pv_remains_supported_without_querying_a_dm_path(self):
        for mapping in self.lvs.values():
            self.replace_slave(mapping, self.partition)
        self.cmdline.write_text('ordinary boot')
        profile = enroll.collect(self.identity)
        self.assertEqual(profile['guard']['root_lv'], 'ubuntu')
        self.mapper.snapshot.assert_not_called()
        self.candidate.revalidate.assert_called_once_with()

    def test_existing_protected_root_keeps_complete_final_admission(self):
        profile = enroll.collect(self.identity)
        self.assertEqual(profile['guard']['map_uuid'], self.map_uuid)
        self.assertEqual(profile['guard']['partition_sectors'], 2048)
        self.assertEqual(self.mapper.snapshot.call_count, 2)
        self.candidate.revalidate.assert_called_once_with()
        self.admission.verify.return_value.__exit__.assert_called_once()

    def test_protected_topology_requires_both_explicit_boot_flags(self):
        for flags in ('nompath', 'ram_rescue_guard=1', ''):
            self.cmdline.write_text(flags)
            with self.subTest(flags=flags), self.assertRaisesRegex(RuntimeError, 'explicit protected boot'):
                enroll.collect(self.identity)
        self.admission.verify.assert_not_called()

    def test_mixed_raw_and_protected_lvs_are_rejected(self):
        self.replace_slave(self.lvs['shared'], self.partition)
        with self.assertRaisesRegex(RuntimeError, 'do not share'):
            enroll.collect(self.identity)
        self.admission.verify.assert_not_called()

    def test_foreign_name_or_uuid_cannot_be_adopted(self):
        for key, expected in [('name', 'ram-rescue-path'), ('uuid', self.map_uuid)]:
            path = self.stable / 'dm' / key
            path.write_text('foreign')
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, 'do not share'):
                enroll.collect(self.identity)
            path.write_text(expected)
        self.admission.verify.assert_not_called()

    def test_duplicate_reserved_identity_is_rejected(self):
        self.mapping('dm-3', 'another-name', self.map_uuid, self.partition)
        with self.assertRaisesRegex(RuntimeError, 'unique reserved'):
            enroll.collect(self.identity)
        self.admission.verify.assert_not_called()

    def test_pending_suspended_readonly_or_foreign_table_is_rejected(self):
        original = deepcopy(self.snapshot)
        variants = []
        for flag in ('suspended', 'internal_suspend', 'deferred_remove', 'read_only'):
            changed = deepcopy(original)
            changed['info'][flag] = 1
            variants.append(changed)
        variants += [{**original, 'inactive': original['active']},
                     {**original, 'uuid': 'foreign'}]
        for changed in variants:
            self.snapshot = changed
            with self.subTest(snapshot=changed), self.assertRaisesRegex(RuntimeError, 'ready enrolled'):
                enroll.collect(self.identity)
        self.admission.verify.assert_not_called()

    def test_backend_geometry_and_queue_policy_must_match_the_enrolled_pv(self):
        tables = [expected_table(2048, '8:33'), expected_table(2047, '8:17'),
                  [[0, 2048, 'multipath',
                    expected_table(2048, '8:17')[0][3].replace('3 queue_if_no_path', '2')]]]
        for table in tables:
            self.snapshot['active'] = table
            with self.subTest(table=table), self.assertRaises(RuntimeError):
                enroll.collect(self.identity)
        self.admission.verify.assert_not_called()

    def test_kernel_table_and_sysfs_backing_must_agree(self):
        other = self.partition.parent / 'sdb2'
        other.mkdir()
        self.replace_slave(self.stable, other)
        with self.assertRaisesRegex(RuntimeError, 'backing partition differs'):
            enroll.collect(self.identity)
        self.admission.verify.assert_not_called()

    def test_table_changed_during_identity_verification_is_rejected(self):
        self.candidate.revalidate.side_effect = lambda: self.snapshot.update(inactive=self.snapshot['active'])
        with self.assertRaisesRegex(RuntimeError, 'ready enrolled'):
            enroll.collect(self.identity)
        self.candidate.revalidate.assert_called_once_with()
        self.admission.verify.return_value.__exit__.assert_called_once()

    def test_identity_revalidation_failure_cannot_produce_a_profile(self):
        self.candidate.revalidate.side_effect = RuntimeError('Disk instance changed')
        with self.assertRaisesRegex(RuntimeError, 'Disk instance changed'):
            enroll.collect(self.identity)
        self.assertEqual(self.mapper.snapshot.call_count, 1)
        self.admission.verify.return_value.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
