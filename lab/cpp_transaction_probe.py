#!/usr/bin/env python3
"""Run the existing fault transaction matrix against the actual native owner.

Ubuntu guests use the current production root-service template, adapting only
the isolated laboratory root/configuration paths and lab boot flag. The phase
gates, candidate substitution, held NBD requests and terminal invariants remain.
Minimal guests have no systemd and are excluded from service-policy acceptance.
All generated guest sources and the adapted runner are saved with hashes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import cpp_guard_probe as full
from run import WORK

BASE = Path(__file__).resolve().parent
NATIVE = full.NATIVE
CONFIG = '/etc/rescue/path-guard.json'
SERVICE_TEMPLATE = BASE.parent / 'guard/integration/ram-rescue-guard.service'


def transaction_service():
    """Keep the shipped lifecycle/limits, mapping only disposable lab paths."""
    value = SERVICE_TEMPLATE.read_text()
    for before, after in (
        ('ConditionKernelCommandLine=ram_rescue_guard=1', 'ConditionKernelCommandLine=ram_rescue_lab=1'),
        ('RootDirectory=/run/ram-rescue-demo', 'RootDirectory=/run/rescue'),
        ('--config /run/ram-rescue-guard/config.json', '--config ' + CONFIG),
    ):
        expected = 2 if before.startswith('--config ') else 1
        if value.count(before) != expected:
            raise RuntimeError('Production service adaptation contract changed: ' + before)
        value = value.replace(before, after)
    return value


def policy_probe(service_digest):
    return f'''unit = Path('/proc/1/root/etc/systemd/system/lab-guard.service')
assert hashlib.sha256(unit.read_bytes()).hexdigest() == {service_digest!r}
pid = owner_pid()
status = dict(line.split(':',1) for line in Path(f'/proc/{{pid}}/status').read_text().splitlines() if ':' in line)
assert status['NoNewPrivs'].strip() == '1'
assert status['Seccomp'].strip() == '2'
# Linux capability bits: CHOWN=0, DAC_READ_SEARCH=2, SYS_ADMIN=21, MKNOD=27.
assert int(status['CapBnd'].strip(),16) == (1 << 0) | (1 << 2) | (1 << 21) | (1 << 27)
p = subprocess.run(['/bin/chroot','/proc/1/root','/usr/bin/systemctl','show','lab-guard.service',
    '-p','DropInPaths,NoNewPrivileges,CapabilityBoundingSet,ProtectSystem,ReadWritePaths,MemoryDenyWriteExecute,RestrictNamespaces,SystemCallFilter,ControlGroup'],
    text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5,check=True)
properties = dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
assert properties['DropInPaths'] == ''
assert properties['NoNewPrivileges'] == 'yes'
assert properties['ProtectSystem'] == 'strict'
assert properties['MemoryDenyWriteExecute'] == 'yes'
# Compare the paths after normalization; the exact unit digest above separately
# requires the shipped +/run +/dev root-relative text.
assert {{path.removeprefix('+') for path in properties['ReadWritePaths'].split()}} == {{'/run','/dev'}}
cgroup = Path('/proc/1/root/sys/fs/cgroup') / properties['ControlGroup'].lstrip('/')
assert (cgroup/'cpu.max').read_text().strip() == '4000 20000'
answer = {{'verified':True,'service_sha256':hashlib.sha256(unit.read_bytes()).hexdigest(),
          'service_text':unit.read_text(),'properties':properties,
          'process_security':{{name:status[name].strip() for name in ('CapInh','CapPrm','CapEff','CapBnd','NoNewPrivs','Seccomp')}},
          'mountinfo':Path(f'/proc/{{pid}}/mountinfo').read_text()}}
'''



def replace_once(source, before, after):
    if source.count(before) != 1:
        raise RuntimeError('Transaction fixture contract changed: ' + before)
    return source.replace(before, after)


def build_fixture(folder, original, binary):
    build = json.loads((original / 'build.json').read_text())
    for name, field in [('vmlinuz', 'kernel_sha256'), ('initramfs.cpio.gz', 'initramfs_sha256')]:
        if full.boot.sha256(original / name) != build[field]:
            raise RuntimeError('Transaction base image hash mismatch: ' + name)
    release, revision = full.frozen_python(folder)
    image = folder / 'initramfs.cpio.gz'
    shutil.copyfile(original / 'initramfs.cpio.gz', image)
    shutil.copyfile(original / 'vmlinuz', folder / 'vmlinuz')
    staging = folder / 'overlay'
    lab = staging / 'opt/lab'
    lab.mkdir(parents=True)
    # All observations/setup use one fixed Python release; none run as owner.
    for path in (release / 'guard/runtime').glob('*.py'):
        shutil.copyfile(path, lab / path.name)
    shutil.copyfile(release / 'ram-rescue-demo/src/rescue.py', lab / 'rescue.py')
    for path in (BASE / 'guest').iterdir():
        if path.is_file():
            shutil.copyfile(path, lab / path.name)
    (lab / 'root-init.sh').chmod(0o755)
    init = (BASE / 'guest/init.sh').read_text()
    # The prior tiny guest used tmpfs's permissive default mode. Production
    # control paths require a non-writable root-owned ancestor, just like /run
    # in the real Ubuntu initramfs.
    init = replace_once(init, 'mount -t tmpfs -o noswap tmpfs /run',
                        'mount -t tmpfs -o noswap,mode=0755 tmpfs /run')
    init = replace_once(init, 'mount -t tmpfs -o noswap,size=256M tmpfs /run/rescue',
                        'mount -t tmpfs -o noswap,size=256M,mode=0700 tmpfs /run/rescue')
    init = replace_once(init, 'mount --bind /dev /run/rescue/dev',
        '# The historical initrd carries group-writable /etc directories.\n'
        '# Normalize only the freshly copied disposable control inputs.\n'
        'chmod 0755 /run/rescue/etc\n'
        'chmod 0700 /run/rescue/etc/rescue\n'
        'chmod 0600 /run/rescue/etc/rescue/*.json\n'
        "stat -c 'LAB_CONTROL_MODE %a %u %h %n' /run/rescue /run/rescue/etc /run/rescue/etc/rescue /run/rescue/etc/rescue/*.json\n"
        'mount --bind /dev /run/rescue/dev')
    init = replace_once(init, 'python3 /opt/lab/path_guard.py; exec python3 /opt/lab/path_guard.py --takeover',
                        f'{NATIVE} run --config {CONFIG}; exec {NATIVE} takeover --config {CONFIG}')
    (staging / 'init').write_text(init)
    (staging / 'init').chmod(0o755)
    ubuntu = (lab / 'ubuntu.py').read_text()
    ubuntu = ubuntu.replace('/usr/bin/python3 /opt/lab/path_guard.py --takeover',
                            f'{NATIVE} takeover --config {CONFIG}')
    ubuntu = ubuntu.replace('/usr/bin/python3 /opt/lab/path_guard.py', f'{NATIVE} run --config {CONFIG}')
    ubuntu = replace_once(ubuntu, "    (units/'lab-workload.service').write_text(",
        "    # Terminal Guard failures deliberately isolate emergency.target.\n"
        "    # Keep only the independent RAM observers alive to collect the result.\n"
        "    for observer in ('lab-agent', 'lab-shell'):\n"
        "        observer_unit = units/(observer+'.service')\n"
        "        observer_unit.write_text(observer_unit.read_text().replace('[Unit]\\n', '[Unit]\\nDefaultDependencies=no\\nIgnoreOnIsolate=yes\\n', 1))\n"
        "    (units/'lab-guard.service').write_text(Path('/opt/lab/production-guard.service').read_text())\n"
        "    journal = root/'etc/systemd/journald.conf.d'\n"
        "    journal.mkdir(parents=True, exist_ok=True)\n"
        "    (journal/'lab-console.conf').write_text('[Journal]\\nForwardToConsole=yes\\nMaxLevelConsole=debug\\n')\n"
        "    (units/'lab-workload.service').write_text(")
    (lab / 'ubuntu.py').write_text(ubuntu)
    service = transaction_service()
    (lab / 'production-guard.service').write_text(service)
    agent = (lab / 'agent.py').read_text().replace("b'/opt/lab/path_guard.py'", repr(NATIVE.encode()))
    # This observer deliberately uses the pinned release's read-only methods.
    # Its historical class name is adapted only inside this disposable fixture.
    agent = replace_once(agent, 'from rescue import RescueDiagnostics, command',
                         'from rescue import Recovery as RescueDiagnostics, command')
    (lab / 'agent.py').write_text(agent)
    target = staging / NATIVE.lstrip('/')
    target.parent.mkdir(parents=True)
    shutil.copyfile(binary, target)
    target.chmod(0o755)
    for name, source in full.binary_closure(binary).items():
        target = staging / name.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755)
    full.append_archive(image, staging)
    build.update(implementation='cpp', python_observer_ref=revision,
                 production_service_template_sha256=full.boot.sha256(SERVICE_TEMPLATE),
                 production_service_sha256=full.boot.sha256(lab / 'production-guard.service'),
                 production_service_adaptations=['lab boot flag', 'RAM root path', 'lab config path', 'unit named lab-guard.service'],
                 service_policy_scope='Ubuntu systemd only; minimal guest has no service-policy acceptance',
                 native_binary_sha256=full.boot.sha256(binary),
                 initramfs_sha256=full.boot.sha256(image),
                 overlay_sha256={str(path.relative_to(staging)): full.boot.sha256(path)
                                 for path in staging.rglob('*') if path.is_file()})
    (folder / 'build.json').write_text(json.dumps(build, indent=2) + '\n')
    return build


def adapt_runner(binary_digest, service_digest=None):
    service_digest = service_digest or hashlib.sha256(transaction_service().encode()).hexdigest()
    source = (BASE / 'transaction_probe.py').read_text()
    source = replace_once(source,
        "    # The Python runtime was retired; replay this historical experiment intact.\n"
        "    from historical import run_legacy\n"
        "    raise SystemExit(run_legacy(__file__))",
        '    main()')
    source = replace_once(source,
        'p = subprocess.run(["/usr/bin/python3", "/opt/lab/path_guard.py"], text=True,',
        f'p = subprocess.run(["{NATIVE}", "run", "--config", "{CONFIG}"], text=True,')
    source = source.replace("b'/opt/lab/path_guard.py'", repr(NATIVE.encode()))
    source = replace_once(source, "and '--takeover' in service['stdout']",
                          "and ' takeover --config ' in service['stdout']")
    source = replace_once(source,
        "    assert b'/opt/guard-runtime/guard-runtime' in Path(f'/proc/{pid}/cmdline').read_bytes()",
        "    assert b'/opt/guard-runtime/guard-runtime' in Path(f'/proc/{pid}/cmdline').read_bytes()\n"
        f"    assert hashlib.sha256(Path(f'/proc/{{pid}}/exe').read_bytes()).hexdigest() == {binary_digest!r}")
    # Keep evidence of the observer's own progress: after root failure a read
    # of an unrelated process's userspace-backed proc entry can itself wait.
    source = replace_once(source, 'def observe():\n',
        "def observation_progress(stage):\n"
        "    value = {'stage': stage, 'guest_time': time.monotonic()}\n"
        "    Path('/run/transaction-observation-progress.json').write_text(json.dumps(value))\n"
        "    print('TRANSACTION_PROGRESS=' + json.dumps(value), flush=True)\n"
        "def observe():\n"
        "    observation_progress('begin')\n")
    source = replace_once(source, "            arguments = (path/'cmdline').read_bytes()",
        "            # comm is kernel metadata. Avoid faulting userspace pages\n"
        "            # or waiting on the mm lock of an unrelated broken-root app.\n"
        "            comm = (path/'comm').read_text().strip()\n"
        "            if int(path.name) != tracked_owner and comm not in ('guard-runtime', 'dmsetup'):\n"
        "                continue\n"
        "            observation_progress('proc_cmdline:' + path.name)\n"
        "            arguments = (path/'cmdline').read_bytes()")
    source = replace_once(source, "    inode = str(Path('/run/path-owner.lock').stat().st_ino)",
        "    observation_progress('owner_locks')\n"
        "    inode = str(Path('/run/path-owner.lock').stat().st_ino)")
    source = replace_once(source, "        answer[field] = query(*args)",
        "        observation_progress('dm_query:' + field)\n"
        "        answer[field] = query(*args)")
    source = replace_once(source, "    return answer\n'''", "    observation_progress('complete')\n    return answer\n'''")
    source = replace_once(source,
        "            report['owner_contention'] = ram_action(folder, '00-owner-contention',",
        "            if args.guest == 'ubuntu':\n"
        "                report['production_service_policy'] = ram_action(folder, '00-production-policy', "
        + repr(policy_probe(service_digest)) + ")\n"
        "            report['owner_contention'] = ram_action(folder, '00-owner-contention',")
    source = replace_once(source, "            checks = report['checks']",
        "            checks = report['checks']\n"
        "            if args.guest == 'ubuntu':\n"
        "                checks['production_service_policy_applied'] = report['production_service_policy']['verified']")
    source = replace_once(source, "from auto_run import result, wait_for",
                          f"import sys\nsys.path.insert(0, {str(BASE)!r})\nfrom auto_run import result, wait_for")
    # The generated runner lives in its evidence directory, so retain the
    # original qemu runner location without changing its behavior or hash.
    source = source.replace("Path(__file__).parent / 'run.py'", repr(str(BASE / 'run.py')))
    source = source.replace("sha256(" + repr(str(BASE / 'run.py')) + ")",
                            "sha256(Path(" + repr(str(BASE / 'run.py')) + "))")
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('scenario', choices=('matrix', 'kill', 'manager-absent', 'deadline',
                                            'candidate-replaced', 'blocked-probe', 'random-kill'))
    parser.add_argument('--binary', type=Path, default=WORK / 'cpp-runtime/guard-runtime')
    parser.add_argument('--base-build', type=Path, default=WORK / 'route-refactor-v4')
    parser.add_argument('--guest', choices=('minimal', 'ubuntu'), default='ubuntu',
                        help='Ubuntu verifies current production policy; minimal is transaction-only')
    parser.add_argument('--prepare-only', action='store_true', help='Build the fixture without starting QEMU')
    parser.add_argument('--stage', default='after_load')
    parser.add_argument('--queue-seconds', type=int, default=12)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('Run as an ordinary user; this fixture does not need host root')
    folder = WORK / ('cpp-txb-' + time.strftime('%m%d-%H%M%S') + '-' + str(os.getpid()))
    folder.mkdir(mode=0o700)
    build = build_fixture(folder, args.base_build, args.binary)
    runner = folder / 'transaction-native.py'
    runner.write_text(adapt_runner(build['native_binary_sha256'], build['production_service_sha256']))
    build['adapted_runner_sha256'] = full.boot.sha256(runner)
    build['adapter_sha256'] = full.boot.sha256(Path(__file__))
    build['original_runner_sha256'] = full.boot.sha256(BASE / 'transaction_probe.py')
    (folder / 'build.json').write_text(json.dumps(build, indent=2) + '\n')
    if args.prepare_only:
        print('Native transaction fixture prepared without starting QEMU:', folder, flush=True)
        return
    subprocess.run([sys.executable, str(runner), args.scenario, '--build-dir', str(folder),
                    '--guest', args.guest, '--stage', args.stage,
                    '--queue-seconds', str(args.queue_seconds)], check=True)


if __name__ == '__main__':
    main()
