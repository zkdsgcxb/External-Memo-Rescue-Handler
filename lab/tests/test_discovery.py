"""Read-only discovery separates topology, preparation and recovery evidence."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
sys.path.insert(0, str(BASE / 'ram-rescue-demo/src'))
import discovery
import diagnostics as doctor
import manage
from admin.dm import DeviceMapper, expected_table


def node(name, dev, *, partition=None, parent=None, slaves=(), usb=False, **values):
    return dict(name=name, dev=dev, path='/sys/devices/' + name, partition=partition,
                parent=parent, size=16384 * 512, slaves=list(slaves), dm_name=None,
                dm_uuid=None, diskseq='12', usb=usb, serial='test-serial', model='测试盘', **values)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = doctor.Reader(self.root, os.getuid())
        self.nodes = {'sdb': node('sdb', '8:16', usb=True),
                      'sdb1': node('sdb1', '8:17', usb=True, partition=1, parent='sdb')}
        self.mounts, self.swaps = [], []
        self.kernel, self.timeout = '7.0.0-test', '10'
        self.metadata = Mock(spec=discovery.Metadata)
        self.metadata.inventory.side_effect = lambda: deepcopy(self.nodes)
        self.metadata.mounts.side_effect = lambda: deepcopy(self.mounts)
        self.metadata.swaps.side_effect = lambda: deepcopy(self.swaps)
        self.metadata.text.side_effect = lambda name: self.timeout if name == discovery.TIMEOUT else self.kernel
        self.metadata.filesystem.return_value = {'type': 'ext4', 'source': 'udev_cache', 'identity': 'not_checked'}
        self.mapping = {}
        self.service = {'MainPID': '123', 'ActiveState': 'active', 'ExecMainStartTimestampMonotonic': '21000000'}
        self.owner = {'pid': 123, 'binary_sha256': 'c' * 64, 'runtime_root': doctor.RAM}
        self.options = dict(metadata=self.metadata, reader=self.reader,
            target_reader=Mock(return_value=(1, 15, 0)),
            map_reader=Mock(side_effect=lambda name: deepcopy(self.mapping)),
            service_reader=Mock(side_effect=lambda unit: {'ActiveState': 'inactive'} if unit.startswith('multipathd.') else dict(self.service)),
            process_reader=Mock(side_effect=lambda pid: dict(self.owner)))
        self.write('/proc/sys/kernel/random/boot_id', b'current-boot\n')
        self.write(discovery.PACKAGE_MANIFEST, {'schema': 1, 'kind': 'data-runtime', 'kernel_release': self.kernel})

    def write(self, name, value):
        target = self.root / name.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for parent in target.parents:
            if parent == self.root:
                break
            parent.chmod(0o700)
        target.write_bytes(value if isinstance(value, bytes) else json.dumps(value).encode())
        target.chmod(0o600)
        return target

    def enrolled(self, *, root=True):
        name, uuid = ('ram-rescue-path', 'RAMRESCUE-HOST-test') if root else ('rr-data-test', 'RAMRESCUE-DATA-test')
        config = {'map_name': name, 'map_uuid': uuid, 'partition_sectors': 16384,
                  'kernel_release': self.kernel, 'run_dir': '/run/ram-rescue-guard/state' if root else
                  '/run/ram-rescue-data/rr-data-test/state'}
        if root:
            self.write(doctor.ROOT_CONFIG, config)
            self.write(doctor.ROOT_RECEIPT, {'state': 'installed', 'build': {'native_runtime': {'binary_sha256': 'c' * 64}}})
        else:
            identity = {'kind': 'filesystem', 'vid': '1234', 'pid': '5678', 'usb_serial': 'test-serial',
                        'sectors': 32768, 'partition_number': 1, 'partuuid': 'test-part',
                        'fs_type': 'ext4', 'fs_uuid': 'test-fs'}
            config.update(schema=1, profile='host-data', queue_seconds=8, partition_start=2048,
                          logical_block_size=512, identity_path='/run/ram-rescue-data/rr-data-test/identity.json',
                          layout={key: identity[key] for key in ('kind', 'fs_type', 'fs_uuid', 'partuuid')})
            self.write(doctor.REGISTRY + '/' + name + '.json', {'schema': 1, 'identity': identity, 'guard': config})
        self.nodes['dm-0'] = node('dm-0', '253:0', slaves=['sdb1'])
        self.nodes['dm-0'].update(dm_name=name, dm_uuid=uuid)
        self.mapping = {'uuid': uuid, 'info': dict(suspended=0, internal_suspend=0, deferred_remove=0,
                         read_only=0, live_table=1), 'active': expected_table(16384, '8:17'), 'inactive': []}
        self.transaction = {'schema': 1, 'boot_id': 'current-boot', 'map_name': name, 'map_uuid': uuid,
                            'owner_epoch': 'epoch', 'owner_pid': 123, 'phase': 'idle', 'updated_at': 22}
        self.write(config['run_dir'] + '/path-transaction.json', self.transaction)
        self.write(config['run_dir'] + '/path-state.json', {'owner_epoch': 'epoch', 'state': 'ready', 'time': 23})
        self.config = config
        return config

    def report(self):
        return discovery.collect(**self.options)

    def change_before_final_reads(self, action):
        calls = 0
        def inventory():
            nonlocal calls
            calls += 1
            if calls == 2:
                action()
            return deepcopy(self.nodes)
        self.metadata.inventory.side_effect = inventory

    def assert_configuration_revoked(self, report, scope):
        self.assertFalse(report['snapshot_stable'])
        self.assertFalse(report['configuration_stable'][scope])
        self.assertIn('trusted_configuration_changed_or_unavailable', [row['code'] for row in report['issues']])
        for volume in report['volumes']:
            self.assertEqual(volume['assessment'], 'unknown')
            if scope == 'relationships':
                for check in volume['checks']:
                    if check['source'].startswith('registration_'):
                        self.assertEqual(check['result'], 'unknown')
        for group in report['groups']:
            self.assertEqual(group['evidence']['restart'], 'unknown')
            self.assertEqual(group['evidence']['current_runtime'], 'unknown')
            self.assertEqual(group['evidence']['topology'], 'unknown')
            if group['observation']:
                self.assertEqual(group['observation']['state'], 'unknown')
                self.assertFalse(group['observation']['owner_matches'])
                self.assertIsNone(group['observation']['mapping']['identity_matches'])

    def manager_candidate(self):
        program = '/usr/local/lib/ram-rescue-manager/' + 'a' * 64 + '/guard/manage.py'
        manifest = str(Path(program).parent.parent / 'runtime/manifest.json')
        self.write(program, b'# selected manager\n')
        self.write(manifest, {'schema': 1, 'kind': 'data-runtime', 'binary_sha256': 'a' * 64,
                             'archive_sha256': 'b' * 64, 'base_sha256': 'c' * 64,
                             'native_runtime': {'binary_sha256': 'a' * 64}})
        self.write(doctor.MANAGER_RECEIPT, {'state': 'installed', 'program': program})
        return program, manifest

    def checks(self, report=None):
        return {row['code']: row['result'] for row in (report or self.report())['volumes'][0]['checks']}

    def test_unmounted_volume_is_not_an_enable_decision(self):
        report = self.report()
        self.assertEqual(len(report['volumes']), 1)
        self.assertEqual(report['volumes'][0]['assessment'], 'needs_checks')
        self.assertEqual(self.checks(report)['media_identity'], 'not_checked')
        self.assertEqual(report['environment']['public_support_policy'], 'target_decided_combination_unvalidated')
        self.assertEqual(report['scope']['mutations'], 'none')
        self.options['map_reader'].assert_not_called()

    def test_cached_unsupported_filesystem_is_labelled_as_cache_evidence(self):
        self.metadata.filesystem.return_value = {'type': 'xfs', 'source': 'udev_cache', 'identity': 'not_checked'}
        self.assertEqual(self.checks()['cached_filesystem_unsupported'], 'fail')
        self.metadata.filesystem.return_value = {'type': 'unknown'}
        self.assertNotIn('cached_filesystem_unsupported', self.checks())

    def test_raw_mount_blocks_even_when_registered(self):
        for enrolled in (False, True):
            with self.subTest(enrolled=enrolled):
                if enrolled:
                    self.enrolled(root=False)
                self.mounts = [{'dev': '8:17', 'where': '/media/data', 'fstype': 'ext4', 'options': 'rw'}]
                self.assertEqual(self.checks()['raw_partition_mounted'], 'fail')

    def test_root_shared_lvs_are_one_group_and_efi_is_excluded(self):
        self.enrolled()
        for number, label, mount in ((1, 'vg-root', '/'), (2, 'vg-shared', '/workspace')):
            name = 'dm-' + str(number)
            self.nodes[name] = node(name, '253:' + str(number), slaves=['dm-0'])
            self.nodes[name]['dm_name'] = label
            self.mounts.append({'dev': self.nodes[name]['dev'], 'where': mount})
        self.nodes['sdb2'] = node('sdb2', '8:18', usb=True, partition=2, parent='sdb')
        self.mounts.append({'dev': '8:18', 'where': '/boot/efi'})
        report = self.report()
        root, efi = report['volumes']
        self.assertEqual(root['role'], 'system')
        self.assertEqual(root['mounts'], ['/', '/workspace'])
        self.assertIn('vg-shared', root['members'])
        self.assertEqual(efi['groups'], [])
        self.assertEqual(self.checks({'volumes': [efi]})['system_disk_sibling'], 'fail')
        evidence = report['groups'][0]['evidence']
        self.assertEqual(evidence['topology'], 'pass')
        self.assertEqual(evidence['current_runtime'], 'ready')
        self.assertEqual(evidence['recovery_experiment'], 'not_evaluated')
        self.assertEqual(evidence['restart'], 'binary_matches_dependencies_not_checked')

    def test_enrolled_data_reuses_current_owner(self):
        self.enrolled(root=False)
        report = self.report()
        self.assertEqual(report['volumes'][0]['assessment'], 'recognized_existing')
        self.assertEqual(report['groups'][0]['evidence']['current_runtime'], 'ready')
        self.assertEqual(self.options['map_reader'].call_count, 2)

    def test_foreign_or_ambiguous_dm_never_reaches_table_query(self):
        self.enrolled()
        for mode in ('foreign_uuid', 'duplicate'):
            with self.subTest(mode=mode):
                self.options['map_reader'].reset_mock()
                if mode == 'foreign_uuid':
                    self.nodes['dm-0']['dm_uuid'] = 'foreign'
                else:
                    self.nodes['dm-0']['dm_uuid'] = self.config['map_uuid']
                    self.nodes['dm-9'] = deepcopy(self.nodes['dm-0'])
                report = self.report()
                self.options['map_reader'].assert_not_called()
                self.assertEqual(report['groups'][0]['evidence']['current_runtime'], 'not_observed')

    def test_stale_boot_and_owner_pid_are_unknown(self):
        self.enrolled()
        for changes in ({'boot_id': 'old-boot'}, {'owner_pid': 456}, {'updated_at': 1}):
            with self.subTest(changes=changes):
                self.write(self.config['run_dir'] + '/path-transaction.json', dict(self.transaction, **changes))
                evidence = self.report()['groups'][0]['evidence']
                self.assertEqual(evidence['current_runtime'], 'unknown')
                self.assertFalse(evidence['current_boot_owner'])
                self.assertEqual(evidence['restart'], 'unknown')

    def test_candidate_differs_from_running_binary_requires_cold_start(self):
        self.enrolled()
        self.write(doctor.ROOT_RECEIPT, {'state': 'installed', 'build': {'native_runtime': {'binary_sha256': 'a' * 64}}})
        evidence = self.report()['groups'][0]['evidence']
        self.assertEqual(evidence['restart'], 'required')
        self.assertEqual(evidence['preparation']['integrity'], 'not_checked')
        self.assertEqual(evidence['current_runtime'], 'ready')

    def test_prepared_but_missing_map_does_not_promise_reboot_will_fix(self):
        self.enrolled()
        del self.nodes['dm-0']
        report = self.report()
        evidence = report['groups'][0]['evidence']
        self.assertEqual(evidence['preparation']['state'], 'installed')
        self.assertEqual(evidence['restart'], 'unknown')
        self.assertEqual(evidence['topology'], 'not_checked')
        self.assertIn('重启未必解决', discovery.format_text(report))

    def test_topology_is_not_a_filesystem_integrity_claim(self):
        self.enrolled()
        for field, value in (('suspended', 1), ('read_only', 1), ('live_table', 0)):
            with self.subTest(field=field):
                self.mapping['info'][field] = value
                self.assertEqual(self.report()['groups'][0]['evidence']['topology'], 'fail')
                self.mapping['info'][field] = 0 if field != 'live_table' else 1
        self.mapping['active'] = expected_table(16384, '8:99')
        self.assertEqual(self.report()['groups'][0]['evidence']['topology'], 'fail')

    def test_queue_disabled_fails_even_with_otherwise_matching_table(self):
        self.enrolled()
        self.mapping['active'][0][3] = self.mapping['active'][0][3].replace('3 queue_if_no_path', '2')
        self.assertEqual(self.report()['groups'][0]['evidence']['topology'], 'fail')

    def test_missing_diskseq_does_not_certify_topology(self):
        self.enrolled()
        self.nodes['sdb1']['diskseq'] = None
        self.assertEqual(self.report()['groups'][0]['evidence']['topology'], 'unknown')

    def test_data_upper_layers_are_not_added_to_support_scope(self):
        self.enrolled(root=False)
        self.nodes['dm-1'] = node('dm-1', '253:1', slaves=['dm-0'])
        self.assertEqual(self.checks()['data_upper_layers'], 'fail')

    def test_unmanaged_upper_layers_and_whole_disk_are_rejected(self):
        self.nodes['dm-0'] = node('dm-0', '253:0', slaves=['sdb1'])
        self.assertEqual(self.checks()['unmanaged_upper_layers'], 'fail')
        self.nodes = {'sdb': self.nodes['sdb']}
        self.nodes['sdb']['serial'] = None
        checks = self.checks()
        self.assertEqual(checks['partition_required'], 'fail')
        self.assertEqual(checks['usb_serial_missing'], 'fail')

    def test_swap_partition_file_alias_and_missing_observation(self):
        self.mounts = [{'dev': '8:17', 'where': '/data'}]
        for swap, expected in (({'path': '/dev/sdb1', 'kind': 'partition'}, 'fail'),
                               ({'path': '/data/swapfile', 'kind': 'file'}, 'fail'),
                               ({'path': '/dev/disk/by-uuid/other', 'kind': 'partition'}, 'unknown')):
            with self.subTest(swap=swap):
                self.swaps = [swap]
                self.assertEqual(self.checks()['swap_in_use'], expected)
        self.metadata.swaps.side_effect = PermissionError
        self.assertEqual(self.checks()['swap_in_use'], 'unknown')

    def test_environment_capabilities_are_separate_from_support_policy(self):
        for timeout, expected in (('0', 'fail'), ('9', 'fail'), ('10', 'pass'), ('invalid', 'unknown')):
            with self.subTest(timeout=timeout):
                self.timeout = timeout
                self.assertEqual(self.checks()['queue_timeout'], expected)
        self.options['target_reader'].return_value = (1, 14, 0)
        self.assertEqual(self.checks()['multipath_interface'], 'fail')
        self.options['target_reader'].side_effect = PermissionError
        self.assertEqual(self.checks()['multipath_interface'], 'unknown')
        self.kernel = '7.0.0-new'
        self.assertEqual(self.checks()['package_kernel'], 'fail')
        self.assertEqual(self.checks()['manual_multipathd_processes'], 'not_checked')

    def test_competing_service_blocks_and_unreadable_service_is_unknown(self):
        for state, expected in (('active', 'fail'), ('activating', 'fail'), ('missing', 'unknown')):
            with self.subTest(state=state):
                self.options['service_reader'].side_effect = lambda unit: {'ActiveState': state}
                self.assertEqual(self.checks()['multipathd_units'], expected)

    def test_changed_inventory_mount_swap_or_table_revokes_current_evidence(self):
        self.enrolled()
        for source in ('inventory', 'mounts', 'swaps', 'table'):
            with self.subTest(source=source):
                reader = self.options['map_reader'] if source == 'table' else getattr(self.metadata, source)
                original = reader.side_effect
                before = deepcopy(self.mapping if source == 'table' else
                                  self.nodes if source == 'inventory' else getattr(self, source))
                after = deepcopy(before)
                if source == 'inventory':
                    after['sdb1']['diskseq'] = '99'
                elif source == 'table':
                    after['info']['suspended'] = 1
                else:
                    after.append({'changed': True})
                reader.side_effect = [before, after]
                report = self.report()
                self.assertFalse(report['snapshot_stable'])
                self.assertEqual(report['groups'][0]['evidence']['current_runtime'], 'unknown')
                self.assertEqual(report['groups'][0]['observation']['state'], 'unknown')
                self.assertFalse(report['groups'][0]['observation']['owner_matches'])
                self.assertEqual(report['volumes'][0]['assessment'], 'unknown')
                reader.side_effect = original

    def test_missing_inventory_or_mountinfo_is_not_an_empty_success(self):
        for source in ('inventory', 'mounts'):
            with self.subTest(source=source):
                reader = getattr(self.metadata, source)
                original = reader.side_effect
                reader.side_effect = FileNotFoundError
                report = self.report()
                self.assertFalse(report['snapshot_stable'])
                self.assertTrue(report['issues'])
                reader.side_effect = original

    def test_invalid_registration_never_queries_dm(self):
        self.write(doctor.REGISTRY + '/rr-data-bad.json', {'schema': 1})
        report = self.report()
        self.assertTrue(report['issues'])
        self.options['map_reader'].assert_not_called()

    def test_disappearing_registration_revokes_relationship_and_restart(self):
        self.enrolled(root=False)
        path = self.root / doctor.REGISTRY.lstrip('/') / 'rr-data-test.json'
        self.change_before_final_reads(path.unlink)
        self.assert_configuration_revoked(self.report(), 'relationships')

    def test_registration_identity_change_with_same_filename_is_detected(self):
        self.enrolled(root=False)
        path = doctor.REGISTRY + '/rr-data-test.json'
        record = self.reader.json(path)
        record['identity']['fs_uuid'] = 'different-medium'
        record['guard']['layout']['fs_uuid'] = 'different-medium'
        self.change_before_final_reads(lambda: self.write(path, record))
        self.assert_configuration_revoked(self.report(), 'relationships')

    def test_registration_list_change_is_detected_without_reading_new_members(self):
        self.enrolled(root=False)
        path = doctor.REGISTRY + '/rr-data-new.json'
        self.change_before_final_reads(lambda: self.write(path, {'schema': 1}))
        with patch.object(self.reader, 'json', wraps=self.reader.json) as reads:
            self.assert_configuration_revoked(self.report(), 'relationships')
        self.assertNotIn(path, [call.args[0] for call in reads.call_args_list])

    def test_root_configuration_disappearance_and_content_change_are_detected(self):
        for disappear in (True, False):
            with self.subTest(disappear=disappear):
                self.enrolled()
                path = self.root / doctor.ROOT_CONFIG.lstrip('/')
                action = path.unlink if disappear else lambda: self.write(
                    doctor.ROOT_CONFIG, dict(self.config, partition_sectors=32768))
                self.change_before_final_reads(action)
                self.assert_configuration_revoked(self.report(), 'relationships')

    def test_previously_absent_root_configuration_appearing_is_detected(self):
        self.change_before_final_reads(lambda: self.write(doctor.ROOT_CONFIG, {
            'map_name': 'ram-rescue-path', 'map_uuid': 'RAMRESCUE-HOST-new',
            'run_dir': '/run/ram-rescue-guard/state'}))
        self.assert_configuration_revoked(self.report(), 'relationships')

    def test_registration_becoming_unreadable_revokes_relationships(self):
        self.enrolled(root=False)
        path = self.root / doctor.REGISTRY.lstrip('/') / 'rr-data-test.json'
        self.change_before_final_reads(lambda: path.chmod(0o666))
        self.assert_configuration_revoked(self.report(), 'relationships')

    def test_candidate_receipt_change_revokes_top_level_and_nested_preparation(self):
        for role, receipt in (('root', doctor.ROOT_RECEIPT), ('manager', doctor.MANAGER_RECEIPT)):
            with self.subTest(role=role):
                self.enrolled(root=role == 'root')
                if role == 'manager':
                    (self.root / doctor.ROOT_CONFIG.lstrip('/')).unlink(missing_ok=True)
                    self.manager_candidate()
                self.change_before_final_reads(lambda receipt=receipt: self.write(receipt, {'state': 'upgrading'}))
                report = self.report()
                self.assert_configuration_revoked(report, role)
                self.assertEqual(report['preparation'][role]['state'], 'unknown')
                self.assertEqual(report['groups'][0]['evidence']['preparation']['state'], 'unknown')

    def test_candidate_receipt_disappearance_or_unreadability_is_not_absence(self):
        for disappear in (True, False):
            with self.subTest(disappear=disappear):
                self.enrolled()
                path = self.root / doctor.ROOT_RECEIPT.lstrip('/')
                self.change_before_final_reads(path.unlink if disappear else lambda: path.chmod(0o666))
                report = self.report()
                self.assert_configuration_revoked(report, 'root')
                self.assertEqual(report['preparation']['root']['state'], 'unknown')

    def test_manager_candidate_sources_change_without_receipt_change(self):
        self.enrolled(root=False)
        for source in ('program', 'manifest', 'missing_manifest'):
            with self.subTest(source=source):
                program, manifest = self.manager_candidate()
                if source == 'program':
                    action = lambda: self.write(program, b'# replaced manager\n')
                elif source == 'manifest':
                    updated = self.reader.json(manifest)
                    updated['native_runtime']['library_sha256'] = {'/usr/lib/libtest.so': 'f' * 64}
                    action = lambda: self.write(manifest, updated)
                else:
                    action = (self.root / manifest.lstrip('/')).unlink
                self.change_before_final_reads(action)
                report = self.report()
                self.assert_configuration_revoked(report, 'manager')
                self.assertEqual(report['preparation']['manager']['state'], 'unknown')

    def test_missing_required_candidate_source_cannot_certify_preparation(self):
        self.enrolled(root=False)
        _, manifest = self.manager_candidate()
        (self.root / manifest.lstrip('/')).unlink()
        report = self.report()
        self.assert_configuration_revoked(report, 'manager')
        self.assertEqual(report['preparation']['manager']['state'], 'unknown')

    def test_package_manifest_change_revokes_kernel_comparison(self):
        self.enrolled()
        self.change_before_final_reads(lambda: self.write(discovery.PACKAGE_MANIFEST, {
            'schema': 1, 'kind': 'data-runtime', 'kernel_release': '7.0.0-next'}))
        report = self.report()
        self.assert_configuration_revoked(report, 'package')
        self.assertIsNone(report['environment']['package_kernel_release'])
        self.assertEqual(self.checks(report)['package_kernel'], 'unknown')

    def test_unchanged_sources_are_bounded_and_keep_candidate_evidence(self):
        self.enrolled(root=False)
        program, manifest = self.manager_candidate()
        with patch.object(self.reader, 'json', wraps=self.reader.json) as reads, \
                patch.object(self.reader, 'hash', wraps=self.reader.hash) as hashes, \
                patch.object(self.reader, 'names', wraps=self.reader.names) as names:
            report = self.report()
        self.assertTrue(report['snapshot_stable'])
        self.assertTrue(all(report['configuration_stable'].values()))
        self.assertEqual(report['groups'][0]['evidence']['restart'], 'required')
        self.assertEqual(report['preparation']['manager']['state'], 'installed')
        for path in (doctor.ROOT_CONFIG, doctor.MANAGER_RECEIPT, doctor.ROOT_RECEIPT,
                     discovery.PACKAGE_MANIFEST, doctor.REGISTRY + '/rr-data-test.json', manifest):
            self.assertEqual(sum(call.args[0] == path for call in reads.call_args_list), 2, path)
        self.assertEqual([call.args[0] for call in hashes.call_args_list], [program, program])
        self.assertEqual(names.call_count, 2)

    def test_configuration_snapshot_rejects_unbounded_sources(self):
        snapshot = discovery.ConfigurationSnapshot(doctor.Collector(None, None, None, None, None).read)
        for _ in range(doctor.MAX_DEVICES + 6):
            snapshot.observe('relationships', 'fixture', lambda: {})
        with self.assertRaises(doctor.InputError):
            snapshot.observe('relationships', 'fixture', lambda: {})

    def test_display_limit_does_not_limit_system_disk_safety_classification(self):
        self.nodes = {'sdb': self.nodes['sdb']}
        for number in range(1, 67):
            name = 'sdb' + str(number)
            self.nodes[name] = node(name, '8:' + str(number), usb=True, partition=number, parent='sdb')
        self.mounts = [{'dev': '8:66', 'where': '/'}]
        report = self.report()
        self.assertEqual(len(report['volumes']), doctor.MAX_DEVICES)
        self.assertEqual(self.checks(report)['system_disk_sibling'], 'fail')
        self.assertIn('volume_count_limit', [row['code'] for row in report['issues']])

    def test_collect_never_opens_for_write_or_calls_mutations_or_media_probes(self):
        self.enrolled()
        original_open = os.open
        def read_only_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            self.assertFalse(str(path).startswith('/dev/'))
            return original_open(path, flags, *args, **kwargs)
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*'))
        with ExitStack() as stack:
            stack.enter_context(patch('os.open', side_effect=read_only_open))
            for method in ('register', 'prepare', 'install', 'upgrade', 'uninstall'):
                stack.enter_context(patch.object(manage, method, side_effect=AssertionError('mutation')))
            stack.enter_context(patch.object(manage.services, 'run', side_effect=AssertionError('external tool')))
            self.report()
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob('*')))

    def test_chinese_selection_uses_one_snapshot_and_json_never_prompts(self):
        report = self.report()
        with patch.object(discovery, 'collect', return_value=report) as collect, \
                patch('sys.stdin.isatty', return_value=True), patch('builtins.input', return_value='1'), \
                redirect_stdout(io.StringIO()) as output:
            discovery.show()
        collect.assert_called_once_with()
        self.assertIn('测试盘', output.getvalue())
        with patch.object(discovery, 'collect', return_value=report), \
                patch('builtins.input', side_effect=AssertionError('JSON must not prompt')), \
                redirect_stdout(io.StringIO()) as output:
            discovery.show(json_output=True)
        self.assertEqual(json.loads(output.getvalue())['snapshot_id'], report['snapshot_id'])

    def test_manager_dispatches_discovery_without_control_lock(self):
        with patch.object(sys, 'argv', ['manage.py', 'discover', '--json']), \
                patch.object(discovery, 'show') as show, \
                patch.object(manage.services, 'require_root', side_effect=AssertionError('write path')):
            manage.main()
        show.assert_called_once_with(json_output=True)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.metadata = discovery.Metadata(self.root)

    def write(self, name, content):
        path = self.root / name.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def test_sysfs_partition_and_cached_filesystem_without_block_access(self):
        disk = '/sys/devices/usb1/1-1/host0/block/sdb'
        for name, value in {'/sys/devices/usb1/1-1/idVendor': '1234',
                            '/sys/devices/usb1/1-1/serial': 'test', disk + '/dev': '8:16',
                            disk + '/size': '32768', disk + '/diskseq': '19',
                            disk + '/device/model': '测试 SSD', disk + '/sdb1/partition': '1',
                            disk + '/sdb1/dev': '8:17', disk + '/sdb1/size': '16384'}.items():
            self.write(name, value)
        (self.root / disk.lstrip('/') / 'slaves').mkdir()
        classes = self.root / 'sys/class/block'
        classes.mkdir(parents=True)
        (classes / 'sdb').symlink_to(self.root / disk.lstrip('/'))
        (classes / 'sdb1').symlink_to(self.root / (disk + '/sdb1').lstrip('/'))
        self.write('/run/udev/data/b8:17', 'E:ID_FS_TYPE=ext4\nE:ID_FS_UUID=private\n')
        nodes = self.metadata.inventory()
        self.assertEqual(nodes['sdb1']['diskseq'], '19')
        self.assertTrue(nodes['sdb1']['usb'])
        self.assertEqual(nodes['sdb1']['parent'], 'sdb')
        self.assertEqual(self.metadata.filesystem('8:17'), {'type': 'ext4', 'source': 'udev_cache', 'identity': 'not_checked'})

    def test_bounds_symlink_escape_and_nonregular_inputs(self):
        path = self.write('/sys/big', 'too big')
        with self.assertRaises(ValueError):
            self.metadata.text('/sys/big', 2)
        self.write('/elsewhere', 'x')
        (self.root / 'sys/link').symlink_to(self.root / 'elsewhere')
        with self.assertRaises(ValueError):
            self.metadata.text('/sys/link')
        os.mkfifo(self.root / 'sys/fifo')
        with self.assertRaises(ValueError):
            self.metadata.text('/sys/fifo')
        with self.assertRaises(ValueError):
            self.metadata.names('/sys', limit=1)
        path.unlink()

    def test_mount_and_swap_escaping(self):
        self.write('/proc/1/mountinfo', '1 0 8:17 / /media/test\\040disk rw - ext4 /dev/sdb1 rw\n')
        self.write('/proc/swaps', 'Filename Type Size Used Priority\n/media/test\\040disk/swap file 100 0 -2\n')
        self.assertEqual(self.metadata.mounts()[0]['where'], '/media/test disk')
        self.assertEqual(self.metadata.swaps()[0]['path'], '/media/test disk/swap')

    def test_control_characters_and_bidi_are_not_rendered(self):
        self.assertEqual(discovery.safe_text('中文\x1b[2J\u202e'), '中文\\u001b[2J\\u202e')

    def test_dependency_graph_cycles_missing_nodes_and_shared_dag(self):
        nodes = {'a': node('a', '1:1')}
        for number in range(30):
            nodes[str(number)] = node(str(number), '1:2', slaves=list(nodes)[-2:])
        discovery.check_graph(nodes)
        nodes['a']['slaves'] = ['29']
        with self.assertRaises(ValueError):
            discovery.check_graph(nodes)
        with self.assertRaises(ValueError):
            discovery.check_graph({'a': node('a', '1:1', slaves=['missing'])})


class UUIDQueryTests(unittest.TestCase):
    def mapper(self, *, actual_name=b'enrolled'):
        mapper = DeviceMapper.__new__(DeviceMapper)
        mapper.lib = Mock()
        mapper.lib.dm_task_create.return_value = 1
        mapper.lib.dm_task_get_name.return_value = actual_name
        mapper.lib.dm_task_get_uuid.return_value = b'UUID'
        mapper.lib.dm_get_next_target.return_value = None
        def info(task, pointer):
            pointer._obj.exists = 1
            return 1
        mapper.lib.dm_task_get_info.side_effect = info
        return mapper

    def test_uuid_selector_cannot_fall_back_to_reused_name(self):
        mapper = self.mapper()
        snapshot = mapper.snapshot_by_uuid('enrolled', 'UUID')
        self.assertEqual(snapshot['uuid'], 'UUID')
        self.assertEqual(mapper.lib.dm_task_set_uuid.call_count, 2)
        mapper.lib.dm_task_set_name.assert_not_called()
        self.assertEqual(mapper.lib.dm_task_destroy.call_count, 2)
        self.assertEqual([call.args[0] for call in mapper.lib.dm_task_create.call_args_list], [11, 11])

    def test_uuid_rename_race_rejects_before_consuming_table(self):
        mapper = self.mapper(actual_name=b'foreign')
        with self.assertRaises(RuntimeError):
            mapper.snapshot_by_uuid('enrolled', 'UUID')
        mapper.lib.dm_get_next_target.assert_not_called()
        mapper.lib.dm_task_destroy.assert_called_once()

    def test_uuid_change_rejects_before_consuming_table(self):
        mapper = self.mapper()
        mapper.lib.dm_task_get_uuid.return_value = b'foreign'
        with self.assertRaises(RuntimeError):
            mapper.snapshot_by_uuid('enrolled', 'UUID')
        mapper.lib.dm_get_next_target.assert_not_called()

    def test_existing_name_queries_keep_original_behavior(self):
        mapper = self.mapper()
        mapper.snapshot('enrolled')
        self.assertEqual(mapper.lib.dm_task_set_name.call_count, 2)
        mapper.lib.dm_task_set_uuid.assert_not_called()


if __name__ == '__main__':
    unittest.main()
