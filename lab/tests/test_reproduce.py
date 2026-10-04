"""Clean-checkout experiment boundaries, without QEMU or real devices."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
REPO = BASE.parent
sys.path.insert(0, str(BASE))
import reproduce


class ReproductionTests(unittest.TestCase):
    def setUp(self):
        (BASE/'work').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=BASE/'work')
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def signed_fixture(self):
        archive = self.folder/'root.tar.xz'
        archive.write_bytes(b'signed root archive')
        (self.folder/'rootfs.raw').write_bytes(archive.read_bytes()+bytes(512-archive.stat().st_size))
        (self.folder/'SHA256SUMS').write_text(reproduce.fetch_ubuntu.sha256(archive)+' *'+reproduce.fetch_ubuntu.NAME+'\n')
        (self.folder/'SHA256SUMS.gpg').write_bytes(b'test signature')
        keyring = self.folder/'keyring'
        keyring.write_bytes(b'test keyring')
        return keyring

    def test_default_invocation_starts_no_vm_and_reads_no_disk(self):
        with patch.object(sys, 'argv', ['reproduce.py']), patch.object(reproduce.os, 'geteuid', return_value=1000), \
                patch.object(reproduce.subprocess, 'run') as command, patch('builtins.print'):
            reproduce.main()
        command.assert_not_called()

    def test_root_invocation_is_rejected_before_work(self):
        with patch.object(sys, 'argv', ['reproduce.py', '--run']), \
                patch.object(reproduce.os, 'geteuid', return_value=0), \
                patch.object(reproduce.subprocess, 'run') as command, \
                patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                reproduce.main()
        command.assert_not_called()

    def test_input_directories_cannot_escape_workspace(self):
        with self.assertRaises(ValueError):
            reproduce.safe_directory('/tmp/not-a-lab-input', existing=True)
        with self.assertRaises(ValueError):
            reproduce.safe_directory(BASE/'work')
        (self.folder/'alias').symlink_to('/tmp')
        with self.assertRaises(ValueError):
            reproduce.safe_directory(self.folder/'alias', existing=True)

    def test_cached_source_is_reverified_instead_of_trusting_metadata(self):
        keyring = self.signed_fixture()
        (self.folder/'source.json').write_text('{"signature_verified":true}')
        with patch.object(reproduce.subprocess, 'run') as verifier:
            result = reproduce.verify_ubuntu(self.folder, keyring)
        self.assertTrue(result['signature_verified'])
        self.assertEqual(verifier.call_args.args[0][0], 'gpgv')
        (self.folder/'root.tar.xz').write_bytes(b'tampered')
        with patch.object(reproduce.subprocess, 'run'), self.assertRaisesRegex(ValueError, 'digest differs'):
            reproduce.verify_ubuntu(self.folder, keyring)

    def test_signature_failure_stops_before_archive_acceptance(self):
        keyring = self.signed_fixture()
        with patch.object(reproduce.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'gpgv')), \
                self.assertRaises(subprocess.CalledProcessError):
            reproduce.verify_ubuntu(self.folder, keyring)

    def test_padded_copy_tamper_or_symlink_is_rejected(self):
        keyring = self.signed_fixture()
        disk = self.folder/'rootfs.raw'
        for content in (b'wrong'+bytes(507), b'signed root archive'+bytes(492)+b'x'):
            disk.write_bytes(content)
            with patch.object(reproduce.subprocess, 'run'), self.assertRaises(ValueError):
                reproduce.verify_ubuntu(self.folder, keyring)
        disk.unlink()
        disk.symlink_to(self.folder/'root.tar.xz')
        with patch.object(reproduce.subprocess, 'run'), self.assertRaises(ValueError):
            reproduce.verify_ubuntu(self.folder, keyring)

    def test_seed_result_uses_one_bounded_independent_message(self):
        result = self.folder/'seed-result.json'
        result.write_text('{"ok":true,"value":{"schema":1}}\n')
        self.assertEqual(reproduce.read_seed_result(result, 0), {'schema': 1})
        # Kernel console corruption, duplicate results and partial writes must
        # fail explicitly; the reader never reconstructs plausible JSON.
        for payload in (b'', b'x' * 65537, b'{"ok":false,"error":"seed failed"}',
                        b'{"ok":true,"value":{}}\n{"ok":true,"value":{}}',
                        b'{"ok":true,"value":{"sc[ 1.0] kernel message\nhema":1}}',
                        b'{"ok":true,"value":'):
            with self.subTest(payload=payload[:60]):
                result.write_bytes(payload)
                with self.assertRaises((ValueError, RuntimeError)):
                    reproduce.read_seed_result(result, 0)
        result.write_text('{"ok":true,"value":{}}')
        with self.assertRaises(RuntimeError):
            reproduce.read_seed_result(result, 1)

    def test_payload_modes_ignore_permissive_host_umask(self):
        builder = reproduce.load_builder().base_builder()
        root = self.folder/'payload'
        root.mkdir(mode=0o777)
        (root/'etc').mkdir(mode=0o777)
        (root/'tmp').mkdir()
        config = root/'etc/config'
        config.write_text('explicit config')
        config.chmod(0o666)
        executable = root/'tool'
        executable.write_text('tool')
        executable.chmod(0o777)
        builder.normalize_payload(root)
        self.assertEqual(config.stat().st_mode & 0o777, 0o644)
        self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
        self.assertEqual((root/'etc').stat().st_mode & 0o777, 0o755)
        self.assertEqual((root/'tmp').stat().st_mode & 0o7777, 0o1777)

    def test_frozen_elf_manifest_hashes_tools_and_keeps_package_provenance(self):
        builder = reproduce.load_builder().base_builder()
        import native_payload
        root = self.folder/'elf-root'
        (root/'bin').mkdir(parents=True)
        binary = root/'bin/tool'
        header = b'\x7fELF\x02\x01' + bytes(10)
        binary.write_bytes(header + b'\x02\x00' + b'trusted fixture executable')
        (root/'bin/build.o').write_bytes(header + b'\x01\x00' + b'relocatable build object')
        (root/'bin/truncated').write_bytes(b'\x7fELF')
        (root/'bin/source.py').write_text('# not an ELF dependency')
        (root/'bin/alias').symlink_to('tool')
        metadata = {'/bin/tool': {'package': 'fixture-tools', 'version': '2', 'architecture': 'amd64'}}
        with patch.object(native_payload, 'dependency_packages', return_value=metadata) as query:
            result = builder.dependency_manifest(root)
        self.assertEqual(result['file_sha256'], {'/bin/tool': reproduce.fetch_ubuntu.sha256(binary)})
        self.assertEqual(result['dependency_packages'], metadata)
        self.assertEqual(set(query.call_args.args[0]), {'/bin/tool'})

    def test_frozen_elf_manifest_count_is_bounded_before_package_query(self):
        builder = reproduce.load_builder().base_builder()
        import native_payload
        root = self.folder/'elf-root'
        root.mkdir()
        for index in range(257):
            (root/f'elf-{index}').write_bytes(b'\x7fELF\x02\x02' + bytes(10) + b'\x00\x03')
        with patch.object(native_payload, 'dependency_packages') as query:
            with self.assertRaisesRegex(ValueError, '256'):
                builder.dependency_manifest(root)
        query.assert_not_called()

    def test_seed_guest_refuses_before_first_command_outside_vm(self):
        # Compile just the gated function: importing its optional VM-only
        # administration modules on the host is deliberately unnecessary.
        source = (BASE/'guest/seed_prepare.py').read_text()
        namespace = {'Path': Path, 'os': os}
        function = source[source.index('def prepare():'):source.index("\n\nif __name__")]
        with patch.object(subprocess, 'check_output') as command:
            exec(function, namespace)
            with self.assertRaisesRegex(RuntimeError, 'VM gates'):
                namespace['prepare']()
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
