"""Transaction fault tests must exercise the production service restrictions."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cpp_transaction_probe as probe


def settings(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


class TransactionPolicyTests(unittest.TestCase):
    def test_only_explicit_lab_paths_and_boot_gate_differ_from_shipped_unit(self):
        original = settings(probe.SERVICE_TEMPLATE.read_text())
        selected = settings(probe.transaction_service())
        differences = {key for key in original if original[key] != selected[key]}
        self.assertEqual(differences, {'ConditionKernelCommandLine', 'RootDirectory', 'ExecStart', 'ExecStopPost'})
        self.assertEqual(selected['Type'], 'notify')
        self.assertEqual(selected['NoNewPrivileges'], 'yes')
        self.assertEqual(selected['ProtectSystem'], 'strict')
        self.assertEqual(selected['ReadWritePaths'], '+/run +/dev')
        self.assertEqual(selected['TimeoutStopSec'], original['TimeoutStopSec'])
        self.assertEqual(selected['OnFailure'], original['OnFailure'])
        self.assertIn('takeover --config ' + probe.CONFIG, selected['ExecStopPost'])

    def test_generated_runner_asserts_actual_unit_process_filter_and_capability_mask(self):
        digest = hashlib.sha256(probe.transaction_service().encode()).hexdigest()
        generated = probe.adapt_runner('a' * 64, digest)
        compile(generated, '<transaction-native-policy>', 'exec')
        self.assertIn(digest, generated)
        self.assertIn("production_service_policy_applied", generated)
        self.assertIn("status['NoNewPrivs']", generated)
        self.assertIn("status['CapBnd']", generated)
        self.assertIn("status['Seccomp']", generated)
        self.assertIn("properties['DropInPaths']", generated)
        self.assertNotIn('run_legacy', generated)

    def test_policy_observation_is_valid_python_before_guest_execution(self):
        compile(probe.policy_probe('b' * 64), '<policy-observer>', 'exec')


if __name__ == '__main__':
    unittest.main()
