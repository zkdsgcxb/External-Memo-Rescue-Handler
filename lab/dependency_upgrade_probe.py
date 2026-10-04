#!/usr/bin/env python3
"""Build genuine library-version fixtures, then optionally test cold A/B/A deployment.

The old library comes from a freshly verified signed Ubuntu cloud archive.
Only the offline fixture's dependency resolver is substituted; the installed
manager, package format, native binary and service policy are production code.
Preparation never starts QEMU. Execution uses the isolated ordinary-Ubuntu
fixture, with disposable image files and no host device or network access.
"""
import argparse
from contextlib import contextmanager
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback

import reproduce
import standalone_data_probe as standalone
from auto_run import wait_for
from efi_mount_probe import stop_vm
from run import Channel, WORK

BASE = Path(__file__).resolve().parent
REPO = BASE.parent
GUEST = BASE / 'guest/dependency_upgrade_probe.py'
LIBRARY = 'libcrypto.so.3'
PACKAGE = 'libssl3t64'
sha256 = standalone.sha256


def package_status(text, package):
    matches = []
    for stanza in text.split('\n\n'):
        fields = dict(line.split(': ', 1) for line in stanza.splitlines()
                      if ': ' in line and not line.startswith(' '))
        if fields.get('Package') == package and fields.get('Status') == 'install ok installed':
            matches.append({'package': package + ':' + fields['Architecture'],
                            'version': fields['Version'], 'architecture': fields['Architecture']})
    if len(matches) != 1:
        raise ValueError('Expected one installed library package in the signed Ubuntu archive')
    return matches[0]


def extract_old_library(archive, destination, source):
    """Read two bounded regular members; never extract archive-supplied paths."""
    expected = str(source.resolve()).lstrip('/')
    found = {}
    with tarfile.open(archive, 'r|xz') as stream:
        for member in stream:
            name = member.name.removeprefix('./')
            if name not in (expected, 'var/lib/dpkg/status'):
                continue
            if name in found or not member.isfile() or not 0 < member.size <= 16 * 1024**2:
                raise ValueError('Unexpected signed-archive library or package-status member')
            found[name] = stream.extractfile(member).read()
    if set(found) != {expected, 'var/lib/dpkg/status'}:
        raise ValueError('Signed archive lacks the expected real library/package status')
    destination.write_bytes(found[expected])
    destination.chmod(0o755)
    status = package_status(found['var/lib/dpkg/status'].decode(), PACKAGE)
    return {**status, 'archive_member': expected,
            'dpkg_status_sha256': hashlib.sha256(found['var/lib/dpkg/status']).hexdigest(),
            'sha256': sha256(destination)}


def elf_identity(path):
    header = Path(path).read_bytes()[:20]
    if len(header) != 20 or header[:4] != b'\x7fELF':
        raise ValueError('Expected an ELF library')
    dynamic = subprocess.check_output(['readelf', '-d', str(path)], text=True)
    names = re.findall(r'\(SONAME\).*\[([^]]+)\]', dynamic)
    if names != [LIBRARY]:
        raise ValueError('Expected the libcrypto.so.3 ABI')
    return {'class': header[4], 'endianness': header[5], 'machine': header[18:20].hex(), 'soname': names[0]}


def old_base(base, output, library, metadata):
    """Repack actual older bytes and their provenance together, preserving all other files."""
    details = json.loads((base / 'manifest.json').read_text())
    if sha256(base / 'rescue-root.tar.gz') != details['sha256']:
        raise ValueError('Base archive digest differs')
    output.mkdir(mode=0o700)
    changed = []
    dependencies = None
    archive = output / 'rescue-root.tar.gz'
    with tarfile.open(base / 'rescue-root.tar.gz', 'r:gz') as source, \
            archive.open('wb') as compressed, \
            gzip.GzipFile(filename='', fileobj=compressed, mode='wb', mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode='w') as target:
        for member in source:
            info = copy.copy(member)
            if not info.isfile():
                target.addfile(info)
                continue
            content = source.extractfile(member).read()
            if Path(member.name).name == LIBRARY:
                content = library.read_bytes()
                changed.append('/' + member.name.lstrip('/'))
            elif member.name == 'etc/rescue/base-runtime.json':
                dependencies = json.loads(content)
                for name in dependencies['file_sha256']:
                    if Path(name).name == LIBRARY:
                        dependencies['file_sha256'][name] = metadata['sha256']
                        dependencies['dependency_packages'][name] = {
                            key: metadata[key] for key in ('package', 'version', 'architecture')}
                content = (json.dumps(dependencies, indent=2, sort_keys=True) + '\n').encode()
            info.size = len(content)
            target.addfile(info, io.BytesIO(content))
    if not changed or dependencies is None:
        raise ValueError('Base tools lack the library or ELF dependency manifest')
    details.update(sha256=sha256(archive), archive_bytes=archive.stat().st_size,
                   dependencies=dependencies,
                   fixture_dependency_override={'paths': changed, 'source': metadata})
    (output / 'manifest.json').write_text(json.dumps(details, indent=2) + '\n')
    (output / 'rescue-root.sha256').write_text(details['sha256'] + '  rescue-root.tar.gz\n')
    return details


