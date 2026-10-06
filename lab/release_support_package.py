#!/usr/bin/env python3
"""Build an inert support-data package after sealed-package VM acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(report_path, reference, output):
    report = json.loads(report_path.read_text())
    if (report.get('passed') is not True or report.get('host_deployed') is not False
            or report.get('scope') != 'public_data_lifecycle_four_boot_vm'
            or report.get('seed_readonly_backing_stat_unchanged') is not True):
        raise ValueError('A completed isolated release lifecycle report is required')
    actions = {row['action'] for row in report['actions']}
    if not {'prepare', 'lifecycle_checks', 'finish_workload', 'reboot_disabled',
            'reboot_enabled', 'uninstall_reinstall'} <= actions:
        raise ValueError('The report does not cover the complete lifecycle')
    subject = report['subject']
    source = json.loads((reference / 'reference/source.json').read_text())
    if subject['kernel']['image_sha256'] != source['image']['sha256']:
        raise ValueError('Kernel reference differs from the accepted subject')
    output.mkdir(parents=True, exist_ok=False)
    acceptance = {'schema': 1, 'version': '0.0.1-beta',
                  'purpose': 'data_activation_release_acceptance', 'result': 'passed',
                  'filesystems': ['ext4'], 'subjects': [subject], 'evidence_sha256': sha(report_path),
                  'package_sha256': report['package']['sha256'],
                  'validation_scope': 'isolated_QEMU_software_acceptance_no_physical_hardware_claim'}
    (output / 'acceptance.json').write_text(json.dumps(acceptance, sort_keys=True, indent=2) + '\n')
    stage = output / 'package'
    release_dir = stage / 'usr/share/ram-rescue-handler/releases'
    release_dir.mkdir(parents=True)
    shutil.copyfile(output / 'acceptance.json', release_dir / '0.0.1-beta.json')
    kernel_dir = stage / 'usr/share/ram-rescue-handler/kernels' / source['release']
    kernel_dir.mkdir(parents=True)
    for name in ('source.json', 'InRelease', 'Packages'):
        shutil.copyfile(reference / 'reference' / name, kernel_dir / name)
    documents = stage / 'usr/share/doc/ram-rescue-handler-support'
    documents.mkdir(parents=True)
    shutil.copyfile(report_path, documents / 'acceptance-report.json')
    (documents / 'copyright').write_text('Support metadata: 0BSD, copyright 2026 zkdsgcxb.\n'
        'Ubuntu archive metadata and signatures retain their original notices.\n'
        'This package contains no kernel or module binaries.\n')
    control = stage / 'DEBIAN'; control.mkdir()
    package_version = '0.0.1~beta+' + report['package']['administration_version']
    (control / 'control').write_text('Package: ram-rescue-handler-support\nVersion: ' + package_version + '\n'
        'Architecture: all\nMaintainer: External-Memo-Rescue-Handler contributors\n'
        'Depends: ram-rescue-handler (= ' + package_version + '), ubuntu-keyring, gpgv\n'
        'Section: admin\nPriority: optional\n'
        'Description: Exact-combination acceptance and Ubuntu kernel reference metadata\n'
        ' Installation does not activate protection or install a kernel.\n')
    for path in [stage, *stage.rglob('*')]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    package = output / ('ram-rescue-handler-support_' + package_version + '_all.deb')
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(package)], check=True)
    record = {'package': package.name, 'sha256': sha(package), 'acceptance_sha256': sha(output / 'acceptance.json'),
              'evidence_sha256': sha(report_path), 'automatic_activation': False}
    (output / 'package.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--reference-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.report, args.reference_dir, args.output)
