#!/usr/bin/python3
"""Start an enrolled existing map under the shared Guard's single owner.

systemd invokes this RAM entrypoint once for a registered DM device. It does
not create maps, mount filesystems, or reinterpret a returning disk as a new
enrollment. The invocation receipt keeps ExecStopPost from taking over an
older controller when this service never acquired that controller's fence.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from admission import readonly
from guard_state import atomic_json, digest, load_json
import path_guard
from registry import current_profile, record_from_profile, validate_record


RECEIPT = 'manager-invocation.json'


def invocation_id():
    value = os.environ.get('INVOCATION_ID', '')
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise RuntimeError('Maintenance must run as its systemd service invocation')
    return value


def read_record(path):
    record = load_json(path)
    validate_record(record)
    if record['guard']['profile'] != 'host-data':
        raise RuntimeError('The enrolled root map is already maintained by its boot service')
    return record


def configure(record):
    config = record['guard']
    path_guard.configure(config)
    path_guard.validate_environment(config)
    return Path(config['identity_path']).parent, Path(config['run_dir'])


def start(record, invocation):
    directory, run = configure(record)
    for location in (directory.parent, directory, run):
        if location.is_symlink():
            raise RuntimeError('Maintenance state directory must not be a symlink')
        location.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path_guard.acquire_owner(False) as owner:
        if (run / 'path-transaction.json').exists():
            raise RuntimeError('Existing transaction requires takeover, not owner restart')
        runner = lambda args, timeout=3: readonly(args, timeout, owner_fd=owner.fd)
        profile = current_profile(record, runner=runner)
        if record_from_profile(profile) != record:
            raise RuntimeError('Fresh device observations changed the registered policy')
        config = profile['guard']
        for filename in ('identity.json', 'config.json', RECEIPT):
            location = (run if filename == RECEIPT else directory) / filename
            if location.is_symlink():
                raise RuntimeError('Maintenance state file must not be a symlink')
        atomic_json(directory / 'identity.json', profile['identity'])
        atomic_json(directory / 'config.json', config)
        atomic_json(run / RECEIPT, {'schema': 1, 'invocation_id': invocation,
                                   'owner_epoch': owner.epoch, 'config_digest': digest(config)})
        path_guard.configure(config)
        # Keep this exact Owner, including all inherited helper descriptors,
        # from first admission through the shared restoration event loop.
        path_guard.run_owned(config, owner)


def takeover(record, invocation):
    directory, run = configure(record)
    receipt_path = run / RECEIPT
    journal_path = run / 'path-transaction.json'
    if not receipt_path.exists():
        return
    receipt = load_json(receipt_path)
    if receipt.get('invocation_id') != invocation:
        # ExecStart may have refused an older active owner. This invocation
        # gained no authority over that owner's map, even when its lock clears.
        return
    if not journal_path.exists():
        return
    if receipt.get('schema') != 1:
        raise RuntimeError('Untrusted maintenance invocation receipt')
    journal = load_json(journal_path)
    if journal.get('owner_epoch') != receipt.get('owner_epoch'):
        raise RuntimeError('Transaction belongs to another maintenance owner')
    with path_guard.acquire_owner(True) as owner:
        # Recheck after waiting for an inherited helper's fence to close.
        if load_json(receipt_path) != receipt:
            raise RuntimeError('Maintenance invocation changed while waiting for its fence')
        if load_json(journal_path).get('owner_epoch') != receipt['owner_epoch']:
            raise RuntimeError('Transaction owner changed while waiting for its fence')
        config = load_json(directory / 'config.json')
        identity = load_json(directory / 'identity.json')
        profile = {'schema': 1, 'identity': identity, 'guard': config}
        if (record_from_profile(profile) != record or
                digest(config) != receipt.get('config_digest')):
            raise RuntimeError('Maintenance runtime differs from its registered invocation')
        path_guard.configure(config)
        path_guard.run_owned(config, owner, taking_over=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', required=True, type=Path)
    parser.add_argument('--takeover', action='store_true')
    args = parser.parse_args(argv)
    try:
        record = read_record(args.record)
        operation = takeover if args.takeover else start
        operation(record, invocation_id())
    except Exception as exc:
        # Do not overwrite another controller's evidence after a refused lock.
        # systemd records this result without requiring another RAM state store.
        print(json.dumps({'state': 'blocked', 'reason': str(exc),
                          'outcome': 'maintenance_entry_refused'}), file=sys.stderr)
        raise


if __name__ == '__main__':
    main()
