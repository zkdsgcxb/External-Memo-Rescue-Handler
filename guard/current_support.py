"""Bind a cold candidate plan to its actual installed code, kernel and artifacts.

Trust root and the kernel. Build IDs identify a build, not every byte of live
memory. Kernel reference files are locally trusted extraction records from
verified Ubuntu packages, independent of candidate qualification records.
"""
from copy import deepcopy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile

import diagnostics as doctor
import ram_environment
from admin.admission import digest
from native_payload import dependency_packages, _linked_libraries
from trusted_paths import open_trusted

KERNELS = '/usr/share/ram-rescue-handler/kernels'
KEYRING = '/usr/share/keyrings/ubuntu-archive-keyring.gpg'
ROOT_MODULES = ('dm_mod', 'dm_multipath', 'dm_round_robin', 'usbcore', 'usb_storage', 'uas',
                'scsi_mod', 'scsi_common', 'sd_mod', 'xhci_hcd', 'xhci_pci',
                'ext4', 'jbd2', 'mbcache', 'vfat', 'fat', 'nls_base', 'nls_cp437', 'nls_iso8859_1', 'exfat')
MANDATORY = frozenset(('dm_mod', 'dm_multipath', 'dm_round_robin', 'usbcore', 'scsi_mod', 'sd_mod'))
_ENTRY = None


def bind_entry(evidence):
    """Called only by the verified bootstrap in this process; no env/file flag."""
    global _ENTRY
    if _ENTRY is not None:
        raise RuntimeError('Administration binding already supplied')
    _ENTRY = deepcopy(evidence)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def build_id(notes):
    offset, found = 0, []
    while offset < len(notes):
        require(offset + 12 <= len(notes), 'truncated_elf_note')
        namesz, descsz, kind = struct.unpack_from('<III', notes, offset)
        offset += 12
        end_name = offset + namesz
        desc = (end_name + 3) & ~3
        end = desc + descsz
        require(end <= len(notes), 'truncated_elf_note')
        if notes[offset:end_name] == b'GNU\0' and kind == 3:
            require(4 <= descsz <= 64, 'invalid_build_id')
            found.append(notes[desc:end].hex())
        offset = (end + 3) & ~3
        require(offset <= len(notes), 'truncated_elf_note_padding')
    require(bool(found), 'missing_build_id')
    require(len(found) == 1, 'duplicate_build_id')
    return found[0]


def elf_build_id(raw):
    """Offline artifact helper; runtime reads only small sysfs note sections."""
    require(raw[:6] == b'\x7fELF\x02\x01' and len(raw) >= 64
            and struct.unpack_from('<H', raw, 18)[0] == 62, 'expected_amd64_elf')
    start = struct.unpack_from('<Q', raw, 40)[0]
    size, count = struct.unpack_from('<HH', raw, 58)
    found = []
    for index in range(count):
        at = start + index * size
        require(size >= 64 and at + size <= len(raw), 'invalid_elf_sections')
        if struct.unpack_from('<I', raw, at + 4)[0] == 7:
            offset, length = struct.unpack_from('<QQ', raw, at + 24)
            require(offset + length <= len(raw), 'invalid_elf_notes')
            try:
                found.append(build_id(raw[offset:offset + length]))
            except ValueError as error:
                if str(error) != 'missing_build_id':
                    raise
    require(len(found) == 1, 'missing_or_duplicate_build_id')
    return found[0]


def authenticate_index(inrelease, packages, *, keyring=KEYRING):
    """Authenticate this bounded Ubuntu index, not the whole installed database."""
    require(len(inrelease) <= 1024**2 and len(packages) <= 16 * 1024**2, 'source_index_limit')
    verified = subprocess.run(['/usr/bin/gpgv', '--keyring', str(keyring), '--output', '-'],
                              input=inrelease, capture_output=True, timeout=10)
    require(verified.returncode == 0, 'ubuntu_signature_failed')
    release = verified.stdout.decode()
    require('Origin: Ubuntu\n' in release and 'Codename: noble\n' in release, 'wrong_ubuntu_source')
    section = release.split('\nSHA256:\n', 1)
    require(len(section) == 2, 'missing_signed_sha256')
    rows = []
    for line in section[1].splitlines():
        if not line.startswith(' '):
            break
        fields = line.split()
        if len(fields) == 3 and fields[2] == 'main/binary-amd64/Packages':
            rows.append(fields)
    require(len(rows) == 1 and rows[0][0] == sha(packages) and int(rows[0][1]) == len(packages),
            'ubuntu_packages_digest_mismatch')
    return sha(inrelease), sha(packages)


