"""Guard FSM contracts with controlled worker completion and real RAM journals."""
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'guard/runtime'))
import guard_state
import path_guard

_UNSET = object()


class FakeOperation:
    """Explicit completion replaces time-dependent threads only in FSM tests."""
    def __init__(self, fd):
        self.fd = fd
        self.calls = []
        self.pending = None
        self.result = None
        self.abandoned = False
        self.cleanup = None
        self.on_poll = None

    @property
    def busy(self):
        return self.pending is not None

    def start(self, kind, fn):
        if self.abandoned or self.busy:
            raise RuntimeError('Executor cannot accept another operation')
        token = len(self.calls) + 1
        self.calls.append(kind)
        self.pending = (kind, token, fn)
        return token

    def fence_fd(self):
        return self.fd

    def complete(self, *, value=_UNSET, error=None, token=None, kind=None):
        pending_kind, pending_token, fn = self.pending
        if value is _UNSET and error is None:
            try:
                value = fn()
            except BaseException as exc:
                error = {'type': type(exc).__name__, 'message': str(exc), 'traceback': ''}
                value = None
        self.result = {'kind': pending_kind if kind is None else kind,
                       'token': pending_token if token is None else token,
                       'value': None if value is _UNSET else value,
                       'error': error, 'elapsed': .01}
        if self.abandoned:
            outcome, self.result, self.pending = self.result, None, None
            self.cleanup(outcome)

    def poll(self):
        if self.result is None or self.abandoned:
            return None
        result, self.result, self.pending = self.result, None, None
        if self.on_poll:
            self.on_poll()
        return result

    def abandon(self, cleanup):
        if self.abandoned:
            return
        self.abandoned = True
        self.cleanup = cleanup
        if self.result is not None:
            outcome, self.result, self.pending = self.result, None, None
            cleanup(outcome)

    def close(self):
        if self.busy:
            raise AssertionError('Pending operation requires explicit cleanup')
        self.abandoned = True


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
        self.old_dev, self.new_dev = os.makedev(8, 3), os.makedev(8, 19)
        self.clock = 100.
        self.owner = guard_state.Owner(self.run)
        self.addCleanup(self.owner.close)
        self.operation = FakeOperation(self.owner.fd)
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
        self.start_patch('path_guard.OwnedOperation', return_value=self.operation)
        self.probe = self.start_patch('path_guard.probe_paths', side_effect=lambda device, token:
            {'token': token, 'errno': 0, 'elapsed': .01, 'status': 'completed', 'source': 'ioctl'})
        self.start_patch('path_guard.time.monotonic', side_effect=lambda: self.clock)
        self.start_patch('path_guard.fault_hook')
        self.start_patch('path_guard.Observations.sample', return_value={'state': 'no_io'})

        def mapped_path(value):
            path = Path(value)
            return self.sys / path.relative_to('/sys') if path.is_relative_to('/sys') else path

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
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.guard.shutdown()
        if self.operation.busy:
            self.operation.complete()

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
                              'inactive': self.inactive, 'info': {'suspended': self.suspended}})

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

    def begin(self, kind='verify'):
        self.disconnect()
        self.guard.step()
        self.advance_to(kind)

    def advance_to(self, kind):
        for _ in range(10):
            if self.guard.pending_kind == kind:
                return
            self.assertIsNotNone(self.guard.pending_kind)
            self.operation.complete()
            self.guard.step()
        self.fail('FSM did not reach '+kind)

    def complete_stage(self, **kwargs):
        self.operation.complete(**kwargs)
        self.guard.step()

    def start_recovery(self):
        self.begin('probe')
        self.assertEqual(self.guard.state, 'probing')
        self.assertEqual(self.operation.calls, ['verify', 'load', 'revalidate', 'commit', 'preprobe', 'probe'])
        self.assertIsNotNone(self.guard.deadline)
        self.assertEqual(self.guard.recoveries, 0)

    def complete_probe(self, error=0, token=None):
        value = {'token': self.guard.generation if token is None else token, 'errno': error,
                 'elapsed': .01, 'status': {0: 'completed', errno.ENOTCONN: 'no_paths'}.get(error, 'error')}
        self.complete_stage(value=value)
        if self.guard.pending_kind == 'confirm':
            self.complete_stage()

    def assert_not_recovered(self):
        self.assertNotEqual(self.guard.state, 'ready')
        self.assertEqual(self.guard.recoveries, 0)
        self.assertIsNotNone(self.guard.deadline)


