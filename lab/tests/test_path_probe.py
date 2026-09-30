"""Synchronous probe semantics; scheduling and fencing belong to OwnedOperation."""
import errno
import os
from pathlib import Path
import select
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'guest'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'guard/runtime'))
from dm_monitor import probe_paths
from guard_state import Owner
from owned_operation import OwnedOperation


class PathProbeTests(unittest.TestCase):
    def test_success_returns_kernel_result_and_owns_only_its_device_fd(self):
        with patch('dm_monitor.os.open', return_value=41) as opening, \
                patch('dm_monitor.os.close') as closing, \
                patch('dm_monitor.fcntl.ioctl', return_value=0) as ioctl, \
                patch('threading.Thread') as thread:
            result = probe_paths('/dev/mapper/example', 7)
        opening.assert_called_once_with('/dev/mapper/example', os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        ioctl.assert_called_once_with(41, 0xfd12)
        closing.assert_called_once_with(41)
        thread.assert_not_called()
        self.assertEqual((result['token'], result['status'], result['errno']), (7, 'completed', 0))
        self.assertEqual(result['source'], 'ioctl')
        self.assertGreaterEqual(result['elapsed'], 0)

    def test_ioctl_errors_never_become_success_and_close_device(self):
        for error in (errno.ENOTTY, errno.EINVAL, errno.EIO):
            with self.subTest(error=error), \
                    patch('dm_monitor.os.open', return_value=43), \
                    patch('dm_monitor.os.close') as closing, \
                    patch('dm_monitor.fcntl.ioctl', side_effect=OSError(error, 'I/O failed')):
                result = probe_paths('/dev/mapper/example', 3)
                self.assertEqual((result['errno'], result['status']), (error, 'error'))
                closing.assert_called_once_with(43)

    def test_no_paths_has_distinct_result(self):
        with patch('dm_monitor.os.open', return_value=44), \
                patch('dm_monitor.os.close') as closing, \
                patch('dm_monitor.fcntl.ioctl', side_effect=OSError(errno.ENOTCONN, 'no paths')):
            result = probe_paths('/dev/mapper/example', 4)
        self.assertEqual((result['errno'], result['status']), (errno.ENOTCONN, 'no_paths'))
        closing.assert_called_once_with(44)

    def test_open_failure_never_closes_an_unowned_descriptor(self):
        with patch('dm_monitor.os.open', side_effect=OSError(errno.ENOENT, 'missing map')), \
                patch('dm_monitor.os.close') as closing, patch('dm_monitor.fcntl.ioctl') as ioctl:
            result = probe_paths('/dev/mapper/example', 5)
        self.assertEqual((result['errno'], result['status']), (errno.ENOENT, 'error'))
        closing.assert_not_called()
        ioctl.assert_not_called()

    def test_unexpected_exception_closes_device_and_is_left_to_owned_executor(self):
        with patch('dm_monitor.os.open', return_value=45), \
                patch('dm_monitor.os.close') as closing, \
                patch('dm_monitor.fcntl.ioctl', side_effect=RuntimeError('unexpected failure')):
            with self.assertRaisesRegex(RuntimeError, 'unexpected failure'):
                probe_paths('/dev/mapper/example', 6)
        closing.assert_called_once_with(45)

    def test_blocked_probe_runs_under_one_owned_worker_and_rejects_late_result(self):
        (BASE / 'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=BASE / 'work') as temporary:
            folder = Path(temporary)
            device = folder / 'mock-block-device'
            device.touch()
            owner = Owner(folder)
            operation = OwnedOperation(owner.fd)
            entered, release = threading.Event(), threading.Event()
            cleaned = []

            def ioctl(*args):
                entered.set()
                if not release.wait(3):
                    raise OSError(errno.ETIMEDOUT, 'test did not release ioctl')

            try:
                with patch('dm_monitor.fcntl.ioctl', side_effect=ioctl):
                    operation.start('probe', lambda: probe_paths(str(device), 7))
                    try:
                        self.assertTrue(entered.wait(1))
                        self.assertIsNone(operation.poll())
                        self.assertEqual(select.select([operation.fileno()], [], [], 0)[0], [])
                        with self.assertRaises(RuntimeError):
                            operation.start('probe', lambda: probe_paths(str(device), 8))
                        operation.abandon(cleaned.append)
                        owner.close()
                        with self.assertRaises(BlockingIOError):
                            Owner(folder)
                    finally:
                        release.set()
                    deadline = time.monotonic() + 3
                    while operation.busy and time.monotonic() < deadline:
                        time.sleep(.001)
                self.assertFalse(operation.busy)
                self.assertIsNone(operation.poll())
                self.assertEqual(len(cleaned), 1)
                self.assertIsNone(cleaned[0]['error'])
                self.assertEqual(cleaned[0]['value']['token'], 7)
                self.assertEqual(cleaned[0]['value']['status'], 'completed')
                with Owner(folder):
                    pass
            finally:
                release.set()
                operation.abandon(lambda outcome: None)
                owner.close()


if __name__ == '__main__':
    unittest.main()
