#!/usr/bin/env python3
"""Cold-boot a disposable overlay to verify acknowledged fsync data from a run."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from run import WORK, qemu_command
from auto_run import wait_for
from fetch_ubuntu import sha256

MAX_AUDIT_BYTES = 64 * 1024 * 1024


def acknowledged_prefix(rows):
    """Hash the complete, contiguous ACK prefix with one record in memory."""
    if not rows:
        raise ValueError('Requires a nonempty stream without failed writes')
    expected_bytes = 0
    expected_sha = hashlib.sha256()
    for sequence, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or row.get('ok') is not True
                or type(row.get('seq')) is not int or row['seq'] != sequence):
            raise ValueError('Requires successful ACKs with contiguous sequence starting at 1')
        payload = (str(sequence) + '\n').encode() + b'x' * 4096
        expected_bytes += len(payload)
        if expected_bytes > MAX_AUDIT_BYTES:
            raise ValueError('ACK prefix exceeds the bounded audit size')
        expected_sha.update(payload)
    return expected_bytes, expected_sha.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report',type=Path)
    parser.add_argument('--build-dir',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()==0:
        parser.error('Run as a normal user')
    source=args.report.resolve()
    disk=source.parent/'usb.raw'
    if not source.is_relative_to(WORK.resolve()) or not disk.is_file() or disk.is_symlink():
        parser.error('Only a recorded experiment under lab/work is supported')
    source_bytes=source.read_bytes()
    report=json.loads(source_bytes)
    rows=[json.loads(line) for line in report['after']['workload'].splitlines()]
    try:
        expected_bytes,expected_sha=acknowledged_prefix(rows)
    except ValueError as exc:
        parser.error(str(exc))
    build_dir=args.build_dir.resolve()
    build=json.loads((build_dir/'build.json').read_text())
    for name,key in [('vmlinuz','kernel_sha256'),('initramfs.cpio.gz','initramfs_sha256')]:
        if sha256(build_dir/name)!=build[key]:
            parser.error('Audit build hash mismatch: '+name)
    source_image_sha=sha256(disk)
    folder=WORK/(time.strftime('%Y%m%d-%H%M%S')+'-cold-audit-'+str(os.getpid()))
    folder.mkdir(mode=0o700)
    overlay=folder/'usb.qcow2'
    subprocess.run(['qemu-img','create','-q','-f','qcow2','-F','raw','-b',str(disk),str(overlay)],check=True)
    (folder/'decoy.raw').touch()
    with (folder/'decoy.raw').open('r+b') as stream:
        stream.truncate(2*1024**3)
    command=qemu_command(folder,extra_kernel_args=f'ram_rescue_cold_audit=1 ram_rescue_audit_bytes={expected_bytes} ram_rescue_audit_sha={expected_sha}',
        kernel=build_dir/'vmlinuz',initramfs=build_dir/'initramfs.cpio.gz')
    for index,argument in enumerate(command):
        if argument=='-blockdev':
            device=json.loads(command[index+1])
            if device['node-name']=='usbdisk':
                device.update(driver='qcow2',file={'driver':'file','filename':str(overlay)},
                    backing={'driver':'raw','read-only':True,
                             'file':{'driver':'file','filename':str(disk)}})
                command[index+1]=json.dumps(device)
    result={'source_report':str(source.relative_to(WORK.parent.parent)),
        'source_report_sha256':hashlib.sha256(source_bytes).hexdigest(),
        'source_image':str(disk.relative_to(WORK.parent.parent)),
        'source_image_sha256_before':source_image_sha,
        'acknowledged_records':len(rows),'expected_bytes':expected_bytes,
        'expected_sha256':expected_sha,'build':build,
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'qemu_runner_sha256':sha256(Path(__file__).parent/'run.py'),
        'qemu_version':subprocess.check_output(['qemu-system-x86_64','--version'],text=True).splitlines()[0],
        'limitation':'QEMU guest cache independence, not physical USB power-loss durability'}
    (folder/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    with (folder/'qemu.log').open('w') as output:
        vm=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT)
        try:
            def finished():
                path=folder/'console.log'
                text=path.read_text(errors='replace') if path.exists() else ''
                for line in text.splitlines():
                    if line.startswith('COLD_AUDIT_RESULT='):
                        return json.loads(line.split('=',1)[1])
                if vm.poll() is not None or 'Kernel panic' in text:
                    raise RuntimeError('Cold audit guest failed to boot')
                return None
            result['audit']=wait_for(finished,90,'cold ACK read')
        except BaseException as exc:
            result['error']=repr(exc)
            raise
        finally:
            vm.terminate()
            try:
                vm.wait(timeout=10)
            except subprocess.TimeoutExpired:
                vm.kill();vm.wait()
            result['source_image_sha256_after']=sha256(disk)
            result['source_image_unchanged']=result['source_image_sha256_after']==source_image_sha
            result['passed']=(result.get('audit',{}).get('passed') is True
                              and result['source_image_unchanged'])
            (folder/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result['audit'],indent=2))
    print('Report:',folder/'report.json')
    if not result['passed']:
        raise SystemExit('Cold ACK audit failed')


if __name__=='__main__':
    main()
