import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rescue', BASE / 'src/rescue.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class RecoveryTests(unittest.TestCase):
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
        self.segtype = 'linear'
        self.lv_uuid = 'l' * 32
        self.r = mod.Recovery(self.c, self.sys, self.root / 'dev', self.runner)

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
            return json.dumps({'report': [{'pv': [{'pv_uuid': 'pv-id', 'vg_uuid': 'v' * 32, 'vg_name': 'vgportable'}]}]})
        if args[1] == 'lvs':
            return json.dumps({'report': [{'lv': [{'lv_uuid': self.lv_uuid, 'vg_uuid': 'v' * 32, 'segtype': self.segtype}]}]})
        if args[1] == 'lvchange':
            (self.dm / 'slaves/sda3').unlink()
            (self.dm / 'slaves/sdc3').touch()
            return ''
        raise AssertionError(args)

    def assert_no_write(self):
        self.assertFalse(any('lvchange' in c for c in self.calls))

    def test_reenumerated_device_with_matching_identity(self):
        self.assertTrue(self.r.verify().endswith('/dev/sdc3'))
        self.assert_no_write()

    def test_duplicate_serial_refuses_before_probing(self):
        self.disk('sdd')
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(self.calls, [])

    def test_wrong_serial_refuses(self):
        self.c['usb_serial'] = 'different'
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assert_no_write()

    def test_wrong_capacity_refuses(self):
        self.c['sectors'] = 123
        with self.assertRaises(mod.Refuse): self.r.verify()
        self.assertEqual(self.calls, [])

    def test_wrong_pv_uuid_refuses(self):
        self.props['UUID'] = 'other-pv'
        with self.assertRaises(mod.Refuse): self.r.refresh('ubuntu')
        self.assert_no_write()

    def test_wrong_partition_uuid_refuses(self):
        self.props['PART_ENTRY_UUID'] = 'other-partition'
        with self.assertRaises(mod.Refuse): self.r.refresh('ubuntu')
        self.assert_no_write()

    def test_already_attached_is_noop(self):
        (self.dm / 'slaves/sda3').rename(self.dm / 'slaves/sdc3')
        self.assertFalse(self.r.refresh('ubuntu'))
        self.assert_no_write()

    def test_wrong_lv_uuid_refuses(self):
        self.lv_uuid = 'x' * 32
        with self.assertRaises(mod.Refuse): self.r.refresh('ubuntu')
        self.assert_no_write()

    def test_non_linear_lv_refuses(self):
        self.segtype = 'thin'
        with self.assertRaises(mod.Refuse): self.r.refresh('ubuntu')
        self.assert_no_write()

    def test_cancellation_never_refreshes(self):
        with self.assertRaises(mod.Refuse): self.r.refresh('ubuntu', lambda _: 'no')
        self.assert_no_write()

    def test_device_disappears_during_prompt(self):
        def confirm(_):
            (self.sys / 'class/block/sdc').unlink()
            return 'REFRESH vgportable/ubuntu'
        with self.assertRaises(mod.Refuse): self.r.refresh('ubuntu', confirm)
        self.assert_no_write()

    def test_only_requested_verified_lv_refreshed(self):
        self.assertTrue(self.r.refresh('ubuntu', lambda _: 'REFRESH vgportable/ubuntu'))
        writes = [c for c in self.calls if 'lvchange' in c]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][-1], 'vgportable/ubuntu')
        self.assertIn('--noudevsync', writes[0])
        self.assertNotIn('--force', writes[0])


if __name__ == '__main__':
    unittest.main()
