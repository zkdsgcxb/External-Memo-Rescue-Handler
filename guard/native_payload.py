"""Build and stage the native Guard's complete userspace library closure.

Staging operates only on a caller-provided image directory. An existing RAM
runtime is verified in place and never upgraded underneath a live controller.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from host_files import sha256
from trusted_paths import read_trusted_json, trusted_sha256

BASE = Path(__file__).resolve().parent
BINARY = Path('/opt/guard-runtime/guard-runtime')
MANIFEST = Path('/opt/guard-runtime/runtime.json')
ENTRYPOINT = Path('/opt/guard-runtime/maintain')
ENTRYPOINT_SCRIPT = '#!/bin/sh\nexec /opt/guard-runtime/guard-runtime maintain "$@"\n'


def dependency_packages(paths):
    """Record local dpkg provenance at build time, without network access.

    A locally built/unpackaged file stays explicitly unknown. Package names
    and versions describe the build input; they are not publisher signatures.
    """
    paths = sorted({str(Path(path)) for path in paths})
    aliases = {path: {path, str(Path(path).resolve())} for path in paths}
    for names in aliases.values():
        for name in list(names):
            if name.startswith(('/lib/', '/lib64/', '/bin/', '/sbin/')):
                names.add('/usr' + name)
            elif name.startswith(('/usr/lib/', '/usr/lib64/', '/usr/bin/', '/usr/sbin/')):
                names.add(name[4:])
    query = subprocess.run(['dpkg-query', '-S', *sorted(set().union(*aliases.values()))],
                           text=True, capture_output=True, timeout=15)
    owners = {}
    for line in query.stdout.splitlines():
        if ': ' in line:
            package, filename = line.split(': ', 1)
            if re.fullmatch(r'[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?', package):
                owners[filename] = package
    versions = {}
    if owners:
        result = subprocess.run(['dpkg-query', '-W', '-f', '${binary:Package}\t${Version}\t${Architecture}\n',
                                 *sorted(set(owners.values()))],
                                text=True, capture_output=True, timeout=15)
        for line in result.stdout.splitlines():
            fields = line.split('\t')
            if len(fields) == 3:
                versions[fields[0]] = {'package': fields[0], 'version': fields[1], 'architecture': fields[2]}
    return {path: next((versions[owners[name]] for name in sorted(names)
                        if name in owners and owners[name] in versions),
                       {'package': None, 'version': None, 'architecture': None})
            for path, names in aliases.items()}


def build_runtime(work, *, tests=True):
    source = BASE / 'native/build_runtime.py'
    spec = importlib.util.spec_from_file_location('rescue_native_builder', source)
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder.build(Path(work), tests=tests)


def _elf_identity(path):
    with Path(path).open('rb') as stream:
        header = stream.read(20)
    if len(header) != 20 or header[:4] != b'\x7fELF':
        raise ValueError('Native runtime and libraries must be ELF files: ' + str(path))
    # Class, byte order and machine distinguish x86_64/aarch64/riscv64 and
    # reject an installed compatibility library from a different ABI.
    return header[4:6], header[18:20]


def _linked_libraries(binary):
    from diagnostics import bounded_output
    output = bounded_output(['/usr/bin/ldd', str(binary)], allowed_returncodes=(0, 1))
    if 'not found' in output:
        raise RuntimeError('Native dependency is absent: ' + output)
    paths = re.findall(r'(?:=>\s+|^\s*)(/[^\s]+)', output, re.M)
    if not paths and not any(text in output for text in ('not a dynamic executable', 'statically linked')):
        raise RuntimeError('Cannot resolve native dependencies: ' + output)
    return {str(Path(path)): Path(path).resolve(strict=True) for path in paths}


def binary_closure(binary):
    """Inspect a trusted local executable, including its dlopen-only DM library."""
    binary = Path(binary)
    architecture = _elf_identity(binary)
    result = _linked_libraries(binary)
    cache = subprocess.check_output(['/sbin/ldconfig', '-p'], text=True)
    candidates = re.findall(r'^\s*libdevmapper\.so\.1\.02\.1\s+[^\n]*=>\s+(/\S+)', cache, re.M)
    matching = [Path(path) for path in candidates if _elf_identity(path) == architecture]
    if not matching:
        raise RuntimeError('No matching libdevmapper.so.1.02.1 for the native runtime ABI')
    mapper = matching[0]
    result[str(mapper)] = mapper.resolve(strict=True)
    result.update(_linked_libraries(mapper))
    if any(_elf_identity(path) != architecture for path in result.values()):
        raise RuntimeError('Native runtime dependency ABI differs')
    return dict(sorted(result.items()))


def _destination(root, absolute):
    absolute = Path(absolute)
    if not absolute.is_absolute() or '..' in absolute.parts:
        raise ValueError('Runtime manifest requires absolute, normalized paths')
    root = Path(root).resolve(strict=True)
    target = root / absolute.relative_to('/')
    if not target.resolve().is_relative_to(root):
        raise RuntimeError('Runtime path escapes the staged image: ' + str(absolute))
    return target


def reject_instrumented_runtime(closure):
    """Sanitizer helpers belong only to offline tests, never the RAM payload."""
    for name, source in closure.items():
        if any(re.match(r'lib(?:asan|ubsan)(?:\.|$)', Path(path).name)
               for path in (name, source)):
            raise RuntimeError('Sanitizer runtime cannot be staged for production: ' + str(name))


def stage_runtime(root, binary):
    """Copy an immutable executable and libraries into a new/offline image."""
    root, binary = Path(root), Path(binary)
    closure = binary_closure(binary)
    reject_instrumented_runtime(closure)
    files = {str(BINARY): binary, **closure}
    for path, source in files.items():
        target = _destination(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755)
    entrypoint = _destination(root, ENTRYPOINT)
    entrypoint.write_text(ENTRYPOINT_SCRIPT)
    entrypoint.chmod(0o755)
    manifest = {'schema': 1, 'runtime': 'cpp', 'binary_path': str(BINARY),
                'binary_sha256': sha256(binary),
                'entrypoint_path': str(ENTRYPOINT), 'entrypoint_sha256': sha256(entrypoint),
                'library_sha256': {path: sha256(source) for path, source in closure.items()},
                'payload_file_bytes': sum(source.stat().st_size for source in files.values()) + entrypoint.stat().st_size}
    provenance = dependency_packages(closure.values())
    manifest['dependency_packages'] = {path: provenance[str(source)] for path, source in closure.items()}
    output = _destination(root, MANIFEST)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    output.chmod(0o444)
    verify_runtime(root)
    return manifest


def verify_runtime(root):
    """Require a complete, unchanged native runtime before maintenance starts."""
    root = Path(root)
    binary, manifest_path = _destination(root, BINARY), _destination(root, MANIFEST)
    entrypoint = _destination(root, ENTRYPOINT)
    if not any(path.exists() or path.is_symlink() for path in (binary, manifest_path, entrypoint)):
        raise RuntimeError('Native Guard runtime is missing; build and validate a C++ protection image first')
    if any(path.is_symlink() or not path.is_file() for path in (binary, manifest_path, entrypoint)):
        raise RuntimeError('Native runtime is partial or uses unexpected symlinks')
    if manifest_path.stat().st_size > 65536:
        raise RuntimeError('Oversized native runtime manifest')
    # Offline builds are unprivileged. Runtime startup additionally requires
    # every resolved file and parent to be root-owned and non-writable.
    trusted = os.geteuid() == 0
    checksum = trusted_sha256 if trusted else sha256
    manifest = read_trusted_json(manifest_path) if trusted else json.loads(manifest_path.read_text())
    if (manifest.get('schema') != 1 or manifest.get('runtime') != 'cpp'
            or manifest.get('binary_path') != str(BINARY)
            or manifest.get('entrypoint_path') != str(ENTRYPOINT)
            or not isinstance(manifest.get('library_sha256'), dict)
            or not manifest['library_sha256']):
        raise RuntimeError('Unsupported native runtime manifest')
    if checksum(binary) != manifest.get('binary_sha256'):
        raise RuntimeError('Native runtime executable checksum differs')
    if checksum(entrypoint) != manifest.get('entrypoint_sha256') or not entrypoint.stat().st_mode & 0o111:
        raise RuntimeError('Native runtime entrypoint checksum or permission differs')
    for path, checksum in manifest['library_sha256'].items():
        if not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise RuntimeError('Invalid native library checksum')
        target = _destination(root, path)
        if not target.is_file() or (trusted_sha256(target.resolve(strict=True)) if trusted else sha256(target)) != checksum:
            raise RuntimeError('Native runtime library checksum differs: ' + path)
    if not binary.stat().st_mode & 0o111:
        raise RuntimeError('Native runtime executable permission is absent')
    return binary
