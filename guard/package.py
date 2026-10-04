#!/usr/bin/env python3
"""Build an offline Debian package without installation or maintainer scripts.

The operator must establish the source/package's authenticity before dpkg
installation. The root-owned entry subsequently enforces local integrity;
an adjacent SHA256 file alone is not publisher authentication.
"""
import argparse
import hashlib
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

from host_files import sha256
from native_payload import stage_runtime

REPO = Path(__file__).resolve().parent.parent


def source_files():
    roots = (REPO / 'guard', REPO / 'ram-rescue-demo/src')
    paths = [p for root in roots for p in root.rglob('*')
             if p.is_file() and '__pycache__' not in p.parts and not p.is_symlink()]
    paths.extend(REPO / 'ram-rescue-demo' / name for name in ('build.py', 'session_payload.py', 'install.py'))
    return sorted(paths)


def runtime_package(base, binary, destination):
    base = Path(base)
    details = json.loads((base / 'manifest.json').read_text())
    if sha256(base / 'rescue-root.tar.gz') != details['sha256']:
        raise RuntimeError('Base tools package checksum differs')
    root = destination.parent / 'runtime-staging'
    root.mkdir()
    with tarfile.open(base / 'rescue-root.tar.gz', 'r:gz') as archive:
        archive.extractall(root, filter='data')
    (root / 'etc/rescue/base-source.json').write_text(json.dumps(
        {'schema': 1, 'sha256': details['sha256']}) + '\n')
    # Data maintenance does not start a rescue login or carry a disk identity.
    for name in ('identity.json', 'enrollment.json'):
        (root / 'etc/rescue' / name).unlink(missing_ok=True)
    (root / 'etc/shadow').write_text('root:!:20000:0:99999:7:::\nrescue:!:20000:0:99999:7:::\n')
    (root / 'etc/shadow').chmod(0o600)
    native = stage_runtime(root, binary)
    for path in [root, *root.rglob('*')]:
        if path.is_symlink():
            continue
        path.chmod((0o1777 if path == root / 'tmp' else 0o755) if path.is_dir()
                   else path.stat().st_mode & 0o755)
    destination.mkdir()
    archive_path = destination / 'tools.tar.gz'
    with archive_path.open('wb') as compressed, gzip.GzipFile(
            filename='', fileobj=compressed, mode='wb', compresslevel=3, mtime=0) as stream, \
            tarfile.open(fileobj=stream, mode='w') as archive:
        for path in sorted(root.rglob('*')):
            info = archive.gettarinfo(path, str(path.relative_to(root)))
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            info.mtime = 0
            if info.isfile():
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    manifest = {'schema': 1, 'kind': 'data-runtime', 'kernel_release': os.uname().release,
                'archive_sha256': sha256(archive_path), 'binary_sha256': native['binary_sha256'],
                'native_runtime': native, 'base_sha256': details['sha256']}
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    shutil.rmtree(root)
    return manifest


def build(output, *, base_rescue_dir, native_binary):
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    content = {str(p.relative_to(REPO)): p.read_bytes() for p in source_files()}
    digest = hashlib.sha256()
    for name, data in content.items():
        digest.update(name.encode() + b'\0' + data + b'\0')
    staging = output / 'package'
    package_root = staging / 'usr/lib/ram-rescue-handler/pending'
    package_root.mkdir(parents=True)
    for name, data in content.items():
        path = package_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o644)
    manifest = {'schema': 1, 'files': {p: hashlib.sha256(data).hexdigest() for p, data in content.items()}}
    (package_root / 'administration.json').write_text(json.dumps(manifest, indent=2) + '\n')
    runtime = runtime_package(base_rescue_dir, native_binary, package_root / 'runtime')
    digest.update(runtime['archive_sha256'].encode())
    version = digest.hexdigest()[:24]
    completed = package_root.with_name(version)
    package_root.rename(completed)
    package_root = completed
    entry = staging / 'usr/bin/rescue-guard-admin'
    entry.parent.mkdir(parents=True)
    entry.write_text((REPO / 'guard/admin_entry.py').read_text().replace(
        '/usr/lib/ram-rescue-handler/PACKAGE_VERSION', '/usr/lib/ram-rescue-handler/' + version))
    entry.chmod(0o755)
    control = staging / 'DEBIAN'
    control.mkdir()
    architecture = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    (control / 'control').write_text(
        f'Package: ram-rescue-handler\nVersion: 0.3.0+{version}\nArchitecture: {architecture}\n'
        'Maintainer: External-Memo-Rescue-Handler contributors\n'
        'Depends: python3 (>= 3.12), systemd, udev, lvm2, dmsetup, util-linux\n'
        'Section: admin\nPriority: optional\n'
        'Description: Offline administration and RAM tools for enrolled USB DM mappings\n'
        ' No maintainer scripts, automatic activation or disk enrollment.\n')
    for path in [staging, *staging.rglob('*')]:
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or path == entry else 0o644)
    package = output / ('ram-rescue-handler_' + version + '_' + architecture + '.deb')
    epoch = subprocess.check_output(['git', 'show', '-s', '--format=%ct', 'HEAD'], cwd=REPO, text=True).strip()
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(staging), str(package)],
                   env={**os.environ, 'SOURCE_DATE_EPOCH': epoch}, check=True)
    record = {'schema': 1, 'package': package.name, 'sha256': sha256(package),
              'administration_version': version, 'runtime': runtime,
              'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
              'installed': False, 'maintainer_scripts': False}
    (output / 'package.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'package': str(package), 'sha256': record['sha256'], 'installed': False}))
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-rescue-dir', type=Path, required=True)
    parser.add_argument('--native-binary', type=Path, required=True)
    args = parser.parse_args()
    build(args.output, base_rescue_dir=args.base_rescue_dir, native_binary=args.native_binary)
