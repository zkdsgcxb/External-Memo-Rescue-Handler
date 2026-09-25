"""Resource observations must not hide process races or double-count views."""
import errno
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from measure_guard import MEMORY_HELPERS, SAMPLE


class MemoryMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.helpers = {}
        exec(compile(MEMORY_HELPERS, '<guest-memory-helpers>', 'exec'), self.helpers)
        work = Path(__file__).resolve().parents[1] / 'work'
        work.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=work)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_process(self, pid, start='321'):
        folder = self.root / 'proc' / str(pid)
        folder.mkdir(parents=True)
        fields = ['S'] + ['0'] * 49
        fields[19] = start
        stat = f'{pid} (worker ) name) ' + ' '.join(fields)
        (folder / 'stat').write_text(stat)
        (folder / 'status').write_text('Name:\tworker\nVmRSS:\t40 kB\nVmLck:\t0 kB\nVmSwap:\t8 kB\n')
        (folder / 'smaps_rollup').write_text(
            '0000-1000 ---p 0000 00:00 0 [rollup]\n'
            'Rss: 40 kB\nPss: 24 kB\nSwap: 8 kB\nSwapPss: 4 kB\nLocked: 0 kB\n')
        return stat

    def write_cgroup(self):
        cg = self.root / 'cgroup'
        cg.mkdir()
        (cg / 'cgroup.procs').write_text('123\n124\n')
        child = cg / 'child'
        child.mkdir()
        (child / 'cgroup.procs').write_text('124\n125\n')
        for key, value in {'memory.current': '123456', 'memory.peak': '234567',
                           'memory.max': 'max', 'memory.swap.max': '0',
                           'memory.events': 'low 2\noom 1\noom_kill 0\n',
                           'memory.stat': 'anon 16384\nfile 32768\nshmem 24576\n'}.items():
            (cg / key).write_text(value + '\n')
        return cg

    def test_embedded_sampler_compiles(self):
        compile(SAMPLE, '<guest-sampler>', 'exec')

    def test_kib_fields_and_nonbyte_values_remain_distinct(self):
        values = self.helpers['byte_fields']('Pss: 12 kB\nSwap: 3 kB\nHugePages_Total: 2\n')
        self.assertEqual(values, {'Pss': 12288, 'Swap': 3072})
        self.assertEqual(self.helpers['key_values']('oom 3\n'), {'oom': 3})

    def test_all_cgroup_descendants_are_counted_once_and_limits_preserved(self):
        for pid in [123, 124, 125]:
            self.write_process(pid)
        result = self.helpers['cgroup_memory'](self.write_cgroup(), self.root / 'proc')
        self.assertEqual(result['process_pids'], [123, 124, 125])
        self.assertEqual(result['process_totals_bytes']['Pss'], 3 * 24 * 1024)
        self.assertEqual(result['process_totals_bytes']['Rss'], 3 * 40 * 1024)
        self.assertEqual(result['process_totals_bytes']['SwapPss'], 3 * 4 * 1024)
        self.assertEqual(result['files']['memory.current'], 123456)
        self.assertEqual(result['files']['memory.max'], 'max')
        self.assertEqual(result['files']['memory.swap.max'], 0)
        self.assertEqual(result['files']['memory.events']['oom'], 1)
        self.assertIsNone(result['files']['memory.swap.current'])
        # Missing optional files are explicitly recorded, never interpreted as 0.
        self.assertTrue(any(item['errno'] == errno.ENOENT for item in result['errors']))
        self.assertFalse(result['membership_atomic'])

    def test_vanished_process_makes_total_unknown(self):
        for pid in [123, 124]:
            self.write_process(pid)
        result = self.helpers['cgroup_memory'](self.write_cgroup(), self.root / 'proc')
        self.assertEqual(result['process_pids'], [123, 124, 125])
        self.assertFalse(result['processes'][-1]['stable_instance'])
        self.assertTrue(result['processes'][-1]['errors'])
        self.assertIsNone(result['process_totals_bytes']['Pss'])

    def test_reused_pid_does_not_mix_old_and_new_memory(self):
        stat = self.write_process(123)
        real_read = self.helpers['read_optional']
        stat_reads = 0

        def read(path, errors):
            nonlocal stat_reads
            if path.name == 'stat':
                stat_reads += 1
                if stat_reads == 2:
                    return stat.replace('321', '999')
            return real_read(path, errors)

        self.helpers['read_optional'] = read
        result = self.helpers['process_memory'](123, self.root / 'proc')
        self.assertFalse(result['stable_instance'])
        self.assertEqual(result['status_bytes'], {})
        self.assertEqual(result['smaps_rollup_bytes'], {})

    def test_tmpfs_usage_and_bind_aliases_do_not_become_additive_totals(self):
        info = self.root / 'mountinfo'
        info.write_text(
            '41 1 0:22 / /run rw,nosuid - tmpfs tmpfs rw,noswap,size=1m\n'
            '42 41 0:22 / /run/rescue rw - tmpfs tmpfs rw,noswap,size=1m\n'
            '43 1 0:23 / /tmp rw - tmpfs tmpfs rw,size=1m\n')
        stats = SimpleNamespace(f_blocks=256, f_bfree=100, f_bavail=98, f_frsize=4096)
        with mock.patch.object(self.helpers['os'], 'statvfs', return_value=stats), \
                mock.patch.object(self.helpers['os'], 'stat', return_value=SimpleNamespace(st_dev=22)):
            result = self.helpers['tmpfs_memory'](info, self.root)
        self.assertEqual(len(result['mounts']), 2)
        first, second = result['mounts']
        self.assertEqual(first['used_bytes'], (256 - 100) * 4096)
        self.assertTrue(first['noswap_option_present'])
        self.assertIsNone(first['duplicate_of'])
        self.assertEqual(second['duplicate_of'], '/run')
        self.assertNotIn('total_bytes', result)


if __name__ == '__main__':
    unittest.main()
