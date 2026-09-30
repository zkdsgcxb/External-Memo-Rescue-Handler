"""Accounting uses elapsed time and avoids charging a parent plus its children."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import performance_probe


class ResourceAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = {'UnifiedProbe': object}
        source = Path(__file__).resolve().parents[1] / 'guest/performance_probe.py'
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
        cls.summarize = staticmethod(namespace['resource_summary'])

    def test_combined_cpu_memory_and_throttling(self):
        samples = []
        for index in range(11):
            groups = {name: {'cpu': {'usage_usec': index * cpu, 'throttled_usec': index * throttle},
                             'memory_bytes': memory}
                      for name, cpu, memory, throttle in [('root', 1000, 100, 2),
                                                         ('data_slice', 2000, 200, 3),
                                                         ('data_child', 2000, 200, 3)]}
            samples.append({'time': index * .02, 'groups': groups})
        result = self.summarize(samples, ['root', 'data_slice'])
        self.assertAlmostEqual(result['cpu_mean_percent'], 15)
        self.assertAlmostEqual(result['cpu_peak_100ms_percent'], 15)
        self.assertEqual(result['cpu_total_usec'], 30000)
        self.assertEqual(result['memory_mean_bytes'], 300)
        self.assertEqual(result['throttled_usec'], 50)

    def test_delayed_samples_use_real_elapsed_time(self):
        samples = [{'time': now, 'groups': {'root': {
            'cpu': {'usage_usec': cpu}, 'memory_bytes': 100}}}
            for now, cpu in [(0, 0), (.02, 1000), (.10, 2000)]]
        result = self.summarize(samples, ['root'])
        self.assertAlmostEqual(result['cpu_mean_percent'], 2)
        self.assertAlmostEqual(result['cpu_peak_20ms_percent'], 5)
        self.assertIsNone(result['cpu_peak_100ms_percent'])


class QuietRecoveryTests(unittest.TestCase):
    def test_no_serial_request_before_quiet_wait_finishes(self):
        order = []
        result = {'label': 'recovery', 'samples': [1, 2]}
        report = {'quiet_wait_seconds': 17}
        with patch.object(performance_probe.time, 'monotonic', side_effect=[10, 27]), \
             patch.object(performance_probe.time, 'sleep', side_effect=lambda delay: order.append(('wait', delay))), \
             patch.object(performance_probe.data, 'ram_call', side_effect=lambda *args: order.append(('rpc', args)) or result):
            self.assertIs(performance_probe.quiet_recovery_result(Path('/vm'), report), result)
        self.assertEqual(order, [('wait', 17), ('rpc', (Path('/vm'), 'sampler_result'))])
        self.assertEqual(report['recovery_rpc_quiet_window'], {
            'clock': 'host monotonic', 'quiet_start': 10, 'quiet_end': 27, 'first_fetch_completed': True})

    def test_incomplete_first_fetch_fails_without_polling(self):
        report = {'quiet_wait_seconds': 17}
        with patch.object(performance_probe.time, 'sleep'), \
             patch.object(performance_probe.data, 'ram_call', return_value=None) as request:
            with self.assertRaisesRegex(RuntimeError, 'incomplete at the first fetch'):
                performance_probe.quiet_recovery_result(Path('/vm'), report)
        request.assert_called_once_with(Path('/vm'), 'sampler_result')
        self.assertFalse(report['recovery_rpc_quiet_window']['first_fetch_completed'])


if __name__ == '__main__':
    unittest.main()
