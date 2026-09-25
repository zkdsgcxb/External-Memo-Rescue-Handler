"""Guard orchestration against real RAM journals and a stateful DM simulation."""
import copy
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
sys.path.insert(0, str(BASE / 'guest'))
import guard_state
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
            'errno': error, 'elapsed': .01,
            'status': {0: 'completed', errno.ENOTCONN: 'no_paths'}.get(error, 'error'),
        }

    def poll(self):
        if self.result is None:
            return None
        result, self.result = self.result, None
        self.busy = False
        return result


class GuardFixture(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.sys = self.root / 'sys'
        self.dev = self.root / 'dev'
        self.dev.mkdir()
        (self.sys / 'class/block').mkdir(parents=True)
        self.old_node, self.old_sys = self.disk('sda3', 'old', 10)
        self.node, self.node_sys = self.disk('sdb3', 'new', 11)
        self.old_dev = os.makedev(8, 3)
        self.new_dev = os.makedev(8, 19)
        self.clock = 100.
        self.probe = FakeProbe()
        self.owner = guard_state.Owner(self.run)
        self.addCleanup(self.owner.close)
        self.active = path_guard.table_targets(path_guard.table(2048, '8:3'))
        self.inactive = []
        self.suspended = False
        self.map_uuid = path_guard.UUID
        self.mapper = Mock()
        self.mapper.target_version.return_value = (1, 15, 0)
        self.mapper.last_info = {'major': 253, 'minor': 999}
        self.mapper.snapshot.side_effect = self.snapshot
        self.set_status('0 0 0 1 1 A 0 1 0 8:3 A 0')
        self.recovery = Mock()
        self.candidate = Mock()
        self.candidate.node = str(self.node)
        self.candidate.sys_path = str(self.node_sys)
        self.candidate.dev = self.new_dev
        self.candidate.diskseq = 11
        self.candidate.partition_sectors = 2048
        self.candidate.to_dict.return_value = {
            'schema': 1, 'owner_epoch': self.owner.epoch, 'deadline': 106.,
            'instance': {'node': str(self.node), 'dev': self.new_dev, 'diskseq': 11}}
        self.admission = Mock()
        self.admission.verify.return_value = self.candidate
        self.action_hook = None
        self.phases_at_change = []
        self.dm = self.start_patch('path_guard.dm', side_effect=self.apply_dm)
        self.start_patch('path_guard.Admission', return_value=self.admission)
        self.start_patch('path_guard.DeviceMapper', return_value=self.mapper)
        self.start_patch('path_guard.PathProbe', return_value=self.probe)
        self.start_patch('path_guard.time.monotonic', side_effect=lambda: self.clock)
        self.start_patch('path_guard.fault_hook')
        self.start_patch('path_guard.Observations.sample', return_value={'state': 'no_io'})

        def mapped_path(value):
            path = Path(value)
            if path.is_relative_to('/sys'):
                return self.sys / path.relative_to('/sys')
            return path

        original_stat = os.stat

        def node_stat(value, *args, **kwargs):
            result = original_stat(value, *args, **kwargs)
            if str(value) in (str(self.old_node), str(self.node)):
                return SimpleNamespace(st_rdev=self.old_dev if str(value) == str(self.old_node)
                                       else self.new_dev, st_mode=result.st_mode)
            return result

        self.start_patch('path_guard.Path', side_effect=mapped_path)
        self.start_patch('path_guard.os.stat', side_effect=node_stat)
        self.config = {'initial_node': str(self.old_node), 'initial_diskseq': 10,
                       'initial_sys_path': str(self.old_sys), 'logical_block_size': 512,
                       'queue_seconds': 6, 'partition_sectors': 2048, 'layout': []}
        self.guard = path_guard.Guard(self.config, self.recovery, self.owner)
        self.addCleanup(self.guard.close_candidate)

    @property
    def events(self):
        path = self.run / 'path-events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def disk(self, name, generation, diskseq=11):
        disk = self.sys / 'devices' / generation
        block = disk / name
        block.mkdir(parents=True)
        (disk / 'diskseq').write_text(str(diskseq))
        (block / 'size').write_text('2048')
        (self.sys / 'class/block' / name).symlink_to(block)
        node = self.dev / name
        node.touch()
        return node, block

    def snapshot(self, name):
        self.assertEqual(name, path_guard.NAME)
        return copy.deepcopy({'uuid': self.map_uuid, 'active': self.active,
                              'inactive': self.inactive,
                              'info': {'suspended': self.suspended}})

    def apply_dm(self, *args, lock_fd=None):
        self.assertIsNotNone(lock_fd)
        self.phases_at_change.append((args[0], self.guard.journal.record['phase']))
        if args[0] == 'load':
            self.inactive = path_guard.table_targets(args[-1])
        elif args[0] == 'resume':
            if self.inactive:
                self.active, self.inactive = self.inactive, []
            self.suspended = False
            self.set_status('0 0 0 1 1 A 0 1 0 8:19 A 0')
        elif args[0] == 'clear':
            self.inactive = []
        elif args[0] == 'message':
            self.assertEqual(args[-1], 'fail_if_no_path')
            self.active[0][3] = self.active[0][3].replace(
                '3 queue_if_no_path queue_mode bio', '2 queue_mode bio')
        else:
            self.fail(f'Unexpected DM mutation: {args}')
        if self.action_hook:
            self.action_hook(args)
        return ''

    def set_status(self, status, uuid=path_guard.UUID, kind='multipath'):
        self.mapper.query.return_value = uuid, [(kind, status)]

    def disconnect(self):
        (self.sys / 'class/block/sda3').unlink()

    def start_recovery(self):
        self.disconnect()
        self.guard.step()
        self.assertEqual(self.guard.state, 'probing')
        self.assertEqual(len(self.probe.calls), 1)
        self.assertIsNotNone(self.guard.deadline)
        self.assertEqual(self.guard.recoveries, 0)

    def assert_not_recovered(self):
        self.assertNotEqual(self.guard.state, 'ready')
        self.assertEqual(self.guard.recoveries, 0)
        self.assertIsNotNone(self.guard.deadline)


class GuardProbeTests(GuardFixture):
    def test_healthy_monitoring_never_probes_or_scans_identity(self):
        for _ in range(4):
            self.clock += 1
            self.guard.step()
        self.assertEqual(self.probe.calls, [])
        self.admission.verify.assert_not_called()
        self.candidate.revalidate.assert_not_called()
        self.dm.assert_not_called()

    def test_pending_probe_never_reloads_reverifies_or_starts_a_second_worker(self):
        self.start_recovery()
        calls = list(self.dm.call_args_list)
        verifies = self.admission.verify.call_count
        checks = self.candidate.revalidate.call_count
        (self.sys / 'class/block/sdb3').unlink()
        for _ in range(3):
            self.clock += .2
            self.guard.step()
        self.assertEqual(self.dm.call_args_list, calls)
        self.assertEqual(self.admission.verify.call_count, verifies)
        self.assertEqual(self.candidate.revalidate.call_count, checks)
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
        self.assertEqual(self.events[-1]['observations']['upper_errors']['state'], 'incomplete')
        self.assertEqual(guard_state.load_json(self.run / 'path-transaction.json')['phase'], 'ready')
        self.candidate.close.assert_called_once()

    def test_zero_result_cannot_override_failed_dm_path(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:19 F 1')
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def test_zero_result_cannot_override_changed_sysfs_instance(self):
        self.start_recovery()
        (self.sys / 'class/block/sdb3').unlink()
        self.disk('sdb3', 'replacement', 12)
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def test_zero_result_cannot_override_same_path_new_diskseq(self):
        self.start_recovery()
        (self.node_sys.parent / 'diskseq').write_text('12')
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def test_zero_result_requires_active_entry_for_current_device(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:99 A 0')
        self.probe.complete()
        self.guard.step()
        self.assert_not_recovered()

    def test_probe_errors_never_become_ready(self):
        self.start_recovery()
        for error in (errno.EIO, errno.EINVAL, errno.ENOTTY, errno.ENOTCONN):
            with self.subTest(error=error):
                self.probe.complete(error)
                self.guard.confirming = True
                self.guard.step()
                self.assert_not_recovered()
                self.assertEqual(self.events[-1]['kernel_probe']['errno'], error)

    def test_result_from_another_table_generation_cannot_be_accepted(self):
        self.start_recovery()
        self.probe.complete(token=self.guard.generation - 1)
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            self.guard.step()
        self.assert_not_recovered()

    def test_changed_map_identity_or_target_is_not_accepted(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:19 A 0', uuid='different-map')
        self.probe.complete()
        with self.assertRaisesRegex(RuntimeError, 'identity or target'):
            self.guard.step()
        self.assert_not_recovered()

    def test_changed_active_table_after_probe_is_not_accepted(self):
        self.start_recovery()
        self.active = path_guard.table_targets(path_guard.table(2048, '8:99'))
        self.probe.complete()
        with self.assertRaisesRegex(RuntimeError, 'Committed table'):
            self.guard.step()
        self.assert_not_recovered()

    def test_confirmation_work_cannot_extend_recovery_deadline(self):
        self.start_recovery()
        self.probe.complete()
        def slow_query(name):
            self.clock = self.guard.deadline
            return path_guard.UUID, [('multipath', '0 0 0 1 1 A 0 1 0 8:19 A 0')]
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
                        path_guard.Guard(self.config, self.recovery, self.owner)
                probe.assert_not_called()

    def test_expiry_is_terminal_even_if_the_kernel_probe_finishes_later(self):
        self.start_recovery()
        self.clock = self.guard.deadline
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertTrue(self.events[-1]['probe_pending'])
        self.dm.assert_called_with('message', path_guard.NAME, '0', 'fail_if_no_path', lock_fd=self.owner.fd)
        calls = list(self.dm.call_args_list)
        self.probe.complete()
        self.clock += 1
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.recoveries, 0)
        self.assertEqual(self.dm.call_args_list, calls)

    def test_resume_crossing_deadline_expires_without_starting_probe(self):
        def slow_resume(args):
            if args[0] == 'resume':
                self.clock = self.guard.deadline
        self.action_hook = slow_resume
        self.disconnect()
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.current, str(self.node))
        self.assertEqual(self.guard.recoveries, 0)
        self.assertEqual(self.probe.calls, [])
        self.dm.assert_called_with('message', path_guard.NAME, '0', 'fail_if_no_path', lock_fd=self.owner.fd)


class GuardTransactionTests(GuardFixture):
    def test_swap_uses_one_resume_without_explicit_suspend(self):
        self.start_recovery()
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['load', 'resume'])
        self.assertEqual(self.dm.call_args_list[1].args,
                         ('resume', '--noflush', '--nolockfs', path_guard.NAME))
        self.assertEqual(self.phases_at_change, [('load', 'load_intent'), ('resume', 'commit_intent')])
        self.assertEqual(self.inactive, [])
        self.assertFalse(self.suspended)

    def test_new_diskseq_reusing_active_device_number_is_rejected_before_load(self):
        self.candidate.dev = self.old_dev
        self.disconnect()
        self.guard.step()
        self.assertEqual(self.guard.state, 'rejected')
        self.assertIn('reuses active dev_t', self.events[-1]['reason'])
        self.dm.assert_not_called()
        self.candidate.close.assert_called_once()

    def test_failed_final_admission_clears_known_inactive_without_resume(self):
        self.candidate.revalidate.side_effect = [None, RuntimeError('layout changed')]
        original = copy.deepcopy(self.active)
        self.disconnect()
        self.guard.step()
        self.assertEqual(self.guard.state, 'rejected')
        self.assertEqual(self.active, original)
        self.assertEqual(self.inactive, [])
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['load', 'clear'])
        self.assertEqual(self.probe.calls, [])

    def test_unknown_inactive_table_is_never_cleared(self):
        self.candidate.revalidate.side_effect = [None, RuntimeError('layout changed')]
        def replace_inactive(args):
            if args[0] == 'load':
                self.inactive = path_guard.table_targets(path_guard.table(2048, '8:99'))
        self.action_hook = replace_inactive
        self.disconnect()
        with self.assertRaisesRegex(RuntimeError, 'unknown inactive'):
            self.guard.step()
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['load'])

    def test_ambiguous_load_failure_is_terminal_for_supervisor(self):
        def fail_after_load(args):
            if args[0] == 'load':
                raise TimeoutError('load returned late after installing inactive table')
        self.action_hook = fail_after_load
        self.disconnect()
        with self.assertRaises(TimeoutError):
            self.guard.step()
        self.assertEqual(self.guard.state, 'failed')
        self.assertEqual(self.probe.calls, [])

    def test_ambiguous_resume_failure_is_terminal_for_supervisor(self):
        def fail_after_resume(args):
            if args[0] == 'resume':
                raise TimeoutError('resume returned late after committing table')
        self.action_hook = fail_after_resume
        self.disconnect()
        with self.assertRaises(TimeoutError):
            self.guard.step()
        self.assertEqual(self.guard.state, 'failed')
        self.assertEqual(self.probe.calls, [])

    def test_each_terminal_state_prevents_new_work(self):
        for state in path_guard.TERMINAL:
            with self.subTest(state=state):
                self.guard.state = state
                self.guard.deadline = 99.
                self.guard.step()
        self.dm.assert_not_called()
        self.admission.verify.assert_not_called()

    def test_restart_cannot_overwrite_existing_terminal_journal(self):
        self.guard.journal.write('expired')
        before = (self.run / 'path-transaction.json').read_bytes()
        self.owner.close()
        actual_load = guard_state.load_json
        def configured_load(path):
            return self.config if str(path) == '/etc/rescue/path-guard.json' else actual_load(path)
        with patch('agent.guard'), patch('path_guard.load_json', side_effect=configured_load), \
                patch('path_guard.Owner', side_effect=lambda: guard_state.Owner(self.run)), \
                patch('path_guard.Guard') as constructor, patch.object(sys, 'argv', ['path_guard.py']):
            with self.assertRaisesRegex(RuntimeError, 'Existing transaction'):
                path_guard.main()
        constructor.assert_not_called()
        self.assertEqual((self.run / 'path-transaction.json').read_bytes(), before)


