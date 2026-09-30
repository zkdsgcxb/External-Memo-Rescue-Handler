"""Host entry gates and isolated RAM state; no real block-device access."""
import builtins
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
sys.path.insert(0, str(BASE / 'guest'))
sys.path.insert(0, str(BASE.parent / 'guard/runtime'))
import guard_state
import path_guard


class HostProfileTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'state'
        self.run.mkdir()
        self.identity = self.root / 'identity.json'
        self.identity.write_text('{"enrolled": true}')
        self.config_path = self.root / 'config.json'
        self.config = {'profile': 'host', 'map_name': 'ram-rescue-path',
                       'map_uuid': 'RAMRESCUE-HOST-test', 'run_dir': str(self.run),
                       'kernel_release': '7.0.0-test', 'identity_path': str(self.identity)}
        self.config_path.write_text(json.dumps(self.config))
        path_guard.configure({})
        self.addCleanup(path_guard.configure, {})

    def test_lab_defaults_are_preserved(self):
        self.assertEqual((path_guard.NAME, path_guard.UUID, path_guard.DEVICE),
                         ('lab-path', 'mpath-RAMRESCUE-LAB', '/dev/mapper/lab-path'))
        self.assertEqual(path_guard.RUN, Path('/run'))
        self.assertTrue(guard_state.ENABLE_LAB_HOOKS)

    def test_invalid_host_settings_do_not_change_runtime(self):
        invalid = [{key: value for key, value in self.config.items() if key != missing}
                   for missing in ('map_name', 'map_uuid', 'run_dir', 'kernel_release')]
        invalid.extend({**self.config, key: value} for key, value in (
            ('profile', 'unknown'), ('map_name', '../root'), ('map_uuid', 'two words'),
            ('run_dir', 'state'), ('run_dir', '/'), ('run_dir', '/run/../etc'),
            ('identity_path', 'identity.json'), ('kernel_release', 7)))
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                path_guard.configure(config)
            self.assertEqual(path_guard.NAME, 'lab-path')
            self.assertEqual(path_guard.RUN, Path('/run'))
        self.assertEqual(list(self.run.iterdir()), [])

    def test_host_environment_requires_explicit_flag_and_exact_kernel(self):
        path_guard.configure(self.config)
        for commandline in ('root=/dev/dm-0', 'ram_rescue_guard=10', 'ram_rescue_guard=0'):
            with self.subTest(commandline=commandline), \
                    patch('path_guard.Path.read_text', return_value=commandline), \
                    patch('path_guard.os.uname', return_value=SimpleNamespace(release='7.0.0-test')):
                with self.assertRaisesRegex(RuntimeError, 'explicit'):
                    path_guard.validate_environment(self.config)
        with patch('path_guard.Path.read_text', return_value='ram_rescue_guard=1'), \
                patch('path_guard.os.uname', return_value=SimpleNamespace(release='6.8.0-test')):
            with self.assertRaisesRegex(RuntimeError, 'kernel'):
                path_guard.validate_environment(self.config)

    def test_valid_host_environment_does_not_import_lab_agent(self):
        original_import = builtins.__import__
        def import_without_agent(name, *args, **kwargs):
            if name == 'agent':
                self.fail('Host entry must not depend on the lab agent')
            return original_import(name, *args, **kwargs)
        with patch('path_guard.Path.read_text', return_value='root=/dev/dm-0 ram_rescue_guard=1 quiet'), \
                patch('path_guard.os.uname', return_value=SimpleNamespace(release='7.0.0-test')), \
                patch('builtins.__import__', side_effect=import_without_agent):
            path_guard.validate_environment(self.config)

    def test_cli_environment_rejection_precedes_owner_and_dm(self):
        with patch('path_guard.validate_environment', side_effect=RuntimeError('boot gate')), \
                patch('path_guard.acquire_owner') as owner, patch('path_guard.dm') as dm:
            with self.assertRaisesRegex(RuntimeError, 'boot gate'):
                path_guard.main(['--config', str(self.config_path)])
        owner.assert_not_called()
        dm.assert_not_called()
        self.assertEqual(list(self.run.iterdir()), [])

    def test_default_owner_evidence_journal_use_configured_directory(self):
        path_guard.configure(self.config)
        self.assertFalse(guard_state.ENABLE_LAB_HOOKS)
        with guard_state.Owner() as owner:
            self.assertEqual(owner.run, self.run)
            guard_state.Journal(owner, path_guard.NAME, path_guard.UUID).write('idle')
            guard_state.Evidence().event({'state': 'ready'})
        self.assertEqual({path.name for path in self.run.iterdir()},
                         {'path-owner.lock', 'path-transaction.json',
                          'path-events.jsonl', 'path-state.json'})

    def test_takeover_wait_evidence_uses_configured_directory(self):
        path_guard.configure(self.config)
        with guard_state.Owner(self.run) as existing:
            with patch('path_guard.Owner', side_effect=[BlockingIOError(), existing]) as constructor, \
                    patch('path_guard.time.sleep') as sleep:
                self.assertIs(path_guard.acquire_owner(True), existing)
            self.assertEqual(constructor.call_args_list[0].args, (self.run,))
            self.assertEqual(constructor.call_args_list[1].args, (self.run,))
            sleep.assert_called_once_with(1)
        evidence = guard_state.load_json(self.run / 'path-supervisor.json')
        self.assertEqual(evidence['state'], 'waiting_for_owner')

    def test_takeover_error_uses_configured_directory(self):
        with patch('path_guard.validate_environment'), \
                patch('path_guard.takeover', side_effect=RuntimeError('untrusted journal')):
            with self.assertRaisesRegex(RuntimeError, 'untrusted journal'):
                path_guard.main(['--config', str(self.config_path), '--takeover'])
        for filename in ('path-supervisor.json', 'path-state.json'):
            self.assertEqual(guard_state.load_json(self.run / filename)['state'], 'blocked')

    def manager(self):
        manager = Mock(current='/dev/mock3', state='ready')
        manager.check_map.return_value = 'enrolled active status'
        manager.current_present.return_value = True
        manager.current_active.return_value = True
        manager.event.side_effect = lambda state, **details: setattr(manager, 'state', state)
        manager.step.side_effect = lambda: setattr(manager, 'state', 'failed')
        return manager

    def run_main(self, manager):
        return patch.multiple('path_guard', validate_environment=Mock(),
                              Recovery=Mock(), Guard=Mock(return_value=manager),
                              Events=Mock(), Schedule=Mock())

    def test_main_loads_configured_identity_and_announces_verified_ready(self):
        manager = self.manager()
        with self.run_main(manager), patch('path_guard.notify_ready') as notify:
            recovery = path_guard.Recovery
            path_guard.main(['--config', str(self.config_path)])
        recovery.assert_called_once_with({'enrolled': True}, runner=path_guard.readonly)
        manager.current_present.assert_called_once()
        manager.current_active.assert_called_once_with('enrolled active status')
        notify.assert_called_once_with()
        manager.shutdown.assert_called_once_with()
        self.assertTrue((self.run / 'path-guard.pid').exists())

    def test_initial_missing_or_failed_path_never_announces_ready(self):
        for present, active in ((False, True), (True, False)):
            manager = self.manager()
            manager.current_present.return_value = present
            manager.current_active.return_value = active
            with self.subTest(present=present, active=active), self.run_main(manager), \
                    patch('path_guard.notify_ready') as notify:
                with self.assertRaisesRegex(RuntimeError, 'absent or not active'):
                    path_guard.main(['--config', str(self.config_path)])
            notify.assert_not_called()
            manager.event.assert_not_called()
            manager.shutdown.assert_called_once_with()
        self.assertFalse((self.run / 'path-guard.pid').exists())

    def test_notification_failure_is_failed_start_and_shuts_down(self):
        manager = self.manager()
        with self.run_main(manager), patch('path_guard.notify_ready', side_effect=OSError('no listener')):
            with self.assertRaisesRegex(OSError, 'no listener'):
                path_guard.main(['--config', str(self.config_path)])
        self.assertEqual(manager.state, 'failed')
        self.assertEqual(manager.event.call_args.kwargs['outcome'], 'startup_notification_failed')
        manager.shutdown.assert_called_once_with()

    def test_notify_supports_filesystem_and_abstract_socket(self):
        for address in (str(self.root / 'notify'), '@ram-rescue-test-' + str(os.getpid())):
            with self.subTest(address=address), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
                receiver.settimeout(1)
                receiver.bind('\0' + address[1:] if address.startswith('@') else address)
                with patch.dict(os.environ, {'NOTIFY_SOCKET': address}):
                    path_guard.notify_ready()
                self.assertEqual(receiver.recv(128), b'READY=1')

    def test_notify_without_socket_has_no_socket_call(self):
        with patch.dict(os.environ, {}, clear=True), patch('socket.socket') as constructor:
            path_guard.notify_ready()
        constructor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
