"""Diagnostics never confuse historical evidence, expose secrets or mutate maps."""
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
import diagnostics as doctor


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = doctor.Reader(self.root, os.getuid())
        self.config = {'map_name': 'ram-rescue-path', 'map_uuid': 'RAMRESCUE-HOST-private_uuid',
                       'run_dir': '/run/ram-rescue-guard/state'}
        self.transaction = {'schema': 1, 'boot_id': 'current-boot-secret',
                            'map_name': self.config['map_name'], 'map_uuid': self.config['map_uuid'],
                            'owner_epoch': 'private-owner', 'owner_pid': 123, 'phase': 'idle', 'updated_at': 22}
        self.event = {'owner_epoch': 'private-owner', 'state': 'ready', 'time': 23, 'recoveries': 0}
        self.mapping = {'uuid': self.config['map_uuid'], 'info': {'suspended': 0, 'live_table': 1},
                        'active': [[0, 1024, 'multipath', 'private-device-parameters']], 'inactive': []}
        self.service = {'MainPID': '123', 'ActiveState': 'active', 'NoNewPrivileges': 'yes', 'PrivateDevices': 'no',
                        'ExecMainStartTimestampMonotonic': '21000000'}
        self.owner = {'pid': 123, 'started_at': 21, 'binary_sha256': 'c' * 64, 'runtime_root': doctor.RAM}
        self.options = {'reader': self.reader, 'service_reader': Mock(side_effect=lambda unit: dict(self.service)),
                        'map_reader': Mock(side_effect=lambda name: deepcopy(self.mapping)),
                        'process_reader': Mock(side_effect=lambda pid: dict(self.owner)),
                        'package_reader': Mock(return_value={})}
        self.write('/proc/sys/kernel/random/boot_id', b'current-boot-secret\n')

    def write(self, path, value):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for parent in target.parents:
            if parent == self.root:
                break
            parent.chmod(0o700)
        target.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        target.chmod(0o600)
        return target

    def ready(self):
        self.write(doctor.ROOT_CONFIG, self.config)
        self.write(self.config['run_dir'] + '/path-transaction.json', self.transaction)
        self.write(self.config['run_dir'] + '/path-state.json', self.event)
        return doctor.doctor(**self.options)

    def test_not_installed_and_installed_not_started_are_distinct(self):
        self.assertEqual(doctor.doctor(**self.options)['state'], 'not_installed')
        self.write(doctor.ROOT_RECEIPT, {'state': 'installed'})
        self.assertEqual(doctor.doctor(**self.options)['state'], 'installed_not_running')
        self.write(doctor.ROOT_RECEIPT, {'state': 'uninstalled'})
        self.assertEqual(doctor.doctor(**self.options)['state'], 'not_installed')
        self.options['service_reader'].assert_not_called()
        self.options['map_reader'].assert_not_called()

    def test_current_owner_and_mapping_are_required_for_ready(self):
        report = self.ready()
        self.assertEqual(report['state'], 'ready')
        self.assertTrue(report['devices'][0]['owner_matches'])
        self.assertEqual(report['devices'][0]['running_binary_sha256'], 'c' * 64)
        self.assertEqual(report['devices'][0]['security']['NoNewPrivileges'], 'yes')

    def test_old_boot_epoch_pid_and_reused_pid_do_not_imply_ready(self):
        for field, value in [('boot_id', 'old-boot'), ('owner_epoch', 'old-owner'),
                             ('owner_pid', 456), ('updated_at', 19), ('map_uuid', 'other-map')]:
            with self.subTest(field=field):
                before = self.transaction[field]
                self.transaction[field] = value
                self.assertEqual(self.ready()['state'], 'unknown')
                self.transaction[field] = before
        self.event['time'] = 2
        self.assertEqual(self.ready()['state'], 'unknown')

    def test_inactive_service_cannot_reuse_ready_event(self):
        self.service['ActiveState'] = 'inactive'
        self.assertEqual(self.ready()['state'], 'installed_not_running')

    def test_failure_recovery_and_refusal_remain_distinct(self):
        for event, wanted in [('waiting', 'recovering'), ('probing', 'recovering'),
                              ('rejected', 'refused'), ('expired', 'failed'), ('failed', 'failed')]:
            with self.subTest(event=event):
                self.event['state'] = event
                self.assertEqual(self.ready()['state'], wanted)

    def test_suspended_wrong_uuid_or_wrong_target_cannot_be_ready(self):
        original = deepcopy(self.mapping)
        for change in ({'uuid': 'wrong'}, {'info': {'suspended': 1, 'live_table': 1}},
                       {'inactive': [[0, 1024, 'linear', '8:1']]},
                       {'active': [[0, 1024, 'linear', '8:1']]}):
            with self.subTest(change=change):
                self.mapping = {**original, **change}
                self.assertEqual(self.ready()['state'], 'unknown')

    def test_owner_change_during_collection_is_unknown(self):
        self.options['service_reader'] = Mock(side_effect=[self.service, {**self.service, 'MainPID': '124'}])
        self.assertEqual(self.ready()['state'], 'unknown')

    def test_redaction_uses_only_whitelisted_fields_and_local_tokens(self):
        secret = 'alice-host /home/alice private-serial private-filesystem-uuid'
        self.config.update(hostname=secret, identity={'usb_serial': secret}, password=secret)
        self.event.update(reason='serial does not match the enrolled disk ' + secret,
                          node=secret, arbitrary={'shadow': secret, 'private_key': secret}, state='rejected')
        self.write(self.config['run_dir'] + '/path-events.jsonl', (json.dumps(self.event) + '\n').encode())
        report = self.ready()
        encoded = doctor.encode(report).decode()
        for value in (secret, 'current-boot-secret', 'private-owner', self.config['map_uuid'], 'private-device-parameters'):
            self.assertNotIn(value, encoded)
        last = report['devices'][0]['last_event']
        self.assertEqual(last['reason_code'], 'identity_mismatch')
        self.assertEqual(last['reason_token'], report['devices'][0]['timeline'][0]['reason_token'])
        self.assertEqual(last['owner'], report['devices'][0]['transaction']['owner'])
        self.assertNotEqual(last['owner'], self.ready()['devices'][0]['last_event']['owner'])

    def test_permission_denied_is_unknown_not_absent(self):
        self.write(doctor.ROOT_CONFIG, self.config).chmod(0o666)
        report = doctor.doctor(**self.options)
        self.assertEqual(report['state'], 'unknown')
        self.assertTrue(report['issues'])

    def test_parent_permissions_symlinks_and_nonregular_files_are_rejected(self):
        target = self.write('/trusted/value.json', {'ok': True})
        self.assertTrue(self.reader.json('/trusted/value.json')['ok'])
        target.parent.chmod(0o777)
        with self.assertRaises(doctor.InputError):
            self.reader.json('/trusted/value.json')
        target.parent.chmod(0o700)
        target.unlink()
        target.symlink_to('/etc/shadow')
        with self.assertRaises(OSError):
            self.reader.json('/trusted/value.json')
        target.unlink()
        os.mkfifo(target, 0o600)
        with self.assertRaises(doctor.InputError):
            self.reader.json('/trusted/value.json')

    def test_directory_path_replacement_stays_on_pinned_original(self):
        self.write('/trusted/value.json', {'safe': True})
        self.write('/other/value.json', {'safe': False})
        original_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = original_open(path, flags, *args, **kwargs)
            if path == 'trusted' and not swapped:
                swapped = True
                (self.root / 'trusted').rename(self.root / 'original')
                (self.root / 'trusted').symlink_to(self.root / 'other')
            return fd

        with patch.object(doctor.os, 'open', side_effect=swapping_open):
            self.assertTrue(self.reader.json('/trusted/value.json')['safe'])

    def test_wrong_owner_and_oversized_input_are_rejected(self):
        self.write('/trusted/value.json', {'safe': True})
        other_uid = doctor.Reader(self.root, os.getuid() + 1)
        with self.assertRaises(doctor.InputError):
            other_uid.json('/trusted/value.json')
        self.write('/trusted/large.json', b'a' * (doctor.JSON_LIMIT + 1))
        with self.assertRaises(doctor.InputError):
            self.reader.json('/trusted/large.json')

    def test_malformed_and_nonfinite_fields_do_not_escape(self):
        self.event.update(state=['private'], reason={'secret': 'private'}, time=float('nan'), recoveries=-1)
        report = self.ready()
        self.assertEqual(report['state'], 'unknown')
        self.assertIsNone(report['devices'][0]['last_event']['time'])
        self.assertIsNone(report['devices'][0]['last_event']['recoveries'])
        doctor.encode(report)
        self.event['time'] = 23
        self.event['reason'] = '\ud800'
        self.service['CapabilityBoundingSet'] = 'cap_private_hostname'
        report = self.ready()
        self.assertEqual(report['state'], 'unknown')
        self.assertNotIn('private_hostname', doctor.encode(report).decode())

    def test_invalid_run_directory_is_never_opened(self):
        self.config['run_dir'] = '/etc'
        self.write(doctor.ROOT_CONFIG, self.config)
        with patch.object(self.reader, 'json', wraps=self.reader.json) as read:
            report = doctor.doctor(**self.options)
        self.assertEqual(report['state'], 'unknown')
        self.assertNotIn('/etc/path-state.json', [call.args[0] for call in read.call_args_list])

    def test_unregistered_maps_are_never_queried(self):
        self.ready()
        self.options['map_reader'].assert_called_once_with('ram-rescue-path')

    def test_timeline_limit_and_invalid_lines(self):
        events = [json.dumps({**self.event, 'time': index + 100}) for index in range(200)]
        events.insert(-1, '{unfinished')
        self.write(self.config['run_dir'] + '/path-events.jsonl', ('\n'.join(events) + '\n').encode())
        report = self.ready()
        self.assertLessEqual(len(report['devices'][0]['timeline']), doctor.MAX_EVENTS)
        self.assertTrue(report['issues'])

    def test_dependency_comparison_and_secret_path_rejection(self):
        import hashlib
        expected = hashlib.sha256(b'old-library').hexdigest()
        library = '/usr/lib/test/libexample.so.1'
        self.write(doctor.RAM + library, b'old-library')
        self.write(library, b'new-library')
        self.write(doctor.RUNTIME, {'binary_sha256': 'a' * 64, 'library_sha256': {
            library: expected, '/etc/shadow': 'a' * 64, '/usr/lib/../../etc/shadow': 'a' * 64}})
        self.write(doctor.ROOT_RECEIPT, {'image_sha256': 'b' * 64, 'build': {'native_runtime': {'binary_sha256': 'a' * 64}}})
        report = doctor.doctor(**self.options)
        dependencies = report['versions']['runtimes'][0]['dependencies']
        self.assertEqual(len(dependencies), 1)
        self.assertTrue(dependencies[0]['frozen_matches'])
        self.assertTrue(dependencies[0]['host_differs'])
        self.assertEqual(sum(x['code'] == 'invalid_dependency' for x in report['issues']), 2)

    def test_library_symlink_escape_does_not_open_credentials(self):
        import hashlib
        path = '/usr/lib/test/libescape.so.1'
        target = self.write('/etc/shadow', b'do-not-read')
        (self.root / 'usr/lib/test').mkdir(parents=True)
        for name in ('usr', 'usr/lib', 'usr/lib/test'):
            (self.root / name).chmod(0o700)
        (self.root / path.lstrip('/')).symlink_to(target)
        self.write(doctor.RUNTIME, {'library_sha256': {path: hashlib.sha256(b'no').hexdigest()}})
        with patch.object(self.reader, 'hash', wraps=self.reader.hash) as hash_file:
            report = doctor.doctor(**self.options)
        self.assertNotIn('/etc/shadow', [call.args[0] for call in hash_file.call_args_list])
        self.assertIsNone(report['versions']['runtimes'][0]['dependencies'][0]['host_sha256'])

    def test_standalone_explicit_data_controller_without_root_boot(self):
        self.config = {'map_name': 'rr-data-test', 'map_uuid': 'RAMRESCUE-DATA-test',
                       'run_dir': '/run/ram-rescue-data/rr-data-test/state'}
        self.transaction.update(map_name=self.config['map_name'], map_uuid=self.config['map_uuid'])
        self.mapping['uuid'] = self.config['map_uuid']
        self.owner['runtime_root'] = doctor.PRIVATE_RAM
        self.write('/run/ram-rescue-data/rr-data-test/config.json', self.config)
        self.write(self.config['run_dir'] + '/path-transaction.json', self.transaction)
        self.write(self.config['run_dir'] + '/path-state.json', self.event)
        report = doctor.doctor(**self.options)
        self.assertEqual(report['state'], 'ready')
        self.assertEqual(report['devices'][0]['runtime_context'], 'standalone_data')
        self.assertEqual(report['devices'][0]['role'], 'data')
        self.options['service_reader'].assert_called_with('ram-rescue-data-rr-data-test.service')

    def test_package_version_and_hash_changes_are_independent(self):
        import hashlib
        path = '/usr/lib/test/libexample.so.1'
        expected = hashlib.sha256(b'unchanged').hexdigest()
        package = {'package': 'libexample:amd64', 'version': '1.0-1', 'architecture': 'amd64'}
        self.write(doctor.RAM + path, b'unchanged')
        self.write(path, b'unchanged')
        self.write(doctor.RUNTIME, {'library_sha256': {path: expected}, 'dependency_packages': {path: package}})
        self.options['package_reader'].return_value = {'libexample:amd64': {**package, 'version': '1.0-2'}}
        report = doctor.doctor(**self.options)
        dependency = report['versions']['runtimes'][0]['dependencies'][0]
        self.assertFalse(dependency['host_differs'])
        self.assertTrue(dependency['package_version_differs'])
        self.assertEqual(dependency['host_package']['version'], '1.0-2')
        self.options['package_reader'].assert_called_once_with({'libexample:amd64'})

    def test_foreign_process_root_is_not_a_current_guard(self):
        self.owner['runtime_root'] = '/untrusted'
        self.assertEqual(self.ready()['state'], 'unknown')

    def test_service_monotonic_start_rejects_previous_invocation(self):
        self.service['ExecMainStartTimestampMonotonic'] = '30000000'
        self.assertEqual(self.ready()['state'], 'unknown')
        self.service.pop('ExecMainStartTimestampMonotonic')
        self.assertEqual(self.ready()['state'], 'unknown')

    def test_base_elf_provenance_is_scoped_and_compared(self):
        import hashlib
        path = '/usr/bin/busybox'
        expected = hashlib.sha256(b'old-tool').hexdigest()
        self.write(doctor.RAM + path, b'old-tool')
        self.write(path, b'new-tool')
        self.write(doctor.RUNTIME, {'library_sha256': {}})
        self.write(doctor.RAM + '/etc/rescue/base-runtime.json', {
            'schema': 1, 'kind': 'base-rescue-tools', 'file_sha256': {path: expected, '/etc/shadow': 'a' * 64}})
        report = doctor.doctor(**self.options)
        runtime = report['versions']['runtimes'][0]
        self.assertTrue(runtime['base_manifest_present'])
        self.assertEqual(len(runtime['base_dependencies']), 1)
        self.assertTrue(runtime['base_dependencies'][0]['frozen_matches'])
        self.assertTrue(runtime['base_dependencies'][0]['host_differs'])

    def test_partial_package_query_does_not_drop_available_rows(self):
        import subprocess
        original = subprocess.Popen

        def partial_query(args, **kwargs):
            self.assertEqual(args[0], '/usr/bin/dpkg-query')
            return original([sys.executable, '-c',
                'print("libexample:amd64\\t1.0-2\\tamd64"); raise SystemExit(1)'], **kwargs)

        with patch.object(doctor.subprocess, 'Popen', side_effect=partial_query):
            result = doctor.package_versions({'libexample:amd64', 'missing-package'})
        self.assertEqual(result['libexample:amd64']['version'], '1.0-2')
        self.assertNotIn('missing-package', result)

    def test_export_private_no_overwrite_or_symlink(self):
        output = self.root / 'output/report.json'
        with patch.object(doctor, 'doctor', return_value={'schema': 1}):
            result = doctor.export_report(output)
            self.assertFalse(result['uploaded'])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
            with self.assertRaises(FileExistsError):
                doctor.export_report(output)
            target = self.root / 'target'
            target.write_text('unchanged')
            output.unlink()
            output.symlink_to(target)
            with self.assertRaises(FileExistsError):
                doctor.export_report(output)
            self.assertEqual(target.read_text(), 'unchanged')

    def test_default_export_creates_separate_private_directories(self):
        with patch.object(doctor, 'doctor', return_value={'schema': 1}), patch.object(doctor.Path, 'cwd', return_value=self.root):
            first = Path(doctor.export_report()['path'])
            second = Path(doctor.export_report()['path'])
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(stat.S_IMODE(first.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)

    def test_export_rejects_public_or_symlink_parent_and_size(self):
        folder = self.root / 'public'
        folder.mkdir(mode=0o755)
        with patch.object(doctor, 'doctor', return_value={'schema': 1}):
            with self.assertRaises(doctor.InputError):
                doctor.export_report(folder / 'report.json')
            (self.root / 'link').symlink_to(folder)
            with self.assertRaises(OSError):
                doctor.export_report(self.root / 'link/report.json')
        with patch.object(doctor, 'doctor', return_value={'large': 'a' * doctor.EXPORT_LIMIT}):
            with self.assertRaises(doctor.InputError):
                doctor.export_report(self.root / 'private/report.json')
        self.assertFalse((self.root / 'private').exists())

    def test_export_path_replacement_does_not_redirect_write(self):
        folder = self.root / 'output'
        folder.mkdir(mode=0o700)
        (self.root / 'other').mkdir(mode=0o700)
        original_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = original_open(path, flags, *args, **kwargs)
            if path == 'output' and not swapped:
                swapped = True
                folder.rename(self.root / 'original')
                folder.symlink_to(self.root / 'other')
            return fd

        with patch.object(doctor, 'doctor', return_value={'schema': 1}), patch.object(doctor.os, 'open', side_effect=swapping_open):
            with self.assertRaises(OSError):
                doctor.export_report(folder / 'report.json')
        self.assertFalse((self.root / 'original/report.json').exists())
        self.assertFalse((self.root / 'other/report.json').exists())


if __name__ == '__main__':
    unittest.main()
