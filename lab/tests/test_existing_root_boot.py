"""Existing-root activation contracts using RAM fixtures and no host DM calls."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
sys.path.insert(0, str(BASE.parent / 'guard/runtime'))
import boot as existing_boot
import guard_state
import path_guard


class ExistingRootBootTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'run/ram-rescue-guard/state'
        self.state.mkdir(parents=True)
        self.sys = self.root / 'sys'
        (self.sys / 'class/block').mkdir(parents=True)
        (self.sys / 'dev/block').mkdir(parents=True)
        self.stable_sys = self.sys / 'devices/virtual/block/dm-9'
        self.stable_sys.mkdir(parents=True)
        (self.sys / 'dev/block/253:9').symlink_to(self.stable_sys)
        self.timeout = self.sys / 'module/dm_multipath/parameters/queue_if_no_path_timeout_secs'
        self.timeout.parent.mkdir(parents=True)
        self.timeout.write_text('0\n')
        (self.root / 'proc').mkdir()
        self.cmdline = self.root / 'proc/cmdline'
        self.cmdline.write_text('root=/dev/mapper/vg--usb-ubuntu--root ram_rescue_guard=1 nompath')
        self.identity = {'vg_name': 'vg-usb', 'vg_uuid': 'v' * 32,
                         'lvs': {'ubuntu-root': {'dm_uuid': 'LVM-' + 'v' * 32 + 'r' * 32},
                                 'shared': {'dm_uuid': 'LVM-' + 'v' * 32 + 's' * 32}}}
        self.config = {'profile': 'host', 'map_name': 'ram-rescue-path',
                       'map_uuid': 'RAMRESCUE-HOST-test', 'run_dir': str(self.state),
                       'kernel_release': '7.0.0-test', 'queue_seconds': 8,
                       'root_lv': 'ubuntu-root', 'partition_sectors': 2048,
                       'logical_block_size': 512, 'layout': [{'segtype': 'linear'}]}
        self.enrollment = {'schema': 1, 'identity': self.identity, 'guard': self.config}
        path_guard.configure({})
        self.addCleanup(path_guard.configure, {})
        self.recovery = Mock()
        self.recovery.candidate_node.return_value = '/dev/sdb3'
        self.mappings = {}
        self.recovery.mapping.side_effect = lambda name: self.mappings[name]
        self.candidate = MagicMock(node='/dev/sdb3', sys_path='/sys/devices/usb/block/sdb/sdb3',
                                   diskseq=12, partition_sectors=2048, dev=os.makedev(8, 19))
        self.candidate.__enter__.return_value = self.candidate
        self.candidate.to_dict.return_value = {'instance': {'dev': os.makedev(8, 19), 'diskseq': 12}}
        self.admission = Mock()
        self.admission.verify.return_value = self.candidate
        self.mapper = Mock()
        self.mapper.target_version.return_value = (1, 15, 0)
        self.active = path_guard.table_targets(path_guard.table(2048, '8:19'))
        self.mapper.snapshot.side_effect = lambda name: copy.deepcopy({
            'uuid': self.config['map_uuid'], 'active': self.active, 'inactive': [],
            'info': {'suspended': False}})
        self.patch('boot.Path', side_effect=self.mapped_path)
        self.patch('path_guard.Path', side_effect=self.mapped_path)
        self.patch('path_guard.os.uname', return_value=SimpleNamespace(release='7.0.0-test'))
        original_stat = os.stat
        self.patch('boot.os.stat', side_effect=lambda path, *args, **kwargs:
                   SimpleNamespace(st_rdev=os.makedev(253, 9)) if str(path) == '/dev/mapper/ram-rescue-path'
                   else original_stat(path, *args, **kwargs))
        self.patch('boot.Recovery', return_value=self.recovery)
        self.admission_constructor = self.patch('boot.Admission', return_value=self.admission)
        self.mapper_constructor = self.patch('boot.DeviceMapper', return_value=self.mapper)
        self.dm = self.patch('path_guard.dm', side_effect=self.dm_change)
        self.command = self.patch('boot.command', side_effect=self.activate_lvs)
        self.patch('boot.atomic_json', side_effect=lambda path, value:
                   guard_state.atomic_json(self.mapped_path(path), value))
        self.patch('boot.print', create=True)

    def patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def mapped_path(self, value):
        path = Path(value)
        if any(path.is_relative_to(prefix) for prefix in ('/sys', '/proc', '/run')):
            return self.root / path.relative_to('/')
        return path

    @property
    def handoff(self):
        return self.root / 'run/ram-rescue-guard/config.json'

    def assert_owned(self, fd):
        os.fstat(fd)
        with self.assertRaises(BlockingIOError):
            guard_state.Owner(self.state)

    def dm_change(self, *args, lock_fd=None):
        self.assert_owned(lock_fd)
        self.assertIn(args[0], ('create', 'mknodes'))

    def activate_lvs(self, args, *, timeout, pass_fds):
        self.assert_owned(pass_fds[0])
        self.assertEqual(args, ['/sbin/lvm', 'lvchange', '-ay', '--devices',
                               '/dev/mapper/ram-rescue-path',
                               '--config', 'activation { udev_rules=1 udev_sync=1 }',
                               'vg-usb/shared', 'vg-usb/ubuntu-root'])
        self.assertEqual(timeout, 15)
        for name in self.identity['lvs']:
            mapping = self.root / 'mappings' / name
            (mapping / 'slaves').mkdir(parents=True)
            (mapping / 'slaves/dm-9').symlink_to(self.stable_sys)
            self.mappings[name] = mapping
        return ''

    def assert_no_mapping_changes(self):
        self.dm.assert_not_called()
        self.command.assert_not_called()
        self.assertEqual(self.timeout.read_text(), '0\n')
        self.assertFalse(self.handoff.exists())

    def test_normal_boot_creates_only_stable_map_and_activates_enrolled_lvs(self):
        result = existing_boot.activate(self.enrollment)
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['create', 'mknodes'])
        creation = self.dm.call_args_list[0].args
        self.assertEqual(creation[:5], ('create', 'ram-rescue-path', '--uuid', 'RAMRESCUE-HOST-test', '--table'))
        self.assertEqual(creation[-1], path_guard.table(2048, '8:19'))
        self.command.assert_called_once()
        self.assertEqual(result['initial_node'], '/dev/sdb3')
        self.assertEqual(result['initial_diskseq'], 12)
        self.assertEqual(json.loads(self.handoff.read_text()), result)
        self.assertEqual(json.loads((self.state / 'boot.json').read_text())['phase'], 'prepared')
        self.assertEqual(self.timeout.read_text(), '10\n')
        self.assertEqual(self.candidate.revalidate.call_count, 3)
        self.candidate.__exit__.assert_called_once()
        with guard_state.Owner(self.state):
            pass

    def test_wrong_missing_or_duplicate_root_argument_never_creates_mapping(self):
        for roots in ('', 'root=UUID=some-filesystem', 'root=/dev/mapper/other-root',
                      'root=/dev/mapper/vg--usb-ubuntu--root root=/dev/mapper/vg--usb-ubuntu--root'):
            with self.subTest(roots=roots):
                self.cmdline.write_text(roots + ' ram_rescue_guard=1 nompath')
                with self.assertRaisesRegex(RuntimeError, 'root argument'):
                    existing_boot.activate(self.enrollment)
                self.assert_no_mapping_changes()
        self.mapper_constructor.assert_not_called()

    def test_kernel_and_explicit_boot_flag_gate_all_mapping_changes(self):
        self.cmdline.write_text('root=/dev/mapper/vg--usb-ubuntu--root')
        with self.assertRaisesRegex(RuntimeError, 'explicit'):
            existing_boot.activate(self.enrollment)
        self.cmdline.write_text('root=/dev/mapper/vg--usb-ubuntu--root ram_rescue_guard=1')
        self.config['kernel_release'] = 'other-kernel'
        with self.assertRaisesRegex(RuntimeError, 'kernel'):
            existing_boot.activate(self.enrollment)
        self.assert_no_mapping_changes()
        self.mapper_constructor.assert_not_called()

    def test_nonhost_bad_budget_and_unenrolled_root_are_rejected(self):
        for key, value in (('profile', 'lab'), ('queue_seconds', 61), ('root_lv', 'other')):
            original = self.config[key]
            self.config[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                existing_boot.activate(self.enrollment)
            self.config[key] = original
            self.assert_no_mapping_changes()
        self.mapper_constructor.assert_not_called()

    def test_missing_probe_interface_or_nonlinear_layout_never_creates_mapping(self):
        self.mapper.target_version.return_value = (1, 14, 0)
        with self.assertRaisesRegex(RuntimeError, 'probe interface'):
            existing_boot.activate(self.enrollment)
        self.mapper.target_version.return_value = (1, 15, 0)
        self.config['layout'] = [{'segtype': 'thin'}]
        with self.assertRaisesRegex(RuntimeError, 'linear layout'):
            existing_boot.activate(self.enrollment)
        self.assert_no_mapping_changes()

    def test_existing_target_vg_or_stable_name_blocks_new_mapping(self):
        mapping = self.sys / 'class/block/dm-0/dm'
        mapping.mkdir(parents=True)
        for name, uuid in (('unrelated-name', self.identity['lvs']['shared']['dm_uuid']),
                           ('ram-rescue-path', 'unexpected-uuid')):
            with self.subTest(name=name):
                (mapping / 'name').write_text(name)
                (mapping / 'uuid').write_text(uuid)
                with self.assertRaisesRegex(RuntimeError, 'activated before protection'):
                    existing_boot.activate(self.enrollment)
                self.assert_no_mapping_changes()
        self.admission_constructor.assert_not_called()

    def test_transaction_created_before_owner_acquisition_is_not_overwritten(self):
        original = {'phase': 'prepared', 'evidence': 'earlier-owner'}
        def acquire(run):
            guard_state.atomic_json(run / 'boot.json', original)
            return guard_state.Owner(run)
        with patch('boot.Owner', side_effect=acquire):
            with self.assertRaisesRegex(RuntimeError, 'already has a protection transaction'):
                existing_boot.activate(self.enrollment)
        self.assertEqual(json.loads((self.state / 'boot.json').read_text()), original)
        self.assert_no_mapping_changes()

    def test_admission_helpers_inherit_the_owner_fence(self):
        def verify(deadline, epoch):
            self.recovery.run(['/sbin/blkid', '-p', '/dev/sdb3'], timeout=2)
            return self.candidate
        def readonly(args, timeout, *, owner_fd):
            self.assert_owned(owner_fd)
            self.assertEqual(timeout, 2)
            return ''
        self.admission.verify.side_effect = verify
        with patch('boot.readonly', side_effect=readonly) as reader:
            existing_boot.activate(self.enrollment)
        reader.assert_called_once()

    def test_wrong_created_backend_never_activates_lvs(self):
        self.active = path_guard.table_targets(path_guard.table(2048, '8:99'))
        with self.assertRaisesRegex(RuntimeError, 'Stable map is not ready'):
            existing_boot.activate(self.enrollment)
        self.command.assert_not_called()
        self.assertFalse(self.handoff.exists())
        self.assertEqual(json.loads((self.state / 'boot.json').read_text())['phase'], 'create_intent')
        self.candidate.__exit__.assert_called_once()

    def test_wrong_lv_dependency_refuses_handoff(self):
        activate = self.activate_lvs
        def wrong_topology(*args, **kwargs):
            activate(*args, **kwargs)
            slave = self.mappings['ubuntu-root'] / 'slaves/dm-9'
            slave.unlink()
            slave.symlink_to(self.sys / 'devices/raw-usb-partition')
        self.command.side_effect = wrong_topology
        with self.assertRaisesRegex(RuntimeError, 'solely on the stable map'):
            existing_boot.activate(self.enrollment)
        self.assertFalse(self.handoff.exists())
        self.assertEqual(json.loads((self.state / 'boot.json').read_text())['phase'], 'activate_intent')
        self.candidate.__exit__.assert_called_once()

    def test_changed_candidate_after_activation_refuses_handoff(self):
        self.candidate.revalidate.side_effect = [None, None, RuntimeError('new disk instance')]
        with self.assertRaisesRegex(RuntimeError, 'new disk instance'):
            existing_boot.activate(self.enrollment)
        self.assertFalse(self.handoff.exists())
        self.candidate.__exit__.assert_called_once()


if __name__ == '__main__':
    unittest.main()
