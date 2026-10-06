"""Static candidate prerequisites, separate from local admission and release approval.

A trusted local qualification does not establish publisher authenticity or prove
which kernel/artifacts are currently executing. Those bindings need independent
current evidence. There is no CLI override or production success record here.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

from admin.admission import digest
from admin.identity import FILESYSTEM_TYPES
from diagnostics import Reader

TARGET = {'distribution': 'Ubuntu', 'version': '24.04', 'architecture': 'amd64',
          'kernel_route': 'official_hwe', 'acceptance': 'per_combination'}
QUALIFICATIONS = '/usr/share/ram-rescue-handler/qualifications'
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
QUALIFICATION_LIMIT = 16 * 1024
CURRENT_LIMIT = 64 * 1024
PURPOSE = 'candidate_preparation_prerequisites'
TOPOLOGY = 'existing_single_path_dm'
ELIGIBLE = 'eligible_for_candidate_preparation'
BINDINGS = ('executing_management', 'running_kernel', 'loaded_modules',
            'official_kernel_origin', 'runtime_artifacts', 'host_dependencies')


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names), 'invalid_fields')


def checksum(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'invalid_sha256')
    return value


def architecture(value):
    """The sole amd64/x86_64 alias boundary; no other architecture is promised."""
    require(value in ('amd64', 'x86_64'), 'unsupported_architecture')
    return 'amd64'


def project(value):
    """Strict static identity only; local plan inputs never enter this subject."""
    fields(value, ('schema', 'platform', 'kernel', 'administration', 'runtime', 'host_dependencies'))
    require(type(value['schema']) is int and value['schema'] == 1, 'invalid_schema')
    platform = value['platform']
    fields(platform, ('id', 'version_id', 'architecture'))
    require(platform['id'] == 'ubuntu' and platform['version_id'] == '24.04', 'unsupported_platform')
    result = {'schema': 1, 'platform': {'id': 'ubuntu', 'version_id': '24.04',
                                      'architecture': architecture(platform['architecture'])}}
    parts = {'kernel': ('release', 'image_sha256', 'modules_manifest_sha256'),
             'administration': ('manifest_sha256', 'entrypoint_sha256'),
             'runtime': ('manifest_sha256', 'archive_sha256', 'binary_sha256', 'libraries_manifest_sha256'),
             'host_dependencies': ('manifest_sha256',)}
    for part, names in parts.items():
        fields(value[part], names)
        result[part] = {}
        for name in names:
            item = value[part][name]
            if part == 'kernel' and name == 'release':
                require(isinstance(item, str) and re.fullmatch('[A-Za-z0-9._+-]{1,128}', item), 'invalid_release')
                result[part][name] = item
            else:
                result[part][name] = checksum(item)
    return result


def current_evidence(reader=None):
    """Collect actual bindings independently of qualification records."""
    from current_support import collect
    return collect(reader)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_field')
            result[key] = value
        return result

    def constant(_):
        raise ValueError('nonfinite_json')

    return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def validate_qualification(value):
    fields(value, ('schema', 'purpose', 'result', 'subject_sha256', 'scope', 'evidence'))
    require(type(value['schema']) is int and value['schema'] == 1, 'invalid_schema')
    require(value['purpose'] == PURPOSE, 'invalid_purpose')
    require(value['result'] == 'passed', 'prerequisites_not_passed')
    checksum(value['subject_sha256'])
    scope = value['scope']
    fields(scope, ('profile', 'topology', 'filesystems'))
    require(scope['profile'] == 'host-data' and scope['topology'] == TOPOLOGY, 'invalid_scope')
    filesystems = scope['filesystems']
    require(isinstance(filesystems, list) and 0 < len(filesystems) <= len(FILESYSTEM_TYPES)
            and all(isinstance(item, str) and item in FILESYSTEM_TYPES for item in filesystems)
            and len(set(filesystems)) == len(filesystems), 'invalid_filesystems')
    evidence = value['evidence']
    fields(evidence, ('prerequisite_report_sha256', 'origin_report_sha256', 'test_source_sha256', 'package_sha256'))
    for item in evidence.values():
        checksum(item)
    return value


def load_qualification(subject_sha256, reader=None):
    """One bounded trusted read: parse and hash the same bytes, including whitespace.

    Reader pins all parent directories, rejects links (including hard links),
    nonregular files, wrong owners and group/other writes. Root ownership only
    establishes local trust. Evidence hashes are references, not signatures.
    """
    path = QUALIFICATIONS + '/' + checksum(subject_sha256) + '.json'
    result = {'state': 'not_checked', 'sha256': None, 'record': None}
    try:
        raw = (reader or Reader()).read(path, limit=QUALIFICATION_LIMIT)
    except FileNotFoundError:
        result['state'] = 'absent'
        return result
    except (OSError, ValueError):
        result['state'] = 'unreadable'
        return result
    result['sha256'] = hashlib.sha256(raw).hexdigest()
    try:
        record = validate_qualification(strict_json(raw))
        require(record['subject_sha256'] == subject_sha256, 'subject_mismatch')
    except (ValueError, TypeError, RecursionError):
        result['state'] = 'invalid'
        return result
    result.update(state='valid', record=record)
    return result


def sample(*, current=None, reader=None):
    """Current evidence and historical prerequisite record are independent inputs.

    Injection is internal for fixtures, never a user-selectable file/provider.
    No registry, mount, boot ID or device identity is projected into the subject.
    """
    result = {'subject': None, 'subject_sha256': None, 'current_sha256': None,
              'observations': {},
              'bindings': dict.fromkeys(BINDINGS, 'unknown'), 'issues': [],
              'qualification': {'state': 'not_checked', 'sha256': None, 'record': None}}
    try:
        evidence = (current or current_evidence)()
        fields(evidence, ('subject', 'bindings', 'observations'))
        fields(evidence['bindings'], BINDINGS)
        require(all(item in ('pass', 'unknown', 'fail') for item in evidence['bindings'].values()), 'invalid_bindings')
        require(isinstance(evidence['observations'], dict), 'invalid_observations')
        require(len(json.dumps(evidence, allow_nan=False).encode()) <= CURRENT_LIMIT, 'current_evidence_limit')
        subject = project(evidence['subject']) if evidence['subject'] is not None else None
        result['current_sha256'] = digest(dict(evidence, subject=subject))
        result['observations'] = deepcopy(evidence['observations'])
        result['subject'] = subject
        result['bindings'] = deepcopy(evidence['bindings'])
        if subject is None:
            result['issues'].append('static_subject_incomplete')
        else:
            result['subject_sha256'] = digest(subject)
            result['qualification'] = load_qualification(result['subject_sha256'], reader)
    except (OSError, ValueError, TypeError, RecursionError):
        result['issues'].append('current_evidence_invalid')
    return result


def evaluate(before, after, filesystem, *, profile='host-data', topology=TOPOLOGY):
    reasons = list(before['issues'])
    reasons.extend('current_' + key + '_' + value for key, value in before['bindings'].items() if value != 'pass')
    qualification = before['qualification']
    if qualification['state'] != 'valid':
        reasons.append('qualification_' + qualification['state'])
    else:
        scope = qualification['record']['scope']
        if (scope['profile'] != profile or scope['topology'] != topology or filesystem not in scope['filesystems']):
            reasons.append('qualification_scope_mismatch')
    if before != after:
        reasons.append('support_inputs_changed')
    return {'product_target': deepcopy(TARGET), 'target_state': 'decided',
            'combination': {'state': ELIGIBLE if not reasons else 'unvalidated',
                            'subject': before['subject'], 'subject_sha256': before['subject_sha256'],
                            'current_bindings': before['bindings'], 'qualification': qualification,
                            'current_observations': before['observations'],
                            'reasons': sorted(set(reasons)),
                            'authorization': 'save_candidate_only' if not reasons else None,
                            'publisher_authentication': 'not_established_by_local_ownership',
                            'release_acceptance': 'not_evaluated', 'recovery_acceptance': 'not_evaluated'}}
