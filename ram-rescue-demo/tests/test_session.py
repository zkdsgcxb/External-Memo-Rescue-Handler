"""Real distribution shell tests in an unprivileged, isolated user namespace."""
import importlib.util
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

BASE = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


payload = module('session_payload_test', BASE / 'session_payload.py')
installer = module('session_installer_test', BASE / 'install.py')


class PasswordPolicyTests(unittest.TestCase):
    def test_new_password_length_boundary(self):
        self.assertFalse(installer.valid_new_password(''))
        self.assertFalse(installer.valid_new_password('a' * 14))
        self.assertTrue(installer.valid_new_password('a' * 15))

    def test_spaces_and_unicode_do_not_require_character_classes(self):
        self.assertTrue(installer.valid_new_password('correct horse battery staple'))
        self.assertTrue(installer.valid_new_password('独立救援口令请记住并妥善保管这个密码'))

    def test_newlines_are_rejected(self):
        for newline in ('\n', '\r'):
            self.assertFalse(installer.valid_new_password('a' * 20 + newline))

    def test_nul_cannot_pad_a_short_effective_password(self):
        self.assertFalse(installer.valid_new_password('a\0' + 'x' * 14))
        for control in ('\t', '\x1b', '\x7f', '\u200b'):
            self.assertFalse(installer.valid_new_password('a' * 20 + control))

    def test_password_fits_the_login_input_buffer_in_bytes(self):
        self.assertTrue(installer.valid_new_password('a' * 1024))
        self.assertFalse(installer.valid_new_password('a' * 1025))
        self.assertTrue(installer.valid_new_password('密' * 341))
        self.assertFalse(installer.valid_new_password('密' * 342))


class SessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('unshare') or not shutil.which('chroot'):
            raise unittest.SkipTest('unshare and chroot are required')
        probe = subprocess.run(['unshare', '--user', '--map-root-user', 'true'], capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest('Unprivileged user namespaces are unavailable')
        (BASE / 'work').mkdir(exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='session-test-', dir=BASE / 'work')
        cls.root = Path(cls.temp.name)
        cls.receipt = payload.stage_session(cls.root)
        shutil.copyfile('/bin/busybox', cls.root / 'bin/busybox')
        (cls.root / 'bin/busybox').chmod(0o755)
        for directory in ('root', 'dev'):
            (cls.root / directory).mkdir()
        (cls.root / 'etc/motd').write_text('Disposable session test\n')
        # No host devices or mounts are exposed. Bash needs only its inherited
        # PTY descriptors; the history sink in this disposable root is empty.
        (cls.root / 'dev/null').touch()
        cls.command = ['unshare', '--user', '--map-root-user', 'chroot', str(cls.root),
                       '/bin/busybox', 'sh', '/bin/rescue-session']

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.policy = self.root / 'etc/rescue/session.conf'
        self.policy.write_text('IDLE_TIMEOUT=2\n')

    def session(self, extra_environment=None):
        pid, descriptor = pty.fork()
        if pid == 0:
            os.execvpe(self.command[0], self.command, {'PATH': '/usr/bin:/usr/sbin:/bin',
                                                    'TERM': 'dumb', **(extra_environment or {})})
        self.addCleanup(self.cleanup_session, pid, descriptor)
        return pid, descriptor

    @staticmethod
    def cleanup_session(pid, descriptor):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.close(descriptor)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def read_until(self, descriptor, marker=None, timeout=6):
        output = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not select.select([descriptor], [], [], min(0.1, deadline - time.monotonic()))[0]:
                continue
            try:
                chunk = os.read(descriptor, 65536)
            except OSError:
                return output
            if not chunk:
                return output
            output += chunk
            if b'\x1b[6n' in chunk:
                os.write(descriptor, b'\x1b[1;1R')
            if marker and marker in output:
                return output
        if marker:
            self.fail('Missing PTY marker: ' + repr(marker) + ' in ' + repr(output))
        return output

    def test_prompt_timeout_exits_real_shell(self):
        pid, descriptor = self.session()
        self.read_until(descriptor, b'RAM-RESCUE# ')
        started = time.monotonic()
        output = self.read_until(descriptor)
        self.assertIn(b'auto-logout', output)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(os.waitpid(pid, os.WNOHANG)[0], pid)

    def test_foreground_command_completes_before_idle_exit(self):
        self.policy.write_text('IDLE_TIMEOUT=1\n')
        _, descriptor = self.session()
        self.read_until(descriptor, b'RAM-RESCUE# ')
        os.write(descriptor, b'/bin/busybox sleep 2; echo FOREGROUND_FINISHED\n')
        output = self.read_until(descriptor)
        self.assertIn(b'\r\nFOREGROUND_FINISHED\r\n', output)
        self.assertIn(b'auto-logout', output)

    def test_zero_disables_idle_timeout(self):
        self.policy.write_text('IDLE_TIMEOUT=0\n')
        _, descriptor = self.session()
        self.read_until(descriptor, b'RAM-RESCUE# ')
        output = self.read_until(descriptor, timeout=2.2)
        self.assertNotIn(b'auto-logout', output)
        os.write(descriptor, b'echo STILL_PRESENT; exit\n')
        self.assertIn(b'\r\nSTILL_PRESENT\r\n', self.read_until(descriptor))

    def test_profiles_and_inherited_shell_hooks_are_not_executed(self):
        profiles = ('etc/profile', 'root/.profile', 'root/.bash_profile', 'root/.bashrc')
        for relative in profiles:
            (self.root / relative).write_text('echo UNEXPECTED_PROFILE\n')
            self.addCleanup((self.root / relative).unlink)
        _, descriptor = self.session({'ENV': '/root/.bashrc', 'BASH_ENV': '/root/.bashrc'})
        output = self.read_until(descriptor, b'RAM-RESCUE# ')
        os.write(descriptor, b'echo CLEAN_SHELL; exit\n')
        output += self.read_until(descriptor)
        self.assertNotIn(b'UNEXPECTED_PROFILE', output)
        self.assertIn(b'\r\nCLEAN_SHELL\r\n', output)

    def test_malformed_policy_never_starts_shell_or_executes_text(self):
        values = ('', 'IDLE_TIMEOUT=\n', 'IDLE_TIMEOUT=90000\n', 'IDLE_TIMEOUT=-1\n',
                  'IDLE_TIMEOUT=09\n', 'IDLE_TIMEOUT=1\nIDLE_TIMEOUT=2\n',
                  'UNKNOWN=2\n', 'IDLE_TIMEOUT=$(touch /injected)\n',
                  'IDLE_TIMEOUT=2; touch /injected\n', '=invalid\n')
        for value in values:
            with self.subTest(policy=value):
                self.policy.write_text(value)
                result = subprocess.run(self.command, capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('RAM-RESCUE# ', result.stdout)
                self.assertIn('refusing shell', result.stderr)
                self.assertFalse((self.root / 'injected').exists())

    def test_missing_policy_refuses_shell(self):
        self.policy.unlink()
        result = subprocess.run(self.command, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refusing shell', result.stderr)

    def test_staging_refuses_symlink_escape(self):
        with tempfile.TemporaryDirectory(dir=BASE / 'work') as directory:
            root = Path(directory)
            (root / 'usr').symlink_to('/usr', target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'escapes the image'):
                payload.stage_session(root)


if __name__ == '__main__':
    unittest.main()
