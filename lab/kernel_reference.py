#!/usr/bin/env python3
"""Freeze an Ubuntu kernel reference from authenticated cached packages, offline.

Writes only a new lab/work directory. This is artifact preparation, not host
installation, a candidate qualification, or a claim about a running kernel.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / 'guard'), str(REPO / 'ram-rescue-demo/src')]
import current_support as current


def hash_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def freeze(output, release):
    output = Path(output).resolve()
    if not output.is_relative_to(REPO / 'lab/work') or output.exists() or os.geteuid() == 0:
        raise ValueError('Use a fresh lab/work directory without host root')
    output.mkdir(mode=0o700, parents=True)
    base = Path('/var/lib/apt/lists/cn.archive.ubuntu.com_ubuntu_dists_noble-updates_')
    inrelease = Path(str(base) + 'InRelease').read_bytes()
    packages = Path(str(base) + 'main_binary-amd64_Packages').read_bytes()
    index_sha, packages_sha = current.authenticate_index(inrelease, packages)
    selected = current.package_rows(packages, release)
    extracted = output / 'extracted'
    extracted.mkdir()
    archives = {}
    for name, row in selected.items():
        package = Path('/var/cache/apt/archives') / Path(row['Filename']).name
        if not package.is_file() or package.stat().st_size != int(row['Size']) or hash_file(package) != row['SHA256']:
            raise ValueError('Authenticated cached package unavailable or changed: ' + str(package))
        identity = subprocess.check_output(['dpkg-deb', '-f', str(package), 'Package', 'Version', 'Architecture'], text=True)
        if any(field + ': ' + row[field] not in identity for field in ('Package', 'Version', 'Architecture')):
            raise ValueError('Package control differs from authenticated index')
        subprocess.run(['dpkg-deb', '-x', str(package), str(extracted)], check=True)
        archives[name] = {'path': str(package), 'sha256': hash_file(package), 'size': package.stat().st_size}
    image = extracted / 'boot' / ('vmlinuz-' + release)
    family = '.'.join(release.split('-')[0].split('.')[:2])
    script = Path('/usr/src/linux-hwe-' + family + '-headers-' + release.removesuffix('-generic') + '/scripts/extract-vmlinux')
    if not script.is_file():
        raise ValueError('Existing extract-vmlinux helper absent: ' + str(script))
    # Ubuntu's helper hardcodes /tmp. Keep its disposable copy's temporary file
    # inside this experiment too, and record both helper digests below.
    helper = output / 'extract-vmlinux'
    helper.write_text(script.read_text().replace('mktemp /tmp/vmlinux-XXX', 'mktemp "$TMPDIR/vmlinux-XXX"'))
    elf = subprocess.check_output(['sh', str(helper), str(image)], env={**os.environ, 'TMPDIR': str(output)}, timeout=60)
    image_record = {'sha256': hash_file(image), 'build_id': current.elf_build_id(elf)}
    del elf
    module_root = extracted / 'usr/lib/modules' / release
    if not module_root.is_dir():
        module_root = extracted / 'lib/modules' / release
    builtins = {Path(line).name.removesuffix('.ko').replace('-', '_') for line in (module_root / 'modules.builtin').read_text().splitlines()}
    available = {path.name.split('.ko')[0].replace('-', '_'): path for path in (module_root / 'kernel').rglob('*.ko*')}
    records = {}
    def add(name):
        if name in records:
            return
        if name in builtins:
            records[name] = {'builtin': True, 'path': None, 'sha256': None, 'build_id': None, 'srcversion': None, 'depends': []}
            return
        path = available[name]
        def field(key):
            return subprocess.check_output(['modinfo', '-F', key, str(path)], text=True).strip()
        raw = subprocess.check_output(['zstd', '-dc', str(path)]) if path.suffix == '.zst' else path.read_bytes()
        dependencies = [item.replace('-', '_') for item in field('depends').split(',') if item]
        records[name] = {'builtin': False, 'path': '/usr/lib/modules/' + release + '/' + str(path.relative_to(module_root)),
                         'sha256': hash_file(path), 'build_id': current.elf_build_id(raw),
                         'srcversion': field('srcversion') or None, 'depends': sorted(dependencies)}
        for dependency in dependencies:
            add(dependency)
    for name in current.ROOT_MODULES:
        add(name)
    bundle = output / 'reference'
    bundle.mkdir()
    (bundle / 'InRelease').write_bytes(inrelease)
    (bundle / 'Packages').write_bytes(packages)
    shutil.copyfile(current.KEYRING, bundle / 'ubuntu-archive-keyring.gpg')
    source = {'schema': 1, 'release': release, 'image': image_record, 'modules': dict(sorted(records.items())),
              'metadata': {name: hash_file(module_root / name) for name in ('modules.builtin', 'modules.builtin.modinfo')},
              'packages': selected, 'origin': {'inrelease_sha256': index_sha, 'packages_sha256': packages_sha,
                                             'keyring_sha256': hash_file(current.KEYRING)},
              'generator_sha256': {str(Path(__file__).relative_to(REPO)): hash_file(__file__),
                                  'guard/current_support.py': hash_file(REPO / 'guard/current_support.py'),
                                  str(script): hash_file(script), 'staged_extract_vmlinux': hash_file(helper)}}
    (bundle / 'source.json').write_text(json.dumps(source, indent=2, sort_keys=True) + '\n')
    report = {'schema': 1, 'passed': True, 'scope': 'authenticated_package_extraction_only',
              'archive_inputs': archives, 'reference_sha256': hash_file(bundle / 'source.json'),
              'module_root': str(module_root), 'image': str(image),
              'candidate_qualification': False, 'running_kernel_verified': False}
    (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--release', required=True)
    args = parser.parse_args()
    freeze(args.output, args.release)
