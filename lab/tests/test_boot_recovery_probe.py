"""Keep repeat boot non-destructive and fault schedules explicitly opt-in."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from boot_recovery_probe import GUEST, parse_args


class BootProbeTests(unittest.TestCase):
    def test_default_and_explicit_normal_mode_do_not_select_fault_matrix(self):
        self.assertTrue(parse_args([])[1].normal_only)
        self.assertTrue(parse_args(['--normal-only'])[1].normal_only)
        self.assertFalse(parse_args(['--fault-matrix'])[1].normal_only)

    def test_existing_root_rejects_missing_enrollment_before_loading_control_code(self):
        guest = ast.parse(GUEST)
        function = next(node for node in guest.body
                        if isinstance(node, ast.FunctionDef) and node.name == 'activate_existing_root')
        isolated = ast.Module(body=[function], type_ignores=[])
        for records in [(None, None), ({'pv_uuid':'example'}, None), (None, {'queue_seconds':6})]:
            with self.subTest(records=records):
                values = iter(records)
                scope = {'read':lambda path: next(values)}
                exec(compile(isolated, '<existing-root>', 'exec'), scope)
                # Imports of VM-only control code must not occur at all when
                # enrollment is incomplete. An import failure would fail this.
                with self.assertRaisesRegex(RuntimeError, 'configuration is missing'):
                    scope['activate_existing_root']({'scenario':'existing-boot'})

    def test_embedded_guest_compiles(self):
        compile(GUEST, '<boot-evolution-guest>', 'exec')


if __name__ == '__main__':
    unittest.main()
