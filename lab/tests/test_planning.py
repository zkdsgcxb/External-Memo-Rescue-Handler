"""Plans reuse discovery/admission, bind effects and inputs, and never persist."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import test_discovery as fixtures
import discovery
import diagnostics as doctor
import manage
import planning
import support
import support_fixtures
from admin.admission import digest


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DiscoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.enrolled(root=False)
        self.profile = f.reader.json(doctor.REGISTRY + '/rr-data-test.json')
        (f.root / doctor.REGISTRY.lstrip('/') / 'rr-data-test.json').unlink()
        self.profile['guard'].update(initial_node='/dev/sdb1', initial_sys_path=f.nodes['sdb1']['path'], initial_diskseq=12)
        f.mapping['info'].update(major=253, minor=0)
        f.mounts = [{'dev': '253:0', 'where': '/media/测试盘', 'fstype': 'ext4', 'options': 'rw'}]
        f.manager_candidate()
        self.item = {'name': 'rr-data-test', 'uuid': 'RAMRESCUE-DATA-test', 'sys': Path('/sys/class/block/dm-0')}
        self.raw = {'/proc/1/mountinfo': '1 0 253:0 / /media/data rw - ext4 /dev/dm-0 rw',
                    '/proc/swaps': 'Filename Type Size Used Priority', '/proc/1/root/etc/fstab': '# no raw selection',
                    '/sys/class/block/sdb1/start': '2048', '/sys/class/block/sdb1/partition': '1',
                    '/sys/class/block/sdb/queue/logical_block_size': '512'}
        self.raw[planning.ADMINISTRATION_MANIFEST] = '{"schema": 1, "files": {"guard/manage.py": "fixture"}}'
        self.holders = {'/sys/class/block/sdb1/holders': ['dm-0'], '/sys/class/block/dm-0/holders': []}
        self.metadata = Mock(spec=discovery.Metadata)
        self.metadata.text.side_effect = lambda name, limit: self.raw[name]
        self.metadata.names.side_effect = lambda name: list(self.holders[name])
        self.current_support = support_fixtures.current()
        f.write(planning.ADMINISTRATION_MANIFEST, self.raw[planning.ADMINISTRATION_MANIFEST].encode())
        self.current_support['subject']['administration']['manifest_sha256'] = support_fixtures.sha(
            self.raw[planning.ADMINISTRATION_MANIFEST])
        self.options = {
            'current_support': lambda: deepcopy(self.current_support), 'support_reader': f.reader,
            'discover': Mock(side_effect=lambda inputs: discovery.collect(**f.options, inputs=inputs)),
            'resolver': Mock(side_effect=lambda device: (deepcopy(self.item), None)),
            'metadata': self.metadata,
            'attest': Mock(side_effect=lambda name, node: deepcopy(self.profile)),
            'map_reader': Mock(side_effect=lambda name, uuid: deepcopy(f.mapping)),
            'environment_check': Mock(return_value=None),
        }

    def enable_qualification(self):
        self.qualification_path = support_fixtures.write(self.fixture.root, self.current_support)
        return self.qualification_path

    def plan(self):
        return planning.build('/dev/mapper/rr-data-test', **self.options)

    def blockers(self, report):
        return report['confirmation']['blockers']

    def test_successful_admission_still_blocked_by_unvalidated_combination(self):
        report = self.plan()
        self.assertEqual(self.blockers(report), ['combination_unvalidated'])
        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['io']['media'], 'admission_passed')
        self.assertEqual(report['confirmation']['support_policy']['target_state'], 'decided')
        self.assertEqual(report['confirmation']['support_policy']['product_target']['kernel_route'], 'official_hwe')
        self.options['attest'].assert_called_once_with('rr-data-test', '/dev/sdb1')
        self.options['environment_check'].assert_called_once_with(self.profile['guard'])
        self.assertEqual(self.options['discover'].call_count, 2)

    def test_digest_ignores_new_diagnostic_tokens_and_capture_times(self):
        with patch('discovery.time.time', return_value=100):
            first = self.plan()
        with patch('discovery.time.time', return_value=900):
            second = self.plan()
        self.assertNotEqual(first['observation']['snapshot_id'], second['observation']['snapshot_id'])
        self.assertNotEqual(first['observation']['captured_at_unix'], second['observation']['captured_at_unix'])
        self.assertEqual(first['plan_digest'], second['plan_digest'])
        self.assertNotIn('id-', json.dumps(first['confirmation']))
        self.assertNotIn('captured_at_unix', first['confirmation'])

    def test_confirmed_effects_record_and_instance_are_inside_digest(self):
        report = self.plan()
        confirmed = report['confirmation']
        self.assertEqual(report['plan_digest'], digest(confirmed))
        self.assertEqual(confirmed['effects']['activation'], 'none')
        self.assertFalse(confirmed['effects']['reboot_activates'])
        self.assertEqual(confirmed['effects']['candidate_record']['identity']['fs_uuid'], 'test-fs')
        self.assertEqual(confirmed['inputs']['instance']['initial_diskseq'], 12)
        self.assertNotIn('initial_diskseq', confirmed['effects']['candidate_record']['guard'])
        changed = deepcopy(confirmed)
        changed['effects']['activation'] = 'start_service'
        self.assertNotEqual(digest(changed), report['plan_digest'])
        changed = deepcopy(confirmed)
        changed['effects']['destination_class'] = '/etc/ram-rescue-manager/devices'
        self.assertNotEqual(digest(changed), report['plan_digest'])

    def test_different_attested_identity_changes_digest(self):
        before = self.plan()['plan_digest']
        self.profile['identity']['fs_uuid'] = 'different-filesystem'
        self.profile['guard']['layout']['fs_uuid'] = 'different-filesystem'
        self.assertNotEqual(self.plan()['plan_digest'], before)

    def test_device_mount_registration_receipt_and_package_inputs_change_digest(self):
        before = self.plan()['plan_digest']
        f = self.fixture
        changes = [lambda: f.nodes['sdb1'].update(diskseq='13'),
                   lambda: f.mounts[0].update(options='ro'),
                   lambda: f.write(doctor.REGISTRY + '/invalid-name.json', {'changed': True}),
                   lambda: f.write(doctor.ROOT_RECEIPT, {'state': 'installed'}),
                   lambda: f.write(discovery.PACKAGE_MANIFEST, {'schema': 1, 'kind': 'data-runtime',
                                                                'kernel_release': '7.0.0-next'})]
        for change in changes:
            with self.subTest(change=change):
                change()
                report = self.plan()
                self.assertNotEqual(report['plan_digest'], before)
                before = report['plan_digest']

    def test_context_fstab_swap_bind_root_and_holders_changes_change_digest(self):
        before = self.plan()['plan_digest']
        for path in ('/proc/1/root/etc/fstab', '/proc/swaps', '/proc/1/mountinfo',
                     '/sys/class/block/sdb1/start', '/sys/class/block/sdb/queue/logical_block_size',
                     planning.ADMINISTRATION_MANIFEST):
            with self.subTest(path=path):
                self.raw[path] += ' changed'
                if path == planning.ADMINISTRATION_MANIFEST:
                    self.fixture.write(path, self.raw[path].encode())
                after = self.plan()['plan_digest']
                self.assertNotEqual(before, after)
                before = after
        self.holders['/sys/class/block/dm-0/holders'] = ['dm-9']
        self.assertNotEqual(before, self.plan()['plan_digest'])

    def test_inputs_changed_during_media_check_make_old_plan_invalid(self):
        def attest(name, node):
            self.fixture.mounts[0]['where'] = '/another-mount'
            return deepcopy(self.profile)
        self.options['attest'].side_effect = attest
        report = self.plan()
        self.assertIn('inputs_changed', self.blockers(report))
        self.assertEqual(report['status'], 'blocked')

    def test_fstab_or_holder_changes_during_admission_are_detected(self):
        for kind in ('fstab', 'holders'):
            with self.subTest(kind=kind):
                def attest(name, node):
                    if kind == 'fstab':
                        self.raw['/proc/1/root/etc/fstab'] += '\nUUID=test-fs /raw ext4 defaults 0 2'
                    else:
                        self.holders['/sys/class/block/sdb1/holders'].append('dm-9')
                    return deepcopy(self.profile)
                self.options['attest'].side_effect = attest
                self.assertIn('inputs_changed', self.blockers(self.plan()))

    def test_same_receipt_candidate_manifest_change_invalidates_plan(self):
        _, manifest = self.fixture.manager_candidate()
        def attest(name, node):
            value = self.fixture.reader.json(manifest)
            value['native_runtime']['library_sha256'] = {'library': 'f' * 64}
            self.fixture.write(manifest, value)
            return deepcopy(self.profile)
        self.options['attest'].side_effect = attest
        self.assertIn('inputs_changed', self.blockers(self.plan()))

    def test_selected_dm_table_change_is_detected(self):
        def attest(name, node):
            self.fixture.mapping['active'][0][3] = self.fixture.mapping['active'][0][3].replace('8:17', '8:33')
            return deepcopy(self.profile)
        self.options['attest'].side_effect = attest
        self.assertIn('map_changed', self.blockers(self.plan()))
        self.assertEqual(self.options['map_reader'].call_count, 2)

    def test_incidental_dm_open_count_does_not_change_digest(self):
        before = self.plan()['plan_digest']
        self.fixture.mapping['info'].update(open_count=50, event_nr=23)
        self.assertEqual(self.plan()['plan_digest'], before)

    def test_wrong_admission_instance_does_not_become_persistent_candidate(self):
        for key, value in (('initial_diskseq', 13), ('initial_node', '/dev/sdc1'),
                           ('map_uuid', 'RAMRESCUE-DATA-other'), ('kernel_release', '7.0.0-next')):
            with self.subTest(key=key):
                original = self.profile['guard'][key]
                self.profile['guard'][key] = value
                report = self.plan()
                self.assertIn('admission_instance_changed', self.blockers(report))
                self.assertIsNone(report['confirmation']['effects']['candidate_record'])
                self.profile['guard'][key] = original

    def test_admission_exception_or_timeout_is_a_blocker_not_a_retry(self):
        for error in (RuntimeError('identity mismatch'), subprocess.TimeoutExpired(['blkid'], 3)):
            with self.subTest(error=error):
                self.options['attest'].reset_mock()
                self.options['attest'].side_effect = error
                report = self.plan()
                self.assertIn('admission_failed', self.blockers(report))
                self.assertIsNone(report['confirmation']['effects']['candidate_record'])
                self.assertEqual(report['io']['media'], 'attempted')
                self.options['attest'].assert_called_once()

    def test_post_admission_competing_daemon_check_is_not_omitted(self):
        self.options['environment_check'].side_effect = RuntimeError('multipathd')
        self.assertIn('environment_changed', self.blockers(self.plan()))

    def test_unavailable_context_or_dm_control_prevents_media_probe(self):
        for failure in ('context', 'map'):
            with self.subTest(failure=failure):
                mocked = self.metadata.text if failure == 'context' else self.options['map_reader']
                original = mocked.side_effect
                mocked.side_effect = PermissionError
                report = self.plan()
                self.assertEqual(report['io']['media'], 'not_run')
                self.options['attest'].assert_not_called()
                mocked.side_effect = original

    def test_unmounted_raw_partition_without_map_gets_blocked_plan(self):
        self.options['resolver'].side_effect = RuntimeError('no supported mapping')
        report = self.plan()
        self.assertIn('object_not_resolved', self.blockers(report))
        self.options['attest'].assert_not_called()

    def test_raw_mount_is_refused_before_media_probe(self):
        self.fixture.mounts.append({'dev': '8:17', 'where': '/raw', 'fstype': 'ext4', 'options': 'rw'})
        self.assertIn('raw_partition_mounted', self.blockers(self.plan()))
        self.options['attest'].assert_not_called()

    def test_registered_data_or_root_is_not_reenrolled(self):
        for root in (False, True):
            with self.subTest(root=root):
                self.fixture.enrolled(root=root)
                self.item.update(name=self.fixture.config['map_name'], uuid=self.fixture.config['map_uuid'])
                self.assertIn('existing_registration', self.blockers(self.plan()))
                self.options['attest'].assert_not_called()

    def test_other_root_owner_does_not_block_selected_data_mapping(self):
        self.options['resolver'].side_effect = lambda device: (deepcopy(self.item), {'guard': {'map_uuid': 'RAMRESCUE-HOST-other'}})
        self.assertEqual(self.plan()['io']['media'], 'admission_passed')

    def test_unknown_package_or_untrusted_registration_prevents_probe(self):
        self.fixture.write(doctor.REGISTRY + '/rr-data-bad.json', {'schema': 1})
        report = self.plan()
        self.assertIn('discovery_incomplete', self.blockers(report))
        self.options['attest'].assert_not_called()
        (self.fixture.root / doctor.REGISTRY.lstrip('/') / 'rr-data-bad.json').unlink()
        (self.fixture.root / discovery.PACKAGE_MANIFEST.lstrip('/')).unlink()
        self.assertIn('package_kernel', self.blockers(self.plan()))
        self.options['attest'].assert_not_called()

    def test_plan_has_no_write_lock_staging_or_service_actions(self):
        f = self.fixture
        before = sorted(str(path.relative_to(f.root)) for path in f.root.rglob('*'))
        original = os.open
        def readonly_open(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC))
            return original(path, flags, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch('os.open', side_effect=readonly_open))
            for name in ('register', 'prepare', 'install', 'upgrade', 'uninstall', 'write_json', 'atomic'):
                stack.enter_context(patch.object(manage, name, side_effect=AssertionError('mutation')))
            stack.enter_context(patch.object(manage.services, 'run', side_effect=AssertionError('service action')))
            report = self.plan()
        self.assertEqual(report['current_effects']['persistent_writes'], [])
        self.assertEqual(before, sorted(str(path.relative_to(f.root)) for path in f.root.rglob('*')))

    def test_chinese_output_and_json_warn_before_probe_without_prompt(self):
        report = self.plan()
        self.assertIn('候选准备前置资格或当前产物关联证据不足', planning.format_text(report))
        self.assertIn('没有硬性总时限', planning.format_text(report))
        self.assertFalse(report['io']['hard_total_deadline'])
        self.assertEqual(report['io']['identity_command_timeout_seconds'], 3)
        self.assertEqual(report['io']['mount_resolution_timeout_seconds'], 45)
        self.assertEqual(report['io']['systemd_query_timeout_seconds'], 4)
        stderr = io.StringIO()
        def build(device):
            self.assertIn('可能读取选中分区', stderr.getvalue())
            return report
        with patch.object(planning, 'build', side_effect=build), redirect_stderr(stderr), \
                redirect_stdout(io.StringIO()) as stdout, patch('builtins.input', side_effect=AssertionError('prompt')):
            planning.show('/dev/mapper/rr-data-test', json_output=True)
        self.assertEqual(json.loads(stdout.getvalue())['plan_digest'], report['plan_digest'])

    def test_manager_dispatches_plan_without_entering_mutation_path(self):
        with patch.object(sys, 'argv', ['manage.py', 'plan', '--device', '/data', '--json']), \
                patch.object(planning, 'show') as show, \
                patch.object(manage.services, 'require_root', side_effect=AssertionError('mutation')):
            manage.main()
        show.assert_called_once_with('/data', json_output=True)

    def test_manager_dispatches_candidate_commands(self):
        import candidate_ui
        for command, extra in [('stage', ['--device', '/data']), ('cancel', ['--operation', 'a' * 32])]:
            with self.subTest(command=command), patch.object(sys, 'argv', ['manage.py', command, *extra, '--json']), \
                    patch.object(candidate_ui, 'show') as show:
                manage.main()
                self.assertEqual(show.call_args.args[0].command, command)

    def test_independent_current_evidence_and_qualification_are_required(self):
        self.enable_qualification()
        ready = self.plan()
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['confirmation']['blockers'], [])
        self.current_support['bindings']['running_kernel'] = 'unknown'
        blocked = self.plan()
        self.assertIn('combination_unvalidated', self.blockers(blocked))
        policy = blocked['confirmation']['support_policy']['combination']
        self.assertEqual(policy['qualification']['state'], 'valid')
        self.assertIn('current_running_kernel_unknown', policy['reasons'])

    def test_qualification_raw_bytes_changes_invalidate_confirmation(self):
        path = self.enable_qualification()
        before = self.plan()
        path.write_bytes(path.read_bytes() + b' ')
        after = self.plan()
        self.assertEqual(after['status'], 'ready')
        self.assertNotEqual(before['plan_digest'], after['plan_digest'])
        self.assertEqual(before['confirmation']['support_policy']['combination']['subject_sha256'],
                         after['confirmation']['support_policy']['combination']['subject_sha256'])

    def test_different_hosts_and_mounts_share_subject_but_require_new_confirmation(self):
        self.enable_qualification()
        before = self.plan()
        self.fixture.nodes['sdb']['serial'] = 'another-disk'
        self.fixture.nodes['sdb1']['diskseq'] = '88'
        self.profile['identity']['usb_serial'] = 'another-disk'
        self.profile['guard']['initial_diskseq'] = 88
        self.fixture.write('/proc/sys/kernel/random/boot_id', b'another-boot')
        self.fixture.mounts[0]['where'] = '/other-host/data'
        self.raw['/proc/1/mountinfo'] = self.raw['/proc/1/mountinfo'].replace('/media/data', '/other-host/data')
        self.raw['/proc/1/root/etc/fstab'] += '# other local fstab'
        after = self.plan()
        self.assertEqual(before['status'], 'ready')
        self.assertEqual(after['status'], 'ready')
        self.assertEqual(before['confirmation']['support_policy'], after['confirmation']['support_policy'])
        self.assertNotEqual(before['plan_digest'], after['plan_digest'])

    def test_qualification_changed_disappeared_or_unreadable_during_admission(self):
        for change in ('bytes', 'delete', 'permissions'):
            with self.subTest(change=change):
                path = self.enable_qualification()
                def attest(name, node):
                    if change == 'bytes':
                        path.write_bytes(path.read_bytes() + b' ')
                    elif change == 'delete':
                        path.unlink()
                    else:
                        path.chmod(0o666)
                    return deepcopy(self.profile)
                self.options['attest'].side_effect = attest
                report = self.plan()
                self.assertIn('support_inputs_changed', self.blockers(report))
                self.assertEqual(report['status'], 'blocked')

    def test_qualification_appearing_during_admission_does_not_authorize_old_plan(self):
        def attest(name, node):
            self.enable_qualification()
            return deepcopy(self.profile)
        self.options['attest'].side_effect = attest
        self.assertIn('support_inputs_changed', self.blockers(self.plan()))

    def test_current_binding_changed_during_admission_invalidates_plan(self):
        self.enable_qualification()
        def attest(name, node):
            self.current_support['bindings']['loaded_modules'] = 'unknown'
            return deepcopy(self.profile)
        self.options['attest'].side_effect = attest
        self.assertIn('support_inputs_changed', self.blockers(self.plan()))

    def test_architecture_alias_does_not_change_plan_or_qualification(self):
        self.enable_qualification()
        before = self.plan()
        self.current_support['subject']['platform']['architecture'] = 'x86_64'
        after = self.plan()
        self.assertEqual(before['plan_digest'], after['plan_digest'])
        self.assertEqual(after['status'], 'ready')

    def test_static_artifact_change_invalidates_old_qualification(self):
        self.enable_qualification()
        before = self.plan()
        self.current_support['subject']['runtime']['binary_sha256'] = 'f' * 64
        after = self.plan()
        self.assertIn('combination_unvalidated', self.blockers(after))
        self.assertNotEqual(before['plan_digest'], after['plan_digest'])
        self.assertEqual(after['confirmation']['support_policy']['combination']['qualification']['state'], 'absent')

    def test_current_binding_contradicting_observed_kernel_or_admin_is_blocked(self):
        for part, key in (('kernel', 'release'), ('administration', 'manifest_sha256')):
            with self.subTest(part=part):
                original = self.current_support['subject'][part][key]
                self.current_support['subject'][part][key] = '7.0.0-other' if part == 'kernel' else 'f' * 64
                self.enable_qualification()
                self.assertIn('support_context_mismatch', self.blockers(self.plan()))
                self.current_support['subject'][part][key] = original


if __name__ == '__main__':
    unittest.main()