class TakeoverTests(GuardFixture):
    def interrupted(self, phase='loaded', suspended=False):
        previous = guard_state.describe(self.snapshot(path_guard.NAME))
        candidate = path_guard.table_targets(path_guard.table(2048, '8:19'))
        self.inactive = candidate
        self.suspended = suspended
        self.guard.journal.write(phase, previous=previous,
            snapshot=guard_state.describe(self.snapshot(path_guard.NAME)),
            candidate_table_digest=guard_state.table_digest(candidate), deadline=106.)
        self.owner.close()
        replacement = guard_state.Owner(self.run)
        self.addCleanup(replacement.close)
        return replacement

    def test_loaded_takeover_discards_candidate_and_stops_admission(self):
        replacement = self.interrupted()
        result = path_guard.takeover(replacement, self.config)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual(result['previous_owner_epoch'], self.owner.epoch)
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['clear', 'message'])
        self.assertEqual(self.inactive, [])
        self.assertEqual(guard_state.load_json(self.run / 'path-transaction.json')['owner_epoch'], replacement.epoch)
        self.admission.verify.assert_not_called()

    def test_suspended_takeover_clears_candidate_before_resuming_old_table(self):
        replacement = self.interrupted(suspended=True)
        old_digest = guard_state.table_digest(self.active)
        path_guard.takeover(replacement, self.config)
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['clear', 'resume', 'message'])
        self.assertEqual(guard_state.table_digest(self.active), old_digest)
        self.assertFalse(self.suspended)

    def test_unknown_active_table_blocks_takeover_before_mutation(self):
        replacement = self.interrupted()
        self.active = path_guard.table_targets(path_guard.table(2048, '8:99'))
        with self.assertRaisesRegex(RuntimeError, 'Unknown active table'):
            path_guard.takeover(replacement, self.config)
        self.dm.assert_not_called()

    def test_unknown_inactive_table_blocks_takeover_before_mutation(self):
        replacement = self.interrupted()
        self.inactive = path_guard.table_targets(path_guard.table(2048, '8:99'))
        with self.assertRaisesRegex(RuntimeError, 'Unknown inactive table'):
            path_guard.takeover(replacement, self.config)
        self.dm.assert_not_called()

    def test_candidate_live_before_commit_intent_is_not_trusted(self):
        replacement = self.interrupted('loaded')
        self.active, self.inactive = self.inactive, []
        with self.assertRaisesRegex(RuntimeError, 'Unknown active table'):
            path_guard.takeover(replacement, self.config)
        self.dm.assert_not_called()

    def test_candidate_live_after_commit_intent_can_be_stopped_without_reopening_it(self):
        replacement = self.interrupted('commit_intent')
        self.active, self.inactive = self.inactive, []
        result = path_guard.takeover(replacement, self.config)
        self.assertEqual(result['state'], 'interrupted')
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['message'])
        self.admission.verify.assert_not_called()

    def test_expired_outcome_is_preserved_by_takeover(self):
        replacement = self.interrupted('expired')
        self.inactive = []
        result = path_guard.takeover(replacement, self.config)
        self.assertEqual(result['state'], 'expired')
        self.assertEqual(guard_state.load_json(self.run / 'path-transaction.json')['phase'], 'expired')

    def test_changed_enrollment_blocks_takeover_before_mutation(self):
        replacement = self.interrupted()
        with self.assertRaisesRegex(RuntimeError, 'Untrusted transaction'):
            path_guard.takeover(replacement, {**self.config, 'partition_sectors': 4096})
        self.dm.assert_not_called()


if __name__ == '__main__':
    unittest.main()
