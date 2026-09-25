"""The synchronous kernel probe must never block the Guard's deadline loop."""
import builtins
import errno
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'guest'))
from dm_monitor import PathProbe
from guard_state import Owner


class PathProbeTests(unittest.TestCase):
    def result(self, probe):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = probe.poll()
            if result is not None:
                return result
            time.sleep(.005)
        self.fail('Probe worker did not publish its result')

    def test_blocking_ioctl_is_async_and_only_one_result_can_be_outstanding(self):
        entered = threading.Event()
        release = threading.Event()
        returned = threading.Event()
        outcomes = []

        def ioctl(*args):
            entered.set()
            release.wait(3)
            return 0

        probe = PathProbe()
        with patch('dm_monitor.os.open', return_value=41), \
             patch('dm_monitor.os.close') as close, \
             patch('dm_monitor.fcntl.ioctl', side_effect=ioctl) as call:
            def start():
                outcomes.append(probe.start('/dev/mapper/example', 7))
                returned.set()

            starter = threading.Thread(target=start, daemon=True)
            starter.start()
            try:
                self.assertTrue(entered.wait(1), 'Probe did not reach the mock ioctl')
                self.assertTrue(returned.wait(1), 'start() waited for a blocked ioctl')
                self.assertEqual(outcomes, [None])
                self.assertTrue(probe.busy)
                self.assertIsNone(probe.poll())
                with self.assertRaises(RuntimeError):
                    probe.start('/dev/mapper/example', 8)
                call.assert_called_once()
            finally:
                release.set()
                starter.join(2)
            # Completing the syscall must not permit overwriting an unread result.
            deadline = time.monotonic() + 2
            while close.call_count == 0 and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(probe.busy)
            with self.assertRaises(RuntimeError):
                probe.start('/dev/mapper/example', 9)
            result = self.result(probe)
            self.assertEqual(result['token'], 7)
            self.assertEqual(result['errno'], 0)
            self.assertEqual(result['status'], 'completed')
            self.assertGreaterEqual(result['elapsed'], 0)
            self.assertFalse(probe.busy)
            close.assert_called_once_with(41)
            self.assertEqual(call.call_args.args[:2], (41, 0xfd12))

    def test_ioctl_error_is_not_a_success_and_closes_device(self):
        for error in (errno.ENOTTY, errno.EINVAL, errno.EIO):
            with self.subTest(error=error):
                probe = PathProbe()
                with patch('dm_monitor.os.open', return_value=43), \
                     patch('dm_monitor.os.close') as close, \
                     patch('dm_monitor.fcntl.ioctl', side_effect=OSError(error, 'I/O failed')):
                    probe.start('/dev/mapper/example', 3)
                    result = self.result(probe)
                self.assertEqual((result['errno'], result['status']), (error, 'error'))
                self.assertEqual(result['source'], 'ioctl')
                close.assert_called_once_with(43)

    def test_no_paths_has_distinct_result(self):
        probe = PathProbe()
        with patch('dm_monitor.os.open', return_value=44), \
             patch('dm_monitor.os.close') as close, \
             patch('dm_monitor.fcntl.ioctl', side_effect=OSError(errno.ENOTCONN, 'no paths')):
            probe.start('/dev/mapper/example', 4)
            result = self.result(probe)
        self.assertEqual((result['errno'], result['status']), (errno.ENOTCONN, 'no_paths'))
        close.assert_called_once_with(44)

    def test_open_failure_never_closes_an_unowned_descriptor(self):
        probe = PathProbe()
        with patch('dm_monitor.os.open', side_effect=OSError(errno.ENOENT, 'missing map')), \
             patch('dm_monitor.os.close') as close, \
             patch('dm_monitor.fcntl.ioctl') as ioctl:
            probe.start('/dev/mapper/example', 5)
            result = self.result(probe)
        self.assertEqual((result['errno'], result['status']), (errno.ENOENT, 'error'))
        close.assert_not_called()
        ioctl.assert_not_called()


