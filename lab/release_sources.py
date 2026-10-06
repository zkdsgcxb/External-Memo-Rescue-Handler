#!/usr/bin/env python3
"""Collect notices and exact Ubuntu corresponding sources for a sealed runtime.

Downloads are inert files; no source is executed or installed. Launchpad is the
Ubuntu primary archive publisher. The signed dsc is retained and its SHA256
file list is checked against every downloaded source archive.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
import urllib.parse
import urllib.request


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def request(url):
    for attempt in range(4):
        try:
            return urllib.request.urlopen(url, timeout=45)
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2)


def fetch(source, output):
    name, version = source
    directory = output / 'sources' / name
    directory.mkdir(parents=True, exist_ok=True)
    query = urllib.parse.urlencode({'ws.op': 'getPublishedSources', 'source_name': name,
                                   'version': version, 'exact_match': 'true'})
    with request('https://api.launchpad.net/1.0/ubuntu/+archive/primary?' + query) as response:
        rows = json.load(response)['entries']
    matching = [row for row in rows if row['source_package_name'] == name and row['source_package_version'] == version]
    if not matching:
        raise RuntimeError('Source publication not found: ' + str(source))
    with request(matching[0]['self_link'] + '?ws.op=sourceFileUrls') as response:
        urls = json.load(response)
    files = []
    for url in urls:
        filename = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit('/', 1)[-1])
        if Path(filename).name != filename or not filename:
            raise ValueError('Invalid archive filename')
        target = directory / filename
        if not target.exists():
            partial = target.with_name(target.name + '.partial')
            with request(url) as response, partial.open('wb') as stream:
                shutil.copyfileobj(response, stream)
            partial.replace(target)
        files.append({'file': str(target.relative_to(output)), 'url': url,
                      'size': target.stat().st_size, 'sha256': sha(target)})
    descriptors = list(directory.glob('*.dsc'))
    if len(descriptors) != 1:
        raise RuntimeError('Expected one source descriptor')
    text = descriptors[0].read_text()
    if '\nSource: ' + name + '\n' not in text or '\nVersion: ' + version + '\n' not in text:
        raise RuntimeError('Source descriptor identity mismatch')
    block = text.split('\nChecksums-Sha256:\n', 1)[1]
    verified = set()
    for line in block.splitlines():
        if not line.startswith(' '):
            break
        expected, size, filename = line.split()
        target = directory / filename
        if not re.fullmatch('[0-9a-f]{64}', expected) or Path(filename).name != filename:
            raise RuntimeError('Invalid source descriptor file entry')
        if target.stat().st_size != int(size) or sha(target) != expected:
            raise RuntimeError('Corresponding source checksum mismatch: ' + filename)
        verified.add(filename)
    if not verified or {Path(row['file']).name for row in files} != verified | {descriptors[0].name}:
        raise RuntimeError('Source descriptor does not cover the entire download')
    print('Source complete:', name, version, flush=True)
    return {'source': name, 'version': version, 'publication': matching[0]['self_link'], 'files': files}


def collect(runtime, output):
    output.mkdir(parents=True, exist_ok=True)
    with tarfile.open(runtime) as archive:
        base = json.load(archive.extractfile('etc/rescue/base-runtime.json'))
        native = json.load(archive.extractfile('opt/guard-runtime/runtime.json'))
    packages = {row['package']: row for table in (base['dependency_packages'], native['dependency_packages'])
                for row in table.values()}
    if None in packages:
        raise RuntimeError('Unknown bundled package provenance')
    sources = set()
    notices = output / 'copyright'; notices.mkdir(exist_ok=True)
    shutil.copytree('/usr/share/common-licenses', output / 'common-licenses', dirs_exist_ok=True)
    inventory = []
    for name, expected in sorted(packages.items()):
        fields = subprocess.check_output(['/usr/bin/dpkg-query', '-W',
            '-f=${Version}\t${source:Package}\t${source:Version}', name], text=True).split('\t')
        if fields[0] != expected['version']:
            raise RuntimeError('Installed notice/source version differs from frozen runtime: ' + name)
        source, version = fields[1:]
        sources.add((source, version))
        notice = Path('/usr/share/doc') / name.split(':')[0] / 'copyright'
        target = notices / (name.replace(':', '_') + '.copyright')
        shutil.copyfile(notice, target)
        inventory.append({**expected, 'source_package': source, 'source_version': version,
                          'copyright': str(target.relative_to(output)), 'copyright_sha256': sha(target)})
    (output / 'packages.json').write_text(json.dumps(inventory, indent=2) + '\n')
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda source: fetch(source, output), sorted(sources)))
    (output / 'sources.json').write_text(json.dumps(records, indent=2) + '\n')
    return records


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    collect(args.runtime, args.output)