def package_rows(packages, release):
    wanted = {'linux-image-' + release, 'linux-modules-' + release}
    selected = {}
    for paragraph in packages.decode().split('\n\n'):
        fields = dict(line.split(': ', 1) for line in paragraph.splitlines() if ': ' in line and not line.startswith(' '))
        name = fields.get('Package')
        if name in wanted:
            require(name not in selected and fields.get('Architecture') == 'amd64', 'ambiguous_kernel_package')
            family = '.'.join(release.split('-')[0].split('.')[:2])
            source = ('linux-signed-hwe-' if name.startswith('linux-image-') else 'linux-hwe-') + family
            require(fields.get('Source', '').split(' ')[0] == source, 'not_official_hwe_source')
            selected[name] = {key: fields[key] for key in ('Package', 'Version', 'Architecture', 'Source', 'Filename', 'Size', 'SHA256')}
    require(set(selected) == wanted, 'missing_official_kernel_packages')
    return selected


def kernel_evidence(reader, release):
    from support import strict_json, fields, checksum
    require(re.fullmatch('[A-Za-z0-9._+-]{1,128}', release), 'invalid_kernel_release')
    base = KERNELS + '/' + release
    raw = reader.read(base + '/source.json', limit=128 * 1024)
    source = strict_json(raw)
    fields(source, ('schema', 'release', 'image', 'modules', 'metadata', 'packages', 'origin', 'generator_sha256'))
    require(type(source['schema']) is int and source['schema'] == 1 and source['release'] == release, 'kernel_reference_mismatch')
    image = source['image']
    fields(image, ('sha256', 'build_id'))
    checksum(image['sha256'])
    origin = source['origin']
    fields(origin, ('inrelease_sha256', 'packages_sha256', 'keyring_sha256'))
    require(sha(reader.read(KEYRING, limit=1024**2)) == origin['keyring_sha256'], 'archive_keyring_changed')
    inrelease = reader.read(base + '/InRelease', limit=1024**2)
    packages = reader.read(base + '/Packages', limit=16 * 1024**2)
    actual = authenticate_index(inrelease, packages)
    require(actual == (origin['inrelease_sha256'], origin['packages_sha256']), 'source_material_changed')
    require(package_rows(packages, release) == source['packages'], 'source_package_record_changed')
    require(reader.hash('/boot/vmlinuz-' + release, limit=64 * 1024**2) == image['sha256'], 'kernel_disk_image_changed')
    require(build_id(reader.read('/sys/kernel/notes', limit=64 * 1024)) == image['build_id'], 'running_kernel_build_id_mismatch')
    # No claim that an enabled livepatch still equals the qualified image.
    livepatch = reader.root / 'sys/kernel/livepatch'
    require(not livepatch.exists() or not any(livepatch.iterdir()), 'livepatch_not_covered')
    modules = source['modules']
    require(isinstance(modules, dict) and set(ROOT_MODULES) <= set(modules) and len(modules) <= 64, 'module_closure_incomplete')
    metadata = source['metadata']
    fields(metadata, ('modules.builtin', 'modules.builtin.modinfo'))
    builtin_names = set()
    for name, expected in metadata.items():
        content = reader.read('/usr/lib/modules/' + release + '/' + name, limit=4 * 1024**2)
        require(sha(content) == checksum(expected), 'builtin_metadata_changed')
        if name == 'modules.builtin':
            builtin_names = {Path(line).name.removesuffix('.ko').replace('-', '_') for line in content.decode().splitlines()}
    states = {}
    for name, record in modules.items():
        require(re.fullmatch('[a-zA-Z0-9_]{1,64}', name), 'invalid_module_name')
        fields(record, ('builtin', 'path', 'sha256', 'build_id', 'srcversion', 'depends'))
        require(type(record['builtin']) is bool and isinstance(record['depends'], list)
                and set(record['depends']) <= set(modules), 'module_dependencies_incomplete')
        require(record['builtin'] == (name in builtin_names), 'module_builtin_classification_mismatch')
        if record['builtin']:
            require(record['path'] is None and record['sha256'] is None and record['build_id'] is None, 'invalid_builtin_record')
            states[name] = 'builtin_in_verified_image'
            continue
        path = record['path']
        prefix = '/usr/lib/modules/' + release + '/kernel/'
        require(isinstance(path, str) and path.startswith(prefix) and '..' not in Path(path).parts, 'invalid_module_path')
        require(reader.hash(path, limit=32 * 1024**2) == checksum(record['sha256']), 'module_disk_file_changed')
        sysroot = '/sys/module/' + name
        if not (reader.root / sysroot.lstrip('/')).exists():
            require(name not in MANDATORY, 'required_module_not_loaded')
            states[name] = 'not_loaded'
            continue
        # sysfs attributes report a 4096-byte st_size even for a few bytes.
        # Keep the trusted reader's size check and allow one bounded sysfs page.
        require(reader.read(sysroot + '/initstate', limit=4096).strip() == b'live', 'module_not_live')
        require(build_id(reader.read(sysroot + '/notes/.note.gnu.build-id', limit=4096)) == record['build_id'], 'loaded_module_build_id_mismatch')
        if record['srcversion']:
            require(reader.read(sysroot + '/srcversion', limit=4096).decode().strip() == record['srcversion'], 'module_srcversion_mismatch')
        states[name] = 'loaded_identity_matches'
    require(any(states[name] != 'not_loaded' for name in ('usb_storage', 'uas')), 'usb_transport_not_loaded')
    return {'release': release, 'image_sha256': image['sha256'],
            'modules_manifest_sha256': digest({'modules': modules, 'metadata': metadata})}, {
                'source_sha256': sha(raw), 'running_build_id': image['build_id'], 'modules': states,
                'origin': origin, 'association': 'trusted_reference_extraction_and_runtime_build_ids'}


