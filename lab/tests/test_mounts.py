"""Mount plans cannot bypass enrolled identity or rewrite a live filesystem."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE / 'guard'))
spec = importlib.util.spec_from_file_location('protected_mounts', BASE / 'guard/mounts.py')
mounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mounts)
from admin import registry
from test_manager import profile, root_profile


class MountPlanTests(unittest.TestCase):
    def setUp(self):
        record = mounts.validate_record(registry.record_from_profile(profile()))
        self.records = {record['guard']['map_name']: record}
        self.plan = {'schema': 1, 'mounts': [
            {'map': 'rr-data-test', 'where': '/mnt/protected', 'automount': True},
            {'bind': '/mnt/protected/work', 'where': '/mnt/alias'},
        ]}

    def test_native_units_wait_for_controller_and_the_enrolled_dm_uuid(self):
        units = mounts.render_units(self.plan, self.records)
        mount = units['mnt-protected.mount']
        self.assertIn('What=/dev/disk/by-id/dm-uuid-RAMRESCUE-DATA-test\n', mount)
        self.assertIn('Requires=ram-rescue-maintain@rr-data-test.service\n', mount)
        self.assertIn('BindsTo=dev-disk-by\\x2did-dm\\x2duuid\\x2dRAMRESCUE\\x2dDATA\\x2dtest.device\n', mount)
        self.assertNotIn('/dev/sd', mount)
        self.assertNotIn('local-fs.target', mount)
        self.assertIn('TimeoutIdleSec=0\n', units['mnt-protected.automount'])
        self.assertIn('RequiresMountsFor=/mnt/protected/work\n', units['mnt-alias.mount'])
        self.assertNotIn('Exec', ''.join(units.values()))

    def test_bind_source_outside_declared_filesystems_is_rejected(self):
        self.plan['mounts'][1]['bind'] = '/some/unprotected/data'
        with self.assertRaisesRegex(ValueError, 'declared protected'):
            mounts.render_units(self.plan, self.records)

    def test_cycle_through_bind_source_and_destination_is_rejected(self):
        self.plan['mounts'][1].update(bind='/mnt/protected/work', where='/mnt')
        with self.assertRaisesRegex(ValueError, 'underneath|cycle'):
            mounts.render_units(self.plan, self.records)

    def test_unit_syntax_path_traversal_and_system_mounts_are_rejected(self):
        for where in ('/', '/mnt/../usr', '/mnt//data', '/mnt/disk/', '/mnt/a\nWhat=/dev/sda',
                      '/mnt/%i', '/run/user/disk', '/sys/disk', '/boot/efi'):
            with self.subTest(where=where):
                plan = deepcopy(self.plan)
                plan['mounts'][0]['where'] = where
                with self.assertRaises(ValueError):
                    mounts.render_units(plan, self.records)

    def test_arbitrary_source_and_filesystem_type_cannot_override_enrollment(self):
        for key, value in [('what', '/dev/sdb1'), ('type', 'vfat'), ('map', 'foreign')]:
            with self.subTest(key=key):
                plan = deepcopy(self.plan)
                plan['mounts'][0][key] = value
                with self.assertRaises(ValueError):
                    mounts.render_units(plan, self.records)

    def test_remount_and_lifetime_options_are_rejected(self):
        for options in (['rw', 'ro'], ['remount', 'rw'], ['x-systemd.device-bound=false'],
                        ['rw', 'rw'], ['bind'], 'rw', []):
            with self.subTest(options=options):
                plan = deepcopy(self.plan)
                plan['mounts'][0]['options'] = options
                with self.assertRaises(ValueError):
                    mounts.render_units(plan, self.records)

    def test_readonly_request_is_preserved_without_repair_or_rewrite(self):
        self.plan['mounts'][0]['options'] = ['ro', 'nosuid', 'nodev']
        units = mounts.render_units(self.plan, self.records)
        self.assertIn('Options=ro,nosuid,nodev\n', units['mnt-protected.mount'])
        self.assertNotIn('fsck', ''.join(units.values()))

    def test_duplicate_destinations_and_nested_automounts_are_rejected(self):
        for where in ('/mnt/protected', '/mnt/protected/child'):
            plan = deepcopy(self.plan)
            plan['mounts'].append({'map': 'rr-data-test', 'where': where, 'automount': True})
            with self.subTest(where=where), self.assertRaises(ValueError):
                mounts.render_units(plan, self.records)

    def test_root_lv_cannot_be_mounted_again_and_shared_lv_keeps_boot_owner(self):
        record = registry.record_from_profile(root_profile())
        record['identity']['lvs']['shared'] = {'dm_uuid': 'LVM-shared-uuid'}
        records = {'ram-rescue-path': record}
        plan = {'schema': 1, 'mounts': [{'map': 'ram-rescue-path', 'lv': 'ubuntu',
                                        'type': 'ext4', 'where': '/mnt/root-copy'}]}
        with self.assertRaisesRegex(ValueError, 'Root filesystem'):
            mounts.render_units(plan, records)
        plan['mounts'][0].update(lv='shared', where='/mnt/shared')
        text = mounts.render_units(plan, records)['mnt-shared.mount']
        self.assertIn('What=/dev/disk/by-id/dm-uuid-LVM-shared-uuid\n', text)
        self.assertIn('Requires=ram-rescue-guard.service\n', text)
        self.assertIn('AssertKernelCommandLine=ram_rescue_guard=1\n', text)

    def test_enrollment_tampering_is_rejected_before_rendering(self):
        self.records['rr-data-test']['identity']['fs_type'] = 'vfat'
        with self.assertRaisesRegex(ValueError, 'layout differs'):
            mounts.render_units(self.plan, self.records)

    def test_systemd_path_escape_matches_hyphens_and_leading_dot(self):
        self.assertEqual(mounts.unit_name('/mnt/test-disk'), r'mnt-test\x2ddisk.mount')
        self.assertEqual(mounts.unit_name('/.data'), r'\x2edata.mount')


if __name__ == '__main__':
    unittest.main()
