"""Native parser checks and benchmark accounting without opening host DM."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2] / 'guard/native'
SPEC = importlib.util.spec_from_file_location('native_benchmark', SOURCE / 'benchmark.py')
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)


@unittest.skipUnless(shutil.which('g++'), 'Optional native prototype requires g++')
class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / 'guard-observe'
        subprocess.run(['python3', str(SOURCE / 'build.py'), '--output', cls.directory.name],
                       check=True, stdout=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_cli_rejects_missing_identity_before_opening_dm(self):
        result = subprocess.run([str(self.binary)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['state'], 'control-uncertain')
        self.assertIn('Required:', json.loads(result.stdout)['reason'])

    def test_cli_rejects_invalid_duration_and_diskseq(self):
        for key, value in [('seconds', 'nan'), ('seconds', 'inf'), ('seconds', '-1'),
                           ('diskseq', '0'), ('diskseq', '-1'), ('diskseq', '1junk')]:
            with self.subTest(key=key, value=value):
                result = subprocess.run([str(self.binary), '--' + key, value], text=True, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout)['state'], 'control-uncertain')


class AccountingTests(unittest.TestCase):
    def test_cpu_uses_one_core_and_actual_window_lengths(self):
        samples = [
            {'time': 2., 'cpu_usec': 1000, 'memory_bytes': 100, 'process_bytes': {'Pss': 50}},
            {'time': 2.1, 'cpu_usec': 2000, 'memory_bytes': 200, 'process_bytes': {'Pss': 70}},
            {'time': 2.3, 'cpu_usec': 3000, 'memory_bytes': 300, 'process_bytes': {'Pss': 90}},
        ]
        result = BENCHMARK.summarize(samples)
        self.assertAlmostEqual(result['cpu_mean_one_core_percent'], 2 / 3)
        self.assertAlmostEqual(result['cpu_peak_sample_window_percent'], 1)
        self.assertEqual(result['memory_mean_bytes'], 200)
        self.assertEqual(result['process_bytes']['Pss']['mean_bytes'], 70)
        self.assertEqual(result['process_bytes']['Pss']['peak_bytes'], 90)

    def test_insufficient_samples_fail(self):
        with self.assertRaises(RuntimeError):
            BENCHMARK.summarize([])


if __name__ == '__main__':
    unittest.main()
