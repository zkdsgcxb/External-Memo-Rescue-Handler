"""Independent trace examples for the diagnostic report's attribution math."""
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_cpu_diagnostic import analyze


class CpuDiagnosticAnalysisTests(unittest.TestCase):
    def fixture(self, folder, broken_switch=False):
        names = {'root': 10, 'ext4': 20, 'vfat': 30}
        groups = {name: '/proc/1/root/sys/fs/cgroup/' + name for name in names}
        groups['data_slice'] = '/proc/1/root/sys/fs/cgroup/data_slice'
        config = {'pids': names, 'groups': groups,
                  'initial_tasks': {name: [{'tid': pid, 'policy': 0}] for name, pid in names.items()}}
        amounts = {'root': 6000, 'ext4': 12000, 'vfat': 0, 'data_slice': 12000}
        samples = []
        for index, middle in enumerate((1.0, 1.02)):
            samples.append({'groups': {name: {'before': middle - .0001, 'after': middle + .0001,
                            'cpu': {'usage_usec': index * value}} for name, value in amounts.items()},
                            'stat': ['cpu  0 0 0 0 0 0 0 0']})
        accounting = {'config': config, 'samples': samples, 'tasks': {}, 'trace_stats': {}}
        # The child is intentionally never sampled in /proc. Only the actual
        # kernel fork format (pid=, not parent_pid=) connects it to its budget.
        events = [
            (0.999, 20, 'sched_process_fork: comm=python3 pid=20 child_comm=python3 child_pid=21'),
            (1.0, 0, 'sched_switch: prev_pid=0 prev_prio=120 ==> next_pid=0 next_prio=120'),
            (1.002, 0, 'sched_switch: prev_pid=0 prev_prio=120 ==> next_pid=21 next_prio=120'),
            (1.005, 21, 'irq_handler_entry: irq=4 name=ttyS0'),
            (1.006, 21, 'irq_handler_entry: irq=24 name=xhci_hcd'),
            (1.007, 21, 'irq_handler_exit: irq=24 ret=handled'),
            (1.008, 21, 'irq_handler_exit: irq=4 ret=handled'),
            (1.010, 21, 'sys_sched_setscheduler(pid: 0, policy: 0, param: 0x1)'),
            (1.011, 21, 'throttle_cfs_rq_work <-task_work_run'),
            (1.012, 21, 'sched_stat_runtime: comm=blkid pid=21 runtime=10000000 [ns]'),
            (1.012, 21, f'sched_switch: prev_pid={99 if broken_switch else 21} prev_prio=120 ==> next_pid=10 next_prio=120'),
            (1.018, 10, 'sched_switch: prev_pid=10 prev_prio=120 ==> next_pid=21 next_prio=120'),
            (1.022, 21, 'sched_switch: prev_pid=21 prev_prio=120 ==> next_pid=0 next_prio=120'),
        ]
        trace = '\n'.join(f'task-{pid} [000] ..... {timestamp:.6f}: {text}'
                          for timestamp, pid, text in events)
        (folder / 'accounting.json.gz').write_bytes(gzip.compress(json.dumps(accounting).encode()))
        (folder / 'trace.txt.gz').write_bytes(gzip.compress(trace.encode()))

    def test_fork_child_and_nested_irq_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.fixture(folder)
            result = analyze(folder)
        self.assertEqual(result['tracked_tasks'], 4)
        self.assertEqual(result['sched_switch_continuity_errors'], 0)
        peak = result['groups']['ext4']['peak_windows'][0]
        self.assertAlmostEqual(peak['scheduled_residence_usec'], 12000)
        # Nested handlers consume a union of three milliseconds, not four.
        self.assertAlmostEqual(peak['traced_irq_overlap_usec'], 3000)
        self.assertAlmostEqual(peak['residence_minus_traced_irq_usec'], 9000)
        self.assertEqual(peak['runtime_events_usec'], 10000)
        self.assertEqual(result['policy_change_syscalls'][0]['pid'], 21)
        self.assertEqual(result['throttle_callback_count'], 1)

    def test_parent_slice_does_not_add_child_cpu_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.fixture(folder)
            result = analyze(folder)
        parent = result['groups']['data_slice']['peak_windows'][0]
        leaf = result['groups']['ext4']['peak_windows'][0]
        self.assertAlmostEqual(parent['percent_midpoints'], 60)
        self.assertEqual(parent['usage_usec'], 12000)
        self.assertEqual(parent['scheduled_residence_usec'], leaf['scheduled_residence_usec'])
        self.assertAlmostEqual(result['groups']['root']['peak_windows'][0]['scheduled_residence_usec'], 6000)
        self.assertLess(parent['percent_widest_read_bounds'], 60)
        self.assertGreater(parent['percent_narrowest_read_bounds'], 60)

    def test_missing_switch_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.fixture(folder, broken_switch=True)
            result = analyze(folder)
        self.assertEqual(result['sched_switch_continuity_errors'], 1)


if __name__ == '__main__':
    unittest.main()