def runtime_evidence(root):
    """Verify the selected package's inert payload; never create RAM mounts."""
    from support import fields, checksum
    payload = root / 'runtime'
    reader = doctor.Reader()
    raw = reader.read(str(payload / 'manifest.json'), limit=65536)
    manifest = json.loads(raw)
    native = manifest['native_runtime']
    required = {native['binary_path']: native['binary_sha256'], native['entrypoint_path']: native['entrypoint_sha256'],
                **native['library_sha256']}
    require(native.get('runtime') == 'cpp' and 2 < len(required) <= 128, 'invalid_native_runtime')
    require(native['binary_path'] == '/opt/guard-runtime/guard-runtime'
            and native['entrypoint_path'] == '/opt/guard-runtime/maintain', 'invalid_native_paths')
    for path, expected in required.items():
        require(path.startswith(('/opt/guard-runtime/', '/lib/', '/lib64/', '/usr/lib/'))
                and '..' not in Path(path).parts, 'invalid_runtime_member')
        checksum(expected)
    # Reuse the same limits as RAM unpack, but stream only: no extraction/writes.
    seen, actual, total, embedded = set(), {}, 0, None
    with open_trusted(payload / 'tools.tar.gz') as fd:
        before = os.fstat(fd)
        require(before.st_size <= 240 * 1024**2, 'compressed_runtime_archive_limit')
        require(ram_environment.package_manifest(payload, archive_fd=fd) == manifest, 'runtime_manifest_changed')
        with os.fdopen(os.dup(fd), 'rb') as stream, tarfile.open(fileobj=stream, mode='r|gz') as archive:
            for member in archive:
                total += member.size
                name = '/' + member.name.removeprefix('./').lstrip('/')
                require(name not in seen and len(seen) < 20000 and 0 <= member.size <= 240 * 1024**2
                        and total <= 240 * 1024**2, 'runtime_archive_limits')
                seen.add(name)
                if name in required or name == '/opt/guard-runtime/runtime.json':
                    require(member.isfile(), 'runtime_member_not_regular')
                    with archive.extractfile(member) as content:
                        if name in required:
                            actual[name] = hashlib.file_digest(content, 'sha256').hexdigest()
                        else:
                            require(member.size <= 65536, 'native_manifest_limit')
                            embedded = json.load(content)
        after = os.fstat(fd)
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'runtime_archive_changed')
    require(embedded == native, 'embedded_runtime_manifest_mismatch')
    require(actual == required and manifest['binary_sha256'] == native['binary_sha256'], 'runtime_content_mismatch')
    require(sha(reader.read(str(payload / 'manifest.json'), limit=65536)) == sha(raw), 'runtime_manifest_changed')
    return {'manifest_sha256': sha(raw), 'archive_sha256': manifest['archive_sha256'],
            'binary_sha256': native['binary_sha256'], 'libraries_manifest_sha256': digest(native['library_sha256'])}


