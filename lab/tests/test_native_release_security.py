"""Offline instrumentation cannot accidentally become a production RAM runtime."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

GUARD = Path(__file__).resolve().parents[2] / 'guard'
sys.path.insert(0, str(GUARD))
import native_payload
from security_policy import controller_restrictions


class ReleasePayloadTests(unittest.TestCase):
    def test_sanitizer_rejected_before_any_payload_copy(self):
        for name in ('libasan.so.8', 'libubsan.so.1', 'libasan.so', 'libubsan.a'):
            with self.subTest(library=name), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                closure = {'/lib/' + name: directory / name}
                with patch.object(native_payload, 'binary_closure', return_value=closure):
                    with self.assertRaisesRegex(RuntimeError, 'Sanitizer runtime'):
                        native_payload.stage_runtime(directory, directory / 'binary')
                self.assertEqual(list(directory.iterdir()), [])

    def test_renamed_link_to_sanitizer_library_also_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'Sanitizer runtime'):
            native_payload.reject_instrumented_runtime({'/lib/unexpected.so': Path('/usr/lib/libasan.so.8.0.0')})

    def test_regular_release_closure_is_accepted(self):
        native_payload.reject_instrumented_runtime({'/lib/libc.so.6': Path('/usr/lib/libc.so.6'),
                                                    '/lib/libcrypto.so.3': Path('/usr/lib/libcrypto.so.3')})


class LoggerPolicyTests(unittest.TestCase):
    def test_all_controller_templates_keep_writable_paths_inside_root_directory(self):
        expected = controller_restrictions()
        root = (GUARD / 'integration/ram-rescue-guard.service').read_text()
        self.assertIn(expected, root)
        settings = dict(line.split('=', 1) for line in expected.splitlines() if '=' in line)
        self.assertEqual(settings['ReadWritePaths'], '+/run +/dev')
        self.assertEqual(settings['ProtectSystem'], 'strict')

    def test_logger_does_not_inherit_device_mapper_control_permission(self):
        unit = (GUARD.parent / 'ram-rescue-demo/src/ram-rescue-log.service').read_text()
        settings = dict(line.split('=', 1) for line in unit.splitlines() if '=' in line)
        self.assertEqual(settings['CapabilityBoundingSet'], 'CAP_SYSLOG')
        self.assertEqual(settings['ReadWritePaths'], '+/var/log')
        self.assertEqual(settings['NoNewPrivileges'], 'yes')
        self.assertEqual(settings['RestrictNamespaces'], 'yes')
        self.assertNotEqual(settings.get('PrivateDevices'), 'yes')
        self.assertNotEqual(settings.get('ProtectKernelLogs'), 'yes')


if __name__ == '__main__':
    unittest.main()
