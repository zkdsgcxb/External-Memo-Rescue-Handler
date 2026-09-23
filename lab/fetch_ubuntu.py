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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', default='20260911', help='Pinned Ubuntu noble image date')
    parser.add_argument('--keyring', type=Path, default=Path('/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg'))
    args = parser.parse_args()
    if not re.fullmatch(r'\d{8}', args.build):
        parser.error('build must be YYYYMMDD')
    if not args.keyring.is_file():
        parser.error('Ubuntu cloud image signing keyring is required')
    WORK.mkdir(parents=True, exist_ok=True)
    base = f'https://cloud-images.ubuntu.com/noble/{args.build}/'
    def fetch(remote, local):
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                        '--retry', '3', '--max-time', '600', '-o', str(local), base + remote], check=True)
    for name in ['SHA256SUMS', 'SHA256SUMS.gpg']:
        fetch(name, WORK / name)
    subprocess.run(['gpgv', '--keyring', str(args.keyring), str(WORK/'SHA256SUMS.gpg'),
                    str(WORK/'SHA256SUMS')], check=True)
    matches = [line.split()[0] for line in (WORK/'SHA256SUMS').read_text().splitlines()
               if line.split()[-1].lstrip('*') == NAME]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one rootfs checksum')
    archive = WORK/'root.tar.xz'
    if not archive.exists() or sha256(archive) != matches[0]:
        partial = WORK/'root.tar.xz.partial'
        if not partial.exists() or sha256(partial) != matches[0]:
            print('Downloading', base + NAME, flush=True)
            fetch(NAME, partial)
        if sha256(partial) != matches[0]:
            raise RuntimeError('Ubuntu rootfs checksum mismatch')
        partial.replace(archive)
    # QEMU presents whole sectors. Pad a separate copy, leaving the signed archive intact.
    disk = WORK/'rootfs.raw'
    with archive.open('rb') as src, disk.open('wb') as dst:
        shutil.copyfileobj(src, dst)
        dst.write(b'\0' * (-dst.tell() % 512))
    metadata = {'distribution':'Ubuntu Server 24.04 LTS', 'image_build':args.build,
                'url':base + NAME, 'archive_sha256':matches[0], 'seed_sha256':sha256(disk),
                'signed_checksums_sha256':sha256(WORK/'SHA256SUMS'), 'signature_verified':True}
    (WORK/'source.json').write_text(json.dumps(metadata, indent=2)+'\n')
    print('Verified Ubuntu rootfs ready:', disk)


if __name__ == '__main__':
    main()
