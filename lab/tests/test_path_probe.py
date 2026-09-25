"""The synchronous kernel probe must never block the Guard's deadline loop."""
import errno
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'guest'))
from dm_monitor import PathProbe


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


if __name__ == '__main__':
    unittest.main()
