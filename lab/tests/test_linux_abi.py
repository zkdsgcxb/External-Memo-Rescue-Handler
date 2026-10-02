"""Reject unvalidated ioctl layouts before a guard can access block devices."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'guard'))
from admin import linux_abi


class LinuxABITest(unittest.TestCase):
    def test_validated_targets_use_the_same_lp64_contract(self):
        for machine in linux_abi.SUPPORTED_MACHINES:
            with self.subTest(machine=machine):
                self.assertEqual(machine, linux_abi.validate_abi(
                    machine, system='Linux', pointer_bytes=8,
                    size_t_bytes=8, int_bytes=4, byteorder='little'))

    def test_rejects_unvalidated_architectures_and_process_layouts(self):
        good = {'machine': 'x86_64', 'system': 'Linux', 'pointer_bytes': 8,
                'size_t_bytes': 8, 'int_bytes': 4, 'byteorder': 'little'}
        for changed in [{'machine': 'mips64'}, {'machine': 'ppc64le'},
                        {'machine': 'armv7l'}, {'pointer_bytes': 4},
                        {'size_t_bytes': 4}, {'int_bytes': 8},
                        {'byteorder': 'big'}, {'system': 'Darwin'}]:
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    RuntimeError, 'validated Linux LP64'):
                linux_abi.validate_abi(**{**good, **changed})


if __name__ == '__main__':
    unittest.main()
