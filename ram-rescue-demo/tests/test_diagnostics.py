import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import mock_open, patch

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rescue', BASE / 'src/rescue.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        (BASE / 'work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=BASE / 'work')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / 'sys'
        (self.sys / 'class/block').mkdir(parents=True)
        self.c = dict(vid='21c4', pid='00c0', usb_serial='demo-serial', sectors=10000,
                      partition_number=3, partuuid='partition-id', pv_uuid='pv-id',
                      vg_name='vgportable', vg_uuid='v' * 32,
                      lvs={'ubuntu': {'dm_uuid': 'LVM-' + 'v' * 32 + 'l' * 32}})
        self.disk('sdc')
        self.dm = self.sys / 'class/block/dm-0'
        (self.dm / 'dm').mkdir(parents=True)
        (self.dm / 'dm/uuid').write_text(self.c['lvs']['ubuntu']['dm_uuid'])
        (self.dm / 'slaves').mkdir()
        (self.dm / 'slaves/sda3').touch()
        self.calls = []
        self.props = {'TYPE': 'LVM2_member', 'UUID': 'pv-id', 'PART_ENTRY_UUID': 'partition-id'}
        self.vg_uuid = 'v' * 32
        self.r = mod.RescueDiagnostics(self.c, self.sys, self.root / 'dev', self.runner)

    def disk(self, name, serial='demo-serial'):
        usb = self.sys / 'devices' / name / 'usb'
        usb.mkdir(parents=True)
        for n, value in [('idVendor', '21c4'), ('idProduct', '00c0'), ('serial', serial + '   ')]:
            (usb / n).write_text(value)
        block = usb / 'block' / name
        block.mkdir(parents=True)
        (block / 'size').write_text('10000')
        (block / (name + '3')).mkdir()
        (block / (name + '3') / 'partition').write_text('3')
        (self.sys / 'class/block' / name).symlink_to(block)

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == '/sbin/blkid':
            return '\n'.join(k + '=' + v for k, v in self.props.items())
        if args[1] == 'pvs':
            self.assertIn('--readonly', args)
            return json.dumps({'report': [{'pv': [{'pv_uuid': 'pv-id', 'vg_uuid': self.vg_uuid, 'vg_name': 'vgportable'}]}]})
        raise AssertionError(args)

    def test_reenumerated_device_with_matching_identity(self):
        self.assertTrue(self.r.verify().endswith('/dev/sdc3'))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([p.name for p in (self.dm / 'slaves').iterdir()], ['sda3'])

    def test_duplicate_serial_refuses_before_probing(self):
        self.disk('sdd')
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(self.calls, [])

    def test_partition_not_ready_never_probes_disk(self):
        shutil.rmtree(self.sys / 'class/block/sdc/sdc3')
        with self.assertRaises(mod.Refuse):
            self.r.verify()
        self.assertEqual(self.calls, [])

    def test_wrong_serial_refuses(self):
        self.c['usb_serial'] = 'different'
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(self.calls, [])

    def test_wrong_capacity_refuses(self):
        self.c['sectors'] = 123
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(self.calls, [])

    def test_wrong_pv_uuid_refuses(self):
        self.props['UUID'] = 'other-pv'
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_partition_uuid_refuses(self):
        self.props['PART_ENTRY_UUID'] = 'other-partition'
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(len(self.calls), 1)

    def test_wrong_vg_uuid_refuses(self):
        self.vg_uuid = 'x' * 32
        with self.assertRaises(mod.Refuse): self.r.verify()

    def test_missing_or_ambiguous_lv_is_not_reported_as_unique(self):
        self.assertEqual(self.r.mapping('ubuntu'), self.dm)
        other = self.sys / 'class/block/dm-1/dm'
        other.mkdir(parents=True)
        (other / 'uuid').write_text(self.c['lvs']['ubuntu']['dm_uuid'])
        with self.assertRaises(mod.Refuse): self.r.mapping('ubuntu')
        self.c['lvs']['ubuntu']['dm_uuid'] = 'LVM-missing'
        with self.assertRaises(mod.Refuse): self.r.mapping('ubuntu')
        self.assertEqual(self.calls, [])

    def test_status_reads_kernel_state_without_probing_storage(self):
        original_read = mod.read
        with patch.object(mod, 'read', side_effect=lambda path:
                          '' if str(path) == '/proc/1/mountinfo' else original_read(path)), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.r.status()
        self.assertIn("ubuntu dm-0 depends on ['sda3']", output.getvalue())
        self.assertEqual(self.calls, [])


class CommandLineTests(unittest.TestCase):
    def test_retired_refresh_is_rejected_before_accessing_files_or_devices(self):
        with patch.object(mod, 'read') as read, patch.object(mod.subprocess, 'run') as run, \
                patch('builtins.open') as opened, patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as error:
                mod.main(['refresh', 'ubuntu'])
        self.assertEqual(error.exception.code, 2)
        read.assert_not_called()
        run.assert_not_called()
        opened.assert_not_called()

    def test_help_and_log_work_without_device_enrollment(self):
        for action in ('help', 'log'):
            with self.subTest(action=action), patch.object(mod, 'read') as read, \
                    patch.object(mod.os, 'execv') as execute, patch('builtins.open', mock_open()), \
                    patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(mod.main([action]), 0)
                read.assert_not_called()
                if action == 'log':
                    execute.assert_called_once_with('/bin/busybox', ['busybox', 'dmesg'])
                else:
                    execute.assert_not_called()

    def test_missing_enrollment_returns_a_diagnostic_without_probing(self):
        with patch.object(mod, 'read', side_effect=FileNotFoundError('identity not enrolled')), \
                patch.object(mod.subprocess, 'run') as run, patch('builtins.open', mock_open()), \
                patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(mod.main(['status']), 1)
        self.assertIn('identity not enrolled', error.getvalue())
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
