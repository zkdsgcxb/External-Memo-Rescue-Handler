"""Public lifecycle refusal and persistence boundaries without host mutations."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'guard'))
import lifecycle as life
import release_support
from admin.admission import digest


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.name = 'rr-data-test'
        self.record = {'identity': {'fs_type': 'ext4'}, 'guard': {'map_name': self.name}}
        self.value = {'schema': 1, 'enabled': False, 'record': self.record}

    def test_noninteractive_confirmation_never_defaults_yes(self):
        with patch.object(life.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '--expect-plan'):
                life.confirmation(None, 'a' * 64, 'enable')

    def test_stale_confirmation_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            life.confirmation('b' * 64, 'a' * 64, 'enable')

    def test_matching_confirmation(self):
        life.confirmation('a' * 64, 'a' * 64, 'enable')

    def test_disable_revokes_future_boot_even_when_native_stop_refuses(self):
        value = {**self.value, 'enabled': True}
        events = []
        with patch.object(life, 'load', return_value=value), \
                patch.object(life, 'write', side_effect=lambda p, v: events.append(('write', copy.deepcopy(v)))), \
                patch.object(life, 'journal', side_effect=lambda *a, **k: events.append(('journal', a))), \
                patch.object(life, 'stop_owner', side_effect=RuntimeError('busy')):
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                life.stop.__wrapped__(self.name, persistent=True)
        self.assertFalse(events[0][1]['enabled'])
        self.assertEqual(events[1][1][1], 'disable_pending')

    def test_temporary_stop_does_not_revoke_boot(self):
        value = {**self.value, 'enabled': True}
        with patch.object(life, 'load', return_value=value), patch.object(life, 'write') as write, \
                patch.object(life, 'journal'), patch.object(life, 'stop_owner', return_value={'state': 'stopped'}):
            result = life.stop.__wrapped__(self.name)
        write.assert_not_called()
        self.assertTrue(result['enabled_at_boot'])

    def test_remove_enabled_device_has_no_mutation(self):
        value = {**self.value, 'enabled': True}
        with patch.object(life, 'load', return_value=value), patch.object(life.data, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Disable'):
                life.remove.__wrapped__(self.name, digest(value))
        run.assert_not_called()

    def test_missing_map_is_not_proof_of_owner_exit(self):
        with patch.object(life, 'load', return_value=self.value), \
                patch.object(life, 'map_entry', return_value=None), \
                patch.object(life, 'stop_owner', side_effect=RuntimeError('owner unresolved')), \
                patch.object(life, 'config_path') as path:
            with self.assertRaisesRegex(RuntimeError, 'owner unresolved'):
                life.remove.__wrapped__(self.name, digest(self.value))
        path.assert_not_called()

    def test_activation_failure_records_incomplete_consent(self):
        events = []
        with patch.object(life, 'load', return_value=copy.deepcopy(self.value)), \
                patch.object(life.release_support, 'require_supported'), \
                patch.object(life, 'install_boot'), patch.object(life, 'write'), \
                patch.object(life, 'journal', side_effect=lambda *a, **k: events.append(a[1])), \
                patch.object(life, 'start', side_effect=RuntimeError('busy')):
            with self.assertRaisesRegex(RuntimeError, 'busy'):
                life.enable.__wrapped__(self.name, digest(self.value))
        self.assertEqual(events, ['enabling', 'enable_incomplete'])

    def test_unsupported_enable_does_not_write_boot_or_consent(self):
        with patch.object(life, 'load', return_value=self.value), \
                patch.object(life.release_support, 'require_supported', side_effect=RuntimeError('unsupported')), \
                patch.object(life, 'install_boot') as install, patch.object(life, 'write') as write:
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                life.enable.__wrapped__(self.name, digest(self.value))
        install.assert_not_called(); write.assert_not_called()

    def test_missing_confirmation_precedes_enable_effects(self):
        with patch.object(life, 'load', return_value=self.value), patch.object(life, 'install_boot') as install:
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                life.enable.__wrapped__(self.name, 'stale')
        install.assert_not_called()

    def test_boot_skips_disabled_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder); (config / (self.name + '.json')).write_text('{}')
            with patch.object(life, 'CONFIG', config), patch.object(life, 'load', return_value=self.value), \
                    patch.object(life, 'start') as start:
                self.assertEqual(life.boot.__wrapped__(), {'devices': []})
            start.assert_not_called()

    def test_uninstall_refuses_configured_devices_before_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder); (config / (self.name + '.json')).write_text('{}')
            with patch.object(life, 'CONFIG', config), patch.object(life.data, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'every configured'):
                    life.uninstall.__wrapped__()
            run.assert_not_called()

    def test_foreign_boot_integration_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            unit = Path(folder) / 'unit'; unit.write_text('foreign configuration')
            with patch.object(life, 'BOOT', unit), patch.object(life.data, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'changed independently'):
                    life.install_boot()
            run.assert_not_called()
            self.assertEqual(unit.read_text(), 'foreign configuration')


class ReleaseSupportTests(unittest.TestCase):
    def setUp(self):
        self.subject = {'platform': {'id': 'ubuntu', 'version_id': '24.04', 'architecture': 'amd64'}}
        self.acceptance = {'schema': 1, 'version': '0.0.1-beta', 'result': 'passed',
                           'purpose': 'data_activation_release_acceptance', 'filesystems': ['ext4'],
                           'subjects': [self.subject], 'evidence_sha256': 'a' * 64}

    def evaluate(self, record, subject=None):
        with patch.object(release_support, 'read_trusted_json', return_value=record), \
                patch.object(release_support, 'subject', return_value=subject or self.subject):
            return release_support.require_supported()

    def test_candidate_only_acceptance_cannot_activate(self):
        with self.assertRaisesRegex(RuntimeError, 'No valid'):
            self.evaluate({**self.acceptance, 'purpose': 'candidate_preparation_prerequisites'})

    def test_changed_combination_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'combination'):
            self.evaluate(self.acceptance, {**self.subject, 'kernel': 'different'})

    def test_missing_report_reference_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'evidence'):
            self.evaluate({**self.acceptance, 'evidence_sha256': ''})

    def test_exact_qualified_combination(self):
        self.assertEqual(len(self.evaluate(self.acceptance)), 64)


if __name__ == '__main__':
    unittest.main()
