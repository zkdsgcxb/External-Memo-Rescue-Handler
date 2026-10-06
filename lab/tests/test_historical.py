"""Historical comparisons must not silently consume current or changed code."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import historical


class HistoricalSnapshotTests(unittest.TestCase):
    @contextmanager
    def checkout(self):
        historical.WORK.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=historical.WORK) as temporary:
            root = historical.snapshot(destination=Path(temporary) / 'release')
            yield root

    def test_full_checkout_preserves_commit_and_legacy_imports(self):
        with self.checkout() as root:
            self.assertEqual(historical.git(root, 'rev-parse', 'HEAD'),
                             historical.PYTHON_REVISION)
            self.assertEqual((root / 'lab/work').resolve(), historical.WORK.resolve())
            self.assertEqual(historical.snapshot(destination=root), root)
            loaded = subprocess.check_output([sys.executable, '-B', '-c',
                'import sys; sys.path[:0]=["guard/runtime","ram-rescue-demo/src"]; '
                'import path_guard; print(path_guard.__file__)'], cwd=root, text=True).strip()
            self.assertEqual(Path(loaded).resolve(), root / 'guard/runtime/path_guard.py')

    def test_changed_tracked_source_is_refused(self):
        with self.checkout() as root:
            (root / 'guard/runtime/path_guard.py').write_text('changed\n')
            with self.assertRaises(subprocess.CalledProcessError):
                historical.snapshot(destination=root)

    def test_untracked_module_cannot_shadow_a_historical_dependency(self):
        with self.checkout() as root:
            (root / 'guard/runtime/shadow.py').write_text('unexpected = True\n')
            with self.assertRaisesRegex(RuntimeError, 'untracked source'):
                historical.snapshot(destination=root)

    def test_snapshot_cannot_escape_lab_work_or_choose_an_unpinned_revision(self):
        with self.assertRaisesRegex(ValueError, 'full historical commit'):
            historical.snapshot('HEAD')
        with self.assertRaisesRegex(ValueError, 'below lab/work'):
            historical.snapshot(destination=Path('/tmp/historical-guard'))

    def test_legacy_launcher_rejects_path_traversal_before_running(self):
        with self.assertRaisesRegex(ValueError, 'experiment path'):
            historical.run_legacy('lab/../../guard/build.py', [])

    def test_manual_scenario_entrypoint_uses_the_complete_pinned_release(self):
        result = subprocess.run([sys.executable, '-B',
            str(historical.REPO / 'lab/run.py'), '--help'],
            text=True, capture_output=True, check=True)
        self.assertIn('Historical Python experiment: ' + historical.PYTHON_REVISION,
                      result.stderr)
        self.assertIn('queued-write', result.stdout)


if __name__ == '__main__':
    unittest.main()
