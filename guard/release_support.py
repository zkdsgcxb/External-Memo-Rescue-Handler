"""External release acceptance, never inferred from candidate-only qualification.

Acceptance is distributed separately after testing the sealed package. Root
ownership protects the installed record; the operator authenticates its source.
"""
import hashlib
import json
from pathlib import Path

from trusted_paths import read_trusted_json
from version import VERSION

ACCEPTANCE = Path('/usr/share/ram-rescue-handler/releases') / (VERSION + '.json')


def subject():
    from current_support import collect
    from support import BINDINGS, project
    evidence = collect()
    if any(evidence['bindings'].get(key) != 'pass' for key in BINDINGS) or evidence['subject'] is None:
        raise RuntimeError('Current release bindings are incomplete: ' + json.dumps(evidence['observations']))
    return project(evidence['subject'])


def require_supported():
    value = read_trusted_json(ACCEPTANCE)
    if (value.get('schema') != 1 or value.get('version') != VERSION
            or value.get('purpose') != 'data_activation_release_acceptance'
            or value.get('result') != 'passed' or value.get('filesystems') != ['ext4']
            or not isinstance(value.get('subjects'), list) or not value['subjects']
            or len(value['subjects']) > 16):
        raise RuntimeError('No valid activation release acceptance is installed')
    current = subject()
    if current['platform'] != {'id': 'ubuntu', 'version_id': '24.04', 'architecture': 'amd64'} or current not in value['subjects']:
        raise RuntimeError('This kernel/package combination has not passed release acceptance')
    evidence = value.get('evidence_sha256', '')
    if len(evidence) != 64 or any(c not in '0123456789abcdef' for c in evidence):
        raise RuntimeError('Release acceptance has no bounded evidence reference')
    return hashlib.sha256(json.dumps(current, sort_keys=True).encode()).hexdigest()