def host_dependencies(reader):
    """Finite actual ELF closure plus explicit package versions; no full dpkg scan."""
    ctypes.CDLL('libdevmapper.so.1.02.1')  # load only, no DM task/device operation
    executable = Path('/proc/self/exe').resolve(strict=True)
    paths = {executable}
    maps = Path('/proc/self/maps').read_text().splitlines()
    require(len(maps) <= 2048, 'process_mapping_limit')
    for row in maps:
        parts = row.split(maxsplit=5)
        if len(parts) == 6 and parts[5].startswith('/') and parts[4] != '0':
            path = Path(parts[5])
            require(not parts[5].endswith(' (deleted)'), 'mapped_dependency_deleted')
            info = path.stat()
            major, minor = (int(part, 16) for part in parts[3].split(':'))
            require((info.st_ino, os.major(info.st_dev), os.minor(info.st_dev)) == (int(parts[4]), major, minor), 'mapped_dependency_replaced')
            # Locale data is not executable code and varies with user language.
            if 'x' in parts[1] and path.name != 'gconv-modules.cache':
                paths.add(path.resolve(strict=True))
    require(any('libdevmapper.so.' in path.name for path in paths), 'actual_dm_library_unknown')
    for command in ('/usr/sbin/blkid', '/usr/bin/findmnt', '/usr/bin/systemctl', '/usr/bin/gpgv'):
        path = Path(command).resolve(strict=True)
        reader.hash(str(path))  # trust the executable before ldd inspects it
        paths.add(path)
        paths.update(_linked_libraries(path).values())
    require(0 < len(paths) <= 96, 'dependency_closure_limit')
    files = {str(path): reader.hash(str(path), limit=64 * 1024**2) for path in sorted(paths)}
    owners = dependency_packages(files)
    require(all(value['package'] and value['version'] for value in owners.values()), 'dependency_package_unknown')
    versions = doctor.package_versions({'python3.12-minimal', 'libpython3.12-stdlib', *[row['package'] for row in owners.values()]})
    require('python3.12-minimal' in versions and 'libpython3.12-stdlib' in versions, 'python_runtime_package_unknown')
    # Alias keys from package_versions are removed from the canonical manifest.
    packages = {row['package']: row for row in versions.values()}
    return {'manifest_sha256': digest({'files': files, 'packages': packages})}, {
        'file_count': len(files), 'packages': packages, 'python_stdlib_scope': 'trusted_distribution_package_versions'}


def collect(reader=None):
    reader = reader or doctor.Reader()
    from support import BINDINGS, PACKAGE_ROOT, strict_json
    bindings = dict.fromkeys(BINDINGS, 'unknown')
    observations = {'limitations': 'trusted_root_and_kernel_build_identity_not_memory_attestation'}
    parts = {}

    def attempt(name, callback):
        try:
            result = callback()
            bindings[name] = 'pass'
            return result
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError, EOFError) as error:
            observations[name + '_error'] = str(error)[:256]
            return None

    def management():
        require(_ENTRY is not None and _ENTRY['package_root'] == str(PACKAGE_ROOT), 'not_called_by_verified_entry')
        require(sha(reader.read(str(PACKAGE_ROOT / 'administration.json'), limit=1024**2)) == _ENTRY['manifest_sha256'], 'administration_manifest_changed')
        require(sha(reader.read('/usr/bin/rescue-guard-admin', limit=65536)) == _ENTRY['entrypoint_sha256'], 'administration_entry_changed')
        return {key: _ENTRY[key] for key in ('manifest_sha256', 'entrypoint_sha256')}

    parts['administration'] = attempt('executing_management', management)
    if parts['administration'] is None:
        return {'subject': None, 'bindings': bindings, 'observations': observations}
    # Preload libc/crypto/DM dependencies before inspecting the process mapping set.
    hashlib.sha256(b'').digest()
    kernel = attempt('official_kernel_origin', lambda: kernel_evidence(reader, os.uname().release))
    if kernel is not None:
        parts['kernel'], observations['kernel'] = kernel
        bindings['running_kernel'] = bindings['loaded_modules'] = 'pass'
    parts['runtime'] = attempt('runtime_artifacts', lambda: runtime_evidence(PACKAGE_ROOT))
    dependencies = attempt('host_dependencies', lambda: host_dependencies(reader))
    if dependencies:
        parts['host_dependencies'], observations['dependencies'] = dependencies
    try:
        platform = dict(line.split('=', 1) for line in reader.read('/usr/lib/os-release', limit=8192).decode().splitlines() if '=' in line)
        require(platform['ID'].strip('"') == 'ubuntu' and platform['VERSION_ID'].strip('"') == '24.04', 'unsupported_platform')
        from support import architecture
        parts['platform'] = {'id': 'ubuntu', 'version_id': '24.04', 'architecture': architecture(os.uname().machine)}
    except (OSError, ValueError, KeyError) as error:
        observations['platform_error'] = str(error)[:256]
    subject = dict(schema=1, **parts) if len(parts) == 5 and all(value is not None for value in parts.values()) else None
    return {'subject': subject, 'bindings': bindings, 'observations': observations}
