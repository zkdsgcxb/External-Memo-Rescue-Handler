"""A cold audit verifies the exact consecutive ACK prefix, including its order."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location('cold_audit_runner', BASE / 'cold_audit.py')
cold_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cold_audit)


class AcknowledgedPrefixTests(unittest.TestCase):
    def test_prefix_includes_every_record_and_sequence_in_order(self):
        rows = [{'seq': sequence, 'ok': True} for sequence in range(1, 13)]
        expected = b''.join((str(row['seq']) + '\n').encode() + b'x' * 4096 for row in rows)
        self.assertEqual(cold_audit.acknowledged_prefix(rows),
                         (len(expected), hashlib.sha256(expected).hexdigest()))

    def test_missing_repeated_out_of_order_or_noninteger_sequence_is_rejected(self):
        for sequences in ([2], [1, 3], [1, 1], [1, 3, 2], [True], ['1']):
            with self.subTest(sequences=sequences):
                with self.assertRaisesRegex(ValueError, 'contiguous sequence'):
                    cold_audit.acknowledged_prefix([{'seq': sequence, 'ok': True} for sequence in sequences])

    def test_failed_or_empty_stream_is_not_a_durability_success(self):
        for rows in ([], [{'seq': 1, 'ok': False}], [{'seq': 1, 'ok': 'yes'}], [{}]):
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    cold_audit.acknowledged_prefix(rows)

    def test_prefix_size_limit_is_checked_while_streaming(self):
        with patch.object(cold_audit, 'MAX_AUDIT_BYTES', 4098):
            self.assertEqual(cold_audit.acknowledged_prefix([{'seq': 1, 'ok': True}])[0], 4098)
            with self.assertRaisesRegex(ValueError, 'bounded audit size'):
                cold_audit.acknowledged_prefix([{'seq': 1, 'ok': True}, {'seq': 2, 'ok': True}])


if __name__ == '__main__':
    unittest.main()
