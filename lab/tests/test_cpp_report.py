"""Reports from different execution environments must not form one comparison."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_report


def reports():
    values = []
    for index, implementation in enumerate(('python', 'cpp', 'python', 'cpp')):
        report = {'implementation': implementation, 'scenario': 'performance', 'quota_percent': 20,
                  'baseline_ref': 'release', 'source_image_sha256_before': 'image',
                  'build': {'kernel_sha256': 'kernel'}, 'recovery_rpc_quiet': True,
                  'experiment_manifest': {'binary_sha256': 'native', 'library_sha256': {'libc': 'library'}},
                  'fault': {'root': {'removed': index}}}
        values.append((Path(str(index)), report, b'report'))
    return values


class ComparisonEnvironmentTests(unittest.TestCase):
    def load(self, values):
        # Raw report verification belongs to the existing shared loader. This
        # seam tests the additional policy for comparisons across valid runs.
        with patch.object(cpp_report.performance_report, 'load_reports', return_value=values):
            return cpp_report.load_trials([], 2)

    def test_same_execution_environment_is_accepted(self):
        self.assertEqual(len(self.load(reports())), 4)

    def test_same_root_image_with_another_kernel_is_refused(self):
        values = reports()
        values[-1][1]['build']['kernel_sha256'] = 'different-kernel'
        with self.assertRaisesRegex(ValueError, 'kernel'):
            self.load(values)

    def test_same_native_binary_with_different_library_is_refused(self):
        values = reports()
        values[-1][1]['experiment_manifest']['library_sha256']['libc'] = 'different-library'
        with self.assertRaisesRegex(ValueError, 'library closures'):
            self.load(values)

    def test_recovery_metrics_follow_label_when_phase_order_changes(self):
        values = reports()
        metrics = {key: 1 for key in ('cpu_mean_percent', 'cpu_total_usec', 'cpu_peak_20ms_percent',
                                     'cpu_peak_100ms_percent', 'memory_sampled_peak_bytes')}
        for _, report, _ in values:
            report['phases'] = []
            for label in ('recovery', 'idle', 'relevant', 'unrelated'):
                phase = {'label': label, 'groups': {'aggregate': copy.copy(metrics)},
                         'after': {name: {'process_totals_bytes': {'Pss': 1}} for name in ('root', 'data_slice')}}
                if label == 'recovery':
                    phase['recovery_windows'] = {'aggregate': {'journal_seconds': 3,
                                                             'accounting': {'cpu_total_usec': 81, 'cpu_mean_percent': 2}}}
                report['phases'].append(phase)
            report['root_audit'] = {'max_write_seconds': 3}
            report['audit'] = {'audit': {name: {'max_write_and_direct_read_seconds': 3}
                                         for name in ('rr-data-vm-ext4', 'rr-data-vm-vfat')}}
        result = cpp_report.summarize(values)
        self.assertEqual(result['cpp']['active_recovery']['cpu_total_usec']['median'], 81)


if __name__ == '__main__':
    unittest.main()
