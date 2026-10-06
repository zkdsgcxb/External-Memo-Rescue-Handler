"""Private, inert candidate receipts. Call mutations under manage's control lock.

Nothing consumes this store to activate protection. No arbitrary paths, commands,
recursive deletion, or repair of unknown files are accepted. Directory descriptors
pin writes/unlinks; ownership and hashes are rechecked before each mutation.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from admin.admission import digest
from admin.registry import validate_record
from host_files import atomic
from trusted_paths import open_directory, read_descriptor
import support

BASE = Path('/var/lib/ram-rescue-plans')
LIMIT = 1024 * 1024
MAX_ACTIVE, MAX_HISTORY = 16, 256
FILES = {'operation.json', 'manifest.json', 'plan.json', 'candidate.json'}
PAYLOADS = {'plan.json', 'candidate.json'}
STATES = {'preparing', 'prepared', 'failed_needs_review', 'cancelled'}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def expected_digest(value):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value), 'invalid_plan_digest')
    return value


def checked_plan(plan, expected):
    expected_digest(expected)
    require(plan['plan_digest'] == expected == digest(plan['confirmation']), 'plan_changed')
    confirmation = plan['confirmation']
    require(plan['status'] == 'ready' and not confirmation['blockers']
            and confirmation['support_policy']['combination']['state'] == support.ELIGIBLE
            and confirmation['support_policy']['combination']['authorization'] == 'save_candidate_only', 'plan_blocked')
    effects = confirmation['effects']
    require(effects['action'] == 'save_candidate_only' and effects['activation'] == 'none'
            and effects['reboot_activates'] is False, 'invalid_candidate_effects')
    record = effects['candidate_record']
    validate_record(record)
    require(record['guard']['profile'] == 'host-data', 'data_candidate_only')
    return confirmation


class Store:
    def __init__(self, base=BASE, *, uid=0, anchor=Path('/')):
        # Custom roots/owners are an internal fixture seam, never CLI arguments.
        self.base, self.uid, self.anchor = Path(base), uid, Path(anchor)

    @contextmanager
    def directory(self, path):
        for parent in path.parents:
            if parent == self.base or parent == self.base / 'operations':
                ancestor = open_directory(parent, uid=self.uid, anchor=self.anchor)
                try:
                    require(not os.fstat(ancestor).st_mode & 0o077, 'candidate_directory_not_private')
                finally:
                    os.close(ancestor)
        fd = open_directory(path, uid=self.uid, anchor=self.anchor)
        try:
            require(not os.fstat(fd).st_mode & 0o077, 'candidate_directory_not_private')
            yield fd
        finally:
            os.close(fd)

    def names(self, fd, limit):
        result = []
        with os.scandir(fd) as entries:
            for entry in entries:
                require(len(result) < limit, 'candidate_count_limit')
                result.append(entry.name)
        return sorted(result)

    def read(self, fd, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid
                    and not info.st_mode & 0o077 and info.st_nlink == 1, 'untrusted_candidate_file')
            return read_descriptor(descriptor, limit=LIMIT)
        finally:
            os.close(descriptor)

    def replace(self, fd, name, data, previous=None):
        try:
            actual = self.read(fd, name)
        except FileNotFoundError:
            actual = None
        require(actual == previous, 'candidate_changed')
        # /proc/self/fd names the descriptor we already verified, not a caller path.
        atomic(Path('/proc/self/fd') / str(fd) / name, data)

    def initialize(self):
        parent = open_directory(self.base.parent, uid=self.uid, anchor=self.anchor)
        try:
            try:
                os.mkdir(self.base.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            os.fsync(parent)
        finally:
            os.close(parent)
        with self.directory(self.base) as fd:
            require(set(self.names(fd, 2)) <= {'operations'}, 'unknown_candidate_store_member')
            try:
                os.mkdir('operations', 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            os.fsync(fd)
        with self.directory(self.base / 'operations'):
            pass

    def path(self, operation):
        require(isinstance(operation, str) and re.fullmatch('[0-9a-f]{32}', operation), 'invalid_operation_id')
        return self.base / 'operations' / operation

    def load(self, operation):
        with self.directory(self.path(operation)) as fd:
            names = set(self.names(fd, len(FILES) + 1))
            require(names <= FILES and 'operation.json' in names, 'candidate_members_need_review')
            raw = {name: self.read(fd, name) for name in names}
        require(sum(map(len, raw.values())) <= LIMIT, 'candidate_size_limit')
        receipt = json.loads(raw['operation.json'])
        require(isinstance(receipt, dict)
                and set(receipt) == {'schema', 'id', 'plan_digest', 'state', 'cleanup_pending',
                                     'reason', 'manifest', 'selected_object'}
                and receipt.get('schema') == 1 and receipt.get('id') == operation
                and receipt.get('state') in STATES and isinstance(receipt.get('cleanup_pending'), bool),
                'invalid_candidate_receipt')
        require(not receipt['cleanup_pending'] or receipt['state'] == 'cancelled', 'invalid_candidate_receipt')
        require(receipt['reason'] is None or (isinstance(receipt['reason'], str)
                                             and len(receipt['reason']) <= 128), 'invalid_candidate_receipt')
        require(isinstance(receipt.get('selected_object'), dict)
                and set(receipt['selected_object']) == {'map_name', 'map_uuid'}
                and all(isinstance(value, str) and 1 <= len(value) <= 128
                        for value in receipt['selected_object'].values()), 'invalid_candidate_receipt')
        expected_digest(receipt['plan_digest'])
        manifest = receipt['manifest']
        require(isinstance(manifest, dict) and set(manifest) == {'schema', 'plan_digest', 'files'}
                and manifest.get('schema') == 1
                and manifest.get('plan_digest') == receipt['plan_digest']
                and isinstance(manifest.get('files'), dict) and set(manifest['files']) == PAYLOADS,
                'invalid_candidate_manifest')
        for value in manifest['files'].values():
            expected_digest(value)
        if 'manifest.json' in raw:
            require(raw['manifest.json'] == encode(manifest), 'candidate_changed')
        for name in PAYLOADS & names:
            require(sha(raw[name]) == manifest['files'][name], 'candidate_changed')
        if 'plan.json' in raw:
            plan = json.loads(raw['plan.json'])
            require(digest(plan) == receipt['plan_digest']
                    and plan['selected_object'] == receipt['selected_object']
                    and sha(encode(plan['effects']['candidate_record'])) == manifest['files']['candidate.json'],
                    'candidate_changed')
        if receipt['state'] == 'prepared':
            require(names == FILES, 'candidate_incomplete')
        if receipt['state'] == 'cancelled' and not receipt['cleanup_pending']:
            require(not names & PAYLOADS, 'cancelled_candidate_has_payload')
        return receipt, raw

    def summary(self, receipt):
        return {key: receipt[key] for key in ('id', 'plan_digest', 'state', 'cleanup_pending', 'reason')} | {
            'enabled': False, 'reboot_activates': False, 'scope': 'candidate_only'}

    def list(self):
        try:
            with self.directory(self.base / 'operations') as fd:
                names = self.names(fd, MAX_ACTIVE + MAX_HISTORY)
        except FileNotFoundError:
            return []
        result = []
        for name in names:
            try:
                receipt, _ = self.load(name)
                result.append(self.summary(receipt))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                # Empty mkdir remnants, temporary files and tampering are preserved.
                broken = {'id': name, 'state': 'failed_needs_review', 'reason': 'untrusted_or_incomplete'}
                try:
                    with self.directory(self.path(name)) as fd:
                        claim = json.loads(self.read(fd, 'operation.json'))
                    if isinstance(claim, dict):
                        broken['claimed_plan_digest'] = expected_digest(claim['plan_digest'])
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    pass
                # A claim may only refuse a duplicate, never authorize cleanup.
                result.append(broken)
        return result

    def update(self, operation, receipt, raw, **changes):
        updated = dict(receipt, **changes)
        with self.directory(self.path(operation)) as fd:
            self.replace(fd, 'operation.json', encode(updated), raw['operation.json'])
        return updated

    def finish(self, device, expected, receipt, plan_bytes, candidate_bytes, build):
        """New and interrupted preparations share the same final revalidation."""
        operation = receipt['id']
        try:
            checked_plan(build(device), expected)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            saved, raw = self.load(operation)
            require(saved == receipt, 'candidate_changed')
            self.update(operation, saved, raw, state='failed_needs_review', reason='plan_changed_after_write')
            raise
        saved, raw = self.load(operation)
        require(raw['plan.json'] == plan_bytes and raw['candidate.json'] == candidate_bytes
                and saved == receipt, 'candidate_changed')
        return self.summary(self.update(operation, saved, raw, state='prepared', reason=None))

    def stage(self, device, expected, *, build):
        confirmation = checked_plan(build(device), expected)
        plan_bytes = encode(confirmation)
        candidate_bytes = encode(confirmation['effects']['candidate_record'])
        manifest = {'schema': 1, 'plan_digest': expected,
                    'files': {'plan.json': sha(plan_bytes), 'candidate.json': sha(candidate_bytes)}}
        receipt = {'schema': 1, 'id': None, 'plan_digest': expected, 'state': 'preparing',
                   'cleanup_pending': False, 'reason': None, 'manifest': manifest,
                   'selected_object': confirmation['selected_object']}
        require(len(plan_bytes) + len(candidate_bytes) + len(encode(manifest))
                + len(encode(receipt)) + 128 <= LIMIT, 'candidate_size_limit')
        self.initialize()
        existing = self.list()
        for row in existing:
            require(row.get('claimed_plan_digest') != expected,
                    'candidate_needs_review:' + row['id'])
        for summary in existing:
            if summary.get('plan_digest') != expected or summary['state'] not in ('prepared', 'preparing'):
                continue
            operation = summary['id']
            saved, raw = self.load(operation)
            require(saved['manifest'] == manifest, 'candidate_changed')
            if set(raw) != FILES:
                self.update(operation, saved, raw, state='failed_needs_review', reason='incomplete_write')
                raise RuntimeError('candidate_incomplete_replan_or_cancel')
            if saved['state'] == 'prepared':
                return self.summary(saved)
            return self.finish(device, expected, saved, plan_bytes, candidate_bytes, build)
        active = sum(item['state'] != 'cancelled' or item.get('cleanup_pending', False) for item in existing)
        # Reserve one history slot per active operation; cancellation never loses space.
        require(active < MAX_ACTIVE and len(existing) < MAX_HISTORY, 'candidate_count_limit')
        operation = secrets.token_hex(16)
        receipt['id'] = operation
        with self.directory(self.base / 'operations') as fd:
            os.mkdir(operation, 0o700, dir_fd=fd)
            os.fsync(fd)
        with self.directory(self.path(operation)) as fd:
            self.replace(fd, 'operation.json', encode(receipt))
            self.replace(fd, 'manifest.json', encode(manifest))
            self.replace(fd, 'plan.json', plan_bytes)
            self.replace(fd, 'candidate.json', candidate_bytes)
        result = self.finish(device, expected, receipt, plan_bytes, candidate_bytes, build)
        leftovers = [row['id'] for row in existing if row['state'] == 'failed_needs_review']
        if leftovers:
            result['preserved_needs_review'] = leftovers
        return result

    def cancel(self, operation, expected, *, active_check, snapshot=None):
        expected_digest(expected)
        receipt, raw = self.load(operation)
        require(receipt['plan_digest'] == expected, 'plan_changed')
        if snapshot is not None:
            require(snapshot == {name: sha(value) for name, value in raw.items()}, 'candidate_changed')
        if receipt['state'] == 'cancelled' and not receipt['cleanup_pending']:
            return self.summary(receipt)
        active_check(receipt['selected_object'])
        if not receipt['cleanup_pending']:
            receipt = self.update(operation, receipt, raw, state='cancelled', cleanup_pending=True)
        pending = receipt
        # Reopen/revalidate each time. Missing payloads also cover interrupted preparation.
        for name in sorted(PAYLOADS):
            receipt, raw = self.load(operation)
            require(receipt == pending, 'candidate_changed')
            if name in raw:
                with self.directory(self.path(operation)) as fd:
                    require(self.read(fd, name) == raw[name], 'candidate_changed')
                    os.unlink(name, dir_fd=fd)
                    os.fsync(fd)
        receipt, raw = self.load(operation)
        require(receipt == pending, 'candidate_changed')
        receipt = self.update(operation, receipt, raw, cleanup_pending=False)
        return self.summary(receipt)
