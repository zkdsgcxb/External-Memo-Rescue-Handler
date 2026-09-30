"""Blocking ownership, late-result cleanup and real flock lifetime contracts."""
import builtins
import errno
import os
import select
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'guest'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'guard/runtime'))
from guard_state import Owner
from owned_operation import OwnedOperation


class OwnedOperationTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.run = Path(temporary.name)
        self.owner = Owner(self.run)
        self.addCleanup(self.owner.close)
        self.operation = self.executor()

    def executor(self):
        operation = OwnedOperation(self.owner.fd)
        self.addCleanup(self.finish, operation)
        return operation

    def finish(self, operation):
        operation.abandon(lambda outcome: None)
        self.wait(lambda: not operation.busy)

    def wait(self, predicate, message='operation did not complete'):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.001)
        self.fail(message)

    def ready(self, operation=None):
        operation = operation or self.operation
        # Observe publication without claiming a resource-owning result.
        with operation._lock:
            return operation._pending is not None and operation._pending.outcome is not None

    def assert_takeover_blocked(self):
        with self.assertRaises(BlockingIOError):
            Owner(self.run)

    def assert_takeover_available(self):
        with Owner(self.run):
            pass

    def test_idle_constructor_creates_no_worker_or_duplicate_fd(self):
        with patch('threading.Thread') as thread, patch('owned_operation.os.dup') as duplicate:
            operation = OwnedOperation(self.owner.fd)
            self.assertFalse(operation.busy)
            self.assertIsNone(operation.poll())
            operation.close()
        thread.assert_not_called()
        duplicate.assert_not_called()

    def test_only_one_running_or_unread_operation_is_allowed(self):
        entered, release = threading.Event(), threading.Event()

        def blocking():
            entered.set()
            release.wait(3)
            return 'result'

        token = self.operation.start('verify', blocking)
        try:
            self.assertTrue(entered.wait(1))
            self.assertIsNone(self.operation.poll())
            with self.assertRaisesRegex(RuntimeError, 'not been consumed'):
                self.operation.start('another', lambda: None)
        finally:
            release.set()
        self.wait(self.ready)
        with self.assertRaisesRegex(RuntimeError, 'not been consumed'):
            self.operation.start('another', lambda: None)
        outcome = self.operation.poll()
        self.assertEqual((outcome['kind'], outcome['token'], outcome['value']), ('verify', token, 'result'))
        self.assertIsNone(outcome['error'])
        self.assertGreaterEqual(outcome['elapsed'], 0)
        self.assertFalse(self.operation.busy)
        self.assertIsNone(self.operation.poll())
        self.assertGreater(self.operation.start('next', lambda: 'next'), token)
        self.assertEqual(self.wait(self.operation.poll)['value'], 'next')

    def test_running_and_completed_unread_result_keep_real_owner_fence(self):
        entered, release = threading.Event(), threading.Event()

        def blocking():
            entered.set()
            release.wait(3)
            return object()

        self.operation.start('verify', blocking)
        try:
            self.assertTrue(entered.wait(1))
            self.owner.close()
            self.assert_takeover_blocked()
        finally:
            release.set()
        self.wait(self.ready)
        self.assert_takeover_blocked()
        self.assertIsNotNone(self.operation.poll())
        self.assert_takeover_available()

    def test_abandon_returns_while_fn_blocks_and_cleans_late_value_once(self):
        entered, release = threading.Event(), threading.Event()
        value = object()
        cleanup = Mock()

        def blocking():
            entered.set()
            release.wait(3)
            return value

        token = self.operation.start('verify', blocking)
        try:
            self.assertTrue(entered.wait(1))
            self.operation.abandon(cleanup)
            self.operation.abandon(Mock(side_effect=AssertionError('second cleanup used')))
            self.assertTrue(self.operation.busy)
            self.assertIsNone(self.operation.poll())
            cleanup.assert_not_called()
            self.owner.close()
            self.assert_takeover_blocked()
            with self.assertRaisesRegex(RuntimeError, 'cannot restart'):
                self.operation.start('next', lambda: None)
        finally:
            release.set()
        self.wait(lambda: not self.operation.busy)
        cleanup.assert_called_once()
        self.assertIs(cleanup.call_args.args[0]['value'], value)
        self.assertEqual(cleanup.call_args.args[0]['token'], token)
        self.assertIsNone(self.operation.poll())
        self.assert_takeover_available()
        with self.assertRaisesRegex(RuntimeError, 'cannot restart'):
            self.operation.start('next', lambda: None)

    def test_abandon_cleans_already_published_unread_result(self):
        value = object()
        cleanup = Mock()
        self.operation.start('verify', lambda: value)
        self.wait(self.ready)
        self.owner.close()
        self.assert_takeover_blocked()
        self.operation.abandon(cleanup)
        self.wait(lambda: not self.operation.busy)
        cleanup.assert_called_once()
        self.assertIs(cleanup.call_args.args[0]['value'], value)
        self.assertIsNone(self.operation.poll())
        self.assert_takeover_available()

    def test_abandoned_exception_is_delivered_to_cleanup_not_poll(self):
        entered, release = threading.Event(), threading.Event()
        cleanup = Mock()

        def failing():
            entered.set()
            release.wait(3)
            raise SystemExit('worker failure')

        self.operation.start('ioctl', failing)
        try:
            self.assertTrue(entered.wait(1))
            self.operation.abandon(cleanup)
        finally:
            release.set()
        self.wait(lambda: not self.operation.busy)
        error = cleanup.call_args.args[0]['error']
        self.assertEqual(error['type'], 'SystemExit')
        self.assertIn('worker failure', error['message'])
        self.assertIn('failing', error['traceback'])
        self.assertIsNone(cleanup.call_args.args[0]['value'])
        self.assertIsNone(self.operation.poll())

    def test_fence_covers_blocked_cleanup_until_it_finishes(self):
        entered, release = threading.Event(), threading.Event()

        def cleanup(outcome):
            entered.set()
            release.wait(3)

        self.operation.start('verify', lambda: 'candidate')
        self.wait(self.ready)
        self.operation.abandon(cleanup)
        try:
            self.assertTrue(entered.wait(1))
            self.owner.close()
            self.assert_takeover_blocked()
            self.assertTrue(self.operation.busy)
        finally:
            release.set()
        self.wait(lambda: not self.operation.busy)
        self.assert_takeover_available()

    def test_cleanup_exception_is_bounded_recorded_and_releases_fence(self):
        def cleanup(outcome):
            raise RuntimeError('清理失败' * 5000)

        self.operation.start('verify', lambda: 'candidate')
        self.operation.abandon(cleanup)
        self.owner.close()
        self.wait(lambda: not self.operation.busy)
        error = self.operation.cleanup_error
        self.assertEqual(error['type'], 'RuntimeError')
        self.assertTrue(all(len(value.encode()) <= 4096 for value in error.values()))
        self.assertIn('cleanup', error['traceback'])
        self.assert_takeover_available()
        error['type'] = 'changed by caller'
        self.assertEqual(self.operation.cleanup_error['type'], 'RuntimeError')

    def test_operation_exception_is_bounded_and_result_can_be_polled_once(self):
        def failing():
            raise ValueError('操作失败' * 5000)

        self.operation.start('verify', failing)
        outcome = self.wait(self.operation.poll)
        self.assertEqual(outcome['error']['type'], 'ValueError')
        self.assertTrue(all(len(value.encode()) <= 4096 for value in outcome['error'].values()))
        self.assertIsNone(outcome['value'])
        self.assertIsNone(self.operation.poll())
        self.assertFalse(self.operation.busy)

    def test_idle_abandon_is_permanent_and_does_not_call_cleanup(self):
        cleanup = Mock()
        self.operation.abandon(cleanup)
        cleanup.assert_not_called()
        self.assertFalse(self.operation.busy)
        with self.assertRaisesRegex(RuntimeError, 'cannot restart'):
            self.operation.start('verify', lambda: None)

    def test_thread_constructor_and_event_failures_allocate_no_fence(self):
        for name in ('threading.Event', 'threading.Thread'):
            with self.subTest(name=name), patch(name, side_effect=MemoryError('construction failed')), \
                    patch('owned_operation.os.dup', wraps=os.dup) as duplicate:
                with self.assertRaises(MemoryError):
                    self.operation.start('verify', lambda: None)
                duplicate.assert_not_called()
                self.assertFalse(self.operation.busy)

    def test_import_failure_allocates_no_fence(self):
        original_import = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == 'threading':
                raise ImportError('unavailable')
            return original_import(name, *args, **kwargs)

        with patch('owned_operation.os.dup', wraps=os.dup) as duplicate, \
                patch('builtins.__import__', side_effect=importing):
            with self.assertRaises(ImportError):
                self.operation.start('verify', lambda: None)
        duplicate.assert_not_called()
        self.assertFalse(self.operation.busy)

    def test_dup_failure_never_starts_a_worker(self):
        with patch('owned_operation.os.dup', side_effect=OSError(errno.EMFILE, 'fd limit')), \
                patch('threading.Thread.start') as start:
            with self.assertRaises(OSError):
                self.operation.start('verify', lambda: None)
        start.assert_not_called()
        self.assertFalse(self.operation.busy)

    def test_start_failure_closes_fence_even_after_native_thread_was_created(self):
        original_start = threading.Thread.start
        threads = []
        fn = Mock()

        def started_then_failed(thread):
            threads.append(thread)
            original_start(thread)
            raise RuntimeError('start interrupted after native thread creation')

        with patch('threading.Thread.start', side_effect=started_then_failed, autospec=True):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.operation.start('verify', fn)
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        fn.assert_not_called()
        self.assertFalse(self.operation.busy)
        self.owner.close()
        self.assert_takeover_available()

    def test_start_failure_without_a_native_thread_closes_fence(self):
        with patch('threading.Thread.start', side_effect=RuntimeError('thread limit')):
            with self.assertRaisesRegex(RuntimeError, 'thread limit'):
                self.operation.start('verify', lambda: None)
        self.assertFalse(self.operation.busy)
        self.owner.close()
        self.assert_takeover_available()

    def test_poll_abandon_race_transfers_value_to_exactly_one_recipient(self):
        for index in range(30):
            with self.subTest(index=index):
                operation = self.executor()
                value = object()
                operation.start('verify', lambda: value)
                self.wait(lambda: self.ready(operation))
                recipients = []
                barrier = threading.Barrier(3)

                def poll():
                    barrier.wait()
                    outcome = operation.poll()
                    if outcome is not None:
                        recipients.append(('poll', outcome['value']))

                def abandon():
                    barrier.wait()
                    operation.abandon(lambda outcome: recipients.append(('cleanup', outcome['value'])))

                threads = [threading.Thread(target=poll), threading.Thread(target=abandon)]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                self.wait(lambda: not operation.busy)
                self.assertEqual(len(recipients), 1)
                self.assertIs(recipients[0][1], value)
                self.assertIsNone(operation.poll())

    def test_eventfd_wakes_select_only_after_result_publication_and_poll_drains_it(self):
        entered, release = threading.Event(), threading.Event()

        def blocking():
            entered.set()
            release.wait(3)
            return 'done'

        notification = self.operation.fileno()
        self.assertFalse(os.get_inheritable(notification))
        self.assertFalse(os.get_blocking(notification))
        self.assertEqual(select.select([notification], [], [], 0)[0], [])
        self.operation.start('verify', blocking)
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(select.select([notification], [], [], 0)[0], [])
        finally:
            release.set()
        self.assertEqual(select.select([notification], [], [], 2)[0], [notification])
        self.assertEqual(self.operation.poll()['value'], 'done')
        self.assertEqual(select.select([notification], [], [], 0)[0], [])
        self.operation.start('next', lambda: 'second')
        self.assertEqual(select.select([notification], [], [], 2)[0], [notification])
        self.assertEqual(self.operation.poll()['value'], 'second')

    def test_abandon_keeps_notification_fd_until_worker_cleanup_finishes(self):
        entered, release = threading.Event(), threading.Event()
        cleanup_entered, cleanup_release = threading.Event(), threading.Event()

        def blocking():
            entered.set()
            release.wait(3)
            return 'candidate'

        def cleanup(outcome):
            cleanup_entered.set()
            cleanup_release.wait(3)

        notification = self.operation.fileno()
        self.operation.start('verify', blocking)
        try:
            self.assertTrue(entered.wait(1))
            self.operation.abandon(cleanup)
            os.fstat(notification)  # Still valid while fn may publish to it.
            release.set()
            self.assertTrue(cleanup_entered.wait(1))
            os.fstat(notification)  # Also retained during cleanup.
        finally:
            release.set()
            cleanup_release.set()
        self.wait(lambda: not self.operation.busy)
        with self.assertRaises(OSError) as exc:
            os.fstat(notification)
        self.assertEqual(exc.exception.errno, errno.EBADF)
        with self.assertRaisesRegex(ValueError, 'closed'):
            self.operation.fileno()

    def test_idle_close_releases_notification_and_is_idempotent(self):
        notification = self.operation.fileno()
        self.operation.close()
        self.operation.close()
        with self.assertRaises(OSError):
            os.fstat(notification)
        with self.assertRaisesRegex(RuntimeError, 'cannot restart'):
            self.operation.start('verify', lambda: None)

    def test_close_cannot_discard_a_pending_value_without_cleanup(self):
        self.operation.start('verify', lambda: 'candidate')
        self.wait(self.ready)
        with self.assertRaisesRegex(RuntimeError, 'explicit cleanup'):
            self.operation.close()
        cleanup = Mock()
        self.operation.close(cleanup)
        self.wait(lambda: not self.operation.busy)
        cleanup.assert_called_once()

    def test_worker_gets_its_fence_after_original_owner_fd_was_closed(self):
        entered, release = threading.Event(), threading.Event()
        original = self.owner.fd

        def late_helper_setup():
            entered.set()
            release.wait(3)
            fence = self.operation.fence_fd()
            os.fstat(fence)
            return fence

        self.operation.start('helper', late_helper_setup)
        try:
            self.assertTrue(entered.wait(1))
            self.owner.close()
            self.assert_takeover_blocked()
            with self.assertRaisesRegex(RuntimeError, 'current operation worker'):
                self.operation.fence_fd()
        finally:
            release.set()
        outcome = self.wait(self.operation.poll)
        self.assertIsNone(outcome['error'])
        self.assertNotEqual(outcome['value'], original)
        self.assert_takeover_available()

    def test_no_fence_is_available_outside_an_owned_worker(self):
        with self.assertRaisesRegex(RuntimeError, 'current operation worker'):
            self.operation.fence_fd()
        self.operation.start('helper', lambda: None)
        self.wait(self.operation.poll)
        with self.assertRaisesRegex(RuntimeError, 'current operation worker'):
            self.operation.fence_fd()

    def test_notification_initialization_failure_does_not_duplicate_owner(self):
        with patch('owned_operation.os.eventfd', side_effect=OSError(errno.EMFILE, 'fd limit')), \
                patch('owned_operation.os.dup', wraps=os.dup) as duplicate:
            with self.assertRaises(OSError):
                OwnedOperation(self.owner.fd)
        duplicate.assert_not_called()
        os.fstat(self.owner.fd)

    def test_unprintable_exception_cannot_strand_fence_or_skip_cleanup(self):
        class BrokenError(Exception):
            def __str__(self):
                raise RuntimeError('broken exception formatter')

        def failing():
            raise BrokenError()

        cleanup = Mock()
        self.operation.start('verify', failing)
        self.operation.abandon(cleanup)
        self.owner.close()
        self.wait(lambda: not self.operation.busy)
        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args.args[0]['error']['type'], 'BrokenError')
        self.assertEqual(cleanup.call_args.args[0]['error']['message'], 'Exception formatting failed')
        self.assert_takeover_available()


if __name__ == '__main__':
    unittest.main()