class PathProbeFenceTests(unittest.TestCase):
    result = PathProbeTests.result

    def setUp(self):
        work = Path(__file__).resolve().parents[1] / 'work'
        work.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(temporary.cleanup)
        self.run = Path(temporary.name)
        self.device = self.run / 'mock-block-device'
        self.device.touch()
        self.owner = Owner(self.run)
        self.addCleanup(self.owner.close)

    def assert_takeover_available(self):
        with Owner(self.run):
            pass

    def test_blocked_ioctl_retains_lock_after_main_owner_closes(self):
        entered = threading.Event()
        release = threading.Event()
        probe = PathProbe(owner_fd=self.owner.fd)

        def ioctl(*args):
            entered.set()
            if not release.wait(3):
                raise OSError(errno.ETIMEDOUT, 'test did not release ioctl')

        with patch('dm_monitor.fcntl.ioctl', side_effect=ioctl):
            probe.start(str(self.device), 1)
            try:
                self.assertTrue(entered.wait(1))
                self.owner.close()
                with self.assertRaises(BlockingIOError):
                    Owner(self.run)
                self.assertTrue(probe.busy)
                self.assertIsNone(probe.poll())
            finally:
                release.set()
                result = self.result(probe)
        self.assertEqual(result['status'], 'completed')
        self.assert_takeover_available()

    def test_thread_start_failure_closes_duplicated_lock_fd(self):
        probe = PathProbe(owner_fd=self.owner.fd)
        duplicates = []
        original_dup = os.dup

        def duplicate(fd):
            result = original_dup(fd)
            duplicates.append(result)
            return result

        with patch('dm_monitor.os.dup', side_effect=duplicate), \
                patch('threading.Thread.start', side_effect=RuntimeError('no thread resources')):
            with self.assertRaisesRegex(RuntimeError, 'no thread resources'):
                probe.start(str(self.device), 1)
        self.assertEqual(len(duplicates), 1)
        with self.assertRaises(OSError) as exc:
            os.fstat(duplicates[0])
        self.assertEqual(exc.exception.errno, errno.EBADF)
        self.assertFalse(probe.busy)
        self.owner.close()
        self.assert_takeover_available()

    def test_thread_construction_failure_does_not_acquire_lock_reference(self):
        probe = PathProbe(owner_fd=self.owner.fd)
        with patch('dm_monitor.os.dup', wraps=os.dup) as duplicate, \
                patch('threading.Thread', side_effect=MemoryError('construction failed')):
            with self.assertRaises(MemoryError):
                probe.start(str(self.device), 1)
        duplicate.assert_not_called()
        self.assertFalse(probe.busy)
        self.owner.close()
        self.assert_takeover_available()

    def test_thread_import_failure_does_not_acquire_lock_reference(self):
        probe = PathProbe(owner_fd=self.owner.fd)
        original_import = builtins.__import__

        def importing(name, *args, **kwargs):
            if name == 'threading':
                raise ImportError('threading unavailable')
            return original_import(name, *args, **kwargs)

        with patch('dm_monitor.os.dup', wraps=os.dup) as duplicate, \
                patch('builtins.__import__', side_effect=importing):
            with self.assertRaises(ImportError):
                probe.start(str(self.device), 1)
        duplicate.assert_not_called()
        self.assertFalse(probe.busy)
        self.owner.close()
        self.assert_takeover_available()

    def test_dup_failure_does_not_leave_probe_marked_busy(self):
        probe = PathProbe(owner_fd=self.owner.fd)
        with patch('dm_monitor.os.dup', side_effect=OSError(errno.EMFILE, 'fd limit')):
            with self.assertRaises(OSError):
                probe.start(str(self.device), 1)
        self.assertFalse(probe.busy)
        self.owner.close()
        self.assert_takeover_available()

    def test_device_open_failure_releases_worker_lock_reference(self):
        probe = PathProbe(owner_fd=self.owner.fd)
        probe.start(str(self.run / 'missing-device'), 1)
        result = self.result(probe)
        self.assertEqual(result['errno'], errno.ENOENT)
        self.owner.close()
        self.assert_takeover_available()


if __name__ == '__main__':
    unittest.main()
