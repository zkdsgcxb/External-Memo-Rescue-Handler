"""Temporary service and automount-exclusion lifecycle, with no host mutation."""
import importlib.util
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
spec = importlib.util.spec_from_file_location('data_launcher', BASE / 'guard/data.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class DataLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        self.units = self.root / 'units'
        self.rules = self.root / 'rules'
        self.ram = self.root / 'ram'
        self.ram.mkdir()
        self.name = 'rr-data-test'
        self.profile = {'schema': 1, 'identity': {
            'kind': 'filesystem', 'vid': '1234', 'pid': '5678',
            'usb_serial': 'USB-test-001', 'partition_number': 1,
        }, 'guard': {
            'profile': 'host-data', 'map_name': self.name, 'map_uuid': 'RAMRESCUE-DATA-test',
            'initial_node': '/dev/mock1', 'initial_sys_path': '/sys/devices/mock/mock1',
            'run_dir': str(self.state / self.name / 'state'),
            'identity_path': str(self.state / self.name / 'identity.json'),
        }}
        self.addCleanup(patch.stopall)
        patch.multiple(launcher, RAM=self.ram, STATE=self.state, UNITS=self.units, RULES=self.rules).start()
        patch.object(launcher, 'require_root').start()
        patch.object(launcher, 'require_ram').start()
        self.collect = Mock(return_value=self.profile)
        self.validate = Mock()
        patch.dict(sys.modules, {'data_guard': Mock(collect=self.collect, validate_config=self.validate)}).start()
        self.commands = []
        self.properties = 'ID_FS_TYPE=ext4\nUDISKS_IGNORE=1'
        self.command_runner = patch.object(launcher, 'run', side_effect=self.run_command).start()
        original_stat = os.stat

        def stat(path, *args, **kwargs):
            if str(path) == '/dev/mapper/' + self.name:
                return Mock(st_rdev=os.makedev(252, 7))
            return original_stat(path, *args, **kwargs)
        patch.object(launcher.os, 'stat', side_effect=stat).start()

    def run_command(self, arguments):
        self.commands.append(arguments)
        if arguments[:3] == ['udevadm', 'info', '--query=property']:
            return self.properties
        if arguments[:2] == ['systemctl', 'show']:
            return 'active'
        return ''

    def test_changed_map_is_refused_before_staging_any_rules_or_unit(self):
        self.collect.return_value = {**self.profile, 'schema': 2}
        with self.assertRaisesRegex(RuntimeError, 'changed since enrollment'):
            launcher.start(self.profile)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.rules.exists())
        self.assertFalse(self.units.exists())
        self.assertEqual(self.commands, [])

    def test_udev_identity_metacharacters_cannot_match_other_disks(self):
        for serial in ('*', 'USB?', 'USB[12]', 'USB"1', 'USB\\1', 'USB\n1', ''):
            with self.subTest(serial=serial):
                self.profile['identity']['usb_serial'] = serial
                with self.assertRaisesRegex(ValueError, 'exact udev match'):
                    launcher.start(self.profile)
                self.assertEqual(self.commands, [])
                self.assertFalse(self.state.exists())

    def test_unsafe_map_names_are_never_interpreted_as_unit_or_path(self):
        for name in ('ram-rescue-path', '../rr-data-test', 'rr-data-test.service\nX=1',
                     'rr-data-test;poweroff', 'rr-data-', 'rr-data-' + 'x' * 65):
            with self.subTest(name=name), self.assertRaises(ValueError):
                launcher.map_name(name)

    def test_start_confirms_exclusion_and_revalidates_before_starting_service(self):
        result = launcher.start(self.profile)
        self.assertEqual(result['state'], 'active')
        self.assertEqual(self.collect.call_count, 2)
        reload_index = self.commands.index(['udevadm', 'control', '--reload-rules'])
        raw_index = self.commands.index(['udevadm', 'trigger', '--action=change', '--settle',
                                        self.profile['guard']['initial_sys_path']])
        start_index = self.commands.index(['systemctl', 'start', launcher.service(self.name)])
        self.assertLess(reload_index, raw_index)
        self.assertLess(raw_index, start_index)
        self.assertTrue((self.rules / ('58-ram-rescue-data-' + self.name + '.rules')).is_file())
        unit = (self.units / launcher.service(self.name)).read_text()
        self.assertIn('Slice=ramrescuedata.slice', unit)
        self.assertIn('ExecStopPost=', unit)
        self.assertNotIn('OnFailure=emergency.target', unit)
        self.assertNotIn('[Install]', unit)
        self.assertEqual((self.units / launcher.SLICE).read_text().count('CPUQuota=20%'), 1)
        self.assertFalse((self.ram / 'opt/guard').exists())
        self.assertTrue(Path(result['runtime'], 'path_guard.py').is_file())

    def test_failed_exclusion_prevents_start_and_retains_precaution(self):
        self.properties = 'ID_FS_TYPE=ext4'
        with self.assertRaisesRegex(RuntimeError, 'exclusion was not applied'):
            launcher.start(self.profile)
        self.assertNotIn(['systemctl', 'start', launcher.service(self.name)], self.commands)
        self.assertTrue((self.rules / ('58-ram-rescue-data-' + self.name + '.rules')).exists())
        self.assertTrue((self.state / self.name / 'config.json').exists())

    def test_changed_device_during_rule_installation_prevents_start(self):
        self.collect.side_effect = [self.profile, {**self.profile, 'schema': 2}]
        with self.assertRaisesRegex(RuntimeError, 'changed while installing'):
            launcher.start(self.profile)
        self.assertNotIn(['systemctl', 'start', launcher.service(self.name)], self.commands)

    def test_existing_state_is_never_replaced_for_automatic_restart(self):
        directory = self.state / self.name
        directory.mkdir(parents=True)
        journal = directory / 'old-journal'
        journal.write_text('keep old owner evidence')
        with self.assertRaisesRegex(RuntimeError, 'already has temporary'):
            launcher.start(self.profile)
        self.assertEqual(journal.read_text(), 'keep old owner evidence')
        self.assertEqual(self.commands, [])

    def test_stop_only_stops_controller_and_keeps_map_exclusions_and_evidence(self):
        launcher.start(self.profile)
        before_rules = {path: path.read_bytes() for path in self.rules.iterdir()}
        self.commands.clear()
        result = launcher.stop(self.name)
        self.assertEqual(self.commands, [['systemctl', 'stop', launcher.service(self.name)]])
        self.assertTrue(result['map_retained'])
        self.assertTrue((self.state / self.name / 'config.json').is_file())
        self.assertEqual(before_rules, {path: path.read_bytes() for path in self.rules.iterdir()})

    def test_runtime_versions_are_immutable_and_do_not_overwrite_root_guard(self):
        root_runtime = self.ram / 'opt/guard'
        root_runtime.mkdir(parents=True)
        old = root_runtime / 'path_guard.py'
        old.write_text('already running root controller')
        runtime = launcher.stage_runtime()
        self.assertEqual(launcher.stage_runtime(), runtime)
        (runtime / 'path_guard.py').chmod(0o600)
        (runtime / 'path_guard.py').write_text('changed')
        with self.assertRaisesRegex(RuntimeError, 'has changed'):
            launcher.stage_runtime()
        self.assertEqual(old.read_text(), 'already running root controller')

    def test_concurrent_runtime_publication_reuses_only_identical_complete_version(self):
        def competing_publication(temporary, runtime):
            runtime.mkdir()
            for source in temporary.iterdir():
                (runtime / source.name).write_bytes(source.read_bytes())
            raise OSError(errno.ENOTEMPTY, 'another launcher published this version')

        with patch.object(Path, 'rename', competing_publication):
            runtime = launcher.stage_runtime()
        self.assertEqual(launcher.stage_runtime(), runtime)
        self.assertEqual(list(runtime.parent.iterdir()), [runtime])

    def test_concurrent_runtime_publication_rejects_different_content(self):
        def changed_publication(temporary, runtime):
            runtime.mkdir()
            for source in temporary.iterdir():
                (runtime / source.name).write_bytes(source.read_bytes())
            (runtime / 'path_guard.py').write_text('unexpected code')
            raise OSError(errno.EEXIST, 'another creator published altered content')

        with patch.object(Path, 'rename', changed_publication):
            with self.assertRaisesRegex(RuntimeError, 'has changed'):
                launcher.stage_runtime()
        self.assertTrue(all(not path.name.startswith('.')
                            for path in (self.ram / 'opt/data-guard').iterdir()))

    def test_enrollment_refuses_to_replace_existing_record(self):
        output = self.root / 'enrollment.json'
        output.write_text('existing registration')
        with self.assertRaisesRegex(RuntimeError, 'replace an existing'):
            launcher.enroll(self.name, '/dev/mock1', output)
        self.assertEqual(output.read_text(), 'existing registration')


if __name__ == '__main__':
    unittest.main()
