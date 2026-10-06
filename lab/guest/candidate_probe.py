"""Disposable Ubuntu candidate acceptance; fixed CLI only, no production hooks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import termios
import time
import traceback
import tty

PAYLOAD = Path('/run/candidate')
CONFIG = json.loads((PAYLOAD / 'fixture.json').read_text())
ROOT = Path('/var/lib/candidate-lab')
DEVICE = '/dev/mapper/' + CONFIG['spec']['name']
ENTRY = '/usr/bin/rescue-guard-admin'


def gate():
    flags = Path('/proc/cmdline').read_text().split()
    if 'ram_rescue_lab=1' not in flags or 'ram_rescue_candidate_test=1' not in flags or 'ram_rescue_guard=1' in flags:
        raise RuntimeError('candidate VM gate refused')
    if Path('/sys/class/dmi/id/product_name').read_text().strip() != 'RAMRescueLab':
        raise RuntimeError('candidate VM DMI gate refused')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(name, value):
    ROOT.mkdir(mode=0o700, exist_ok=True)
    path = ROOT / name
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    return path


def host(*args, timeout=180):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        raise RuntimeError(str(args) + ': ' + result.stderr[-4096:] + result.stdout[-4096:])
    return result.stdout


def manager(*args, timeout=240):
    command = [ENTRY, 'manager', *args]
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(str(command) + ': ' + result.stderr[-8192:] + result.stdout[-2048:])
    return {'command': command, 'returncode': result.returncode, 'stderr': result.stderr, 'value': value}


def plan():
    result = manager('plan', '--device', DEVICE, '--json')
    if result['returncode'] != 0:
        raise RuntimeError('Plan CLI failed: ' + json.dumps(result))
    return result


def policy(value):
    return value['confirmation']['support_policy']['combination']


def state():
    files = {}
    roots = ('/etc/ram-rescue-manager', '/var/lib/ram-rescue-manager', '/boot')
    for name in roots:
        root = Path(name)
        for path in sorted(root.rglob('*')) if root.exists() else []:
            if path.is_file() and not path.is_symlink():
                files[str(path)] = sha(path)
    for root in ('/etc/systemd/system', '/etc/udev/rules.d'):
        for path in Path(root).glob('*ram-rescue*'):
            files[str(path)] = str(path.readlink()) if path.is_symlink() else sha(path)
    for name in ('/etc/fstab', '/run/ram-rescue-guard/config.json'):
        path = Path(name)
        files[name] = sha(path) if path.exists() else None
    maps = { (path / 'dm/name').read_text().strip(): {
        'uuid': (path / 'dm/uuid').read_text().strip(), 'slaves': sorted(item.name for item in (path / 'slaves').iterdir()),
        'table': host('/usr/sbin/dmsetup', 'table', (path / 'dm/name').read_text().strip())}
        for path in Path('/sys/class/block').glob('dm-*')}
    mounts = sorted(' '.join(row.split()[2:]) for row in Path('/proc/1/mountinfo').read_text().splitlines())
    services = host('/usr/bin/systemctl', 'show', 'ram-rescue-manager.service', 'ram-rescue-guard.service',
                    'ram-rescue-maintain@' + CONFIG['spec']['name'] + '.service', '-p', 'Id,LoadState,ActiveState,MainPID')
    return {'files': files, 'maps': maps, 'mounts': mounts, 'services': services,
            'runtime_present': Path('/run/ram-rescue-manager/rootfs').exists()}


def require_inactive(current):
    statuses = [line for line in current['services'].splitlines() if line.startswith('ActiveState=')]
    pids = [line for line in current['services'].splitlines() if line.startswith('MainPID=')]
    if (len(statuses) != 3 or set(statuses) != {'ActiveState=inactive'}
            or len(pids) != 3 or set(pids) != {'MainPID=0'}
            or current['files']['/run/ram-rescue-guard/config.json'] is not None
            or current['runtime_present'] or 'ram-rescue-path' in current['maps']):
        raise RuntimeError('Unexpected active protection: ' + json.dumps(current))


def prepare():
    gate()
    if sha(PAYLOAD / 'handler.deb') != CONFIG['package']['sha256']:
        raise RuntimeError('Package transfer differs')
    installed = host('/usr/bin/dpkg', '--install', str(PAYLOAD / 'handler.deb'))
    kernel = CONFIG['kernel']
    reference = Path('/usr/share/ram-rescue-handler/kernels') / kernel['release']
    reference.mkdir(parents=True, mode=0o755)
    for name in ('source.json', 'InRelease', 'Packages'):
        shutil.copyfile(PAYLOAD / 'reference' / name, reference / name)
    keyring = Path('/usr/share/keyrings/ubuntu-archive-keyring.gpg')
    keyring.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PAYLOAD / 'reference/ubuntu-archive-keyring.gpg', keyring)
    shutil.copytree(PAYLOAD / 'kernel-files', '/', dirs_exist_ok=True)
    for path in [reference, *reference.rglob('*')]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    spec = CONFIG['spec']
    link = Path('/dev/disk/by-partuuid') / spec['partuuid']
    deadline = time.monotonic() + 15
    while not link.exists():
        if time.monotonic() > deadline:
            raise RuntimeError('Fixture partition did not appear')
        time.sleep(.1)
    node = link.resolve(strict=True)
    sectors = (Path('/sys/class/block') / node.name / 'size').read_text().strip()
    # This is a VM prerequisite, outside candidate code. No register/install.
    table = '0 ' + sectors + ' multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 ' + str(node) + ' 1'
    host('/usr/sbin/dmsetup', '--noudevsync', 'create', spec['name'], '--uuid', spec['map_uuid'], '--table', table)
    host('/usr/sbin/dmsetup', '--noudevsync', 'mknodes', spec['name'])
    host('/usr/bin/udevadm', 'trigger', '--action=change', '--settle')
    return {'package_install': installed, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'kernel_release': os.uname().release, 'entry_sha256': sha(ENTRY), 'baseline': state()}


def prerequisites():
    result = plan()
    save('preliminary-plan.json', result)
    value = result['value']
    combo = policy(value)
    if (value['io']['media'] != 'admission_passed' or value['confirmation']['blockers'] != ['combination_unvalidated']
            or set(combo['current_bindings'].values()) != {'pass'} or combo['qualification']['state'] != 'absent'
            or combo['subject'] is None):
        return {'passed': False, 'plan': result}
    record = {'schema': 1, 'passed': True, 'scope': 'candidate_preparation_prerequisites',
              'package_sha256': CONFIG['package']['sha256'], 'subject': combo['subject'],
              'subject_sha256': combo['subject_sha256'], 'full_admission': 'passed',
              'source_reference_sha256': sha(Path('/usr/share/ram-rescue-handler/kernels') / os.uname().release / 'source.json'),
              'test_source_sha256': CONFIG['test_source_sha256'], 'original_plan': result,
              'activation_acceptance': 'not_evaluated', 'recovery_acceptance': 'not_evaluated'}
    path = save('prerequisite.json', record)
    return {'passed': True, 'record': record, 'sha256': sha(path)}


def qualify():
    prior = ROOT / 'prerequisite.json'
    record = json.loads(prior.read_text())
    if record['passed'] is not True or record['package_sha256'] != CONFIG['package']['sha256']:
        raise RuntimeError('Prerequisites did not pass')
    qualification = {'schema': 1, 'purpose': 'candidate_preparation_prerequisites', 'result': 'passed',
        'subject_sha256': record['subject_sha256'],
        'scope': {'profile': 'host-data', 'topology': 'existing_single_path_dm', 'filesystems': ['ext4']},
        'evidence': {'prerequisite_report_sha256': sha(prior), 'origin_report_sha256': record['source_reference_sha256'],
                     'test_source_sha256': CONFIG['test_source_sha256'], 'package_sha256': CONFIG['package']['sha256']}}
    folder = Path('/usr/share/ram-rescue-handler/qualifications'); folder.mkdir(mode=0o755)
    path = folder / (record['subject_sha256'] + '.json')
    path.write_text(json.dumps(qualification, sort_keys=True, indent=2) + '\n'); path.chmod(0o644)
    actual = plan()
    if actual['value']['status'] != 'ready':
        raise RuntimeError('Qualified actual CLI is not ready: ' + json.dumps(actual))
    return {'qualification': qualification, 'qualification_sha256': sha(path), 'actual_plan': actual}


def stage(digest):
    return manager('stage', '--device', DEVICE, '--expect-plan', digest, '--json')


def cancel(row):
    return manager('cancel', '--operation', row['id'], '--expect-plan', row['plan_digest'], '--json')


def lifecycle():
    before = state()
    require_inactive(before)
    first = plan()['value']
    created = stage(first['plan_digest'])
    row = created['value']
    if created['returncode'] != 0 or row['state'] != 'prepared' or row['enabled'] or row['reboot_activates']:
        raise RuntimeError('Candidate did not remain inert: ' + json.dumps(created))
    repeated = stage(first['plan_digest'])
    if repeated['returncode'] != 0 or repeated['value']['id'] != row['id']:
        raise RuntimeError('Repeated stage did not reuse the candidate')
    removed, repeated_cancel = cancel(row), cancel(row)
    if (removed['returncode'] != 0 or repeated_cancel['returncode'] != 0
            or removed['value']['state'] != 'cancelled' or repeated_cancel['value'] != removed['value']):
        raise RuntimeError('Cancel was not idempotent')
    if state() != before:
        raise RuntimeError('Stage/cancel changed active state')
    with Path('/etc/fstab').open('a') as stream:
        stream.write('\n# candidate-lab dynamic context change\n')
    changed_baseline = state()
    expected = json.loads(json.dumps(before))
    expected['files']['/etc/fstab'] = sha('/etc/fstab')
    if changed_baseline != expected:
        raise RuntimeError('Observer changed more than the expected fstab content')
    changed = plan()['value']
    stale = stage(first['plan_digest'])
    if (policy(first)['subject_sha256'] != policy(changed)['subject_sha256']
            or first['plan_digest'] == changed['plan_digest'] or stale['returncode'] != 2
            or stale['value'].get('reason') != 'plan_changed'):
        raise RuntimeError('Dynamic local input did not invalidate only the plan')
    # A real SIGKILL after receipt/materialization, during the final actual plan.
    command = [ENTRY, 'manager', 'stage', '--device', DEVICE, '--expect-plan', changed['plan_digest'], '--json']
    output = ROOT / 'interrupted-cli.stdout'; errors = ROOT / 'interrupted-cli.stderr'
    prior = {path.name for path in Path('/var/lib/ram-rescue-plans/operations').iterdir()}
    killed = None
    with output.open('w') as stdout, errors.open('w') as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr,
                    env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline and process.poll() is None:
                for folder in Path('/var/lib/ram-rescue-plans/operations').iterdir():
                    if folder.name in prior or not (folder / 'candidate.json').exists():
                        continue
                    receipt = json.loads((folder / 'operation.json').read_text())
                    if receipt['state'] == 'preparing':
                        process.kill(); process.wait(timeout=15)
                        persisted = json.loads((folder / 'operation.json').read_text())
                        names = {path.name for path in folder.iterdir()}
                        if (process.returncode != -signal.SIGKILL or persisted['state'] != 'preparing'
                                or names != {'operation.json', 'manifest.json', 'plan.json', 'candidate.json'}
                                or any(sha(folder / name) != digest for name, digest in persisted['manifest']['files'].items())):
                            raise RuntimeError('SIGKILL did not leave a complete, unfinished candidate')
                        killed = {'id': folder.name, 'receipt_before_kill': receipt,
                                  'receipt_after_kill': persisted, 'returncode': process.returncode,
                                  'materialized_files_sha256': {name: sha(folder / name) for name in sorted(names)}}
                        break
                if killed:
                    break
                time.sleep(.005)
            if not killed:
                raise RuntimeError('Did not reach real preparing SIGKILL boundary')
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=15)
    resumed = stage(changed['plan_digest'])
    if resumed['returncode'] != 0 or resumed['value']['state'] != 'prepared' or resumed['value']['id'] != killed['id']:
        raise RuntimeError('Real interrupted candidate did not resume: ' + json.dumps(resumed))
    after = state()
    # fstab was an explicit observer change, not part of the candidate store.
    if changed_baseline != after:
        raise RuntimeError('Candidate mutated active state: ' + json.dumps({'before': changed_baseline, 'after': after}))
    kept = {'candidate': resumed['value'], 'before': changed_baseline,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    save('across-boot.json', kept)
    result = {'first_plan': first, 'stage': created, 'repeat_stage': repeated, 'cancel': removed,
              'repeat_cancel': repeated_cancel, 'dynamic_plan': changed, 'stale_confirmation': stale,
              'actual_sigkill': killed, 'resume': resumed, 'active_state_before': before,
              'active_state_after_observer_change': changed_baseline, 'active_state_after': after}
    save('lifecycle.json', result)
    return result


def reboot_check():
    saved = json.loads((ROOT / 'across-boot.json').read_text())
    current = state()
    require_inactive(current)
    maps = current['maps']
    if any(name.startswith('rr-data-') for name in maps) or current['runtime_present']:
        raise RuntimeError('Reboot activated a candidate')
    if any('ram-rescue' in name and value is not None for name, value in current['files'].items()):
        raise RuntimeError('Reboot installed active candidate configuration')
    if current['files'] != saved['before']['files']:
        raise RuntimeError('Reboot changed tracked boot/configuration files')
    if Path('/proc/sys/kernel/random/boot_id').read_text().strip() == saved['boot_id']:
        raise RuntimeError('Boot ID did not change')
    # Revocation must not trap an inert candidate after a reboot.
    qualification = Path('/usr/share/ram-rescue-handler/qualifications')
    revoked = []
    for path in qualification.glob('*.json'):
        revoked.append({'name': path.name, 'sha256': sha(path)}); path.unlink()
    cancelled = cancel(saved['candidate'])
    if cancelled['returncode'] != 0 or cancelled['value']['state'] != 'cancelled':
        raise RuntimeError('Candidate could not be cancelled after reboot/revocation')
    return {'state': current, 'boot_id_changed': True, 'candidate_not_activated': True,
            'revoked_qualifications': revoked, 'cancel_after_reboot_and_revocation': cancelled}


def diagnostics():
    return {'plan': plan(), 'active_state': state()}


def shutdown():
    subprocess.Popen(['/usr/bin/systemctl', 'poweroff'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'requested': True}


def serve():
    gate(); tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
    print(json.dumps({'ready': True, 'protocol': 'candidate-v1'}), flush=True)
    actions = {'prepare': prepare, 'prerequisites': prerequisites, 'qualify': qualify, 'lifecycle': lifecycle,
               'reboot_check': reboot_check, 'diagnostics': diagnostics, 'shutdown': shutdown}
    while line := sys.stdin.buffer.readline(4097):
        request = json.loads(line)
        if len(line) > 4096 or set(request) != {'id', 'action'} or request['action'] not in actions:
            raise RuntimeError('Invalid candidate test action')
        try:
            result = {'id': request['id'], 'ok': True, 'value': actions[request['action']]()}
        except BaseException:
            result = {'id': request['id'], 'ok': False, 'error': traceback.format_exc()}
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    serve()
