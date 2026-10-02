#!/usr/bin/env python3
"""Verify stopped protected boots in disposable Ubuntu QEMU guests.

The candidate must come from the current production mkinitramfs builder.
Lab-only ORDER instrumentation removes the handoff token, exercises native
panic policy, or marks an unreachable continuation; it never adds a shell.
No console commands are sent. Writes are confined to disposable lab images.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import time
import traceback

import cpp_guard_probe as cpp
from efi_mount_probe import stop_vm, validate_inputs
from run import WORK, qemu_command

REPO = Path(__file__).resolve().parents[1]
PHASES = ('local-top', 'init-bottom')
HALTED = 'reboot: System halted'


def digest(content):
    return hashlib.sha256(content).hexdigest()


def instrument_order(order, phase):
    """Mark entry/return around the real script, injecting no alternative flow."""
    script_phase = 'local-top' if phase == 'native-panic' else phase
    invocation = f'/scripts/{script_phase}/ram-rescue-guard "$@"'
    if order.count(invocation) != 1:
        raise ValueError('Expected exactly one production boot-script invocation')
    gate = ('case " $(cat /proc/cmdline) " in *" ram_rescue_lab=1 "*) ;; '
            '*) exit 1 ;; esac\n'
            '[ "$(cat /sys/class/dmi/id/product_name)" = RAMRescueLab ] || exit 1\n')
    fault = 'rm -f /run/ram-rescue-guard/boot-ready\n' if phase == 'init-bottom' else ''
    if phase == 'native-panic':
        fault = 'panic "VM_NATIVE_FAILURE: simulated distribution boot failure"\n'
    before = gate + f'echo BOOT_FAILURE_ENTER={phase}\n' + fault
    after = f'\necho BOOT_FAILURE_FALLTHROUGH={phase}'
    return order.replace(invocation, before + invocation + after)


def create_case_image(folder, original, scripts, orders, phase):
    staging = folder / 'overlay'
    for name, content in scripts.items():
        target = staging / 'scripts' / name / 'ram-rescue-guard'
        target.parent.mkdir(parents=True)
        target.write_bytes(content)
        target.chmod(0o755)
    script_phase = 'local-top' if phase == 'native-panic' else phase
    order = staging / 'scripts' / script_phase / 'ORDER'
    order.write_text(instrument_order(orders[phase], phase))
    image = folder / 'initrd.img'
    shutil.copyfile(original, image)
    cpp.append_archive(image, staging)
    return image


def command_for_case(folder, build_dir, image, seed, phase, panic):
    overlay = folder / 'usb.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw',
                    '-b', str(seed), str(overlay)], check=True)
    with (folder / 'decoy.raw').open('xb') as stream:
        stream.truncate(16 * 1024**2)
    arguments = 'root=/dev/mapper/labrescue-ubuntu ro ram_rescue_guard=1'
    if phase != 'local-top':
        arguments += ' nompath'
    if panic is not None:
        arguments += ' panic=' + panic
    command = qemu_command(folder, same_port=True, kernel=build_dir / 'vmlinuz',
                           initramfs=image, extra_kernel_args=arguments)
    index = command.index('-append') + 1
    # Exercise the actual image default, not the lab launcher's reboot policy.
    words = command[index].split()
    words.remove('rdinit=/init')
    words.remove('panic=-1')
    command[index] = ' '.join(words)
    index = command.index('-serial') + 1
    command[index] = 'unix:' + str(folder / 'console.sock') + ',server=on,wait=off'
    command[command.index('-m') + 1] = '3072'
    for index, word in enumerate(command):
        if word == '-blockdev':
            block = json.loads(command[index + 1])
            if block['node-name'] == 'usbdisk':
                block.update(driver='qcow2', file={'driver': 'file', 'filename': str(overlay)},
                             backing={'driver': 'raw', 'read-only': True,
                                      'file': {'driver': 'file', 'filename': str(seed)}})
                command[index + 1] = json.dumps(block)
    return command


class Console:
    def __init__(self, channel, stream):
        self.channel = channel
        self.stream = stream
        self.output = ''

    def read_until(self, predicate, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate(self.output):
                return True
            readable, _, _ = select.select([self.channel], [], [], max(0, min(.2, deadline - time.monotonic())))
            if not readable:
                continue
            chunk = self.channel.recv(65536)
            if not chunk:
                return bool(predicate(self.output))
            self.stream.write(chunk)
            self.stream.flush()
            self.output += chunk.decode(errors='replace')
            if len(self.output) > 16 * 1024**2:
                raise RuntimeError('Serial output exceeds the experiment limit')
        return bool(predicate(self.output))


def classify_output(output, phase):
    failure = ('VM_NATIVE_FAILURE:' if phase == 'native-panic' else
               'RAM Guard protected boot stopped:')
    return {
        'expected_phase_reached': f'BOOT_FAILURE_ENTER={phase}' in output,
        'expected_failure_reported': failure in output,
        'no_return_to_init': 'BOOT_FAILURE_FALLTHROUGH=' not in output,
        'no_systemd_start': 'Welcome to Ubuntu' not in output and 'systemd[1]:' not in output,
        'system_halted': HALTED in output,
        'no_interactive_shell': '(initramfs)' not in output,
        'no_reboot': 'Rebooting automatically' not in output,
    }


def run_case(folder, build_dir, original, seed, scripts, orders, phase, panic):
    folder.mkdir(mode=0o700)
    image = create_case_image(folder, original, scripts, orders, phase)
    command = command_for_case(folder, build_dir, image, seed, phase, panic)
    result = {'phase': phase, 'variant': 'candidate',
              'panic_argument': panic, 'command': command,
              'template_sha256': {name: digest(value) for name, value in scripts.items()},
              'initrd_sha256': cpp.boot.sha256(image), 'passed': False}
    vm = None
    with (folder / 'qemu.log').open('w') as log, (folder / 'console.log').open('wb') as transcript:
        try:
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            def console_ready():
                if vm.poll() is not None:
                    raise RuntimeError('QEMU exited before the serial socket; see qemu.log')
                return (folder / 'console.sock').exists()
            cpp.wait_for(console_ready, 10, 'serial socket')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.connect(str(folder / 'console.sock'))
                console = Console(channel, transcript)
                if not console.read_until(lambda text: '(initramfs)' in text or HALTED in text, 120):
                    raise TimeoutError('Neither emergency shell nor halted kernel observed')
                # Observe only. An interactive prompt itself fails acceptance.
                console.read_until(lambda text: '(initramfs)' in text or
                                   'BOOT_FAILURE_FALLTHROUGH=' in text, 3)
                result['checks'] = classify_output(console.output, phase)
                result['passed'] = all(result['checks'].values())
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            stop_vm(vm)
    (folder / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    print(folder.name, 'PASS' if result['passed'] else 'FAIL', flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--enrollment', type=Path, default=WORK / 'hb-enroll-0930/enrollment.json')
    parser.add_argument('--seed-report', type=Path, default=WORK / 'bootevo-0930-173036-39819/report.json')
    args = parser.parse_args()
    build_dir, _, build, seed, before_hash = validate_inputs(args)
    candidate = {phase: (REPO / 'guard/integration' / phase).read_bytes() for phase in PHASES}
    for source, expected in build['source_sha256'].items():
        if cpp.boot.sha256(REPO / source) != expected:
            raise ValueError('Build source changed: ' + source)
    # AF_UNIX socket paths must stay below the platform's 108-byte limit.
    folder = WORK / ('bf-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    print('Boot failure matrix:', folder, flush=True)
    report = {'schema': 1, 'scenario': 'protected-boot-failure-console',
              'build': build, 'baseline_runtime_reproduction': False,
              'source_image_sha256_before': before_hash,
              'runner_sha256': cpp.boot.sha256(Path(__file__)),
              'cases': {}, 'passed': False}
    try:
        image = build_dir / 'initrd.img'
        extracted = folder / 'unpacked'
        subprocess.run(['unmkinitramfs', str(image), str(extracted)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        orders = {}
        for phase in PHASES:
            matches = list(extracted.glob('*/scripts/' + phase + '/ORDER'))
            if len(matches) != 1:
                raise RuntimeError('Expected one extracted initramfs ORDER for ' + phase)
            orders[phase] = matches[0].read_text()
            actual = matches[0].with_name('ram-rescue-guard').read_bytes()
            if actual != candidate[phase]:
                raise RuntimeError('Packed boot script differs from current source: ' + phase)
        roots = list(extracted.glob('*/conf/conf.d/ram-rescue-guard'))
        if len(roots) != 1 or roots[0].read_bytes() != (REPO / 'guard/integration/boot-policy.conf').read_bytes():
            raise RuntimeError('Packed image lacks the exact default failure policy')
        halts = list(extracted.glob('*/sbin/halt'))
        if len(halts) != 1 or cpp.boot.sha256(halts[0]) != cpp.boot.sha256(Path('/usr/bin/busybox')):
            raise RuntimeError('Packed image lacks the validated halt executable')
        report['halt_sha256'] = cpp.boot.sha256(halts[0])
        orders['native-panic'] = orders['local-top']
        for phase_index, phase in enumerate(PHASES):
            for variant, (suffix, panic) in enumerate((('unset', None), ('negative', '-1'), ('positive', '1'))):
                key = 'candidate-' + phase + '-' + suffix
                report['cases'][key] = run_case(folder / f'p{phase_index}v{variant}', build_dir, image, seed,
                                               candidate, orders, phase, panic)
        key = 'candidate-native-panic-default'
        report['cases'][key] = run_case(folder / 'native', build_dir, image, seed,
                                       candidate, orders, 'native-panic', None)
        report['checks'] = {
            'all_seven_cases_passed': len(report['cases']) == 7 and
                                     all(case['passed'] for case in report['cases'].values()),
            'candidate_templates_unchanged': all(content == (REPO / 'guard/integration' / phase).read_bytes()
                                                 for phase, content in candidate.items()),
            'runner_unchanged': report['runner_sha256'] == cpp.boot.sha256(Path(__file__)),
            'seed_backing_unchanged': cpp.boot.sha256(seed) == before_hash,
            'production_sources_unchanged': all(cpp.boot.sha256(REPO / name) == value
                                                for name, value in build['source_sha256'].items()),
        }
        report['passed'] = all(report['checks'].values())
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        report['source_image_sha256_after'] = cpp.boot.sha256(seed)
        report['public_summary'] = {
            'schema': 1, 'scenario': report['scenario'], 'baseline_runtime_reproduction': False,
            'kernel_release': build['kernel_release'], 'passed': report['passed'],
            'checks': report.get('checks', {}),
            'cases': {key: {name: value for name, value in case.items()
                            if name in ('phase', 'variant', 'panic_argument', 'template_sha256', 'checks', 'passed')}
                      for key, case in report['cases'].items()},
            'scope': 'Disposable Ubuntu/QEMU only; no host boot failure, no physical-console authentication test; '
                     'passive serial observation with no input; normal boot/recovery covered separately. '
                     'Original failure behavior remains static evidence, not an executed exploit.',
        }
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(folder / 'report.json'), 'passed': report['passed']}), flush=True)
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()
