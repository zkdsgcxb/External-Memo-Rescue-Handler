"""Recovery completion requires the current table, path instance and deadline."""
import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
sys.path.insert(0, str(BASE / 'guest'))
import path_guard


class FakeProbe:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.result = None

    def start(self, device, token):
        if self.busy:
            raise RuntimeError('Probe already outstanding')
        self.calls.append((device, token))
        self.busy = True

    def complete(self, error=0, token=None):
        self.result = {
            'token': self.calls[-1][1] if token is None else token,
            'errno': error,
            'elapsed': .01,
            'status': {0: 'completed', errno.ENOTCONN: 'no_paths'}.get(error, 'error'),
        }

    def poll(self):
        if self.result is None:
            return None
        result, self.result = self.result, None
        self.busy = False
        return result


class GuardProbeTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root / 'sys'
        self.dev = self.root / 'dev'
        self.dev.mkdir()
        (self.sys / 'class/block').mkdir(parents=True)
        self.old_node, self.old_sys = self.disk('sda3', 'old')
        self.node, self.node_sys = self.disk('sdb3', 'new')
        self.clock = 100.
        self.events = []
        self.probe = FakeProbe()
        self.mapper = Mock()
        self.mapper.target_version.return_value = (1, 15, 0)
        # Regular files stand in for nodes, hence st_rdev=0 (0:0) in this fixture.
        self.set_status('0 0 0 1 1 A 0 1 0 0:0 A 0')
        self.recovery = Mock()
        self.recovery.candidate_node.return_value = str(self.node)
        self.recovery.verify.return_value = str(self.node)
        self.dm = self.start_patch('path_guard.dm', return_value='')
        self.layout = self.start_patch('path_guard.layout', return_value=[])
        self.start_patch('path_guard.DeviceMapper', return_value=self.mapper)
        self.start_patch('path_guard.PathProbe', return_value=self.probe)
        self.start_patch('path_guard.time.monotonic', side_effect=lambda: self.clock)

        def mapped_path(value):
            path = Path(value)
            if path.is_relative_to('/sys'):
                return self.sys / path.relative_to('/sys')
            return path

        self.start_patch('path_guard.Path', side_effect=mapped_path)
        config = {'initial_node': str(self.old_node),
                  'initial_sys_path': str(self.old_sys),
                  'queue_seconds': 6, 'partition_sectors': 2048, 'layout': []}
        self.guard = path_guard.Guard(config, self.recovery)

        def event(state, **details):
            self.guard.state = state
            self.events.append({'state': state, **details})

        self.guard.event = event

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def disk(self, name, generation):
        block = self.sys / 'devices' / generation / name
        block.mkdir(parents=True)
        (block / 'size').write_text('2048')
        (self.sys / 'class/block' / name).symlink_to(block)
        node = self.dev / name
        node.touch()
        return node, block

    def set_status(self, status, uuid=path_guard.UUID, kind='multipath'):
        self.mapper.query.return_value = uuid, [(kind, status)]

    def start_recovery(self):
        (self.sys / 'class/block/sda3').unlink()
        self.guard.step()
        self.assertEqual(self.guard.state, 'probing')
        self.assertEqual(len(self.probe.calls), 1)
        self.assertIsNotNone(self.guard.deadline)
        self.assertEqual(self.guard.recoveries, 0)

    def assert_not_recovered(self):
        self.assertNotEqual(self.guard.state, 'ready')
        self.assertEqual(self.guard.recoveries, 0)
        self.assertIsNotNone(self.guard.deadline)

    def test_healthy_monitoring_never_probes_or_scans_identity(self):
        for _ in range(4):
            self.clock += 1
            self.guard.step()
        self.assertEqual(self.probe.calls, [])
        self.recovery.verify.assert_not_called()
        self.layout.assert_not_called()
        self.dm.assert_not_called()

    def test_pending_probe_never_reloads_reverifies_or_starts_a_second_worker(self):
        self.start_recovery()
        calls = list(self.dm.call_args_list)
        verifies = self.recovery.verify.call_count
        layouts = self.layout.call_count
        # A second disappearance while the synchronous ioctl is stuck must not
        # start a new table transaction that could wait on its live reference.
        (self.sys / 'class/block/sdb3').unlink()
        for _ in range(3):
            self.clock += .2
            self.guard.step()
        self.assertEqual(self.dm.call_args_list, calls)
        self.assertEqual(self.recovery.verify.call_count, verifies)
        self.assertEqual(self.layout.call_count, layouts)
        self.assertEqual(len(self.probe.calls), 1)
        self.assert_not_recovered()

    def test_successful_probe_and_current_active_path_complete_recovery(self):
        self.start_recovery()
        self.probe.complete()
        self.guard.step()
        self.assertEqual(self.guard.state, 'ready')
        self.assertEqual(self.guard.recoveries, 1)
        self.assertIsNone(self.guard.deadline)
        self.assertEqual(self.events[-1]['confirmation'], 'kernel-probe-and-state')
        self.assertEqual(self.events[-1]['kernel_probe']['errno'], 0)

    def test_zero_result_cannot_override_failed_dm_path(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 0:0 F 1')
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def test_zero_result_cannot_override_changed_sysfs_instance(self):
        self.start_recovery()
        (self.sys / 'class/block/sdb3').unlink()
        self.disk('sdb3', 'replacement')
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def test_zero_result_requires_active_entry_for_current_device(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:99 A 0')
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def assert_probe_error_not_ready(self, error):
        self.start_recovery()
        self.probe.complete(error)
        self.guard.step()
        self.assert_not_recovered()
        self.assertEqual(self.events[-1]['kernel_probe']['errno'], error)
        self.assertEqual(self.events[-1]['kernel_probe']['status'], 'error')

    def test_io_error_never_becomes_ready(self):
        self.assert_probe_error_not_ready(errno.EIO)

    def test_invalid_ioctl_never_becomes_ready(self):
        self.assert_probe_error_not_ready(errno.EINVAL)

    def test_unavailable_ioctl_never_becomes_ready(self):
        self.assert_probe_error_not_ready(errno.ENOTTY)

    def test_no_paths_never_becomes_ready(self):
        self.start_recovery()
        self.probe.complete(errno.ENOTCONN)
        self.guard.step()
        self.assert_not_recovered()
        self.assertEqual(self.events[-1]['kernel_probe']['errno'], errno.ENOTCONN)

    def test_result_from_another_table_generation_cannot_be_accepted(self):
        self.start_recovery()
        self.probe.complete(token=self.guard.generation - 1)
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            self.guard.step()
        self.assert_not_recovered()

    def test_changed_map_identity_or_target_is_not_accepted(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 0:0 A 0', uuid='different-map')
        self.probe.complete()
        with self.assertRaisesRegex(RuntimeError, 'identity or target'):
            self.guard.step()
        self.assert_not_recovered()

    def test_confirmation_work_cannot_extend_recovery_deadline(self):
        self.start_recovery()
        self.probe.complete()
        def slow_query(name):
            self.clock = self.guard.deadline
            return path_guard.UUID, [('multipath', '0 0 0 1 1 A 0 1 0 0:0 A 0')]
        self.mapper.query.side_effect = slow_query
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.recoveries, 0)

    def test_missing_target_capability_rejects_startup_before_creating_probe(self):
        for version in ((1, 14, 0), (1, 14, 9)):
            with self.subTest(version=version):
                self.mapper.target_version.return_value = version
                with patch('path_guard.PathProbe') as probe:
                    with self.assertRaisesRegex(RuntimeError, 'multipath'):
                        path_guard.Guard(self.guard.config, self.recovery)
                probe.assert_not_called()

    def test_expiry_is_terminal_even_if_the_kernel_probe_finishes_later(self):
        self.start_recovery()
        self.clock = self.guard.deadline
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertTrue(self.events[-1]['probe_pending'])
        self.dm.assert_called_with('message', path_guard.NAME, '0', 'fail_if_no_path')
        calls = list(self.dm.call_args_list)
        self.probe.complete()
        self.clock += 1
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.recoveries, 0)
        self.assertEqual(self.dm.call_args_list, calls)

    def test_resume_crossing_deadline_expires_without_starting_probe(self):
        def slow_resume(*args):
            if args[0] == 'resume':
                self.clock = self.guard.deadline
            return ''
        self.dm.side_effect = slow_resume
        (self.sys / 'class/block/sda3').unlink()
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.current, str(self.node))
        self.assertEqual(self.guard.recoveries, 0)
        self.assertEqual(self.probe.calls, [])
        self.assertFalse(self.probe.busy)
        self.dm.assert_called_with('message', path_guard.NAME, '0', 'fail_if_no_path')


if __name__ == '__main__':
    unittest.main()