class GuardProbeTests(GuardFixture):
    def test_healthy_monitoring_never_starts_worker_or_scans_identity(self):
        for _ in range(4):
            self.clock += 1
            self.guard.step()
        self.assertEqual(self.operation.calls, [])
        self.admission.verify.assert_not_called()
        self.candidate.revalidate.assert_not_called()
        self.dm.assert_not_called()

    def test_pending_probe_never_reloads_reverifies_or_starts_a_second_worker(self):
        self.start_recovery()
        calls = list(self.dm.call_args_list)
        checks = self.candidate.revalidate.call_count
        (self.sys / 'class/block/sdb3').unlink()
        for _ in range(3):
            self.clock += .2
            self.guard.step()
        self.assertEqual(self.dm.call_args_list, calls)
        self.assertEqual(self.candidate.revalidate.call_count, checks)
        self.assertEqual(self.operation.calls.count('verify'), 1)
        self.assertEqual(self.operation.calls.count('probe'), 1)
        self.assert_not_recovered()

    def test_successful_probe_requires_separate_final_confirmation_before_ready(self):
        self.start_recovery()
        self.complete_stage()
        self.assertEqual(self.guard.pending_kind, 'confirm')
        self.assert_not_recovered()
        self.complete_stage()
        self.assertEqual(self.guard.state, 'ready')
        self.assertEqual(self.guard.recoveries, 1)
        self.assertIsNone(self.guard.deadline)
        self.assertEqual(self.events[-1]['confirmation'], 'kernel-probe-and-state')
        self.assertEqual(self.events[-1]['observations']['upper_errors']['state'], 'incomplete')
        self.assertEqual(guard_state.load_json(self.run / 'path-transaction.json')['phase'], 'ready')
        self.candidate.close.assert_called_once()

    def test_zero_result_cannot_override_failed_dm_path(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:19 F 1')
        self.complete_probe()
        self.assert_not_recovered()

    def test_zero_result_cannot_override_changed_sysfs_instance(self):
        self.start_recovery()
        (self.sys / 'class/block/sdb3').unlink()
        self.disk('sdb3', 'replacement', 12)
        self.complete_probe()
        self.assert_not_recovered()

    def test_zero_result_cannot_override_same_path_new_diskseq(self):
        self.start_recovery()
        (self.node_sys.parent / 'diskseq').write_text('12')
        self.complete_probe()
        self.assert_not_recovered()

    def test_zero_result_requires_active_entry_for_current_device(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:99 A 0')
        self.complete_probe()
        self.assert_not_recovered()

    def assert_probe_error(self, error):
        self.start_recovery()
        self.complete_probe(error)
        self.assert_not_recovered()
        self.assertEqual(self.events[-1]['kernel_probe']['errno'], error)

    def test_io_error_never_becomes_ready(self):
        self.assert_probe_error(errno.EIO)

    def test_invalid_ioctl_never_becomes_ready(self):
        self.assert_probe_error(errno.EINVAL)

    def test_unavailable_ioctl_never_becomes_ready(self):
        self.assert_probe_error(errno.ENOTTY)

    def test_no_paths_never_becomes_ready(self):
        self.assert_probe_error(errno.ENOTCONN)

    def test_result_from_another_table_generation_cannot_be_accepted(self):
        self.start_recovery()
        with self.assertRaisesRegex(RuntimeError, 'generation'):
            self.complete_probe(token=self.guard.generation - 1)
        self.assertEqual(self.guard.state, 'failed')

    def test_changed_map_identity_or_target_is_not_accepted(self):
        self.start_recovery()
        self.set_status('0 0 0 1 1 A 0 1 0 8:19 A 0', uuid='different-map')
        with self.assertRaisesRegex(RuntimeError, 'identity or target'):
            self.complete_probe()
        self.assertEqual(self.guard.state, 'failed')

    def test_changed_active_table_after_probe_is_not_accepted(self):
        self.start_recovery()
        self.active = path_guard.table_targets(path_guard.table(2048, '8:99'))
        with self.assertRaisesRegex(RuntimeError, 'Committed table'):
            self.complete_probe()
        self.assertEqual(self.guard.state, 'failed')

    def test_slow_confirmation_cannot_extend_deadline(self):
        self.begin('confirm')
        self.operation.complete()
        self.clock = self.guard.deadline
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.recoveries, 0)
        self.candidate.close.assert_called_once()

    def test_missing_capability_rejects_before_creating_operation_executor(self):
        self.mapper.target_version.return_value = (1, 14, 9)
        with patch('path_guard.OwnedOperation') as operation:
            with self.assertRaisesRegex(RuntimeError, 'multipath'):
                path_guard.Guard(self.config, self.recovery, self.owner)
        operation.assert_not_called()

    def test_expiry_is_terminal_and_queue_disable_is_deferred_to_takeover(self):
        self.start_recovery()
        calls = list(self.dm.call_args_list)
        self.clock = self.guard.deadline
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertTrue(self.events[-1]['probe_pending'])
        self.assertEqual(self.events[-1]['queue_disable'], 'deferred_to_takeover')
        self.operation.complete()
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.recoveries, 0)
        self.assertEqual(self.dm.call_args_list, calls)
        self.candidate.close.assert_called_once()


