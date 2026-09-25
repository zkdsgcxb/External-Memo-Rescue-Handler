"""Admission races using real Recovery policy and synthetic sysfs, never disks."""
import errno
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE.parent / 'ram-rescue-demo/src'))
sys.path.insert(0, str(BASE / 'guest'))
import admission
from rescue import Recovery, Refuse


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sys = self.root / 'sys'
        self.dev = self.root / 'dev'
        (self.sys / 'class/block').mkdir(parents=True)
        self.dev.mkdir()
        self.identity = {'vid': '1234', 'pid': 'abcd', 'usb_serial': 'enrolled',
                         'sectors': 10000, 'partition_number': 1, 'partuuid': 'partition-id',
                         'pv_uuid': 'pv-id', 'vg_uuid': 'vgid', 'vg_name': 'labrescue',
                         'lvs': {'ubuntu': {'dm_uuid': 'LVM-vgidlv-id'}}}
        self.row = {'lv_name': 'ubuntu', 'lv_uuid': 'lv-id', 'vg_uuid': 'vg-id',
                    'segtype': 'linear', 'seg_start': '0', 'seg_size': '1024',
                    'seg_pe_ranges': '/dev/old1:0-127'}
        self.config = {'partition_sectors': 2048, 'logical_block_size': 512,
                       'layout': [{**self.row, 'seg_pe_ranges': '0-127'}]}
        self.disk('sdb')
        self.node = str(self.dev / 'sdb1')
        self.block = (self.sys / 'class/block/sdb1').resolve()
        self.disk_path = self.block.parent
        self.clock = 100.
        self.diskseq = 45
        self.node_dev = os.makedev(8, 17)
        self.size = 2048 * 512
        self.block_size = 512
        self.next_fd = 10000
        self.fds = {}
        self.closed = []
        self.calls = []
        self.open_flags = []
        self.hook = None
        self.props = {'TYPE': 'LVM2_member', 'UUID': 'pv-id', 'PART_ENTRY_UUID': 'partition-id'}
        self.recovery = Recovery(self.identity, self.sys, self.dev, self.runner)
        self.policy = admission.Admission(self.config, self.recovery,
                                           clock=lambda: self.clock, boot_id='this-boot')
        original_stat = os.stat
        original_fstat = os.fstat
        original_open = os.open
        original_close = os.close

        def fake_stat(path, *args, **kwargs):
            if str(path) == self.node:
                return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=self.node_dev)
            return original_stat(path, *args, **kwargs)

        def fake_open(path, flags, *args, **kwargs):
            if str(path) == self.node:
                fd = self.next_fd
                self.next_fd += 1
                self.open_flags.append(flags)
                self.fds[fd] = (self.diskseq, self.node_dev)
                return fd
            return original_open(path, flags, *args, **kwargs)

        def fake_fstat(fd):
            if fd >= 10000:
                if fd not in self.fds:
                    raise OSError(errno.EBADF, 'closed')
                return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=self.fds[fd][1])
            return original_fstat(fd)

        def fake_close(fd):
            if fd >= 10000:
                del self.fds[fd]
                self.closed.append(fd)
            else:
                original_close(fd)

        for name, func in [('stat', fake_stat), ('open', fake_open),
                           ('fstat', fake_fstat), ('close', fake_close)]:
            patcher = patch.object(admission.os, name, side_effect=func)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(admission.fcntl, 'ioctl', side_effect=self.ioctl)
        self.ioctl_mock = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.assert_no_leaked_fds)

    def assert_no_leaked_fds(self):
        self.assertEqual(self.fds, {})

    def disk(self, name):
        usb = self.sys / 'devices' / name / 'usb'
        usb.mkdir(parents=True)
        for key, value in [('idVendor', '1234'), ('idProduct', 'abcd'), ('serial', 'enrolled')]:
            (usb / key).write_text(value)
        disk = usb / 'block' / name
        (disk / 'queue').mkdir(parents=True)
        for key, value in [('size', '10000'), ('diskseq', '45'), ('queue/logical_block_size', '512')]:
            (disk / key).write_text(value)
        partition = disk / (name + '1')
        partition.mkdir()
        for key, value in [('partition', '1'), ('size', '2048'), ('start', '2048'), ('dev', '8:17')]:
            (partition / key).write_text(value)
        (self.sys / 'class/block' / name).symlink_to(disk)
        (self.sys / 'class/block' / (name + '1')).symlink_to(partition)
        (self.dev / (name + '1')).touch()

    def ioctl(self, fd, request, buffer, mutate):
        self.assertTrue(mutate)
        values = {admission.BLKGETDISKSEQ: ('=Q', self.fds[fd][0]),
                  admission.BLKGETSIZE64: ('=Q', self.size),
                  admission.BLKSSZGET: ('=I', self.block_size)}
        fmt, value = values[request]
        buffer[:] = struct.pack(fmt, value)
        return 0

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if self.hook:
            self.hook(args)
        if args[0] == '/sbin/blkid':
            self.assertEqual(args[-1], self.node)
            return '\n'.join(f'{key}={value}' for key, value in self.props.items())
        self.assertEqual(args[0], '/sbin/lvm')
        self.assertIn('--readonly', args)
        self.assertEqual(args[args.index('--devices') + 1], self.node)
        if args[1] == 'pvs':
            return json.dumps({'report': [{'pv': [{'pv_uuid': 'pv-id', 'vg_uuid': 'vgid',
                                                   'vg_name': 'labrescue'}]}]})
        if args[1] == 'lvs':
            return json.dumps({'report': [{'seg': [self.row]}]})
        self.fail(f'Unexpected command: {args}')

    def verify(self):
        return self.policy.verify(106., 'owner-1')

    def test_success_holds_readonly_fd_and_keeps_two_serial_identity_checks(self):
        with self.verify() as candidate:
            self.assertEqual([call[1] for call in self.calls], ['-p', 'pvs', 'lvs', '-p', 'pvs'])
            self.assertEqual(candidate.dev, os.makedev(8, 17))
            self.assertEqual(candidate.diskseq, 45)
            self.assertEqual(candidate.partition_sectors, 2048)
            self.assertEqual(candidate.logical_block_size, 512)
            self.assertEqual(candidate.sys_path, str(self.block))
            self.assertEqual(candidate.verified_at, 100.)
            self.assertEqual(candidate.deadline, 106.)
            self.assertEqual(self.open_flags, [os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC])
            self.assertEqual(len(self.fds), 1)

    def test_readonly_runner_never_allows_unscoped_lvm_and_preserves_argument_list(self):
        args = ['/sbin/lvm', 'pvs', '--readonly', '--devices', self.node]
        with patch('admission.command', return_value='done') as command:
            self.assertEqual(admission.readonly(args, timeout=20), 'done')
            self.assertEqual(command.call_args.kwargs['timeout'], 3)
            self.assertEqual(command.call_args.args[0][-2:],
                             ['--config', 'devices { multipath_component_detection=0 }'])
            self.assertEqual(len(args), 5)
            with self.assertRaises(ValueError):
                admission.readonly(['/sbin/lvm', 'pvs', '--readonly'])
            with self.assertRaises(ValueError):
                admission.readonly(['/sbin/lvm', 'pvs', '--devices', self.node])

    def test_duplicate_enrolled_serial_refuses_before_open_or_media_reads(self):
        self.disk('sdc')
        with self.assertRaisesRegex(Refuse, 'ONE'):
            self.verify()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.open_flags, [])

    def test_missing_partition_refuses_before_open_or_media_reads(self):
        (self.sys / 'class/block/sdb1').unlink()
        (self.block / 'partition').unlink()
        with self.assertRaisesRegex(Refuse, 'partition'):
            self.verify()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.open_flags, [])

    def test_wrong_media_identity_is_rejected_and_fd_closed(self):
        self.props['UUID'] = 'blank-or-wrong-pv'
        with self.assertRaisesRegex(Refuse, 'UUID'):
            self.verify()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.closed), 1)

    def test_wrong_layout_is_rejected_before_second_identity_check(self):
        self.row['seg_pe_ranges'] = '/dev/sdb1:128-255'
        with self.assertRaisesRegex(admission.AdmissionError, 'layout'):
            self.verify()
        self.assertEqual([call[1] for call in self.calls], ['-p', 'pvs', 'lvs'])

    def test_same_name_and_dev_t_reuse_during_verification_is_rejected(self):
        def replace_after_first_identity(args):
            if args[1] == 'pvs':
                (self.disk_path / 'diskseq').write_text('46')
        self.hook = replace_after_first_identity
        with self.assertRaisesRegex(admission.AdmissionError, 'disk instance'):
            self.verify()

    def test_dev_t_reuse_cannot_override_fd_identity(self):
        def replace_node(args):
            self.node_dev = os.makedev(8, 33)
        self.hook = replace_node
        with self.assertRaisesRegex(admission.AdmissionError, 'held device'):
            self.verify()

    def test_unsupported_diskseq_ioctl_fails_closed_without_identity_probe(self):
        self.ioctl_mock.side_effect = OSError(errno.ENOTTY, 'unsupported')
        with self.assertRaises(OSError) as exc:
            self.verify()
        self.assertEqual(exc.exception.errno, errno.ENOTTY)
        self.assertEqual(self.calls, [])

    def test_ioctl_capacity_must_match_sysfs_capacity(self):
        self.size -= 512
        with self.assertRaisesRegex(admission.AdmissionError, 'size'):
            self.verify()
        self.assertEqual(self.calls, [])

    def test_ioctl_logical_block_size_must_match_sysfs(self):
        self.block_size = 4096
        with self.assertRaisesRegex(admission.AdmissionError, 'block size'):
            self.verify()
        self.assertEqual(self.calls, [])

    def test_logical_block_size_must_also_match_original_enrollment(self):
        self.block_size = 4096
        (self.disk_path / 'queue/logical_block_size').write_text('4096')
        with self.assertRaisesRegex(admission.AdmissionError, 'block size'):
            self.verify()
        self.assertEqual(self.calls, [])

    def test_changed_enrollment_invalidates_an_outstanding_credential(self):
        with self.verify() as candidate:
            self.recovery.c['partuuid'] = 'uncoordinated-change'
            before = list(self.calls)
            with self.assertRaisesRegex(admission.AdmissionError, 'Enrollment changed'):
                candidate.revalidate('owner-1')
            self.assertEqual(self.calls, before)

    def test_expired_budget_never_reads_media(self):
        self.clock = 106.
        with self.assertRaisesRegex(admission.AdmissionError, 'deadline'):
            self.verify()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.open_flags, [])

    def test_slow_identity_cannot_extend_deadline(self):
        self.hook = lambda args: setattr(self, 'clock', 106.)
        with self.assertRaisesRegex(admission.AdmissionError, 'deadline'):
            self.verify()
        self.assertEqual([call[1] for call in self.calls], ['-p', 'pvs'])

    def test_overlapping_verification_is_rejected_without_second_fd(self):
        nested = []
        def nested_verification(args):
            if not nested:
                with self.assertRaisesRegex(admission.AdmissionError, 'already running'):
                    self.policy.verify(106., 'owner-2')
                nested.append(True)
        self.hook = nested_verification
        with self.verify():
            self.assertEqual(len(self.open_flags), 1)

    def test_commit_rechecks_layout_and_holds_same_fd(self):
        with self.verify() as candidate:
            self.assertIs(candidate.revalidate('owner-1'), candidate)
            self.assertEqual(len(self.open_flags), 1)
            self.assertEqual([call[1] for call in self.calls].count('pvs'), 2)
            self.assertEqual([call[1] for call in self.calls].count('lvs'), 2)

    def test_commit_rejects_changed_layout(self):
        with self.verify() as candidate:
            self.row['seg_size'] = '1025'
            with self.assertRaisesRegex(admission.AdmissionError, 'layout'):
                candidate.revalidate('owner-1')

    def test_commit_rejects_partition_start_change(self):
        with self.verify() as candidate:
            (self.block / 'start').write_text('4096')
            with self.assertRaisesRegex(admission.AdmissionError, 'changed'):
                candidate.revalidate('owner-1')

    def test_commit_rejects_diskseq_change_during_layout_read(self):
        with self.verify() as candidate:
            self.hook = lambda args: (self.disk_path / 'diskseq').write_text('46')
            with self.assertRaisesRegex(admission.AdmissionError, 'disk instance'):
                candidate.revalidate('owner-1')

    def test_commit_rejects_new_duplicate_even_when_held_instance_survives(self):
        with self.verify() as candidate:
            self.disk('sdc')
            with self.assertRaisesRegex(Refuse, 'ONE'):
                candidate.revalidate('owner-1')

    def test_commit_rejects_wrong_owner_before_any_media_read(self):
        with self.verify() as candidate:
            before = list(self.calls)
            with self.assertRaisesRegex(admission.AdmissionError, 'owner epoch'):
                candidate.revalidate('owner-2')
            self.assertEqual(self.calls, before)

    def test_commit_budget_is_checked_after_layout_read(self):
        with self.verify() as candidate:
            self.hook = lambda args: setattr(self, 'clock', 106.)
            with self.assertRaisesRegex(admission.AdmissionError, 'deadline'):
                candidate.revalidate('owner-1')

    def test_closed_candidate_cannot_be_reused(self):
        candidate = self.verify()
        candidate.close()
        candidate.close()
        with self.assertRaisesRegex(admission.AdmissionError, 'live fd'):
            candidate.revalidate('owner-1')
        self.assertEqual(len(self.closed), 1)

    def test_json_credential_does_not_serialize_fd_or_allow_mutation(self):
        with self.verify() as candidate:
            record = json.loads(json.dumps(candidate.to_dict()))
            self.assertNotIn('fd', record)
            self.assertEqual(record['boot_id'], 'this-boot')
            self.assertEqual(record['deadline'], 106.)
            record['instance']['diskseq'] = 999
            record['owner_epoch'] = 'changed'
            self.assertEqual(candidate.diskseq, 45)
            self.assertEqual(candidate.owner_epoch, 'owner-1')

if __name__ == '__main__':
    unittest.main()
