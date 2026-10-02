"""Unified maintenance starts only registered maps and preserves root ownership."""
from copy import deepcopy
import importlib.util
import hashlib
import json
from pathlib import Path
import stat
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
spec = importlib.util.spec_from_file_location('guard_manager', BASE / 'guard/manage.py')
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


def profile():
    identity = {'kind': 'filesystem', 'vid': '1234', 'pid': '5678',
                'usb_serial': 'test-serial', 'sectors': 32768,
                'partition_number': 1, 'partuuid': 'test-partition',
                'fs_type': 'ext4', 'fs_uuid': 'test-filesystem'}
    return {'schema': 1, 'identity': identity, 'guard': {
        'schema': 1, 'profile': 'host-data', 'map_name': 'rr-data-test',
        'map_uuid': 'RAMRESCUE-DATA-test', 'kernel_release': '7.0.0-test',
        'run_dir': '/run/ram-rescue-data/rr-data-test/state',
        'identity_path': '/run/ram-rescue-data/rr-data-test/identity.json',
        'queue_seconds': 8, 'partition_sectors': 16384, 'partition_start': 2048,
        'logical_block_size': 512,
        'layout': {key: identity[key] for key in ('kind', 'fs_type', 'fs_uuid', 'partuuid')},
        'initial_node': '/dev/sdb1', 'initial_sys_path': '/sys/devices/old/sdb/sdb1',
        'initial_diskseq': 12,
    }}


