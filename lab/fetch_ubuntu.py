#!/usr/bin/env python3
"""Fetch a signed Ubuntu Server rootfs; no host mounts or root privileges."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

WORK = Path(__file__).resolve().parent / 'work/ubuntu'
NAME = 'noble-server-cloudimg-amd64-root.tar.xz'


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch(work=WORK, build='20260911', keyring=Path('/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg')):
    """Verify Canonical's signed index before accepting or downloading a rootfs."""
    work, keyring = Path(work).resolve(), Path(keyring)
    if not re.fullmatch(r'\d{8}', build):
        raise ValueError('build must be YYYYMMDD')
    if not keyring.is_file():
        raise ValueError('Ubuntu cloud image signing keyring is required')
    work.mkdir(parents=True, exist_ok=True)
    base = f'https://cloud-images.ubuntu.com/noble/{build}/'
    def fetch(remote, local):
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                        '--retry', '3', '--max-time', '600', '-o', str(local), base + remote], check=True)
    for name in ['SHA256SUMS', 'SHA256SUMS.gpg']:
        fetch(name, work / name)
    subprocess.run(['gpgv', '--keyring', str(keyring), str(work/'SHA256SUMS.gpg'),
                    str(work/'SHA256SUMS')], check=True)
    matches = [line.split()[0] for line in (work/'SHA256SUMS').read_text().splitlines()
               if line.split()[-1].lstrip('*') == NAME]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one rootfs checksum')
    archive = work/'root.tar.xz'
    if not archive.exists() or sha256(archive) != matches[0]:
        partial = work/'root.tar.xz.partial'
        if not partial.exists() or sha256(partial) != matches[0]:
            print('Downloading', base + NAME, flush=True)
            fetch(NAME, partial)
        if sha256(partial) != matches[0]:
            raise RuntimeError('Ubuntu rootfs checksum mismatch')
        partial.replace(archive)
    # QEMU presents whole sectors. Pad a separate copy, leaving the signed archive intact.
    disk = work/'rootfs.raw'
    with archive.open('rb') as src, disk.open('wb') as dst:
        shutil.copyfileobj(src, dst)
        dst.write(b'\0' * (-dst.tell() % 512))
    metadata = {'distribution':'Ubuntu Server 24.04 LTS', 'image_build':build,
                'url':base + NAME, 'archive_sha256':matches[0], 'seed_sha256':sha256(disk),
                'signed_checksums_sha256':sha256(work/'SHA256SUMS'), 'signature_verified':True}
    (work/'source.json').write_text(json.dumps(metadata, indent=2)+'\n')
    print('Verified Ubuntu rootfs ready:', disk)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', default='20260911', help='Pinned Ubuntu noble image date')
    parser.add_argument('--keyring', type=Path, default=Path('/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg'))
    parser.add_argument('--work-dir', type=Path, default=WORK)
    args = parser.parse_args()
    if not args.work_dir.resolve().is_relative_to(WORK.parent.resolve()):
        parser.error('work-dir must be below lab/work')
    fetch(args.work_dir, args.build, args.keyring)


if __name__ == '__main__':
    main()
