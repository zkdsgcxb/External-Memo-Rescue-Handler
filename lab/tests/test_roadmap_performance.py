"""Production A/B trials must preserve policy and avoid inflated accounting."""
import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import roadmap_performance_probe as probe


class ProcessAccountingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        namespace = {'UnifiedProbe': object}
        source = probe.BASE / 'guest/performance_probe.py'
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
        cls.process_summary = staticmethod(namespace['process_cost_summary'])
        cls.pss_summary = staticmethod(namespace['pss_summary'])

    def samples(self):
        rows = []
        for index in range(3):
            rows.append({'processes': {'root': {'pid': 1, 'start_ticks': '20',
                'self_cpu_ticks': 10 + index, 'waited_child_cpu_ticks': index * 2,
                'threads': 2, 'sampled_helpers': [{'pid': 3, 'start_ticks': str(index)}]}}})
        return rows

    def test_reused_helper_pid_is_not_collapsed_into_one_process(self):
        report = self.process_summary(self.samples(), ['root'])['root']
        self.assertTrue(report['stable_controller'])
        self.assertEqual(report['self_cpu_ticks'], 2)
        self.assertEqual(report['waited_child_cpu_ticks'], 4)
        self.assertEqual(report['observed_helper_count_lower_bound'], 3)
        self.assertEqual(report['sampled_max_helper_concurrency'], 1)

    def test_controller_replacement_does_not_produce_false_cpu_delta(self):
        rows = self.samples()
        rows[-1]['processes']['root']['start_ticks'] = 'replacement'
        report = self.process_summary(rows, ['root'])['root']
        self.assertFalse(report['stable_controller'])
        self.assertIsNone(report['waited_child_cpu_ticks'])

    def test_pss_aggregate_counts_data_parent_once_and_retains_incomplete_sample_count(self):
        samples = [{'groups': {name: {'process_totals_bytes': {'Pss': value}}
                              for name, value in [('root', 10), ('data_slice', 20), ('data_child', 20)]}},
                   {'groups': {name: {'process_totals_bytes': {'Pss': value}}
                              for name, value in [('root', None), ('data_slice', 30)]}}]
        report = self.pss_summary(samples)
        self.assertEqual(report['aggregate'], {'valid_samples': 1, 'total_samples': 2,
                                               'mean_bytes': 30, 'sampled_peak_bytes': 30})


class ProductionCompositionTests(unittest.TestCase):
    def test_logger_verification_preserves_each_releases_policy(self):
        class Parent:
            pass
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'opt/vmprobe/ram-rescue-log.service'
            source.parent.mkdir(parents=True)
            installed = root / 'run/systemd/system/ram-rescue-log.service'
            installed.parent.mkdir(parents=True)
            log = root / 'var/log/kernel-live.log'
            log.parent.mkdir(parents=True)
            log.write_text('kernel snapshot\n')
            manifest = {'rescue_unit_sha256': {}}
            namespace = {'ProductionIntegrationProbe': Parent, 'hashlib': hashlib,
                         'EXPERIMENT': manifest, 'Path': lambda path: root / str(path).lstrip('/')}
            exec(compile(probe.GUEST.read_text(), str(probe.GUEST), 'exec'), namespace)
            instance = namespace['RoadmapPerformanceProbe']()
            instance.ROOT = root
            instance.read_namespace_policy = lambda pid, paths: {'verified': True}
            for restriction, nnp, protect, caps in [
                ('', 'no', 'no', 'cap_chown cap_sys_admin cap_syslog'),
                ('NoNewPrivileges=yes\nProtectSystem=strict\nCapabilityBoundingSet=CAP_SYSLOG\n',
                 'yes', 'strict', 'cap_syslog'),
            ]:
                source.write_text('[Service]\nType=simple\n' + restriction)
                installed.write_bytes(source.read_bytes())
                manifest['rescue_unit_sha256']['ram-rescue-log.service'] = hashlib.sha256(source.read_bytes()).hexdigest()
                state = (f'ActiveState=active\nMainPID=7\nNoNewPrivileges={nnp}\n'
                         f'ProtectSystem={protect}\nCapabilityBoundingSet={caps}\nDropInPaths=\n')
                instance.host = lambda *args: {'stdout': state}
                self.assertTrue(instance.rescue_logging_integrity()['verified'])
                instance.host = lambda *args: {'stdout': state.replace('DropInPaths=\n', 'DropInPaths=/override\n')}
                with self.assertRaisesRegex(RuntimeError, 'Logger policy differs'):
                    instance.rescue_logging_integrity()

    def test_baseline_context_is_restored_after_failure(self):
        original = probe.production.REPO, probe.production.production_build, probe.production.manage
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with probe.selected_package(Path('/baseline'), Path('/bundle')):
                self.assertEqual(probe.production.REPO, Path('/baseline'))
                self.assertIsInstance(probe.production.production_build, probe.BaselineBuilder)
                raise RuntimeError('fixture')
        self.assertEqual((probe.production.REPO, probe.production.production_build, probe.production.manage), original)

    def test_overlay_changes_only_disposable_observer_and_sampler_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / 'image'; folder.mkdir()
            source = folder / 'overlay/opt/host-boot-probe/probe.py'
            source.parent.mkdir(parents=True)
            source.write_text((probe.BASE / 'guest/mount_probe.py').read_text() +
                              '\nSelectedProbe = MountProbe\n' + probe.production.GUEST)
            before = source.read_bytes()
            hook = folder / 'overlay/scripts/init-bottom/zz-host-boot-probe'
            hook.parent.mkdir(parents=True)
            hook.write_text('#!/bin/sh\n# existing test hook\n')
            with patch.object(probe.cpp, 'append_archive') as append:
                result = probe.performance_overlay(folder, root / 'initrd')
            self.assertEqual(source.read_bytes(), before)
            overlay = folder / 'performance-overlay'
            self.assertEqual({str(path.relative_to(overlay)) for path in overlay.rglob('*') if path.is_file()}, {
                'opt/host-boot-probe/probe.py', 'opt/performance-memory.py',
                'scripts/init-bottom/zz-host-boot-probe'})
            self.assertEqual(append.call_count, 1)
            self.assertEqual(len(result['observer_sha256']), 64)
            code = (overlay / 'opt/host-boot-probe/probe.py').read_text()
            self.assertIn('SelectedProbe = PerformanceProbe', code)
            self.assertIn('UnifiedProbe = RoadmapPerformanceProbe', code)

    def test_quota_action_is_readonly_and_cannot_change_production_limits(self):
        class Parent:
            def gate(self):
                pass
        namespace = {'ProductionIntegrationProbe': Parent}
        source = probe.GUEST
        exec(compile(source.read_text(), str(source), 'exec'), namespace)
        instance = namespace['RoadmapPerformanceProbe']()
        instance.policy_snapshot = lambda: {'read_only': True}
        self.assertEqual(instance.action('quota20'), {'read_only': True})
        with self.assertRaisesRegex(RuntimeError, 'shipped 20%'):
            instance.action('quota10')

    def test_summary_excludes_failed_trials(self):
        result = probe.summary([(Path('/failed/report'), {'variant': 'current', 'passed': False})])
        self.assertEqual(result['current']['successful_trials'], 0)
        self.assertIsNone(result['current']['phases']['recovery']['active_recovery_seconds'])


if __name__ == '__main__':
    unittest.main()