@contextmanager
def old_resolver(native, closure, library, metadata):
    """Select real, independently authenticated bytes for this offline build only."""
    original_closure, original_packages = native.binary_closure, native.dependency_packages
    selected = {name: library if Path(name).name == LIBRARY else source for name, source in closure.items()}
    def provenance(paths):
        values = original_packages(paths)
        if str(library) in values:
            values[str(library)] = {key: metadata[key] for key in ('package', 'version', 'architecture')}
        return values
    native.binary_closure = lambda _binary: selected
    native.dependency_packages = provenance
    try:
        yield
    finally:
        native.binary_closure, native.dependency_packages = original_closure, original_packages


def prepare(args):
    if os.geteuid() == 0:
        raise ValueError('Offline fixture preparation never needs host root')
    folder = reproduce.safe_directory(args.output)
    folder.mkdir(mode=0o700)
    base = reproduce.safe_directory(args.base_rescue_dir, existing=True)
    binary = standalone.regular_lab_file(args.native_binary)
    authenticity = reproduce.verify_ubuntu(args.ubuntu_dir)
    sys.path.insert(0, str(REPO / 'guard'))
    import native_payload
    spec = importlib.util.spec_from_file_location('dependency_package_builder', REPO / 'guard/package.py')
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    closure = native_payload.binary_closure(binary)
    choices = [source for name, source in closure.items() if Path(name).name == LIBRARY]
    if len(choices) != 1:
        raise ValueError('Native closure does not have exactly one libcrypto.so.3')
    current = choices[0]
    old = folder / LIBRARY
    prior = extract_old_library(Path(args.ubuntu_dir) / 'root.tar.xz', old, current)
    installed = native_payload.dependency_packages([current])[str(current)]
    if prior['version'] == installed['version'] or sha256(old) == sha256(current):
        raise ValueError('The official old library must differ in both version and actual bytes')
    subprocess.run(['dpkg', '--compare-versions', prior['version'], 'lt', installed['version']], check=True)
    if elf_identity(old) != elf_identity(current):
        raise ValueError('The real old and new library ABI differs')
    smoke = subprocess.run([str(binary), '--version'], env={**os.environ,
        'LD_LIBRARY_PATH': str(folder), 'LD_BIND_NOW': '1'}, text=True, capture_output=True, check=True, timeout=10)
    old_tools = folder / 'base-a'
    old_base(base, old_tools, old, prior)
    with old_resolver(native_payload, closure, old, prior):
        package_a = package.build(folder / 'a', base_rescue_dir=old_tools, native_binary=binary)
    package_b = package.build(folder / 'b', base_rescue_dir=base, native_binary=binary)
    if package_a['administration_version'] == package_b['administration_version']:
        raise ValueError('Actual dependency change did not change the package version')
    administration = []
    for label, details in (('a', package_a), ('b', package_b)):
        manifest = folder / label / 'package/usr/lib/ram-rescue-handler' / details['administration_version'] / 'administration.json'
        administration.append(json.loads(manifest.read_text())['files'])
    if administration[0] != administration[1]:
        raise ValueError('Production sources changed during the A/B build; rebuild both fixtures')
    native_path = next(name for name in closure if Path(name).name == LIBRARY)
    record = {'schema': 1, 'scenario': 'real-libcrypto-cold-upgrade-and-rollback',
              'prepared': True, 'passed': False, 'vm_started': False,
              'signed_ubuntu': authenticity, 'library': native_path,
              'a': prior, 'b': {**installed, 'sha256': sha256(current), 'source': str(current)},
              'abi': elf_identity(old), 'old_library_eager_link_smoke': smoke.stdout.strip(),
              'binary_sha256': sha256(binary), 'packages': {'a': package_a, 'b': package_b},
              'source_sha256': administration[0],
              'scope': 'Same production code and native executable; genuine signed-source old libcrypto versus the current installed library. No file-byte tampering is used to simulate a version change.'}
    (folder / 'fixture.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'prepared': str(folder), 'old': prior['version'], 'new': installed['version'], 'vm_started': False}))


def guest_source():
    source = standalone.guest_source().replace("if __name__ == '__main__':\n    serve()", '')
    return source + '\n' + GUEST.read_text()


def create_initrd(folder, original, fixture, inputs):
    # Reuse ordinary-boot validation/module staging, then append only our
    # bounded observer and two genuine package inputs to the same test overlay.
    image = standalone.create_initrd(folder, original, inputs['a'], fixture['packages']['a'])
    overlay = folder / 'dependency-overlay/opt/standalone'
    overlay.mkdir(parents=True)
    (overlay / 'probe.py').write_text(guest_source())
    (overlay / 'dependency-fixture.json').write_text(json.dumps(fixture))
    # The ordinary fixture already carries A as handler.deb. Keep one copy
    # per package in initramfs and /run rather than duplicating its archive.
    shutil.copyfile(inputs['b'], overlay / 'handler-b.deb')
    staging = overlay.parent.parent
    paths = [Path('.'), *sorted(p.relative_to(staging) for p in staging.rglob('*'))]
    archive = subprocess.run(['cpio', '--null', '-o', '--format=newc', '--owner=0:0'], cwd=staging,
        input=b'\0'.join(str(path).encode() for path in paths) + b'\0', stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True).stdout
    with image.open('ab') as stream:
        stream.write(gzip.compress(archive, mtime=0))
    return image


def runtime_matches(snapshot, fixture, label):
    expected = fixture[label]['sha256']
    return (snapshot['resident']['library_sha256'] == expected
            and snapshot['resident']['binary_sha256'] == fixture['binary_sha256']
            and len(snapshot['controllers']) == len(standalone.data.SPECS)
            and all(row['binary_sha256'] == fixture['binary_sha256']
                    and row['loaded_library_sha256'] == expected for row in snapshot['controllers']))


def run(args):
    fixture_dir = reproduce.safe_directory(args.fixture_dir, existing=True)
    fixture = json.loads((fixture_dir / 'fixture.json').read_text())
    inputs, metadata_a = standalone.validate_inputs(args.reproduction_dir, fixture_dir / 'a')
    _, metadata_b = standalone.validate_inputs(args.reproduction_dir, fixture_dir / 'b')
    if fixture.get('packages') != {'a': metadata_a, 'b': metadata_b}:
        raise ValueError('Dependency fixture package records changed')
    for label in ('a', 'b'):
        inputs[label] = standalone.regular_lab_file(fixture_dir / label / fixture['packages'][label]['package'])
    folder = reproduce.safe_directory(args.output)
    folder.mkdir(mode=0o700)
    report = {'schema': 1, 'scenario': fixture['scenario'], 'passed': False,
              'fixture': fixture, 'input_sha256': {key: sha256(path) for key, path in inputs.items()},
              'observer_sha256': {str(path.relative_to(REPO)): sha256(path) for path in (
                  Path(__file__), GUEST, BASE / 'standalone_data_probe.py', BASE / 'guest/standalone_data_probe.py')}}
    vm = channel = qmp = None
    print('Dependency deployment VM:', folder, flush=True)
    with (folder / 'qemu.log').open('w') as log, (folder / 'qmp.jsonl').open('w') as qlog, \
            (folder / 'actions.jsonl').open('w') as actions:
        try:
            overlay = standalone.data.create_images(folder, inputs['seed'])
            image = create_initrd(folder, inputs['initrd'], fixture, inputs)
            command = standalone.vm_command(folder, inputs, image, overlay)
            report['command'] = command
            report['boots'] = []
            for index, label in enumerate(('a', 'b', 'a')):
                for name in ('qmp.sock', 'agent.sock', 'rescue.sock'):
                    (folder / name).unlink(missing_ok=True)
                vm = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                wait_for(lambda: (folder / 'qmp.sock').exists(), 15, 'dependency VM QMP')
                qmp = Channel(folder / 'qmp.sock', qlog, qmp=True)
                channel = Channel(folder / 'rescue.sock', actions)
                wait_for(lambda: any(row['message'].get('ready') for row in channel.events), 180, 'ordinary Ubuntu dependency observer')
                boot = {'expected': label}
                report['boots'].append(boot)
                if index == 0:
                    boot['configured'] = standalone.call(channel, 'configure_a', timeout=150)
                names = {spec['name']: 0 for spec in standalone.data.SPECS}
                standalone.ready(channel, names)
                boot['runtime'] = standalone.call(channel, 'dependencies', timeout=90)
                # Exercise actual admission hashing with each selected library.
                disk = standalone.data.SPECS[0]
                removed = standalone.data.remove(qmp, disk)
                time.sleep(max(0, .2 - (time.monotonic() - removed)))
                standalone.data.attach(qmp, disk)
                names[disk['name']] = 1
                boot['reconnected'] = standalone.ready(channel, names)
                if index < 2:
                    boot['transition'] = standalone.call(channel, 'upgrade_b' if index == 0 else 'rollback_a', timeout=180)
                else:
                    boot['retired'] = standalone.call(channel, 'retire')
                standalone.call(channel, 'shutdown')
                vm.wait(timeout=150)
                boot['clean_shutdown'] = vm.returncode == 0
                channel.close(); qmp.close()
                channel = qmp = None
                (folder / 'console.log').rename(folder / f'boot-{index + 1}-console.log')
            boots = report['boots']
            checks = report['checks'] = {
                'same_binary_real_dependency_versions_differ': fixture['a']['version'] != fixture['b']['version'] and fixture['a']['sha256'] != fixture['b']['sha256'],
                'three_distinct_boots': len({item['runtime']['boot_id'] for item in boots}) == 3,
                'ordinary_boots_without_root_guard': all(item['runtime']['root_guard_absent'] for item in boots),
                'registrations_survive_both_transitions': len({json.dumps(item['runtime']['registry_sha256'], sort_keys=True) for item in boots}) == 1,
                'clean_shutdowns': all(item['clean_shutdown'] for item in boots),
            }
            for index, boot in enumerate(boots):
                checks[f'boot_{index + 1}_actual_elf_and_loaded_library'] = runtime_matches(boot['runtime'], fixture, boot['expected'])
                if index < 2:
                    transition = boot['transition']
                    checks[f'transition_{index + 1}_resident_ram_unchanged'] = transition['before']['resident'] == transition['after']['resident']
                    result = transition['upgrade']['value']
                    checks[f'transition_{index + 1}_cold_activation_only'] = (result['requires_reboot'] and not result['runtime_prepared']
                        and not result['services_started'] and not transition['after']['controllers'])
                    checks[f'transition_{index + 1}_registry_retained'] = transition['before']['registry_sha256'] == transition['after']['registry_sha256']
        except BaseException:
            report['error'] = traceback.format_exc()
            if channel is not None and vm is not None and vm.poll() is None:
                for action in ('dependencies', 'logs'):
                    try:
                        report['failure_' + action] = standalone.call(channel, action, timeout=20)
                    except Exception:
                        report.setdefault('diagnostic_errors', {})[action] = traceback.format_exc()
        finally:
            stop_vm(vm)
            for connection in (qmp, channel):
                if connection is not None:
                    connection.close()
            report['inputs_unchanged'] = all(sha256(path) == report['input_sha256'][key] for key, path in inputs.items())
            report['passed'] = bool(report.get('checks')) and all(report['checks'].values()) and report['inputs_unchanged'] and not report.get('error')
            (folder / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print('Dependency deployment report:', folder / 'report.json', flush=True)
    if not report['passed']:
        raise SystemExit('Dependency deployment acceptance failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_args = commands.add_parser('prepare', help='Build real A/B packages without starting a VM')
    prepare_args.add_argument('--ubuntu-dir', type=Path, required=True)
    prepare_args.add_argument('--base-rescue-dir', type=Path, required=True)
    prepare_args.add_argument('--native-binary', type=Path, required=True)
    prepare_args.add_argument('--output', type=Path, required=True)
    run_args = commands.add_parser('run', help='Start the isolated three-boot acceptance VM')
    run_args.add_argument('--fixture-dir', type=Path, required=True)
    run_args.add_argument('--reproduction-dir', type=Path, required=True)
    run_args.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    {'prepare': prepare, 'run': run}[args.command](args)


if __name__ == '__main__':
    main()