def root_profile():
    result = profile()
    for key in ('kind', 'fs_type', 'fs_uuid'):
        result['identity'].pop(key)
    result['identity'].update(pv_uuid='test-pv', vg_uuid='test-vg', vg_name='portable',
                              lvs={'ubuntu': {'dm_uuid': 'LVM-test-root'}})
    result['guard'].update(profile='host', map_name='ram-rescue-path',
                           map_uuid='RAMRESCUE-HOST-test',
                           run_dir='/run/ram-rescue-guard/state',
                           identity_path='/etc/rescue/identity.json',
                           root_lv='ubuntu', root_fs_uuid='root-fs',
                           layout=[{'segtype': 'linear', 'lv_name': 'ubuntu'}])
    return result


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        paths = {name: self.root / name.lower() for name in (
            'REGISTRY', 'RUNTIME', 'INSTALL', 'VERSIONS', 'ENTRY', 'UNIT',
            'CONTROLLER', 'SLICE', 'RULE', 'ROOT_CONFIG', 'CONTROL_LOCK')}
        patch.multiple(manager, **paths).start()
        patch.object(manager.services, 'require_root').start()
        patch.object(manager.services, 'require_ram').start()
        self.commands = patch.object(manager.services, 'run', return_value='inactive').start()
        self.ram = self.root / 'ram'
        patch.object(manager.services, 'RAM', self.ram).start()
        self.live_maps = patch.object(manager, 'maps', return_value={}).start()
        self.root_enrollment = patch.object(manager, 'root_profile', return_value=None).start()
        self.addCleanup(patch.stopall)
        self.profile = profile()
        self.record = manager.record_from_profile(self.profile)
        self.map = self.root / 'sys/dm-9'
        (self.map / 'slaves').mkdir(parents=True)
        self.partition = self.root / 'sys/sdb1'
        self.partition.mkdir()
        (self.map / 'slaves/sdb1').symlink_to(self.partition)
        self.item = {'name': 'rr-data-test', 'uuid': 'RAMRESCUE-DATA-test', 'sys': self.map}

    def installed_integration(self):
        """Model a prior Python install without starting any system service."""
        self.commands.return_value = ''
        program = manager.VERSIONS / ('a' * 64) / 'guard/manage.py'
        program.parent.mkdir(parents=True)
        program.write_text('# Previous Python manager\n')
        manager.INSTALL.mkdir()
        manager.REGISTRY.mkdir()
        files = {manager.UNIT: manager.render_manager(program).encode(),
                 manager.CONTROLLER: b'# Previous Python controller\n',
                 manager.SLICE: manager.services.slice_unit().encode(), manager.RULE: b''}
        for path, content in files.items():
            manager.atomic(path, content, mode=0o640)
        record = {'state': 'installed', 'program': str(program),
                  'files': {str(path): hashlib.sha256(value).hexdigest() for path, value in files.items()},
                  'rule_sha256': hashlib.sha256(b'').hexdigest()}
        manager.write_json(manager.INSTALL / 'install.json', record)
        manager.ENTRY.symlink_to(program)
        self.original_files = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                               for path in (*files, manager.INSTALL / 'install.json')}
        self.original_program = program
        runtime = self.ram / 'opt/old-python'
        runtime.mkdir(parents=True)
        (runtime / 'owner').write_text('running root remains untouched')
        (self.ram / 'opt/manager').symlink_to('old-python')
        return record

    def assert_original_integration(self):
        for path, (content, mode) in self.original_files.items():
            self.assertEqual(path.read_bytes(), content, str(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, str(path))
        self.assertEqual(manager.ENTRY.readlink(), self.original_program)
        self.assertEqual((self.ram / 'opt/manager').readlink(), Path('old-python'))

    def test_activation_rules_match_only_registered_dm_identity(self):
        rules = manager.render_rules([self.record])
        active = [line for line in rules.splitlines() if 'SYSTEMD_WANTS' in line]
        self.assertEqual(len(active), 1)
        self.assertIn('ENV{DM_NAME}=="rr-data-test"', active[0])
        self.assertIn('ENV{DM_UUID}=="RAMRESCUE-DATA-test"', active[0])
        self.assertIn('ram-rescue-maintain@rr-data-test.service', active[0])
        raw = next(line for line in rules.splitlines() if 'ATTR{partition}' in line)
        self.assertIn('ATTRS{serial}=="test-serial"', raw)
        self.assertNotIn('SYSTEMD_WANTS', raw)
        self.assertNotIn('BindsTo=', manager.render_controller())
        self.assertNotIn('Restart=always', manager.render_controller())
        self.assertIn('ExecStart=/opt/manager/maintain --record ', manager.render_controller())
        self.assertIn('ExecStopPost=/opt/manager/maintain --record ', manager.render_controller())
        self.assertNotIn('/opt/manager/guard-runtime', manager.render_controller())
        self.assertNotIn('/usr/bin/python3 /opt/manager', manager.render_controller())
        self.assertNotIn('RUN+=', rules)

    def test_root_registration_recognizes_existing_owner_without_new_service(self):
        root = root_profile()
        item = {'name': 'ram-rescue-path', 'uuid': root['guard']['map_uuid']}
        with patch.object(manager, 'resolve_map', return_value=(item, root)), \
                patch.object(manager, 'collect') as collect:
            result = manager.register('/')
        self.assertEqual(result['state'], 'already_managed')
        self.assertEqual(result['owner'], 'ram-rescue-guard.service')
        self.assertFalse(manager.REGISTRY.exists())
        collect.assert_not_called()
        self.commands.assert_not_called()

    def test_persistence_is_private_and_omits_linux_instance_names(self):
        with patch.object(manager, 'resolve_map', return_value=(self.item, None)), \
                patch.object(manager, 'collect', return_value=self.profile):
            result = manager.register('/dev/mapper/rr-data-test')
        path = Path(result['registration'])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        saved = json.loads(path.read_text())
        self.assertEqual(saved, self.record)
        self.assertFalse(any(key.startswith('initial_') for key in saved['guard']))
        self.commands.assert_not_called()

    def test_registration_never_relearns_changed_identity(self):
        manager.REGISTRY.mkdir()
        target = manager.REGISTRY / 'rr-data-test.json'
        manager.write_json(target, self.record)
        changed = deepcopy(self.profile)
        changed['identity']['usb_serial'] = 'different-device'
        with patch.object(manager, 'resolve_map', return_value=(self.item, None)), \
                patch.object(manager, 'collect', return_value=changed):
            with self.assertRaisesRegex(RuntimeError, 'never automatically relearned'):
                manager.register('/dev/mapper/rr-data-test')
        self.assertEqual(manager.read_json(target), self.record)
        self.commands.assert_not_called()

    def test_install_refuses_every_preexisting_integration_path(self):
        for name in ('INSTALL', 'ENTRY', 'UNIT', 'CONTROLLER', 'SLICE', 'RULE'):
            path = getattr(manager, name)
            path.write_text('existing external integration')
            with self.subTest(name=name), patch.object(manager, 'install_sources') as install_sources:
                with self.assertRaisesRegex(RuntimeError, 'already exists'):
                    manager.install()
                install_sources.assert_not_called()
            path.unlink()
        self.commands.assert_not_called()

    def test_only_matching_existing_map_is_started(self):
        wrong_uuid = {**self.item, 'uuid': 'RAMRESCUE-DATA-foreign'}
        self.live_maps.return_value = {'dm-9': wrong_uuid}
        self.assertEqual(manager.activate_present([self.record]), [])
        self.commands.assert_not_called()
        self.live_maps.return_value = {'dm-9': self.item}
        result = manager.activate_present([self.record])
        self.assertEqual(result, ['ram-rescue-maintain@rr-data-test.service'])
        self.commands.assert_called_once_with(['systemctl', 'start', result[0]])

    def test_status_reports_waiting_for_map_when_registered_disk_has_no_map(self):
        with patch.object(manager, 'records', return_value=[self.record]):
            result = manager.status()
        device = result['devices'][0]
        self.assertEqual(device['state'], 'waiting_for_map')
        self.assertFalse(device['map_present'])
        self.assertEqual(device['service_state'], 'inactive')
        self.assertEqual(self.commands.call_count, 1)
        self.assertEqual(self.commands.call_args.args[0][:2], ['systemctl', 'show'])

    def test_status_does_not_reuse_old_ready_state_for_an_absent_map(self):
        record = deepcopy(self.record)
        evidence = self.root / 'old-state'
        evidence.mkdir()
        record['guard']['run_dir'] = str(evidence)
        manager.write_json(evidence / 'path-state.json', {'state': 'ready', 'recoveries': 3})
        with patch.object(manager, 'records', return_value=[record]):
            device = manager.status()['devices'][0]
        self.assertEqual(device['state'], 'waiting_for_map')
        self.assertEqual(device['last_state'], 'ready')
        self.assertEqual(device['recoveries'], 3)

    def test_installation_failure_keeps_incomplete_receipt_and_refuses_retry(self):
        program = self.root / 'program.py'
        program.write_text('installed version')
        def command(arguments):
            if arguments[:3] == ['systemctl', 'enable', '--now']:
                raise RuntimeError('manager service failed')
            return ''
        self.commands.side_effect = command
        with patch.object(manager, 'install_sources', return_value=program):
            with self.assertRaisesRegex(RuntimeError, 'manager service failed'):
                manager.install()
        receipt = manager.read_json(manager.INSTALL / 'install.json')
        self.assertEqual(receipt['state'], 'installing')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            manager.install()
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            manager.receipt()

    def test_installed_management_bundle_imports_without_the_retired_runtime(self):
        with patch.object(manager, 'VERSIONS', self.root / 'versions'):
            program = manager.install_sources()
        bundle = program.parent
        self.assertTrue((bundle / 'admin/__init__.py').is_file())
        self.assertFalse((bundle / 'runtime').exists())
        result = subprocess.run([sys.executable, str(program), '--help'],
                                check=True, text=True, capture_output=True)
        self.assertIn('register', result.stdout)
        self.assertIn('upgrade', result.stdout)

    def test_upgrade_stages_next_boot_only_and_status_reads_existing_python_owner(self):
        old = self.installed_integration()
        with patch.object(manager, 'prepare') as prepare, \
                patch.object(manager.services, 'stage_runtime') as stage_runtime, \
                patch.object(manager, 'activate_present') as activate:
            result = manager.upgrade()
        prepare.assert_not_called()
        stage_runtime.assert_not_called()
        activate.assert_not_called()
        record = manager.receipt()
        self.assertEqual(record['activation'], 'protected_reboot')
        self.assertNotEqual(record['program'], old['program'])
        self.assertEqual(str(manager.ENTRY.readlink()), record['program'])
        self.assertIn(record['program'], manager.UNIT.read_text())
        self.assertEqual(manager.CONTROLLER.read_text(), manager.render_controller())
        self.assertTrue(result['requires_protected_reboot'])
        self.assertFalse(result['running_root_changed'])
        self.assertFalse(result['runtime_prepared'])
        self.assertEqual(result['services_started'], [])
        self.assertEqual((self.ram / 'opt/manager').readlink(), Path('old-python'))
        self.assertEqual((self.ram / 'opt/old-python/owner').read_text(), 'running root remains untouched')
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertEqual([c for c in commands if c[:2] != ['systemctl', 'list-units']],
                         [['systemctl', 'daemon-reload']])
        backup = Path(result['backup'])
        journal = manager.read_json(backup / 'upgrade.json')
        self.assertEqual(journal['state'], 'complete')
        self.assertEqual(journal['entry_target'], old['program'])
        for path, (content, mode) in self.original_files.items():
            saved = journal['files'][str(path)]
            self.assertEqual((backup / saved['backup']).read_bytes(), content)
            self.assertEqual(saved['mode'], mode)
        root = root_profile()
        state = self.root / 'root-state'
        state.mkdir()
        manager.write_json(state / 'path-state.json', {'state': 'ready', 'recoveries': 2})
        root['guard']['run_dir'] = str(state)
        self.root_enrollment.return_value = root
        self.live_maps.return_value = {'dm-0': {'name': root['guard']['map_name'],
                                               'uuid': root['guard']['map_uuid']}}
        self.commands.return_value = 'active'
        status = manager.status()['devices'][0]
        self.assertEqual(status['state'], 'ready')
        self.assertEqual(status['recoveries'], 2)

    def test_upgrade_refuses_modified_integration_or_entry_before_staging(self):
        self.installed_integration()
        for path in (manager.UNIT, manager.CONTROLLER, manager.SLICE, manager.RULE):
            content = path.read_bytes()
            path.write_bytes(b'external edit')
            with self.subTest(path=path), patch.object(manager, 'install_sources') as stage:
                with self.assertRaisesRegex(RuntimeError, 'changed independently'):
                    manager.upgrade()
                stage.assert_not_called()
            path.write_bytes(content)
        manager.ENTRY.unlink()
        manager.ENTRY.symlink_to(self.root / 'other.py')
        with patch.object(manager, 'install_sources') as stage:
            with self.assertRaisesRegex(RuntimeError, 'command changed independently'):
                manager.upgrade()
            stage.assert_not_called()
        self.commands.assert_not_called()

    def test_upgrade_refuses_incomplete_receipt_and_foreign_file_set(self):
        record = self.installed_integration()
        for changed, message in [({**record, 'state': 'installing'}, 'incomplete'),
                                 ({**record, 'files': {**record['files'], '/foreign': '0' * 64}}, 'file set')]:
            manager.write_json(manager.INSTALL / 'install.json', changed)
            with self.subTest(message=message), patch.object(manager, 'install_sources') as stage:
                with self.assertRaisesRegex(RuntimeError, message):
                    manager.upgrade()
                stage.assert_not_called()
        self.commands.assert_not_called()

    def test_upgrade_refuses_any_registration_even_without_a_live_map(self):
        self.installed_integration()
        (manager.REGISTRY / 'unreadable-entry.json').write_text('not a valid registration')
        with patch.object(manager, 'install_sources') as stage:
            with self.assertRaisesRegex(RuntimeError, 'empty data registry'):
                manager.upgrade()
            stage.assert_not_called()
        self.assert_original_integration()
        self.commands.assert_not_called()

    def test_upgrade_refuses_running_or_transitioning_data_controllers(self):
        self.installed_integration()
        for state in ('active', 'activating', 'deactivating', 'reloading', 'failed'):
            self.commands.return_value = f'ram-rescue-data-rr-data-test.service loaded {state} running test'
            with self.subTest(state=state), patch.object(manager, 'install_sources') as stage:
                with self.assertRaisesRegex(RuntimeError, 'may still be active'):
                    manager.upgrade()
                stage.assert_not_called()
        self.assert_original_integration()

    def test_upgrade_restores_files_modes_entry_and_receipt_after_reload_failure(self):
        self.installed_integration()
        reloads = 0
        def command(arguments):
            nonlocal reloads
            if arguments == ['systemctl', 'daemon-reload']:
                reloads += 1
                if reloads == 1:
                    raise RuntimeError('injected daemon-reload failure')
            return ''
        self.commands.side_effect = command
        with self.assertRaisesRegex(RuntimeError, 'rolled_back'):
            manager.upgrade()
        self.assertEqual(reloads, 2)
        self.assert_original_integration()
        backup = next(manager.INSTALL.glob('upgrade-*'))
        journal = manager.read_json(backup / 'upgrade.json')
        self.assertEqual(journal['state'], 'rolled_back')
        self.assertEqual(journal['rollback_errors'], [])
        self.assertTrue(Path(journal['program']).is_file())

    def test_upgrade_rolls_back_an_atomic_write_that_failed_after_replacement(self):
        self.installed_integration()
        write = manager.atomic
        injected = False
        def fail_after_replacement(path, content, mode=0o600):
            nonlocal injected
            write(path, content, mode=mode)
            if path == manager.CONTROLLER and not injected:
                injected = True
                raise OSError('injected directory sync failure')
        with patch.object(manager, 'atomic', side_effect=fail_after_replacement):
            with self.assertRaisesRegex(RuntimeError, 'rolled_back'):
                manager.upgrade()
        self.assertTrue(injected)
        self.assert_original_integration()

    def test_upgrade_does_not_overwrite_a_foreign_edit_during_failed_rollback(self):
        self.installed_integration()
        reloads = 0
        def command(arguments):
            nonlocal reloads
            if arguments == ['systemctl', 'daemon-reload']:
                reloads += 1
                if reloads == 1:
                    manager.CONTROLLER.write_bytes(b'external concurrent edit')
                    raise RuntimeError('injected reload failure')
            return ''
        self.commands.side_effect = command
        with self.assertRaisesRegex(RuntimeError, 'rollback_failed'):
            manager.upgrade()
        self.assertEqual(manager.CONTROLLER.read_bytes(), b'external concurrent edit')
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            manager.receipt()
        backup = next(manager.INSTALL.glob('upgrade-*'))
        journal = manager.read_json(backup / 'upgrade.json')
        self.assertEqual(journal['state'], 'rollback_failed')
        self.assertTrue(journal['rollback_errors'])

    def test_upgrade_staging_failure_preserves_installed_state_and_records_failure(self):
        self.installed_integration()
        with patch.object(manager, 'install_sources', side_effect=OSError('injected staging failure')):
            with self.assertRaisesRegex(RuntimeError, 'rolled_back'):
                manager.upgrade()
        self.assert_original_integration()
        backup = next(manager.INSTALL.glob('upgrade-*'))
        self.assertEqual(manager.read_json(backup / 'upgrade.json')['state'], 'rolled_back')
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertTrue(all(command[:2] == ['systemctl', 'list-units'] for command in commands))

    def test_empty_registry_installation_never_starts_another_root_controller(self):
        program = self.root / 'program.py'
        program.write_text('installed version')
        with patch.object(manager, 'install_sources', return_value=program):
            result = manager.install()
        self.assertEqual(result['services'], [])
        self.assertEqual(result['root_owner'], 'existing_boot_service')
        self.assertEqual(manager.read_json(manager.INSTALL / 'install.json')['state'], 'installed')
        self.assertFalse(manager.ROOT_CONFIG.exists())
        commands = [call.args[0] for call in self.commands.call_args_list]
        self.assertNotIn(['systemctl', 'start', 'ram-rescue-guard.service'], commands)
        self.assertFalse(any(command[0] in ('dmsetup', 'mount', 'umount', 'lvm') for command in commands))

    def test_prepare_stages_only_records_and_runtime_without_creating_maps(self):
        runtime = self.ram / 'opt/data-guard/test-version'
        runtime.mkdir(parents=True)
        with patch.object(manager, 'records', return_value=[self.record]), \
                patch.object(manager.services, 'stage_runtime', return_value=runtime):
            result = manager.prepare()
        self.assertEqual(result['prepared'], 1)
        alias = self.ram / 'opt/manager'
        self.assertEqual(alias.resolve(), runtime)
        self.assertEqual(manager.read_json(manager.RUNTIME / 'entries/rr-data-test.json'), self.record)
        self.commands.assert_not_called()
        self.live_maps.assert_not_called()

    def test_prepare_never_replaces_live_runtime_or_identity(self):
        runtime = self.ram / 'opt/data-guard/test-version'
        runtime.mkdir(parents=True)
        alias = self.ram / 'opt/manager'
        alias.symlink_to('data-guard/test-version')
        folder = manager.RUNTIME / 'entries'
        folder.mkdir(parents=True)
        old = deepcopy(self.record)
        old['identity']['usb_serial'] = 'older-enrollment'
        manager.write_json(folder / 'rr-data-test.json', old)
        with patch.object(manager, 'records', return_value=[self.record]), \
                patch.object(manager.services, 'stage_runtime', return_value=runtime):
            with self.assertRaisesRegex(RuntimeError, 'never replace a live identity'):
                manager.prepare()
        self.assertEqual(manager.read_json(folder / 'rr-data-test.json'), old)
        self.assertEqual(alias.resolve(), runtime)

    def test_prepare_rejects_different_runtime_version(self):
        first = self.ram / 'opt/data-guard/first'
        second = self.ram / 'opt/data-guard/second'
        first.mkdir(parents=True)
        second.mkdir()
        alias = self.ram / 'opt/manager'
        alias.symlink_to('data-guard/first')
        with patch.object(manager, 'records', return_value=[]), \
                patch.object(manager.services, 'stage_runtime', return_value=second):
            with self.assertRaisesRegex(RuntimeError, 'another manager runtime'):
                manager.prepare()
        self.assertEqual(alias.resolve(), first)

    def test_uninstall_refuses_live_registered_map_before_any_service_change(self):
        self.live_maps.return_value = {'dm-9': self.item}
        with patch.object(manager, 'records', return_value=[self.record]), \
                patch.object(manager, 'receipt', return_value={'state': 'installed'}):
            with self.assertRaisesRegex(RuntimeError, 'still exist'):
                manager.uninstall()
        self.commands.assert_not_called()


if __name__ == '__main__':
    unittest.main()
