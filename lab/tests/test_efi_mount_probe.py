"""Failure-path checks for the isolated EFI VM runner; no VM or device access."""
from argparse import Namespace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import efi_mount_probe as probe
from guest.efi_probe import EfiProbe


class EfiMountProbeTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def socket(self, **kwargs):
        connection = MagicMock(**kwargs)
        connection.__enter__.return_value = connection
        return connection

    def test_serial_noise_cannot_extend_total_deadline(self):
        connection = self.socket()
        connection.recv.return_value = b'unsolicited console output\n'
        with patch.object(probe.socket, 'socket', return_value=connection), \
                patch.object(probe.time, 'sleep'), \
                patch.object(probe.time, 'monotonic', side_effect=[0, 0, .6, 1.2, 1.2]):
            with self.assertRaisesRegex(TimeoutError, 'snapshot'):
                probe.ram_call(self.folder, 'snapshot', timeout=1)
        record = json.loads((self.folder / 'actions.jsonl').read_text())
        self.assertIn('TimeoutError', record['error'])
        self.assertIn('unsolicited console output', record['output_tail'])
        self.assertEqual(connection.recv.call_count, 2)

    def test_guest_error_is_logged_with_its_reply(self):
        connection = self.socket()
        response = {'ok': False, 'error': 'guest test failure'}
        connection.recv.return_value = b'EFI_REPLY=' + json.dumps(response).encode() + b'\n'
        with patch.object(probe.socket, 'socket', return_value=connection), \
                patch.object(probe.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'guest test failure'):
                probe.ram_call(self.folder, 'configure')
        record = json.loads((self.folder / 'actions.jsonl').read_text())
        self.assertEqual(record['response'], response)
        self.assertIn('guest test failure', record['error'])

    def test_failed_log_does_not_hide_serial_error(self):
        connection = self.socket()
        connection.recv.return_value = b''
        folder = MagicMock()
        (folder / 'actions.jsonl').open.side_effect = OSError('disk full')
        with patch.object(probe.socket, 'socket', return_value=connection), \
                patch.object(probe.time, 'sleep'), patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'VM serial shell closed'):
                probe.ram_call(folder, 'snapshot')

    def test_cleanup_failures_still_write_original_error_and_audits(self):
        report = {'passed': True, 'error': 'original scenario failure'}
        channel = MagicMock()
        channel.close.side_effect = OSError('close failed')

        def digest(path):
            if path == probe.RENDERER:
                return probe.RENDERER_SHA256
            raise OSError('seed unreadable')

        with patch.object(probe.boot, 'sha256', side_effect=digest), \
                patch.object(probe, 'source_hashes', return_value=probe.SOURCE_SHA256), \
                patch.object(probe, 'runner_hashes', return_value=probe.RUNNER_SOURCES), \
                patch.object(probe, 'audit_fat', return_value={'returncode': 0}), \
                patch('builtins.print'):
            probe.finalize(self.folder, self.folder / 'seed', 'before', report, qmp=channel)
        saved = json.loads((self.folder / 'report.json').read_text())
        self.assertEqual(saved['error'], 'original scenario failure')
        self.assertEqual([item['stage'] for item in saved['cleanup_errors']],
                         ['close_qmp', 'source_digest'])
        self.assertEqual(saved['fat_readonly_audit']['returncode'], 0)
        self.assertFalse(saved['passed'])

    def test_live_vm_prevents_offline_filesystem_audit(self):
        vm = MagicMock()
        vm.poll.return_value = None
        vm.terminate.side_effect = OSError('cannot stop QEMU')
        report = {'passed': True}
        with patch.object(probe.boot, 'sha256', return_value='same'), \
                patch.object(probe, 'audit_fat') as audit, patch('builtins.print'):
            probe.finalize(self.folder, self.folder / 'seed', 'same', report, vm=vm)
        audit.assert_not_called()
        self.assertFalse(report['passed'])
        self.assertEqual([item['stage'] for item in report['cleanup_errors']],
                         ['stop_vm', 'fat_readonly_audit'])

    def test_failed_initrd_build_restores_shared_observer_source(self):
        guest, hook = probe.boot.GUEST, probe.boot.HOOK
        with patch.object(probe.boot, 'overlay_initrd', side_effect=RuntimeError('build failed')):
            with self.assertRaisesRegex(RuntimeError, 'build failed'):
                probe.create_initrd(self.folder, self.folder / 'initrd', {})
        self.assertEqual(probe.boot.GUEST, guest)
        self.assertEqual(probe.boot.HOOK, hook)

    def test_seed_parent_symlink_cannot_escape_lab_work(self):
        # Keep even this negative fixture in lab/work; narrow the runner's work
        # boundary to one child so its sibling represents an outside location.
        work = self.folder / 'allowed'
        outside = self.folder / 'outside'
        work.mkdir()
        outside.mkdir()
        (outside / 'usb.raw').write_bytes(b'outside image')
        (work / 's0').symlink_to(outside)
        (work / 'initrd.img').touch()
        (work / 'vmlinuz').touch()
        identity = {'usb_serial': 'RAMRESCUE-LAB-001'}
        build = {'initramfs_sha256': 'hash', 'kernel_sha256': 'hash',
                 'enrollment_sha256': 'hash'}
        profile = {'identity': identity, 'guard': {'map_uuid': 'RAMRESCUE-HOST-VMTEST'}}
        seed = {'passed': True, 'cases': {'seed': {'observation': {'gate': {'identity': identity}}}}}
        for name, value in [('build.json', build), ('enrollment.json', profile), ('report.json', seed)]:
            (work / name).write_text(json.dumps(value))
        args = Namespace(build_dir=work, enrollment=work / 'enrollment.json',
                         seed_report=work / 'report.json')
        with patch.object(probe, 'WORK', work), \
                patch.object(probe.os, 'geteuid', return_value=1000), \
                patch.object(probe.boot, 'sha256', return_value='hash'):
            with self.assertRaisesRegex(ValueError, 'Seed must be a regular raw file below lab/work'):
                probe.validate_inputs(args)

    def guest_probe(self):
        return EfiProbe(MagicMock(), MagicMock(), uuid='TEST', options='',
                        rule='', path_unit='', fsck_unit='', fsck_dropin='')

    def test_guest_commands_fail_loudly_except_expected_observations(self):
        guest = self.guest_probe()
        result = subprocess.CompletedProcess([], 1, 'output', 'failure')
        with patch('guest.efi_probe.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'reload-rules'):
                guest.host('/usr/bin/udevadm', 'control', '--reload-rules')
            self.assertEqual(guest.host('/bin/false', check=False)['returncode'], 1)

    def test_vm_gate_prevents_all_guest_actions(self):
        guest = self.guest_probe()
        guest.gate.side_effect = RuntimeError('not the disposable VM')
        with patch.object(guest, 'snapshot') as snapshot:
            with self.assertRaisesRegex(RuntimeError, 'not the disposable VM'):
                guest.action('snapshot')
        snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()
