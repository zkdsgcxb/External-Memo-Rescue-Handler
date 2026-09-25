"""Real process fencing and bounded RAM-journal contracts; no device mapper."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'guest'))
import guard_state


class TemporaryRun(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.run = Path(temporary.name)


class OwnerTests(TemporaryRun):
    def test_second_owner_is_rejected_until_first_fd_is_closed(self):
        owner = guard_state.Owner(self.run)
        self.addCleanup(owner.close)
        first_epoch = owner.epoch
        self.assertFalse(os.get_inheritable(owner.fd))
        self.assertEqual((self.run / 'path-owner.lock').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(BlockingIOError):
            guard_state.Owner(self.run)
        owner.close()
        owner.close()
        with guard_state.Owner(self.run) as second:
            self.assertNotEqual(first_epoch, second.epoch)
            self.assertEqual(second.boot_id, owner.boot_id)

    def holding_child(self, owner):
        source = ('import os, sys\n'
                  'fd = int(sys.argv[1])\n'
                  'os.fstat(fd)\n'
                  'print("holding", flush=True)\n'
                  'sys.stdin.buffer.read(1)\n'
                  'os.close(fd)\n')
        child = subprocess.Popen([sys.executable, '-c', source, str(owner.fd)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, pass_fds=(owner.fd,))

        def cleanup():
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=3)
        self.addCleanup(cleanup)
        readable, _, _ = select.select([child.stdout], [], [], 3)
        self.assertTrue(readable, 'child did not confirm inherited fd')
        self.assertEqual(child.stdout.readline(), b'holding\n')
        return child

    def test_helper_inherits_fence_after_parent_closes_its_copy(self):
        owner = guard_state.Owner(self.run)
        self.addCleanup(owner.close)
        child = self.holding_child(owner)
        owner.close()
        with self.assertRaises(BlockingIOError):
            guard_state.Owner(self.run)
        stdout, stderr = child.communicate(input=b'x', timeout=3)
        self.assertEqual((child.returncode, stdout, stderr), (0, b'', b''))
        with guard_state.Owner(self.run):
            pass

    def test_helper_death_releases_last_fence_reference(self):
        owner = guard_state.Owner(self.run)
        self.addCleanup(owner.close)
        child = self.holding_child(owner)
        owner.close()
        with self.assertRaises(BlockingIOError):
            guard_state.Owner(self.run)
        child.kill()
        child.communicate(timeout=3)
        with guard_state.Owner(self.run):
            pass

    def test_initialization_failure_after_flock_closes_fd_and_releases_lock(self):
        with patch.object(guard_state.Path, 'read_text', side_effect=OSError('boot id unavailable')):
            with self.assertRaisesRegex(OSError, 'boot id unavailable'):
                guard_state.Owner(self.run)
        # This would raise BlockingIOError if the failed initializer leaked fd.
        with guard_state.Owner(self.run):
            pass


class JournalTests(TemporaryRun):
    def journal(self):
        owner = guard_state.Owner(self.run)
        self.addCleanup(owner.close)
        return guard_state.Journal(owner, 'test-map', 'test-map-uuid')

    def test_atomic_json_destination_keeps_previous_record_until_replace(self):
        target = self.run / 'record.json'
        old = {'phase': 'old', 'candidate': None}
        new = {'phase': 'new', 'candidate': {'diskseq': 12}}
        guard_state.atomic_json(target, old)
        replace = Path.replace
        calls = []

        def inspect_replace(source, destination):
            self.assertEqual(json.loads(target.read_text()), old)
            self.assertEqual(json.loads(source.read_text()), new)
            self.assertEqual(source.parent, target.parent)
            calls.append((source, destination))
            return replace(source, destination)

        with patch.object(guard_state.Path, 'replace', inspect_replace):
            guard_state.atomic_json(target, new)
        self.assertEqual(len(calls), 1)
        self.assertEqual(guard_state.load_json(target), new)
        self.assertFalse(target.with_name('record.json.tmp').exists())

    def test_journal_persists_owner_identity_candidate_and_deadline(self):
        journal = self.journal()
        candidate = {'instance': {'diskseq': 45}, 'owner_epoch': journal.record['owner_epoch']}
        journal.write('verified', candidate=candidate, deadline=106.)
        self.assertEqual(guard_state.load_json(journal.path), journal.record)
        self.assertEqual(journal.record['phase'], 'verified')
        self.assertEqual(journal.record['owner_pid'], os.getpid())
        self.assertEqual(journal.record['map_uuid'], 'test-map-uuid')
        journal.write('loaded', inactive_digest='table-sha256')
        persisted = guard_state.load_json(journal.path)
        self.assertEqual(persisted['phase'], 'loaded')
        self.assertEqual(persisted['candidate'], candidate)
        self.assertEqual(persisted['deadline'], 106.)
        self.assertEqual(persisted['inactive_digest'], 'table-sha256')

    def test_failed_replace_preserves_both_journal_and_in_memory_phase(self):
        journal = self.journal()
        journal.write('verified', deadline=106.)
        previous = dict(journal.record)
        with patch.object(guard_state.Path, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                journal.write('loaded', inactive_digest='new-table')
        self.assertEqual(journal.record, previous)
        self.assertEqual(guard_state.load_json(journal.path), previous)

    def test_oversized_record_does_not_advance_journal(self):
        journal = self.journal()
        journal.write('verified')
        previous = dict(journal.record)
        with self.assertRaisesRegex(ValueError, 'limit'):
            journal.write('loaded', excessive='x' * guard_state.LOG_LIMIT)
        self.assertEqual(journal.record, previous)
        self.assertEqual(guard_state.load_json(journal.path), previous)

    def test_unserializable_record_does_not_advance_journal(self):
        journal = self.journal()
        journal.write('verified')
        previous = dict(journal.record)
        with self.assertRaises(TypeError):
            journal.write('loaded', invalid=object())
        self.assertEqual(journal.record, previous)
        self.assertEqual(guard_state.load_json(journal.path), previous)

    def test_load_rejects_oversized_file_before_parsing(self):
        path = self.run / 'oversized.json'
        path.write_bytes(b'x' * (guard_state.LOG_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, 'Oversized'):
            guard_state.load_json(path)


class EvidenceTests(TemporaryRun):
    def test_log_rotates_to_one_previous_file_and_all_records_remain_whole(self):
        evidence = guard_state.Evidence(self.run)
        for index in range(30):
            evidence.event({'sequence': index, 'state': 'waiting', 'payload': 'x' * 11000})
        paths = sorted(self.run.glob('path-events*.jsonl'))
        self.assertEqual({path.name for path in paths},
                         {'path-events.jsonl', 'path-events.previous.jsonl'})
        records = []
        for path in paths:
            self.assertLessEqual(path.stat().st_size, guard_state.LOG_LIMIT)
            records.extend(json.loads(line) for line in path.read_text().splitlines())
        self.assertLess(len(records), 30)
        self.assertEqual(max(record['sequence'] for record in records), 29)
        state = guard_state.load_json(self.run / 'path-state.json')
        self.assertEqual(state['sequence'], 29)
        current = [json.loads(line) for line in (self.run / 'path-events.jsonl').read_text().splitlines()]
        self.assertEqual(current[-1], state)

    def test_oversized_event_changes_neither_log_nor_latest_state(self):
        evidence = guard_state.Evidence(self.run)
        evidence.event({'state': 'ready'})
        before = {path.name: path.read_bytes() for path in self.run.iterdir()}
        with self.assertRaisesRegex(ValueError, 'limit'):
            evidence.event({'state': 'waiting', 'payload': 'x' * guard_state.LOG_LIMIT})
        self.assertEqual({path.name: path.read_bytes() for path in self.run.iterdir()}, before)

    def test_unicode_size_budget_is_measured_on_encoded_record(self):
        evidence = guard_state.Evidence(self.run)
        with self.assertRaisesRegex(ValueError, 'limit'):
            evidence.event({'payload': '盘' * (guard_state.LOG_LIMIT // 3)})
        self.assertEqual(list(self.run.iterdir()), [])


class TableDigestTests(unittest.TestCase):
    def target(self, params=None, start=0, size=2048, kind='multipath'):
        return [(start, size, kind, params or
                 '3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 8:17 1')]

    def test_normalizes_queue_policy_and_current_group_only(self):
        baseline = guard_state.table_digest(self.target())
        for params in (
                '2 queue_mode bio 0 1 1 round-robin 0 1 1 8:17 1',
                '3 queue_if_no_path queue_mode bio 0 1 0 round-robin 0 1 1 8:17 1',
                '2 queue_mode bio 0 1 0 round-robin 0 1 1 8:17 1'):
            with self.subTest(params=params):
                self.assertEqual(guard_state.table_digest(self.target(params)), baseline)

    def test_different_backend_geometry_selector_or_mode_is_not_equivalent(self):
        baseline = guard_state.table_digest(self.target())
        changed = [self.target(start=1), self.target(size=4096), self.target(kind='linear'),
                   self.target('3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 8:33 1'),
                   self.target('3 queue_if_no_path queue_mode bio 0 1 1 queue-length 0 1 1 8:17 1'),
                   self.target('3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 8:17 8'),
                   self.target('3 queue_if_no_path queue_mode mq 0 1 1 round-robin 0 1 1 8:17 1'),
                   self.target('4 queue_if_no_path queue_mode bio retain_attached_hw_handler 0 1 1 round-robin 0 1 1 8:17 1')]
        for target in changed:
            with self.subTest(target=target):
                self.assertNotEqual(guard_state.table_digest(target), baseline)

    def test_unsupported_group_count_or_selection_is_rejected(self):
        for params in ('3 queue_if_no_path queue_mode bio 0 2 1 round-robin 0 1 1 8:17 1',
                       '3 queue_if_no_path queue_mode bio 0 1 2 round-robin 0 1 1 8:17 1'):
            with self.subTest(params=params):
                with self.assertRaisesRegex(RuntimeError, 'topology'):
                    guard_state.table_digest(self.target(params))

    def test_describe_preserves_raw_tables_and_does_not_modify_input(self):
        snapshot = {'active': self.target(), 'inactive': self.target(size=4096),
                    'info': {'suspended': False}}
        original = json.loads(json.dumps(snapshot))
        description = guard_state.describe(snapshot)
        self.assertNotEqual(description['active_digest'], description['inactive_digest'])
        self.assertEqual(json.loads(json.dumps(snapshot)), original)
        self.assertEqual(description['active'], snapshot['active'])
        self.assertEqual(description['inactive'], snapshot['inactive'])
        self.assertIsNone(guard_state.describe({'active': self.target(), 'inactive': []})['inactive_digest'])


if __name__ == '__main__':
    unittest.main()
