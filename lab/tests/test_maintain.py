"""Automatic maintenance must not reset or steal an existing Guard owner."""
from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(BASE / 'guard/runtime'), str(BASE / 'ram-rescue-demo/src')]
import data_guard
from guard_state import Owner, atomic_json, digest, load_json
import maintain
import path_guard
from registry import record_from_profile


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'lab/work')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.directory = self.base / 'rr-data-test'
        self.run = self.directory / 'state'
        self.invocation = 'a' * 32
        identity = {'kind': 'filesystem', 'vid': '1234', 'pid': '5678',
                    'usb_serial': 'test-serial', 'sectors': 32768,
                    'partition_number': 1, 'partuuid': 'test-partition',
                    'fs_type': 'ext4', 'fs_uuid': 'test-filesystem'}
        self.profile = {'schema': 1, 'identity': identity, 'guard': {
            'schema': 1, 'profile': 'host-data', 'map_name': 'rr-data-test',
            'map_uuid': 'RAMRESCUE-DATA-test', 'kernel_release': os.uname().release,
            'run_dir': str(self.run), 'identity_path': str(self.directory / 'identity.json'),
            'queue_seconds': 8, 'partition_sectors': 16384, 'partition_start': 2048,
            'logical_block_size': 512,
            'layout': {key: identity[key] for key in ('kind', 'fs_type', 'fs_uuid', 'partuuid')},
            'initial_node': '/dev/sdz1', 'initial_sys_path': '/sys/devices/new/sdz/sdz1',
            'initial_diskseq': 99,
        }}
        policy = patch.object(data_guard, 'DATA_RUN', self.base)
        policy.start()
        self.addCleanup(policy.stop)
        self.addCleanup(path_guard.configure, {})
        self.record = record_from_profile(self.profile)

    def receipt(self, *, invocation=None, owner_epoch='previous-owner', journal=True):
        self.run.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / 'config.json', self.profile['guard'])
        atomic_json(self.directory / 'identity.json', self.profile['identity'])
        atomic_json(self.run / maintain.RECEIPT, {
            'schema': 1, 'invocation_id': invocation or self.invocation,
            'owner_epoch': owner_epoch, 'config_digest': digest(self.profile['guard'])})
        if journal:
            atomic_json(self.run / 'path-transaction.json', {'owner_epoch': owner_epoch})

    def test_same_fence_covers_first_admission_staging_and_shared_controller(self):
        seen = {}

        def admission(record, runner):
            self.assertEqual(record, self.record)
            with self.assertRaises(BlockingIOError):
                Owner(self.run)
            with patch.object(maintain, 'readonly', return_value='probe-result') as readonly:
                self.assertEqual(runner(['blkid']), 'probe-result')
            seen['fd'] = readonly.call_args.kwargs['owner_fd']
            os.fstat(seen['fd'])
            return deepcopy(self.profile)

        def controller(config, owner):
            self.assertEqual(owner.fd, seen['fd'])
            self.assertEqual(config['initial_diskseq'], 99)
            self.assertEqual(load_json(self.directory / 'config.json'), config)
            self.assertEqual(load_json(self.directory / 'identity.json'), self.profile['identity'])
            with self.assertRaises(BlockingIOError):
                Owner(self.run)
            receipt = load_json(self.run / maintain.RECEIPT)
            self.assertEqual(receipt['owner_epoch'], owner.epoch)
            self.assertEqual(receipt['invocation_id'], self.invocation)

        with patch.object(maintain, 'current_profile', side_effect=admission), \
                patch.object(path_guard, 'run_owned', side_effect=controller) as owned:
            maintain.start(self.record, self.invocation)
        owned.assert_called_once()
        with self.assertRaises(OSError):
            os.fstat(seen['fd'])

    def test_wrong_disk_does_not_stage_runtime_or_call_shared_controller(self):
        with patch.object(maintain, 'current_profile', side_effect=RuntimeError('wrong UUID')), \
                patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'wrong UUID'):
                maintain.start(self.record, self.invocation)
        owned.assert_not_called()
        self.assertFalse((self.directory / 'config.json').exists())
        self.assertFalse((self.run / maintain.RECEIPT).exists())

    def test_new_service_cannot_adopt_old_active_owner(self):
        self.receipt(invocation='b' * 32)
        with Owner(self.run), patch.object(maintain, 'current_profile') as admission, \
                patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaises(BlockingIOError):
                maintain.start(self.record, self.invocation)
            # systemd still runs ExecStopPost after a refused ExecStart.
            maintain.takeover(self.record, self.invocation)
        admission.assert_not_called()
        owned.assert_not_called()
        self.assertEqual(load_json(self.run / maintain.RECEIPT)['invocation_id'], 'b' * 32)

    def test_terminal_journal_is_not_reset_or_probed_on_restart(self):
        self.receipt()
        atomic_json(self.run / 'path-transaction.json', {'phase': 'expired', 'owner_epoch': 'old'})
        with patch.object(maintain, 'current_profile') as admission, \
                patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'Existing transaction'):
                maintain.start(self.record, 'b' * 32)
        admission.assert_not_called()
        owned.assert_not_called()
        self.assertEqual(load_json(self.run / 'path-transaction.json')['phase'], 'expired')

    def test_fresh_profile_cannot_silently_replace_enrolled_identity(self):
        changed = deepcopy(self.profile)
        changed['identity']['usb_serial'] = 'replacement'
        with patch.object(maintain, 'current_profile', return_value=changed), \
                patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'registered policy'):
                maintain.start(self.record, self.invocation)
        owned.assert_not_called()
        self.assertFalse((self.directory / 'identity.json').exists())

    def test_no_journal_takeover_makes_no_owner_or_dm_calls(self):
        for receipt_present in (False, True):
            if receipt_present:
                self.receipt(journal=False)
            with self.subTest(receipt=receipt_present), \
                    patch.object(path_guard, 'acquire_owner') as acquire, \
                    patch.object(path_guard, 'run_owned') as owned, \
                    patch.object(path_guard, 'dm') as dm:
                maintain.takeover(self.record, self.invocation)
            acquire.assert_not_called()
            owned.assert_not_called()
            dm.assert_not_called()

    def test_own_journal_takeover_uses_saved_config_without_probing_media(self):
        self.receipt()

        def controller(config, owner, taking_over):
            self.assertTrue(taking_over)
            self.assertEqual(config, self.profile['guard'])
            with self.assertRaises(BlockingIOError):
                Owner(self.run)

        with patch.object(maintain, 'current_profile') as admission, \
                patch.object(path_guard, 'run_owned', side_effect=controller) as owned:
            maintain.takeover(self.record, self.invocation)
        admission.assert_not_called()
        owned.assert_called_once()

    def test_other_owner_journal_never_reaches_takeover(self):
        self.receipt()
        atomic_json(self.run / 'path-transaction.json', {'owner_epoch': 'someone-else'})
        with patch.object(path_guard, 'acquire_owner') as acquire, \
                patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'another maintenance owner'):
                maintain.takeover(self.record, self.invocation)
        acquire.assert_not_called()
        owned.assert_not_called()

    def test_changed_journal_after_wait_does_not_reach_core_takeover(self):
        self.receipt()

        def acquire(taking_over):
            self.assertTrue(taking_over)
            atomic_json(self.run / 'path-transaction.json', {'owner_epoch': 'someone-else'})
            return Owner(self.run)

        with patch.object(path_guard, 'acquire_owner', side_effect=acquire), \
                patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'owner changed'):
                maintain.takeover(self.record, self.invocation)
        owned.assert_not_called()

    def test_tampered_instance_config_is_not_used_for_takeover(self):
        self.receipt()
        changed = deepcopy(self.profile['guard'])
        changed['initial_diskseq'] += 1
        atomic_json(self.directory / 'config.json', changed)
        with patch.object(path_guard, 'run_owned') as owned:
            with self.assertRaisesRegex(RuntimeError, 'registered invocation'):
                maintain.takeover(self.record, self.invocation)
        owned.assert_not_called()

    def test_state_symlink_is_refused_before_owner_and_media_probe(self):
        destination = self.base / 'other'
        destination.mkdir()
        self.directory.symlink_to(destination)
        with patch.object(path_guard, 'acquire_owner') as acquire, \
                patch.object(maintain, 'current_profile') as admission:
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                maintain.start(self.record, self.invocation)
        acquire.assert_not_called()
        admission.assert_not_called()

    def test_invocation_requires_systemd_token(self):
        for value in ('', 'not-a-token', 'a' * 31):
            with self.subTest(value=value), patch.dict(os.environ, {'INVOCATION_ID': value}):
                with self.assertRaisesRegex(RuntimeError, 'systemd'):
                    maintain.invocation_id()
        with patch.dict(os.environ, {'INVOCATION_ID': self.invocation}):
            self.assertEqual(maintain.invocation_id(), self.invocation)

    def test_root_record_cannot_start_a_second_boot_owner(self):
        record = {'guard': {'profile': 'host'}}
        with patch.object(maintain, 'load_json', return_value=record), \
                patch.object(maintain, 'validate_record'), \
                patch.object(path_guard, 'acquire_owner') as acquire:
            with self.assertRaisesRegex(RuntimeError, 'boot service'):
                maintain.read_record(Path('/mock/root.json'))
        acquire.assert_not_called()


if __name__ == '__main__':
    unittest.main()
