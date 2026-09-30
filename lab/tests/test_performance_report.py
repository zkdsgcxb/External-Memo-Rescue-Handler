"""Public evidence cannot silently accept failed or double-counted reports."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import performance_report as report_tool


def fixture():
    samples = [{'time': index * .02, 'groups': {
        'root': {'cpu': {'usage_usec': index * 10}, 'memory_bytes': 100},
        'data_slice': {'cpu': {'usage_usec': index * 20}, 'memory_bytes': 200},
    }} for index in range(6)]
    phase = {'label': 'idle', 'samples': samples, 'period_seconds': .02, 'seconds': .1,
             'groups': {'root': {'cpu_total_usec': 50, 'cpu_mean_percent': .05},
                        'data_slice': {'cpu_total_usec': 100, 'cpu_mean_percent': .1},
                        'aggregate': {'cpu_total_usec': 150, 'cpu_mean_percent': .15}},
             'before': {'root': {'process_totals_bytes': {'Rss': 20, 'Pss': 10},
                                 'files': {'memory.current': 100}}},
             'after': {'root': {'process_totals_bytes': {'Rss': 30, 'Pss': 15},
                                'files': {'memory.current': 110}}}}
    return {'schema': 1, 'variant': 'current', 'quota_percent': 10, 'passed': True,
            'checks': {'original_process': True}, 'source_image_unchanged': True,
            'sources_unchanged': True, 'filesystem_audits': {'ext4': {'returncode': 0}},
            'phases': [{**deepcopy(phase), 'label': label} for label in sorted(report_tool.PHASES)],
            'root_audit': {'max_write_seconds': 2.1, 'kernel_log': 'do not copy'},
            'logs': {'large_private_payload': 'do not copy'}}


class PublicEvidenceTests(unittest.TestCase):
    def test_valid_summary_keeps_hashes_memory_and_workload_without_raw_samples(self):
        report = fixture()
        report_tool.validate_report(report)
        content = json.dumps(report).encode()
        result = report_tool.summarize([(Path('/tmp/example/report.json'), report, content)])
        public = result['reports'][0]
        self.assertEqual(public['source_report']['sha256'], report_tool.digest(content))
        self.assertEqual(public['workloads']['root']['max_write_seconds'], 2.1)
        self.assertEqual(public['phases'][0]['sample_count'], 6)
        peak = public['phases'][0]['aggregate_peak_windows']['nominal_100ms']
        self.assertEqual(peak['cpu_delta_usec'], 150)
        self.assertAlmostEqual(peak['seconds'], .1)
        self.assertAlmostEqual(peak['cpu_percent'], .15)
        self.assertEqual(public['phases'][0]['memory_snapshots']['before']['root']['process_totals_bytes']['Pss'], 10)
        self.assertNotIn('samples', public['phases'][0])
        self.assertNotIn('logs', public)
        self.assertNotIn('kernel_log', public['workloads']['root'])
        self.assertTrue(result['conventions']['data_slice_already_contains_both_data_controllers'])

    def test_failed_report_requires_explicit_opt_in(self):
        report = fixture()
        report['passed'] = False
        with self.assertRaisesRegex(ValueError, 'Refusing a failed'):
            report_tool.validate_report(report)
        report_tool.validate_report(report, allow_failed=True)

    def test_success_claim_cannot_override_failed_checks_or_changed_sources(self):
        for field, value in [('checks', {'bad': False}), ('sources_unchanged', False),
                             ('source_image_unchanged', False), ('filesystem_audits', {})]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                report_tool.validate_report({**fixture(), field: value})

    def test_aggregate_double_counting_and_stale_mean_are_rejected(self):
        for field, value in [('cpu_total_usec', 250), ('cpu_mean_percent', .25)]:
            report = fixture()
            report['phases'][0]['groups']['aggregate'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'differs from raw'):
                report_tool.validate_report(report)

    def test_effective_runtime_and_fault_clock_are_preserved_and_verified(self):
        report = fixture()
        report.update(runtime_payload_sha256={'path_guard.py': 'hash'},
                      runtime_before={'root': {'path_guard.py': 'hash'}, 'data': {'path_guard.py': 'hash'}},
                      runtime_after={'root': {'path_guard.py': 'hash'}, 'data': {'path_guard.py': 'hash'}},
                      healthy_warmup_seconds=3,
                      recovery_rpc_quiet=True, quiet_wait_seconds=17,
                      recovery_rpc_quiet_window={'clock': 'host monotonic', 'quiet_start': 1., 'quiet_end': 18.},
                      fault={'root': {'clock': 'host monotonic', 'delete_requested': 2.,
                                      'removed': 2.1, 'attached': 2.4, 'gap_seconds': .3}})
        report_tool.validate_report(report)
        result = report_tool.compact_report(Path('/tmp/report.json'), report, b'example')
        self.assertEqual(result['runtime_payload_sha256'], {'path_guard.py': 'hash'})
        self.assertEqual(result['healthy_warmup_seconds'], 3)
        self.assertTrue(result['recovery_rpc_quiet'])
        self.assertEqual(result['quiet_wait_seconds'], 17)
        self.assertEqual(result['recovery_rpc_quiet_window']['quiet_end'], 18.)
        self.assertEqual(result['faults']['root']['clock'], 'host monotonic')
        report['runtime_after']['data']['path_guard.py'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'payload hashes differ'):
            report_tool.validate_report(report)

    def test_duplicate_phases_and_nonmonotonic_samples_are_rejected(self):
        report = fixture()
        report['phases'][-1]['label'] = report['phases'][0]['label']
        with self.assertRaisesRegex(ValueError, 'distinct'):
            report_tool.validate_report(report)
        report = fixture()
        report['phases'][0]['samples'][1]['time'] = 0
        with self.assertRaisesRegex(ValueError, 'strictly increasing'):
            report_tool.validate_report(report)

    def test_duplicate_input_and_nonfinite_json_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            path.write_text(json.dumps(fixture()))
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                report_tool.load_reports([path, path])
            duplicate = path.with_name('copy.json')
            duplicate.write_bytes(path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'Duplicate report content'):
                report_tool.load_reports([path, duplicate])
            for value in ('NaN', '1e999'):
                path.write_text('{"bad": ' + value + '}')
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Non-finite'):
                    report_tool.load_reports([path])

    def test_summary_is_deterministic_for_fixed_inputs(self):
        report = fixture()
        source = (Path('/tmp/report.json'), report, json.dumps(report).encode())
        self.assertEqual(report_tool.summarize([source]), report_tool.summarize([source]))

    def test_cli_keeps_all_json_reports_but_plots_only_explicit_subset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / 'baseline.json', root / 'current.json'
            first.write_text(json.dumps({**fixture(), 'variant': 'baseline'}))
            second.write_text(json.dumps(fixture()))
            output, image = root / 'summary.json', root / 'recovery.png'
            def render(loaded, target):
                self.assertEqual([row[0] for row in loaded], [first])
                target.write_bytes(b'plot stub')
                return {'matplotlib_version': 'test'}
            arguments = ['performance_report.py', str(first), str(second), '--output', str(output),
                         '--plot', str(image), '--plot-report', str(first), '--label', 'event-filter revision']
            with patch.object(sys, 'argv', arguments), patch.object(report_tool, 'plot_recovery', side_effect=render), redirect_stdout(io.StringIO()):
                report_tool.main()
            result = json.loads(output.read_text())
            self.assertEqual(len(result['reports']), 2)
            self.assertEqual(result['experiment'], 'event-filter revision')
            self.assertEqual(result['plot']['source_reports'], [str(first)])
            self.assertEqual(result['plot']['sha256'], report_tool.digest(b'plot stub'))

    def test_cli_cannot_replace_original_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'report.json'
            content = json.dumps(fixture())
            original.write_text(content)
            with patch.object(sys, 'argv', ['performance_report.py', str(original), '--output', str(original)]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as failure:
                report_tool.main()
            self.assertEqual(failure.exception.code, 2)
            self.assertEqual(original.read_text(), content)


if __name__ == '__main__':
    unittest.main()