class GuardTransactionTests(GuardFixture):
    def test_swap_uses_one_resume_without_explicit_suspend(self):
        self.start_recovery()
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['load', 'resume'])
        self.assertEqual(self.dm.call_args_list[1].args, ('resume', '--noflush', '--nolockfs', path_guard.NAME))
        self.assertEqual(self.phases_at_change, [('load', 'load_intent'), ('resume', 'commit_intent')])
        self.assertFalse(self.inactive or self.suspended)

    def test_verify_error_can_retry_without_extending_deadline(self):
        self.begin()
        deadline = self.guard.deadline
        self.admission.verify.side_effect = RuntimeError('candidate not ready')
        self.complete_stage()
        self.assertEqual(self.guard.state, 'rejected')
        self.dm.assert_not_called()
        self.guard.step()
        self.assertEqual(self.guard.pending_kind, 'verify')
        self.assertEqual(self.guard.deadline, deadline)

    def test_new_diskseq_reusing_active_device_number_is_rejected_before_load(self):
        self.candidate.dev = self.old_dev
        self.begin()
        self.complete_stage()
        self.assertEqual(self.guard.state, 'rejected')
        self.assertIn('reuses active dev_t', self.events[-1]['reason'])
        self.dm.assert_not_called()
        self.candidate.close.assert_called_once()

    def test_failed_final_admission_stops_and_leaves_inactive_for_takeover(self):
        self.begin('revalidate')
        self.candidate.revalidate.side_effect = RuntimeError('layout changed')
        with self.assertRaisesRegex(RuntimeError, 'revalidate: layout changed'):
            self.complete_stage()
        self.assertEqual(self.guard.state, 'failed')
        self.assertTrue(self.inactive)
        self.assertEqual([call.args[0] for call in self.dm.call_args_list], ['load'])
        self.assertNotIn('commit', self.operation.calls)

    def test_preexisting_inactive_table_is_not_replaced_or_cleared(self):
        self.inactive = path_guard.table_targets(path_guard.table(2048, '8:99'))
        self.begin()
        with self.assertRaisesRegex(RuntimeError, 'pre-existing'):
            self.complete_stage()
        self.assertEqual(self.guard.state, 'failed')
        self.dm.assert_not_called()

    def test_ambiguous_load_failure_is_terminal_for_supervisor(self):
        self.begin('load')
        self.action_hook = lambda args: (_ for _ in ()).throw(TimeoutError('load returned late'))
        with self.assertRaisesRegex(RuntimeError, 'load returned late'):
            self.complete_stage()
        self.assertEqual(self.guard.state, 'failed')
        self.assertTrue(self.inactive)
        self.assertNotIn('commit', self.operation.calls)

    def test_ambiguous_resume_failure_is_terminal_for_supervisor(self):
        self.begin('commit')
        self.action_hook = lambda args: (_ for _ in ()).throw(TimeoutError('resume returned late'))
        with self.assertRaisesRegex(RuntimeError, 'resume returned late'):
            self.complete_stage()
        self.assertEqual(self.guard.state, 'failed')
        self.assertFalse(self.inactive)
        self.assertNotIn('probe', self.operation.calls)

    def test_wrong_worker_kind_or_token_is_not_accepted(self):
        self.begin()
        with self.assertRaisesRegex(RuntimeError, 'different transaction'):
            self.complete_stage(token=999)
        self.assertEqual(self.guard.state, 'failed')

    def test_deadline_crossed_while_polling_verify_closes_returned_candidate_once(self):
        self.begin()
        self.operation.complete()
        self.operation.on_poll = lambda: setattr(self, 'clock', self.guard.deadline)
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.candidate.close.assert_called_once()
        self.assertEqual(self.operation.calls, ['verify'])
        self.dm.assert_not_called()

    def test_deadline_crossed_while_polling_confirm_cannot_announce_ready(self):
        self.begin('confirm')
        self.operation.complete()
        self.operation.on_poll = lambda: setattr(self, 'clock', self.guard.deadline)
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.recoveries, 0)
        self.candidate.close.assert_called_once()

    def test_each_terminal_state_prevents_new_work(self):
        for state in path_guard.TERMINAL:
            with self.subTest(state=state):
                self.guard.state = state
                self.guard.deadline = 99.
                self.guard.step()
        self.dm.assert_not_called()
        self.assertEqual(self.operation.calls, [])

    def test_restart_cannot_overwrite_existing_terminal_journal(self):
        self.guard.journal.write('expired')
        before = (self.run / 'path-transaction.json').read_bytes()
        self.owner.close()
        actual_load = guard_state.load_json
        def configured_load(path):
            return self.config if str(path) == '/etc/rescue/path-guard.json' else actual_load(path)
        with patch('agent.guard'), patch('path_guard.load_json', side_effect=configured_load), \
                patch('path_guard.Owner', side_effect=lambda run: guard_state.Owner(self.run)), \
                patch('path_guard.Guard') as constructor, patch.object(sys, 'argv', ['path_guard.py']):
            with self.assertRaisesRegex(RuntimeError, 'Existing transaction'):
                path_guard.main()
        constructor.assert_not_called()
        self.assertEqual((self.run / 'path-transaction.json').read_bytes(), before)

    def assert_deadline_in_phase(self, kind):
        self.begin(kind)
        calls = list(self.dm.call_args_list)
        count = len(self.operation.calls)
        self.clock = self.guard.deadline
        self.guard.step()
        self.assertEqual(self.guard.state, 'expired')
        self.assertEqual(self.guard.journal.record['operation_pending'], kind)
        self.assertTrue(self.operation.abandoned)
        self.operation.complete()
        self.guard.step()
        self.assertEqual(len(self.operation.calls), count)
        self.assertEqual(self.dm.call_args_list, calls)
        self.assertEqual(self.guard.recoveries, 0)
        self.candidate.close.assert_called_once()


def _deadline_test(kind):
    def test(self):
        self.assert_deadline_in_phase(kind)
    return test


for _kind in ('verify', 'load', 'revalidate', 'commit', 'preprobe', 'probe', 'confirm'):
    setattr(GuardTransactionTests, 'test_deadline_while_' + _kind + '_blocks', _deadline_test(_kind))

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
