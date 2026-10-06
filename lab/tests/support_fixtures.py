"""Synthetic current bindings and independent qualification files; never real evidence."""
import hashlib
import json

import support
from admin.admission import digest


def sha(label):
    return hashlib.sha256(label.encode()).hexdigest()


def current(release='7.0.0-test'):
    subject = {'schema': 1, 'platform': {'id': 'ubuntu', 'version_id': '24.04', 'architecture': 'amd64'},
               'kernel': {'release': release, 'image_sha256': sha('kernel'), 'modules_manifest_sha256': sha('modules')},
               'administration': {'manifest_sha256': sha('admin'), 'entrypoint_sha256': sha('entry')},
               'runtime': {'manifest_sha256': sha('runtime'), 'archive_sha256': sha('archive'),
                           'binary_sha256': sha('elf'), 'libraries_manifest_sha256': sha('libraries')},
               'host_dependencies': {'manifest_sha256': sha('dependencies')}}
    return {'subject': subject, 'bindings': dict.fromkeys(support.BINDINGS, 'pass'), 'observations': {}}


def qualification(evidence, filesystems=('ext4',)):
    return {'schema': 1, 'purpose': support.PURPOSE, 'result': 'passed',
            'subject_sha256': digest(support.project(evidence['subject'])),
            'scope': {'profile': 'host-data', 'topology': support.TOPOLOGY, 'filesystems': list(filesystems)},
            'evidence': {name: sha(name) for name in ('prerequisite_report_sha256', 'origin_report_sha256',
                                                   'test_source_sha256', 'package_sha256')}}


def write(root, evidence, record=None):
    record = qualification(evidence) if record is None else record
    path = root / support.QUALIFICATIONS.lstrip('/') / (digest(support.project(evidence['subject'])) + '.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in path.parents:
        if parent == root:
            break
        parent.chmod(0o700)
    path.write_text(json.dumps(record, sort_keys=True) + '\n')
    path.chmod(0o600)
    return path
