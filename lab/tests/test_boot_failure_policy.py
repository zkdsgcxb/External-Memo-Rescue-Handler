"""Protected initramfs failures never open a shell or return to /init."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest


BASE = Path(__file__).resolve().parents[2]
SCRIPTS = tuple(BASE / 'guard/integration' / name for name in ('local-top', 'init-bottom'))
INITRAMFS_FUNCTIONS = Path('/usr/share/initramfs-tools/scripts/functions')


class ProtectedBootFailureTests(unittest.TestCase):
    def policy(self, script):
        # Execute the real policy, without proceeding to mounts or block I/O.
        prefix = script.read_text().split("\ntrap 'fail_closed", 1)[0]
        return prefix.replace('. /scripts/functions', ':')

    def assert_blocked(self, program, *, signal_during_wait=None):
        process = subprocess.Popen(
            ['/bin/sh', '-c', program], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                process.communicate(timeout=0.15)
            if signal_during_wait:
                process.send_signal(signal_during_wait)
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.15)
        finally:
            # Kill only this test's session, including its blocking sleep.
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate(timeout=2)
        self.assertIn('BLOCKED', output)
        self.assertNotIn('CONTINUED', output)
        self.assertNotIn('INTERACTIVE_SHELL', output)
        return output

    def program(self, script, panic_body, action):
        return self.policy(script) + '\n' + f'''
panic=30
panic() {{ printf 'PANIC=%s\\n' "$panic"; {panic_body}; }}
sleep() {{ printf 'BLOCKED\\n'; /bin/sleep 60; }}
{action}
printf 'CONTINUED\\n'
'''

    def test_prerequisite_queries_have_no_boot_side_effects(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                result = subprocess.run(['/bin/sh', str(script), 'prereqs'],
                                        capture_output=True, text=True, timeout=2)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_dedicated_image_sets_the_native_initramfs_failure_default(self):
        hook = (BASE / 'guard/integration/initramfs-hook').read_text()
        # Exercise policy and halt staging; other tools/modules belong to
        # the full image build, not this unprivileged failure-policy check.
        copy_halt = '''copy_exec() {
    if [ "$2" = /sbin/halt ]; then
        mkdir -p "$DESTDIR/sbin"
        cp "$1" "$DESTDIR$2"
    fi
}'''
        staging = hook.split("# Ubuntu's initramfs BusyBox is smaller", 1)[0].replace(
            '. /usr/share/initramfs-tools/hook-functions', copy_halt)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / 'conf'
            source = conf / 'ram-rescue-guard'
            shutil.copytree(BASE / 'guard/integration', source / 'integration')
            (source / 'tools.tar.gz').write_bytes(b'test-only payload')
            image = root / 'image'
            result = subprocess.run(['/bin/sh', '-c', staging], capture_output=True,
                                    text=True, timeout=2,
                                    env={**os.environ, 'CONFDIR': str(conf),
                                         'DESTDIR': str(image)})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            policy = image / 'conf/conf.d/ram-rescue-guard'
            self.assertEqual(policy.read_bytes(),
                             (BASE / 'guard/integration/boot-policy.conf').read_bytes())
            self.assertEqual((image / 'sbin/halt').read_bytes(), Path('/usr/bin/busybox').read_bytes())
            self.assertTrue(os.access(image / 'sbin/halt', os.X_OK))
            result = subprocess.run(
                ['/bin/sh', '-c', 'panic=; . "$1"; printf "%s" "$panic"', 'test', str(policy)],
                capture_output=True, text=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '0')

    def test_panic_return_or_exit_cannot_continue_boot(self):
        for script in SCRIPTS:
            for behavior in ('return 0', 'return 1', 'exit 0', 'exit 1'):
                with self.subTest(script=script.name, behavior=behavior):
                    output = self.assert_blocked(self.program(
                        script, behavior, 'fail_closed "simulated setup failure"'))
                    self.assertEqual(output.count('PANIC=0'), 1)
                    self.assertEqual(output.count('BLOCKED'), 1)

    def test_unexpected_command_failure_uses_the_exit_trap(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                trap = next(line for line in script.read_text().splitlines()
                            if line.startswith("trap 'fail_closed"))
                output = self.assert_blocked(self.program(script, 'exit 1', trap + '\nfalse'))
                self.assertIn('PANIC=0', output)

    def test_termination_signal_cannot_release_failed_boot(self):
        for script in SCRIPTS:
            for received in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM):
                with self.subTest(script=script.name, signal=received.name):
                    self.assert_blocked(self.program(
                        script, 'return 0', 'fail_closed "simulated setup failure"'),
                        signal_during_wait=received)

    @unittest.skipUnless(INITRAMFS_FUNCTIONS.is_file(), 'Ubuntu initramfs-tools is not installed')
    def test_installed_panic_halts_without_consulting_credentials_or_opening_a_shell(self):
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                program = self.policy(script) + f'''
. {INITRAMFS_FUNCTIONS}
# Safe replacements: this test never calls a real reboot, halt or shell.
chvt() {{ :; }}
halt() {{ printf 'HALT_REQUESTED\\n'; return 1; }}
reboot() {{ printf 'UNEXPECTED_REBOOT\\n'; return 1; }}
run_scripts() {{ printf 'INTERACTIVE_SHELL\\n'; }}
setsid() {{ printf 'INTERACTIVE_SHELL\\n'; }}
sh() {{ printf 'INTERACTIVE_SHELL\\n'; }}
sleep() {{ printf 'BLOCKED\\n'; /bin/sleep 60; }}
panic=
fail_closed 'simulated failure before credentials are available'
printf 'CONTINUED\\n'
'''
                output = self.assert_blocked(program)
                self.assertIn('HALT_REQUESTED', output)
                self.assertNotIn('UNEXPECTED_REBOOT', output)


if __name__ == '__main__':
    unittest.main()
