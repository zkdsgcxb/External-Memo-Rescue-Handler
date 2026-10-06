"""Inert lifecycle tests in temporary trees; no host stage/cancel or DM actions."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from copy import deepcopy
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_planning as fixtures
import candidates
import candidate_ui
import manage
import planning
import support
from admin.admission import digest


class Crash(BaseException):
    """A process loss must bypass ordinary exception recovery."""


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.enable_qualification()
        self.plan = self.fixture.plan()
        self.expected = self.plan['plan_digest']
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = candidates.Store(self.root / 'plans', uid=os.geteuid(), anchor=self.root)
        original_build = planning.build
        self.build = Mock(side_effect=lambda device: original_build(device, **self.fixture.options))
        self.active = Mock(return_value=None)
        self.device = '/dev/mapper/rr-data-test'

    def stage(self):
        return self.store.stage(self.device, self.expected, build=self.build)

    def cancel(self, result):
        return self.store.cancel(result['id'], result['plan_digest'], active_check=self.active)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_stage_only_private_candidate_and_same_plan_reuses_operation(self):
        result = self.stage()
        self.assertEqual(result['state'], 'prepared')
        self.assertFalse(result['enabled'])
        self.assertFalse(result['reboot_activates'])
        before = self.files()
        self.assertEqual(self.stage(), result)
        self.assertEqual(self.files(), before)
        receipt, raw = self.store.load(result['id'])
        self.assertEqual(set(raw), candidates.FILES)
        for path in self.root.rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertNotIn('initial_node', json.loads(raw['candidate.json'])['guard'])
        self.assertEqual(json.loads(raw['plan.json'])['inputs']['instance']['initial_node'], '/dev/sdb1')

    def test_plan_mismatch_or_unvalidated_combination_creates_nothing(self):
        for kind in ('digest', 'combination', 'effects'):
            with self.subTest(kind=kind):
                plan = deepcopy(self.plan)
                expected = self.expected
                if kind == 'digest':
                    expected = 'f' * 64
                elif kind == 'combination':
                    plan['status'] = 'blocked'
                else:
                    plan['confirmation']['effects']['activation'] = 'enable'
                    expected = plan['plan_digest'] = digest(plan['confirmation'])
                with self.assertRaises(RuntimeError):
                    self.store.stage(self.device, expected, build=lambda device: plan)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_actual_rebuilt_plan_rejects_changed_confirmation(self):
        self.fixture.raw['/proc/1/root/etc/fstab'] += '\n# changed'
        with self.assertRaisesRegex(RuntimeError, 'plan_changed'):
            self.stage()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_inputs_changed_after_materialization_mark_failure(self):
        changed = deepcopy(self.plan)
        changed['plan_digest'] = 'b' * 64
        self.build.side_effect = [self.plan, changed]
        with self.assertRaisesRegex(RuntimeError, 'plan_changed'):
            self.stage()
        result = self.store.list()[0]
        self.assertEqual(result['state'], 'failed_needs_review')
        self.assertEqual(result['reason'], 'plan_changed_after_write')
        self.assertEqual(self.cancel(result)['state'], 'cancelled')

    def test_qualification_revocation_does_not_prevent_safe_cancel(self):
        result = self.stage()
        self.fixture.qualification_path.unlink()
        self.assertEqual(self.cancel(result)['state'], 'cancelled')

    def test_qualification_change_after_materialization_prevents_success(self):
        original = self.build.side_effect
        calls = 0
        def rebuilt(device):
            nonlocal calls
            calls += 1
            if calls == 2:
                path = self.fixture.qualification_path
                path.write_bytes(path.read_bytes() + b' ')
            return original(device)
        self.build.side_effect = rebuilt
        with self.assertRaisesRegex(RuntimeError, 'plan_changed'):
            self.stage()
        result = self.store.list()[0]
        self.assertEqual(result['state'], 'failed_needs_review')
        self.assertEqual(self.cancel(result)['state'], 'cancelled')

    def test_crash_before_and_after_every_atomic_stage_write(self):
        original = candidates.atomic
        for boundary in range(1, 6):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    store = candidates.Store(self.root / ('case-%s-%s' % (boundary, after)),
                                             uid=os.geteuid(), anchor=self.root)
                    calls = 0
                    def interrupt(path, data):
                        nonlocal calls
                        calls += 1
                        if calls == boundary and not after:
                            raise Crash()
                        original(path, data)
                        if calls == boundary and after:
                            raise Crash()
                    with patch.object(candidates, 'atomic', side_effect=interrupt), self.assertRaises(Crash):
                        store.stage(self.device, self.expected, build=self.build)
                    row = store.list()[0]
                    operation = row['id']
                    if boundary == 1 and not after:
                        self.assertEqual(row['reason'], 'untrusted_or_incomplete')
                        with self.assertRaises(RuntimeError):
                            store.cancel(operation, self.expected, active_check=self.active)
                        continue
                    receipt, raw = store.load(operation)
                    if set(raw) == candidates.FILES:
                        resumed = store.stage(self.device, self.expected, build=self.build)
                        self.assertEqual(resumed['id'], operation)
                        self.assertEqual(resumed['state'], 'prepared')
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
                            store.stage(self.device, self.expected, build=self.build)
                        self.assertEqual(store.load(operation)[0]['state'], 'failed_needs_review')
                    self.assertEqual(store.cancel(operation, self.expected, active_check=self.active)['state'], 'cancelled')

    def test_atomic_replace_then_sync_error_is_reconciled_from_disk(self):
        original = candidates.atomic
        def fail(path, data):
            original(path, data)
            if json.loads(data).get('state') == 'prepared':
                raise OSError('directory fsync after replacement')
        with patch.object(candidates, 'atomic', side_effect=fail), self.assertRaises(OSError):
            self.stage()
        result = self.store.list()[0]
        self.assertEqual(result['state'], 'prepared')
        self.assertEqual(self.stage()['id'], result['id'])

    def test_cancel_keeps_small_receipts_is_idempotent_and_new_stage_gets_new_id(self):
        result = self.stage()
        cancelled = self.cancel(result)
        self.assertFalse(cancelled['cleanup_pending'])
        self.assertEqual(set(self.store.load(result['id'])[1]), {'operation.json', 'manifest.json'})
        before = self.files()
        self.active.side_effect = RuntimeError('new active registration')
        self.assertEqual(self.cancel(result), cancelled)
        self.assertEqual(self.files(), before)
        self.assertNotEqual(self.stage()['id'], result['id'])

    def test_cancel_write_crashes_resume(self):
        original = candidates.atomic
        for boundary in (1, 2):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    result = self.stage()
                    calls = 0
                    def interrupt(path, data):
                        nonlocal calls
                        calls += 1
                        if calls == boundary and not after:
                            raise Crash()
                        original(path, data)
                        if calls == boundary and after:
                            raise Crash()
                    with patch.object(candidates, 'atomic', side_effect=interrupt), self.assertRaises(Crash):
                        self.cancel(result)
                    self.assertEqual(self.cancel(result)['state'], 'cancelled')
                    self.assertFalse(self.cancel(result)['cleanup_pending'])

    def test_cancel_unlink_crashes_resume(self):
        original = os.unlink
        for boundary in (1, 2):
            for after in (False, True):
                with self.subTest(boundary=boundary, after=after):
                    result = self.stage()
                    calls = 0
                    def interrupt(path, *args, **kwargs):
                        nonlocal calls
                        if str(path) in candidates.PAYLOADS:
                            calls += 1
                            if calls == boundary and not after:
                                raise Crash()
                            original(path, *args, **kwargs)
                            if calls == boundary and after:
                                raise Crash()
                        else:
                            original(path, *args, **kwargs)
                    with patch('os.unlink', side_effect=interrupt), self.assertRaises(Crash):
                        self.cancel(result)
                    self.assertTrue(self.store.load(result['id'])[0]['cleanup_pending'])
                    self.assertFalse(self.cancel(result)['cleanup_pending'])

    def test_foreign_files_and_links_are_preserved(self):
        for kind in ('extra', 'temp', 'symlink', 'hardlink', 'content', 'mode', 'directory_mode'):
            with self.subTest(kind=kind):
                # Separate stores keep deliberately damaged evidence intact.
                self.store = candidates.Store(self.root / kind, uid=os.geteuid(), anchor=self.root)
                result = self.stage()
                directory = self.store.path(result['id'])
                path = directory / 'candidate.json'
                if kind in ('extra', 'temp'):
                    (directory / ('stranger' if kind == 'extra' else '.candidate.json-crash')).write_text('preserve')
                elif kind == 'symlink':
                    path.unlink()
                    path.symlink_to(self.root / 'outside')
                elif kind == 'hardlink':
                    os.link(path, self.root / 'outside-hardlink')
                elif kind == 'content':
                    path.write_text('{}')
                elif kind == 'mode':
                    path.chmod(0o644)
                else:
                    directory.chmod(0o750)
                with self.assertRaises((RuntimeError, OSError)):
                    self.cancel(result)
                self.assertTrue(path.exists() or path.is_symlink())

    def test_changed_manifest_cannot_relabel_foreign_candidate_as_owned(self):
        result = self.stage()
        directory = self.store.path(result['id'])
        receipt, raw = self.store.load(result['id'])
        foreign = b'{}'
        receipt['manifest']['files']['candidate.json'] = candidates.sha(foreign)
        (directory / 'operation.json').write_bytes(candidates.encode(receipt))
        (directory / 'manifest.json').write_bytes(candidates.encode(receipt['manifest']))
        (directory / 'candidate.json').write_bytes(foreign)
        with self.assertRaisesRegex(RuntimeError, 'candidate_changed'):
            self.cancel(result)
        self.assertEqual((directory / 'candidate.json').read_bytes(), foreign)

    def test_malformed_receipts_and_unsupported_future_state_fail_closed(self):
        result = self.stage()
        receipt, _ = self.store.load(result['id'])
        for value in ([], {key: value for key, value in receipt.items() if key != 'reason'},
                      dict(receipt, manifest=[]), dict(receipt, state='committed'),
                      dict(receipt, cleanup_pending=True)):
            with self.subTest(value=value):
                (self.store.path(result['id']) / 'operation.json').write_bytes(candidates.encode(value))
                self.assertEqual(self.store.list()[0]['state'], 'failed_needs_review')
                with self.assertRaises(RuntimeError):
                    self.cancel(result)

    def test_cancel_detects_change_after_confirmation(self):
        result = self.stage()
        _, raw = self.store.load(result['id'])
        snapshot = {name: candidates.sha(value) for name, value in raw.items()}
        receipt = json.loads(raw['operation.json'])
        receipt['reason'] = 'external-change'
        (self.store.path(result['id']) / 'operation.json').write_bytes(candidates.encode(receipt))
        with self.assertRaisesRegex(RuntimeError, 'candidate_changed'):
            self.store.cancel(result['id'], self.expected, snapshot=snapshot, active_check=self.active)

    def test_directory_links_and_owner_mismatch_are_rejected(self):
        result = self.stage()
        original = self.store.path(result['id'])
        moved = self.root / 'saved-operation'
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises((OSError, RuntimeError)):
            self.cancel(result)
        original.unlink()
        moved.rename(original)
        wrong_owner = candidates.Store(self.store.base, uid=os.geteuid() + 1, anchor=self.root)
        with self.assertRaises(RuntimeError):
            wrong_owner.load(result['id'])
        self.assertEqual(self.store.load(result['id'])[0]['state'], 'prepared')

    def test_corrupt_candidate_claim_blocks_same_digest_without_overwrite(self):
        result = self.stage()
        path = self.store.path(result['id']) / 'candidate.json'
        path.write_text('external')
        before = self.files()
        with self.assertRaisesRegex(RuntimeError, 'candidate_needs_review:' + result['id']):
            self.stage()
        self.assertEqual(self.files(), before)

    def test_empty_crash_remnant_is_preserved_and_reported_not_a_global_deadlock(self):
        self.store.initialize()
        orphan = 'a' * 32
        self.store.path(orphan).mkdir(mode=0o700)
        result = self.stage()
        self.assertIn(orphan, result['preserved_needs_review'])
        self.assertEqual(list(self.store.path(orphan).iterdir()), [])

    def test_cancel_interruption_followed_by_foreign_edit_preserves_file(self):
        result = self.stage()
        original = candidates.atomic
        def interrupt(path, data):
            original(path, data)
            if json.loads(data).get('cleanup_pending'):
                raise Crash()
        with patch.object(candidates, 'atomic', side_effect=interrupt), self.assertRaises(Crash):
            self.cancel(result)
        path = self.store.path(result['id']) / 'candidate.json'
        path.write_text('external-change')
        with self.assertRaisesRegex(RuntimeError, 'candidate_changed'):
            self.cancel(result)
        self.assertEqual(path.read_text(), 'external-change')

    def test_preparing_complete_reentry_rechecks_after_reading_candidate(self):
        result = self.stage()
        receipt, raw = self.store.load(result['id'])
        self.store.update(result['id'], receipt, raw, state='preparing')
        changed = deepcopy(self.plan)
        changed['plan_digest'] = 'c' * 64
        self.build.side_effect = [self.plan, changed]
        with self.assertRaisesRegex(RuntimeError, 'plan_changed'):
            self.stage()
        self.assertEqual(self.store.load(result['id'])[0]['state'], 'failed_needs_review')

    def test_preparing_reentry_does_not_overwrite_tampering_during_recheck(self):
        result = self.stage()
        receipt, raw = self.store.load(result['id'])
        self.store.update(result['id'], receipt, raw, state='preparing')
        calls = 0
        path = self.store.path(result['id']) / 'candidate.json'
        def rebuild(device):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.write_text('foreign-change')
            return self.plan
        self.build.side_effect = rebuild
        with self.assertRaisesRegex(RuntimeError, 'candidate_changed'):
            self.stage()
        self.assertEqual(path.read_text(), 'foreign-change')

    def test_active_and_history_limits_preserve_existing_candidates(self):
        result = self.stage()
        before = self.files()
        self.fixture.raw['/proc/1/root/etc/fstab'] += '\n# new intent'
        new_plan = self.fixture.plan()
        with patch.object(candidates, 'MAX_ACTIVE', 1), self.assertRaisesRegex(RuntimeError, 'count_limit'):
            self.store.stage(self.device, new_plan['plan_digest'], build=self.build)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.cancel(result)['state'], 'cancelled')

    def test_registered_object_detection_reuses_trusted_reader(self):
        f = self.fixture.fixture
        reader = f.reader
        record = self.plan['confirmation']['effects']['candidate_record']
        selected = self.plan['confirmation']['selected_object']
        other = {'map_name': 'rr-data-other', 'map_uuid': 'RAMRESCUE-DATA-other'}
        manage.candidate_not_active(selected, reader=reader)
        f.write(str(manage.REGISTRY / 'rr-data-test.json'), record)
        with self.assertRaisesRegex(RuntimeError, 'active_registration'):
            manage.candidate_not_active(selected, reader=reader)
        manage.candidate_not_active(other, reader=reader)
        for conflict in (dict(other, map_name=selected['map_name']), dict(other, map_uuid=selected['map_uuid'])):
            with self.assertRaisesRegex(RuntimeError, 'active_registration'):
                manage.candidate_not_active(conflict, reader=reader)
        original_names = reader.names
        calls = 0
        def changed_names(path):
            nonlocal calls
            calls += 1
            return original_names(path) if calls == 1 else ['rr-data-replaced.json']
        with patch.object(reader, 'names', side_effect=changed_names), \
                self.assertRaisesRegex(RuntimeError, 'active_configuration_changed'):
            manage.candidate_not_active(other, reader=reader)
        path = f.root / str(manage.REGISTRY / 'rr-data-test.json').lstrip('/')
        original_json = reader.json
        calls = 0
        def changed_content(name):
            nonlocal calls
            value = original_json(name)
            if name.endswith('rr-data-test.json'):
                calls += 1
                if calls == 2:
                    value['guard']['queue_seconds'] += 1
            return value
        with patch.object(reader, 'json', side_effect=changed_content), \
                self.assertRaisesRegex(RuntimeError, 'active_configuration_changed'):
            manage.candidate_not_active(other, reader=reader)
        path.unlink()
        path.symlink_to(f.root / 'missing')
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            manage.candidate_not_active(other, reader=reader)
        path.unlink()
        root_config = f.root / str(manage.ROOT_CONFIG).lstrip('/')
        root_config.parent.mkdir(parents=True, exist_ok=True)
        root_config.symlink_to(f.root / 'missing-root')
        with self.assertRaises((OSError, ValueError, RuntimeError)):
            manage.candidate_not_active(other, reader=reader)
        root_config.unlink()
        with patch.object(reader, 'names', side_effect=PermissionError), self.assertRaises(PermissionError):
            manage.candidate_not_active(other, reader=reader)

    def test_active_registration_blocks_cancel_but_missing_media_does_not(self):
        result = self.stage()
        before = self.files()
        self.active.side_effect = RuntimeError('candidate_has_active_registration')
        with self.assertRaisesRegex(RuntimeError, 'active_registration'):
            self.cancel(result)
        self.assertEqual(before, self.files())
        self.active.side_effect = None
        self.build.side_effect = AssertionError('no media reads during cancellation')
        self.assertEqual(self.cancel(result)['state'], 'cancelled')

    def test_path_escape_is_rejected(self):
        for value in ('../outside', '/tmp', 'a' * 31, 'A' * 32):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'operation_id'):
                self.store.load(value)

    def test_candidate_capacity_reserves_history_and_bounds_bytes(self):
        with patch.object(candidates, 'MAX_HISTORY', 1):
            result = self.stage()
            self.cancel(result)
            with self.assertRaisesRegex(RuntimeError, 'count_limit'):
                self.stage()
        plan = deepcopy(self.plan)
        plan['confirmation']['scope']['mounts'] = ['x' * candidates.LIMIT]
        expected = plan['plan_digest'] = digest(plan['confirmation'])
        with self.assertRaisesRegex(RuntimeError, 'size_limit'):
            self.store.stage(self.device, expected, build=lambda device: plan)

    def test_cooperative_lock_conflicts_with_existing_manager_operations(self):
        lock = self.root / 'control.lock'
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with patch.object(manage, 'CONTROL_LOCK', lock), patch.object(manage.services, 'require_root'), \
                patch.object(manage, 'open_directory', side_effect=lambda path: os.open(path, os.O_DIRECTORY)), \
                patch.object(candidates, 'Store', return_value=self.store), \
                patch.object(planning, 'build', self.build):
            with self.assertRaises(BlockingIOError):
                manage.stage_candidate(self.device, self.expected)
            with self.assertRaises(BlockingIOError):
                manage.cancel_candidate('a' * 32, self.expected, {})
        self.build.assert_not_called()

    def test_ui_decline_eof_interrupt_or_missing_digest_never_enters_lock(self):
        for answer in ('否', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer), patch.object(planning, 'build', return_value=self.plan), \
                    patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=[answer]), \
                    patch.object(manage, 'stage_candidate', side_effect=AssertionError('lock entered')), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(candidate_ui.stage(self.device, None)['state'], 'declined')
                self.assertEqual(list(self.root.iterdir()), [])
        with patch.object(planning, 'build', return_value=self.plan), redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, 'confirmation_requires_expect_plan'):
            candidate_ui.stage(self.device, None, json_output=True)

    def test_ui_numbered_selection_and_confirmation_are_bound_to_displayed_plan(self):
        with patch.object(discovery := candidate_ui.discovery, 'collect', return_value={'volumes': [{'device': self.device}]}), \
                patch.object(discovery, 'format_text', return_value='1. 测试盘'), \
                patch.object(planning, 'build', return_value=self.plan) as build, \
                patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['1', '确认']), \
                patch.object(manage, 'stage_candidate', return_value={'state': 'prepared'}) as stage, \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(candidate_ui.stage(None, None)['state'], 'prepared')
            build.assert_called_once_with(self.device)
            stage.assert_called_once_with(self.device, self.expected)

    def test_ui_cancel_without_confirmation_has_no_mutation(self):
        result = self.stage()
        before = self.files()
        with patch.object(candidates, 'Store', return_value=self.store), patch('sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value='否'), redirect_stdout(io.StringIO()), \
                patch.object(manage, 'cancel_candidate', side_effect=AssertionError('lock entered')):
            self.assertEqual(candidate_ui.cancel(result['id'], None)['state'], 'declined')
        self.assertEqual(self.files(), before)

    def test_ui_json_blocked_support_has_no_prompt_or_persistent_write(self):
        self.fixture.qualification_path.unlink()
        blocked = self.fixture.plan()
        with patch.object(planning, 'build', return_value=blocked), \
                patch('builtins.input', side_effect=AssertionError('prompt')), redirect_stderr(io.StringIO()), \
                patch.object(manage, 'stage_candidate', side_effect=AssertionError('lock entered')):
            result = candidate_ui.stage(self.device, blocked['plan_digest'], json_output=True)
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('combination_unvalidated', result['plan']['confirmation']['blockers'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_automated_confirmation_rebuilds_inside_lock_without_active_mutations(self):
        lock = self.root / 'control.lock'
        with ExitStack() as stack:
            stack.enter_context(patch.object(candidates, 'Store', return_value=self.store))
            stack.enter_context(patch.object(planning, 'build', self.build))
            stack.enter_context(patch.object(manage, 'CONTROL_LOCK', lock))
            stack.enter_context(patch.object(manage, 'open_directory', side_effect=lambda path: os.open(path, os.O_DIRECTORY)))
            stack.enter_context(patch.object(manage.services, 'require_root'))
            stack.enter_context(patch.object(manage, 'candidate_not_active', self.active))
            for name in ('register', 'prepare', 'install', 'upgrade', 'uninstall', 'write_json', 'save_rules'):
                stack.enter_context(patch.object(manage, name, side_effect=AssertionError('active mutation: ' + name)))
            for name in ('run', 'start', 'stop', 'stage_runtime', 'enroll'):
                stack.enter_context(patch.object(manage.services, name, side_effect=AssertionError('service/device mutation: ' + name)))
            stack.enter_context(patch('subprocess.run', side_effect=AssertionError('external command')))
            stack.enter_context(patch('subprocess.Popen', side_effect=AssertionError('external command')))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            result = candidate_ui.stage(self.device, self.expected, json_output=True)
            self.assertEqual(self.build.call_count, 3)
            self.assertEqual(candidate_ui.cancel(result['id'], self.expected, json_output=True)['state'], 'cancelled')
        self.assertTrue(all(name == 'control.lock' or name.startswith('plans/operations/') for name in self.files()))


if __name__ == '__main__':
    unittest.main()
