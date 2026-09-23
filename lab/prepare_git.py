#!/usr/bin/env python3
"""Prepare an independent, read-only Linux v6.8 Git source disk for the guest."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

WORK = Path(__file__).resolve().parent/'work'
URL = 'https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-repo',type=Path,default=WORK/'linux-kernel-org.git')
    args=parser.parse_args()
    repo=args.source_repo.resolve()
    if not repo.exists():
        subprocess.run(['git','clone','--bare','--depth','1','--branch','v6.8',URL,str(repo)],check=True)
    def git(*args):
        return subprocess.check_output(['git','--git-dir='+str(repo),*args],text=True).strip()
    head = git('rev-parse','HEAD')
    if head != git('rev-parse','v6.8^{commit}') or git('remote','get-url','origin') not in [URL,'https://github.com/torvalds/linux.git']:
        raise RuntimeError('Unexpected Git source; expected Linux v6.8 from torvalds/linux')
    subprocess.run(['git','--git-dir='+str(repo),'fsck','--full'],check=True)
    staging = WORK/'git-seed'
    staging.mkdir(exist_ok=True)
    if (staging/'linux.git').exists():
        shutil.rmtree(staging/'linux.git')
    shutil.copytree(repo,staging/'linux.git')
    metadata = {'url':git('remote','get-url','origin'),'ref':'v6.8','commit':head,'history':'depth 1',
                'tracked_files':len(git('ls-tree','-r','--name-only','HEAD').splitlines()),
                'object_disk_bytes':sum(p.stat().st_size for p in (repo/'objects').rglob('*') if p.is_file())}
    (staging/'source.json').write_text(json.dumps(metadata,indent=2)+'\n')
    image = WORK/'git-source.raw'
    with image.open('wb') as stream:
        stream.truncate(1024**3)
    subprocess.run(['mkfs.ext4','-F','-q','-E','root_owner=0:0','-d',str(staging),str(image)],check=True)
    # Populate without host root. Give the guest's Git service ownership of the bare
    # repository directory inside the image, rather than disabling Git ownership checks.
    for field in ['uid','gid']:
        subprocess.run(['debugfs','-w','-R',f'set_inode_field /linux.git {field} 0',str(image)],check=True)
    stat=subprocess.check_output(['debugfs','-R','stat /linux.git',str(image)],text=True)
    if 'User:     0   Group:     0' not in stat:
        raise RuntimeError('Guest Git source ownership was not set')
    with image.open('rb') as stream:
        metadata['image_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    (WORK/'git-source.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print(json.dumps(metadata,indent=2))


if __name__=='__main__':
    main()
