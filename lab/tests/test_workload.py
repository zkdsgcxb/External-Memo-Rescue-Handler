"""ACKs require a complete record and both file and directory fsync."""
import errno
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'guest'))
import workload


class Writer:
    def __init__(self, results=()):
        self.results = iter(results)
        self.data = bytearray()

    def write(self, payload):
        result = next(self.results, len(payload))
        if isinstance(result, Exception):
            raise result
        if type(result) is int and result > 0:
            self.data.extend(payload[:result])
        return result

    def fileno(self):
        return 41

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class WriteAllTests(unittest.TestCase):
    def test_short_writes_preserve_all_bytes_in_order(self):
        writer = Writer([2, 1, 3])
        payload = b'1\n' + b'x' * 4096
        workload.write_all(writer, payload)
        self.assertEqual(writer.data, payload)

    def test_zero_none_or_invalid_progress_is_not_acknowledgeable(self):
        for progress in (0, None, -1, 9):
            with self.subTest(progress=progress):
                with self.assertRaises(OSError) as exc:
                    workload.write_all(Writer([progress]), b'abc')
                self.assertEqual(exc.exception.errno, errno.EIO)

    def test_error_after_partial_write_propagates_without_restarting_record(self):
        writer = Writer([2, OSError(errno.ENOSPC, 'full')])
        with self.assertRaises(OSError) as exc:
            workload.write_all(writer, b'1\nxxxx')
        self.assertEqual(exc.exception.errno, errno.ENOSPC)
        self.assertEqual(writer.data, b'1\n')


class AcknowledgementTests(unittest.TestCase):
    def one_iteration(self, writer, failing_fsync=None):
        operations = []
        records = []

        class Log(io.StringIO):
            def write(self, text):
                records.append(json.loads(text))
                operations.append('ack' if records[-1]['ok'] else 'failure')
                return super().write(text)

        def fsync(fd):
            operations.append(('fsync', fd))
            if fd == failing_fsync:
                raise OSError(errno.EIO, 'fsync failed')

        with patch('builtins.open', side_effect=[Log(), writer]), \
                patch('workload.os.open', return_value=42) as open_directory, \
                patch('workload.os.close') as close_directory, \
                patch('workload.os.fsync', side_effect=fsync), \
                patch('workload.time.sleep', side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                workload.main()
        return records[0], operations, open_directory, close_directory

    def test_ack_only_follows_complete_write_and_both_fsyncs(self):
        writer = Writer([1, 7, 16])
        record, operations, open_directory, close_directory = self.one_iteration(writer)
        self.assertTrue(record['ok'])
        self.assertEqual(writer.data, b'1\n' + b'x' * 4096)
        self.assertEqual(operations, [('fsync', 41), ('fsync', 42), 'ack'])
        open_directory.assert_called_once_with('/root', workload.os.O_RDONLY | workload.os.O_DIRECTORY)
        close_directory.assert_called_once_with(42)

    def test_incomplete_write_never_reaches_fsync_or_success_ack(self):
        record, operations, open_directory, _ = self.one_iteration(Writer([2, 0]))
        self.assertFalse(record['ok'])
        self.assertEqual(record['errno'], errno.EIO)
        self.assertEqual(operations, ['failure'])
        open_directory.assert_not_called()

    def test_directory_fsync_failure_cannot_be_reported_as_success(self):
        record, operations, _, close_directory = self.one_iteration(Writer(), failing_fsync=42)
        self.assertFalse(record['ok'])
        self.assertEqual(record['errno'], errno.EIO)
        self.assertEqual(operations, [('fsync', 41), ('fsync', 42), 'failure'])
        close_directory.assert_called_once_with(42)


if __name__ == '__main__':
    unittest.main()
