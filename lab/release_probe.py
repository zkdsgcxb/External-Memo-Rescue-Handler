#!/usr/bin/env python3
"""Four-boot public lifecycle acceptance on ordinary isolated Ubuntu."""
import argparse
import ast
import json
from pathlib import Path
import subprocess
import time
import traceback

import candidate_probe as candidate
import data_guard_probe as data
import standalone_data_probe as standalone
from auto_run import wait_for
from efi_mount_probe import stop_vm
from run import Channel, WORK

REPO = Path(__file__).resolve().parents[1]


def run(reproduction, package_dir, reference, folder, acceptance=None, support_package=None):
    inputs, package = standalone.validate_inputs(reproduction, package_dir)
    source = json.loads((reference / 'reference/source.json').read_text())
    if candidate.sha(inputs['kernel']) != source['image']['sha256']:
        raise RuntimeError('Boot kernel differs from authenticated reference')
    folder.mkdir(mode=0o700)
    workload_tree = ast.parse((REPO / 'lab/guest/data_probe.py').read_text())
    assignment = next(n for n in workload_tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'DATA_WORKLOAD' for t in n.targets))
    fixture = {'package': package, 'kernel': {'release': source['release']},
               'spec': data.SPECS[0], 'workload': ast.literal_eval(assignment.value)}
    if acceptance:
        fixture['acceptance'] = json.loads(acceptance.read_text())
    if support_package:
        fixture['support_package_sha256'] = candidate.sha(support_package)
    report = {'schema': 1, 'passed': False, 'scope': 'public_data_lifecycle_four_boot_vm',
              'qualification_fixture': acceptance is None, 'package': package,
              'host_deployed': False, 'actions': []}
    report['sources'] = {str(p.relative_to(REPO)): candidate.sha(p) for p in
                         (Path(__file__), REPO / 'lab/guest/release_probe.py')}
    report['seed_sha256'] = candidate.sha(inputs['seed'])
    before = inputs['seed'].stat()
    candidate.GUEST = REPO / 'lab/guest/release_probe.py'
    # Cloud seeds expose modules through the initramfs copymods tmpfs. Supply
    # the authenticated reference files on every boot, before product services.
    candidate.HOOK += '\ncp -a /opt/candidate/kernel-files/. "${rootmnt}/"\n'
    vm = channel = qmp = None
    streams = []

    def call(action):
        print('Release VM action:', action, flush=True)
        value = standalone.call(channel, action, timeout=240)
        report['actions'].append({'action': action, 'value': value})
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        return value

    try:
        overlay = data.create_images(folder, inputs['seed'])
        image = candidate.create_initrd(folder, inputs, package, reference, source, fixture,
            extra_files={'support.deb': support_package} if support_package else None)
        command = standalone.vm_command(folder, inputs, image, overlay)
        index = command.index('-append') + 1
        command[index] = command[index].replace('ram_rescue_standalone_test=1', 'ram_rescue_candidate_test=1')
        report['regular_block_inputs'] = candidate.validate_command(command, folder, inputs['seed'])
        report['command'] = command
        for boot in range(1, 5):
            log, qlog, actions = ((folder / f'{boot}-{name}').open('w') for name in ('qemu.log', 'qmp.jsonl', 'actions.jsonl'))
            streams.extend((log, qlog, actions))
            vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            wait_for(lambda: (folder / 'qmp.sock').exists(), 15, 'QMP socket')
            qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
            channel = Channel(folder / 'rescue.sock', actions)
            wait_for(lambda: any(r['message'].get('ready') for r in channel.events), 180, 'release observer')
            if boot == 1:
                prepared = call('prepare')
                report['subject'] = prepared['subject']
                call('busy_cases')
                call('lifecycle_checks')
                baseline = call('start_workload')
                data.remove(qmp, data.SPECS[0])
                call('gap_stop')
                data.attach(qmp, data.SPECS[0])
                def recovered():
                    value = standalone.call(channel, 'snapshot')
                    if value['state'].get('state') in ('expired', 'failed', 'interrupted') or value['errors']:
                        raise RuntimeError('Recovery failed: ' + json.dumps(value))
                    if value['state'].get('recoveries', 0) >= 1 and value['acks'] >= baseline['acks'] + 3:
                        assert value['worker_pid'] == baseline['worker_pid'] and value['worker_poll'] is None
                        return value
                report['recovery'] = wait_for(recovered, 25, 'original worker recovery')
                call('finish_workload')
                call('disable_for_boot')
            else:
                call({2: 'reboot_disabled', 3: 'reboot_enabled', 4: 'uninstall_reinstall'}[boot])
            call('shutdown')
            vm.wait(timeout=90)
            if vm.returncode:
                raise RuntimeError('VM shutdown failed')
            channel.close(); qmp.close(); channel = qmp = None
            for name in ('qmp.sock', 'rescue.sock', 'agent.sock'):
                (folder / name).unlink(missing_ok=True)
        report['passed'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
        if channel and vm and vm.poll() is None:
            try:
                report['diagnostics'] = call('release_diagnostics')
            except BaseException:
                report['diagnostics_error'] = traceback.format_exc()
    finally:
        stop_vm(vm)
        for connection in (qmp, channel):
            if connection:
                connection.close()
        for stream in streams:
            stream.close()
        after = inputs['seed'].stat()
        unchanged = (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        report['seed_readonly_backing_stat_unchanged'] = unchanged
        report['passed'] = report['passed'] and unchanged
        (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        print('Release report:', folder / 'report.json', 'passed:', report['passed'], flush=True)
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('reproduction-dir', 'package-dir', 'reference-dir', 'output'):
        parser.add_argument('--' + option, type=Path, required=True)
    parser.add_argument('--acceptance', type=Path)
    parser.add_argument('--support-package', type=Path)
    args = parser.parse_args()
    for path in (args.reproduction_dir, args.package_dir, args.reference_dir, args.output):
        if not path.resolve().is_relative_to(WORK.resolve()) or path.is_symlink():
            raise SystemExit('Use only regular lab/work artifacts')
    run(args.reproduction_dir.resolve(), args.package_dir.resolve(), args.reference_dir.resolve(),
        args.output.resolve(), args.acceptance, args.support_package)
