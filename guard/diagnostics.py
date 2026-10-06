#!/usr/bin/python3
"""On-demand, read-only diagnosis of explicitly enrolled Guard controllers.

No block device is opened, no recovery is triggered, and no credentials or
general-purpose journal messages are collected. The public report is a strict
projection of bounded inputs; unrecognised text becomes an export-local token.
"""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import selectors
import stat
import subprocess
import sys
import time

from trusted_paths import open_directory, open_trusted, read_trusted

JSON_LIMIT = 256 * 1024
EVENT_LIMIT = 1024 * 1024
EXPORT_LIMIT = 2 * 1024 * 1024
MAX_DEVICES = 64
MAX_EVENTS = 64
RAM = '/run/ram-rescue-demo'
PRIVATE_RAM = '/run/ram-rescue-manager/rootfs'
ROOT_CONFIG = '/run/ram-rescue-guard/config.json'
REGISTRY = '/etc/ram-rescue-manager/devices'
ROOT_RECEIPT = '/var/lib/ram-rescue-guard/install.json'
MANAGER_RECEIPT = '/var/lib/ram-rescue-manager/install.json'
RUNTIME = RAM + '/opt/guard-runtime/runtime.json'
STATES = frozenset(('ready', 'waiting', 'verified', 'probing', 'rejected',
                    'failed', 'expired', 'interrupted', 'blocked', 'starting'))
PHASES = frozenset(('idle', 'starting', 'waiting', 'verified', 'suspend_intent',
                    'suspended', 'load_intent', 'loaded', 'commit_intent', 'committed',
                    'probe_intent', 'probing', 'confirming', 'ready', 'expired',
                    'interrupted', 'failed', 'blocked'))
TERMINAL = frozenset(('failed', 'expired', 'interrupted', 'blocked'))
RECEIPT_STATES = {
    'root': frozenset(('installed', 'preparing', 'upgrading', 'removed', 'failed_rolled_back')),
    'manager': frozenset(('installed', 'installing', 'upgrading', 'upgrade_failed', 'uninstalled')),
}
PENDING_INSTALLATION = frozenset(('preparing', 'installing', 'upgrading'))
PROPERTIES = ('LoadState', 'ActiveState', 'SubState', 'MainPID', 'Result',
              'NoNewPrivileges', 'PrivateDevices', 'ProtectSystem', 'ProtectHome',
              'MemoryMax', 'MemorySwapMax', 'CPUQuotaPerSecUSec',
              'CapabilityBoundingSet', 'RestrictAddressFamilies', 'SystemCallFilter',
              'ExecMainStartTimestampMonotonic')
CAPABILITIES = frozenset(('chown dac_override dac_read_search fowner fsetid kill setgid setuid setpcap '
                         'linux_immutable net_bind_service net_broadcast net_admin net_raw ipc_lock ipc_owner '
                         'sys_module sys_rawio sys_chroot sys_ptrace sys_pacct sys_admin sys_boot sys_nice '
                         'sys_resource sys_time sys_tty_config mknod lease audit_write audit_control setfcap '
                         'mac_override mac_admin syslog wake_alarm block_suspend audit_read perfmon bpf '
                         'checkpoint_restore').split())
ADDRESS_FAMILIES = frozenset(('UNSPEC UNIX LOCAL INET AX25 IPX APPLETALK NETROM BRIDGE ATMPVC X25 INET6 '
                             'ROSE DECnet NETBEUI SECURITY KEY NETLINK ROUTE PACKET ASH ECONET ATMSVC RDS '
                             'SNA IRDA PPPOX WANPIPE LLC IB MPLS CAN TIPC BLUETOOTH IUCV RXRPC ISDN PHONET '
                             'IEEE802154 CAIF ALG NFC VSOCK KCM QIPCRTR SMC XDP MCTP').split())
LIMITATIONS = [
    'Point-in-time observations are not an atomic snapshot or an I/O completion test.',
    'Ready describes the current owner and mapping; it does not prove filesystem or application integrity.',
    'Only enrolled maps, bounded structured Guard events, and selected systemd properties are collected.',
    'No block-device scans, credentials, shadow files, keys, general journal, host name or user database are read.',
    'Identity checks prevent accidental mismatches; clonable identifiers do not authenticate hostile hardware.',
    'Dependency hashes detect differences, not publisher authenticity; this is not a complete security audit.',
]


class InputError(ValueError):
    """An input failed a bounded, read-only trust check."""


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 2**63 else None


def choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else 'unknown'


def checksum(value):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) else None


def object_value(value):
    return value if isinstance(value, dict) else {}


class Tokens:
    """Random per report, stable across all fields inside that report."""
    def __init__(self):
        self.key = secrets.token_bytes(32)

    def __call__(self, value):
        if not isinstance(value, str) or not value or len(value) > 8192:
            return None
        digest = hmac.new(self.key, value.encode('utf-8', errors='surrogatepass'), hashlib.sha256).hexdigest()
        return 'id-' + digest[:24]


class Reader:
    """Pin directories while opening files; never follow configuration symlinks.

    root/uid are dependency-injection points for unprivileged fixture tests.
    Production always uses the real filesystem and root ownership.
    """
    def __init__(self, root=Path('/'), uid=0):
        self.root, self.uid = Path(root), uid

    @contextmanager
    def open(self, path, *, directory=False):
        path = PurePosixPath(path)
        if not path.is_absolute() or '..' in path.parts:
            raise InputError('invalid_path')
        target = self.root / path.relative_to('/')
        try:
            if directory:
                fd = open_directory(target, uid=self.uid, anchor=self.root)
                try:
                    yield fd
                finally:
                    os.close(fd)
            else:
                with open_trusted(target, uid=self.uid, anchor=self.root) as fd:
                    yield fd
        except RuntimeError as error:
            raise InputError('untrusted_input') from error

    def read(self, path, limit=JSON_LIMIT):
        path = PurePosixPath(path)
        if not path.is_absolute() or '..' in path.parts:
            raise InputError('invalid_path')
        try:
            return read_trusted(self.root / path.relative_to('/'), limit=limit, uid=self.uid, anchor=self.root)
        except (RuntimeError, ValueError) as error:
            raise InputError('oversized_untrusted_or_changed_input: ' + str(path) + ': ' + str(error)) from error

    def json(self, path):
        value = json.loads(self.read(path))
        if not isinstance(value, dict):
            raise InputError('expected_object')
        return value

    def names(self, path):
        with self.open(path, directory=True) as fd:
            # scandir stops at the bound instead of materialising a huge list.
            with os.scandir(fd) as entries:
                names = []
                for entry in entries:
                    if len(names) >= MAX_DEVICES:
                        raise InputError('too_many_registrations')
                    names.append(entry.name)
            return sorted(names)

    def hash(self, path, limit=32 * 1024 * 1024):
        with self.open(path) as fd:
            before = os.fstat(fd)
            if before.st_size > limit:
                raise InputError('oversized_input')
            result, size = hashlib.sha256(), 0
            while True:
                chunk = os.read(fd, min(65536, limit + 1 - size))
                if not chunk:
                    after = os.fstat(fd)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise InputError('file_changed_during_hash')
                    return result.hexdigest()
                size += len(chunk)
                if size > limit:
                    raise InputError('oversized_input')
                result.update(chunk)


def bounded_output(args, *, allowed_returncodes=(0,)):
    """Bound both the time and pipe size of a read-only query."""
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'}) as process:
        chunks, size = [], 0
        deadline = time.monotonic() + 4
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() < deadline:
                    if not selector.select(max(0, deadline - time.monotonic())):
                        break
                    data = os.read(process.stdout.fileno(), 8192)
                    if not data:
                        process.wait(timeout=max(.01, deadline - time.monotonic()))
                        if process.returncode not in allowed_returncodes:
                            raise InputError('service_query_failed')
                        return b''.join(chunks).decode()
                    size += len(data)
                    if size > 32768:
                        raise InputError('service_output_limit')
                    chunks.append(data)
            raise InputError('service_query_timeout')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def systemd_properties(unit):
    """Two bounded samples per selected service; no shell or journal reader."""
    output = bounded_output(['/usr/bin/systemctl', 'show', '--no-pager',
                             '--property=' + ','.join(PROPERTIES), unit])
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def package_versions(packages):
    """Query the installed package database, not files or package executables."""
    if not packages:
        return {}
    if len(packages) > 64 or any(not re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,127}(:[a-z0-9-]{1,32})?', p) for p in packages):
        raise InputError('invalid_package_name')
    # dpkg-query returns 1 when one requested package is absent, while still
    # printing installed rows. Preserve those independent comparisons.
    output = bounded_output(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${Architecture}\n', '--', *sorted(packages)],
                            allowed_returncodes=(0, 1))
    rows = {}
    for line in output.splitlines():
        fields = line.split('\t')
        if len(fields) == 3:
            package, version, architecture = fields
            rows[package] = {'package': package, 'version': version, 'architecture': architecture}
            rows.setdefault(package.split(':')[0], rows[package])
    return rows


def process_info(pid):
    """The only followed magic link is the kernel's executable for this PID."""
    if type(pid) is not int or not 0 < pid < 2**31:
        return {}
    base = Path('/proc') / str(pid)
    before = (base / 'stat').read_text()[:8192]
    start = before[before.rfind(')') + 2:].split()[19]
    with (base / 'exe').open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024 or stream.read(4) != b'\x7fELF':
            raise InputError('invalid_running_executable')
        stream.seek(0)
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    root_info = (base / 'root').stat()
    runtime_root = None
    for candidate in (RAM, PRIVATE_RAM):
        try:
            with Reader().open(candidate, directory=True) as fd:
                candidate_info = os.fstat(fd)
                if (root_info.st_dev, root_info.st_ino) == (candidate_info.st_dev, candidate_info.st_ino):
                    runtime_root = candidate
        except OSError:
            continue
    after = (base / 'stat').read_text()[:8192]
    if before[before.rfind(')') + 2:].split()[19] != after[after.rfind(')') + 2:].split()[19]:
        raise InputError('owner_changed_during_query')
    return {'pid': pid, 'start_ticks': start, 'binary_sha256': digest, 'runtime_root': runtime_root}


def reason_code(reason):
    """Keep useful categories without copying arbitrary helper/device text."""
    if not isinstance(reason, str):
        return 'unknown'
    rules = (
        ('identity_mismatch', ('does not match the enrolled disk', 'identity differs', 'Device layout differs')),
        ('ambiguous_device', ('Expected ONE matching USB disk', 'Expected partition not found uniquely')),
        ('device_instance_changed', ('Candidate changed', 'disk instance differs', 'node no longer refers')),
        ('geometry_mismatch', ('Capacity differs', 'capacity differs', 'Partition size differs', 'Partition start differs', 'logical block size differs')),
        ('deadline_exceeded', ('deadline', 'expired')),
        ('owner_ended', ('owner ended',)),
        ('path_confirmation_failed', ('Post-swap path confirmation failed',)),
        ('unsafe_transaction', ('transaction', 'active table', 'inactive table')),
    )
    return next((code for code, phrases in rules if any(p in reason for p in phrases)), 'unclassified')


def project_event(value, token):
    value = object_value(value)
    observations = object_value(value.get('observations'))
    transport = object_value(observations.get('transport'))
    return {'state': choice(value.get('state'), STATES), 'time': number(value.get('time')),
            'recoveries': number(value.get('recoveries')), 'owner': token(value.get('owner_epoch')),
            'node': token(value.get('node')), 'reason_code': reason_code(value.get('reason')),
            'reason_token': token(value.get('reason')),
            'transport': choice(transport.get('state'), {'explicit_failure', 'completion_progress', 'no_io', 'suspected_stall', 'in_flight'}),
            'in_flight_at_event': number(transport.get('in_flight')),
            'control': choice(observations.get('control'), PHASES)}


def config_fields(value, *, root=False):
    value = object_value(value)
    name, uuid = value.get('map_name'), value.get('map_uuid')
    if root:
        valid = name == 'ram-rescue-path' and value.get('run_dir') == '/run/ram-rescue-guard/state'
        prefix = 'RAMRESCUE-HOST-'
    else:
        valid = isinstance(name, str) and re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}', name)
        valid = valid and value.get('run_dir') == '/run/ram-rescue-data/' + name + '/state'
        prefix = 'RAMRESCUE-DATA-'
    if not valid or not isinstance(uuid, str) or not re.fullmatch(prefix + r'[A-Za-z0-9_-]{1,64}', uuid):
        raise InputError('invalid_enrolled_map')
    return {key: value[key] for key in ('map_name', 'map_uuid', 'run_dir')}


def classify(config, service, owner, transaction, event, mapping, boot_id):
    observed_state = choice(event.get('state'), STATES)
    current_boot = transaction.get('boot_id') == boot_id and bool(boot_id)
    identity = transaction.get('map_name') == config['map_name'] and transaction.get('map_uuid') == config['map_uuid']
    same_epoch = bool(event.get('owner_epoch')) and event.get('owner_epoch') == transaction.get('owner_epoch')
    current = current_boot and identity and same_epoch
    try:
        pid = int(service.get('MainPID', '0'))
        # systemd records CLOCK_MONOTONIC at this service invocation. Converting
        # /proc starttime with today's suspend offset would misdate older tasks.
        started_at = int(service.get('ExecMainStartTimestampMonotonic', '0')) / 1e6
    except (TypeError, ValueError):
        pid, started_at = 0, 0
    owner_matches = (current and pid > 0 and pid == transaction.get('owner_pid') == owner.get('pid')
                     and owner.get('runtime_root') in (RAM, PRIVATE_RAM)
                     and (config['map_name'] != 'ram-rescue-path' or owner.get('runtime_root') == RAM)
                     and started_at > 0
                     and number(transaction.get('updated_at')) is not None
                     and transaction['updated_at'] >= started_at
                     and number(event.get('time')) is not None and event['time'] >= started_at)
    active = service.get('ActiveState')
    if active == 'failed':
        state = 'failed'
    elif active in ('inactive', 'deactivating'):
        state = 'failed' if current and observed_state in TERMINAL else 'installed_not_running'
    elif active not in ('active', 'activating') or not owner_matches:
        state = 'unknown'
    elif observed_state in TERMINAL:
        state = 'failed'
    elif observed_state == 'rejected':
        state = 'refused'
    elif mapping.get('uuid') != config['map_uuid']:
        state = 'unknown'
    elif observed_state in ('waiting', 'verified', 'probing', 'starting'):
        state = 'recovering'
    elif (observed_state == 'ready' and transaction.get('phase') in ('idle', 'ready')
          and object_value(mapping.get('info')).get('suspended') == 0
          and object_value(mapping.get('info')).get('live_table') == 1
          and isinstance(mapping.get('active'), list) and len(mapping['active']) == 1
          and isinstance(mapping['active'][0], list) and len(mapping['active'][0]) == 4
          and mapping['active'][0][2] == 'multipath'
          and not mapping.get('inactive')):
        state = 'ready'
    else:
        state = 'unknown'
    return state, bool(current), bool(owner_matches)


class Collector:
    def __init__(self, reader, service_reader, map_reader, process_reader, package_reader):
        self.reader, self.service_reader = reader, service_reader
        self.map_reader, self.process_reader = map_reader, process_reader
        self.package_reader = package_reader
        self.token, self.issues = Tokens(), []

    def read(self, label, function, *, missing=None):
        try:
            return function()
        except FileNotFoundError:
            return missing
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, RecursionError, subprocess.SubprocessError):
            self.issues.append({'source': self.token(label), 'code': 'unavailable_or_untrusted'})
            return missing

    def device(self, config, unit, boot_id):
        run = config['run_dir']
        read_json = lambda name: self.read(run + '/' + name, lambda: self.reader.json(run + '/' + name), missing={})
        service = self.read(unit, lambda: self.service_reader(unit), missing={})
        transaction, event = read_json('path-transaction.json'), read_json('path-state.json')
        mapping = self.read(config['map_name'], lambda: self.map_reader(config['map_name']), missing={})
        pid = service.get('MainPID', '0')
        owner = self.read('owner', lambda: self.process_reader(int(pid)), missing={})
        state, current, owner_matches = classify(config, service, owner, transaction, event, mapping, boot_id)
        events = []
        for filename in ('path-events.previous.jsonl', 'path-events.jsonl'):
            raw = self.read(run + '/' + filename, lambda: self.reader.read(run + '/' + filename, EVENT_LIMIT), missing=b'')
            # Each file is already size-bounded. Malformed/partial rows are omitted.
            for line in raw.splitlines()[-MAX_EVENTS:]:
                entry = self.read('event', lambda: json.loads(line), missing={})
                if isinstance(entry, dict) and entry:
                    events.append(project_event(entry, self.token))
        # A changing owner must never be presented as a verified current owner.
        after = self.read(unit, lambda: self.service_reader(unit), missing={})
        latest = read_json('path-transaction.json')
        if any(after.get(key) != service.get(key) for key in ('MainPID', 'ActiveState', 'ExecMainStartTimestampMonotonic')) or latest != transaction:
            state, owner_matches = 'unknown', False
        info = object_value(mapping.get('info'))
        security = {key: self.security_value(key, service.get(key)) for key in PROPERTIES[5:-1]}
        return {'map': self.token(config['map_name']), 'map_uuid': self.token(config['map_uuid']),
                'service': self.token(unit), 'role': 'root' if unit == 'ram-rescue-guard.service' else 'data',
                'state': state, 'evidence_from_this_boot': current, 'owner_matches': owner_matches,
                'service_state': choice(service.get('ActiveState'), {'active', 'activating', 'inactive', 'deactivating', 'failed'}),
                'running_binary_sha256': checksum(owner.get('binary_sha256')),
                'runtime_context': {RAM: 'protected_boot', PRIVATE_RAM: 'standalone_data'}.get(owner.get('runtime_root'), 'unknown'),
                'last_event': project_event(event, self.token), 'timeline': events[-MAX_EVENTS:],
                'transaction': {'phase': choice(transaction.get('phase'), PHASES),
                                'owner': self.token(transaction.get('owner_epoch')),
                                'boot': self.token(transaction.get('boot_id')),
                                'updated_at': number(transaction.get('updated_at')),
                                'deadline': number(transaction.get('deadline'))},
                'mapping': {'identity_matches': mapping.get('uuid') == config['map_uuid'],
                            'suspended': number(info.get('suspended')),
                            'live_table': number(info.get('live_table')),
                            'inactive_table': number(info.get('inactive_table')),
                            'open_count': number(info.get('open_count'))},
                'security': security}

    def security_value(self, key, value):
        if key in ('NoNewPrivileges', 'PrivateDevices'):
            return choice(value, {'yes', 'no'})
        if key in ('ProtectSystem', 'ProtectHome'):
            return choice(value, {'yes', 'no', 'true', 'false', 'strict', 'full', 'read-only', 'tmpfs'})
        if key in ('MemoryMax', 'MemorySwapMax', 'CPUQuotaPerSecUSec'):
            return value if isinstance(value, str) and re.fullmatch(r'(infinity|[0-9]{1,20}(ms|us|s)?)', value) else 'unknown'
        if key == 'CapabilityBoundingSet' and isinstance(value, str) and all(word.startswith('cap_') and word[4:] in CAPABILITIES for word in value.split()):
            return value
        if key == 'RestrictAddressFamilies' and isinstance(value, str) and all(word.startswith('AF_') and word[3:] in ADDRESS_FAMILIES for word in value.lstrip('~').split()):
            return value
        # Syscall filter can contain arbitrary names; disclose presence and a token.
        return {'configured': bool(value), 'value_token': self.token(value)}

    @staticmethod
    def dependency_path(path, base):
        if not isinstance(path, str) or '..' in PurePosixPath(path).parts:
            return False
        if re.fullmatch(r'/(usr/)?lib(?:64)?/[A-Za-z0-9_+./-]+', path) and '.so' in PurePosixPath(path).name:
            return True
        return bool(base and re.fullmatch(
            r'/(usr/)?s?bin/(busybox|bash|lvm|dmsetup|e2fsck|blkid|blockdev|lsblk|findmnt|python3(?:\.[0-9]+)?)', path))

    def dependency_rows(self, root, libraries, packages, *, base=False):
        dependencies = []
        libraries, packages = object_value(libraries), object_value(packages)
        limit = 256 if base else 64
        if len(libraries) > limit or len(packages) > limit:
            self.issues.append({'source': 'runtime_manifest', 'code': 'dependency_limit'})
        queries = set()
        for metadata in packages.values():
            metadata = object_value(metadata)
            package = metadata.get('package')
            if isinstance(package, str) and re.fullmatch(r'[a-z0-9][a-z0-9+.-]{0,127}(:[a-z0-9-]{1,32})?', package):
                queries.add(package)
        host_packages = self.read('host_package_versions', lambda: self.package_reader(queries), missing={}) if queries else {}
        for path, expected in list(libraries.items())[:limit]:
            if not self.dependency_path(path, base) or not checksum(expected):
                self.issues.append({'source': 'runtime_manifest', 'code': 'invalid_dependency'})
                continue
            frozen = self.read(path, lambda: self.reader.hash(root + path))
            # Follow merged-/usr only into the same allowed tool/library scope.
            def host_hash():
                target = (self.reader.root / path.lstrip('/')).resolve(strict=True)
                relative = '/' + str(target.relative_to(self.reader.root))
                if not self.dependency_path(relative, base):
                    raise InputError('library_path_escape')
                return self.reader.hash(relative)
            host = self.read(path, host_hash)
            frozen_package = self.package_metadata(packages.get(path))
            host_package = self.package_metadata(host_packages.get(frozen_package.get('package')))
            dependencies.append({'library': self.token(path), 'expected_sha256': expected,
                                 'frozen_sha256': frozen, 'host_sha256': host,
                                 'frozen_matches': None if frozen is None else frozen == expected,
                                 'host_differs': None if host is None else host != expected,
                                 'frozen_package': frozen_package, 'host_package': host_package,
                                 'package_version_differs': None if not frozen_package or not host_package else frozen_package != host_package})
        return dependencies

    def runtime_version(self, root, candidates):
        manifest = root + '/opt/guard-runtime/runtime.json'
        runtime = self.read(manifest, lambda: self.reader.json(manifest), missing={})
        if not runtime:
            return None
        base_manifest = root + '/etc/rescue/base-runtime.json'
        base = self.read(base_manifest, lambda: self.reader.json(base_manifest), missing={})
        if base and (base.get('schema') != 1 or base.get('kind') != 'base-rescue-tools'):
            self.issues.append({'source': 'base_runtime_manifest', 'code': 'invalid_manifest'})
            base = {}
        source_path = root + '/etc/rescue/base-source.json'
        source = self.read(source_path, lambda: self.reader.json(source_path), missing={})
        base_sha = checksum(source.get('sha256')) if source.get('schema') == 1 else None
        binary = self.read('frozen_binary', lambda: self.reader.hash(root + '/opt/guard-runtime/guard-runtime'))
        return {'context': 'protected_boot' if root == RAM else 'standalone_data',
                'frozen_binary_sha256': binary, 'frozen_manifest_binary_sha256': checksum(runtime.get('binary_sha256')),
                'base_source_sha256': base_sha,
                'installed_candidate_matches': {
                    role: {'native_manifest': runtime == candidate['native_runtime'] if candidate['native_runtime'] else None,
                           'base_source': base_sha == candidate['base_sha256'] if base_sha and candidate['base_sha256'] else None}
                    if candidate else None for role, candidate in candidates.items()},
                'dependencies': self.dependency_rows(root, runtime.get('library_sha256'), runtime.get('dependency_packages')),
                'base_manifest_present': bool(base),
                'base_dependencies': self.dependency_rows(root, base.get('file_sha256'), base.get('dependency_packages'), base=True)}

    @staticmethod
    def package_metadata(value):
        value = object_value(value)
        patterns = {'package': r'[a-z0-9][a-z0-9+.:_-]{0,159}',
                    'version': r'[A-Za-z0-9.+:~_-]{1,128}', 'architecture': r'[a-z0-9_-]{1,32}'}
        if any(not isinstance(value.get(key), str) or not re.fullmatch(pattern, value[key]) for key, pattern in patterns.items()):
            return {}
        return {key: value[key] for key in patterns}

    def manager_candidate(self, receipt):
        """Read the selected immutable version; never execute or unpack it."""
        program = receipt.get('program')
        if not isinstance(program, str) or not re.fullmatch(
                r'/usr/local/lib/ram-rescue-manager/[0-9a-f]{64}/guard/manage\.py', program):
            raise InputError('invalid_manager_program')
        program_sha = self.reader.hash(program, limit=8 * 1024 * 1024)
        path = str(PurePosixPath(program).parent.parent / 'runtime/manifest.json')
        manifest = self.reader.json(path)
        native = object_value(manifest.get('native_runtime'))
        if (manifest.get('schema') != 1 or manifest.get('kind') != 'data-runtime'
                or not checksum(manifest.get('binary_sha256'))
                or manifest['binary_sha256'] != native.get('binary_sha256')
                or not checksum(manifest.get('archive_sha256'))
                or not checksum(manifest.get('base_sha256'))):
            raise InputError('invalid_manager_runtime_manifest')
        return {'native_runtime': native, 'base_sha256': manifest['base_sha256'],
                'summary': {'program': self.token(program), 'program_sha256': program_sha,
                            'binary_sha256': manifest['binary_sha256'], 'base_sha256': manifest['base_sha256'],
                            'archive_sha256': manifest['archive_sha256'], 'archive_integrity_checked': False}}

    def versions(self, root_receipt, manager_receipt):
        candidates = {'root': None, 'manager': None}
        if root_receipt.get('state') == 'installed':
            build = object_value(root_receipt.get('build'))
            native = object_value(build.get('native_runtime'))
            base_sha = checksum(build.get('base_rescue_payload_sha256'))
            candidates['root'] = {'native_runtime': native, 'base_sha256': base_sha,
                                  'summary': {'image_sha256': checksum(root_receipt.get('image_sha256')),
                                              'binary_sha256': checksum(native.get('binary_sha256')),
                                              'base_sha256': base_sha}}
        if manager_receipt.get('state') == 'installed':
            candidates['manager'] = self.read('installed_manager_candidate', lambda: self.manager_candidate(manager_receipt))
        summaries = {role: candidate['summary'] if candidate else None for role, candidate in candidates.items()}
        root_summary = summaries['root'] or {}
        runtimes = [runtime for root in (RAM, PRIVATE_RAM) if (runtime := self.runtime_version(root, candidates))]
        return {'installed_image_sha256': root_summary.get('image_sha256'),
                'installed_binary_sha256': root_summary.get('binary_sha256'),
                'installed_candidates': summaries,
                'runtimes': runtimes,
                'dependency_scope': 'native_guard_libraries_and_manifested_base_elf_tools_libraries'}

    def collect(self):
        boot_id = self.read('boot_id', lambda: self.reader.read('/proc/sys/kernel/random/boot_id', 128).decode().strip())
        root_receipt = self.read(ROOT_RECEIPT, lambda: self.reader.json(ROOT_RECEIPT), missing={})
        manager_receipt = self.read(MANAGER_RECEIPT, lambda: self.reader.json(MANAGER_RECEIPT), missing={})
        entries = []
        root = self.read(ROOT_CONFIG, lambda: self.reader.json(ROOT_CONFIG))
        if root is not None:
            config = self.read(ROOT_CONFIG, lambda: config_fields(root, root=True))
            if config:
                entries.append((config, 'ram-rescue-guard.service'))
        names = self.read(REGISTRY, lambda: self.reader.names(REGISTRY), missing=[])
        for name in names:
            if not re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}\.json', name):
                self.issues.append({'source': 'registry', 'code': 'invalid_registration_filename'})
                continue
            path = REGISTRY + '/' + name
            value = self.read(path, lambda: self.reader.json(path), missing={})
            config = self.read(path, lambda: config_fields(value.get('guard')))
            if config and config['map_name'] + '.json' == name:
                entries.append((config, 'ram-rescue-maintain@' + config['map_name'] + '.service'))
        known = {config['map_name'] for config, _ in entries}
        # Temporary explicit data enrollments need not have a persistent registry.
        temporary = self.read('data_runtime', lambda: self.reader.names('/run/ram-rescue-data'), missing=[])
        for name in temporary:
            if name in known or not re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}', name):
                continue
            path = '/run/ram-rescue-data/' + name + '/config.json'
            value = self.read(path, lambda: self.reader.json(path), missing={})
            config = self.read(path, lambda: config_fields(value))
            if config and config['map_name'] == name:
                entries.append((config, 'ram-rescue-data-' + name + '.service'))
        if len(entries) > MAX_DEVICES:
            raise InputError('too_many_selected_maps')
        devices = [self.device(config, unit, boot_id) for config, unit in entries]
        versions = self.versions(root_receipt, manager_receipt)
        receipts = (root_receipt, manager_receipt)
        receipt_states = {role: choice(value.get('state'), RECEIPT_STATES[role])
                          for role, value in zip(('root', 'manager'), receipts)}
        uncertain_installation = any(value and receipt_states[role] == 'unknown'
                                     for role, value in zip(('root', 'manager'), receipts))
        uncertain_installation |= any(state in PENDING_INSTALLATION for state in receipt_states.values())
        failed_installation = 'upgrade_failed' in receipt_states.values()
        installed = (any(state == 'installed' for state in receipt_states.values())
                     or root is not None or bool(temporary)
                     or any(device['owner_matches'] or device['service_state'] == 'failed' for device in devices))
        if failed_installation:
            state = 'failed'
        elif uncertain_installation or (self.issues and not devices):
            state = 'unknown'
        elif not installed:
            state = 'not_installed'
        elif not devices:
            state = 'installed_not_running'
        else:
            order = ('failed', 'unknown', 'refused', 'recovering', 'installed_not_running', 'ready')
            state = next(item for item in order if any(d['state'] == item for d in devices))
        result = {'schema': 1, 'state': state, 'captured_at_unix': int(time.time()),
                  'boot': self.token(boot_id),
                  'installed': installed if installed or not (self.issues or uncertain_installation or failed_installation) else None,
                  'devices': devices,
                  'versions': versions, 'issues': self.issues,
                  'installation_receipts': receipt_states,
                  'redaction': 'whitelist_with_per_report_hmac_tokens', 'limitations': LIMITATIONS}
        if len(encode(result)) > EXPORT_LIMIT:
            raise InputError('report_output_limit')
        return result


def encode(value):
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + '\n').encode()


def doctor(*, reader=None, service_reader=systemd_properties, map_reader=None, process_reader=process_info,
           package_reader=package_versions):
    """Return a redacted snapshot. Permission failures are explicit unknowns."""
    if map_reader is None:
        mapper = None

        def map_reader(name):
            nonlocal mapper
            if mapper is None:
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ram-rescue-demo/src'))
                from admin.dm import DeviceMapper
                mapper = DeviceMapper()
            return mapper.snapshot(name)
    return Collector(reader or Reader(), service_reader, map_reader, process_reader, package_reader).collect()


def export_report(output=None, **doctor_options):
    """Create a local 0600 JSON in a private 0700 directory; never overwrite."""
    data = encode(doctor(**doctor_options))
    if len(data) > EXPORT_LIMIT:
        raise InputError('report_output_limit')
    if output is None:
        folder = Path.cwd() / ('rescue-diagnostics-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
        folder.mkdir(mode=0o700)
        output = folder / 'report.json'
    else:
        output = Path(output).absolute()
        output.parent.mkdir(mode=0o700, exist_ok=True)
    # The selected parent is an explicit user destination, but every component
    # must be a real directory. Pin its FD so subsequent path replacement cannot
    # redirect the write. Do not chmod or overwrite a pre-existing user object.
    fd = open_export_directory(output.parent)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise InputError('export_directory_requires_owner_and_mode_0700')
        target = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
        try:
            with os.fdopen(target, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(fd)
            check = open_export_directory(output.parent)
            try:
                after = os.fstat(check)
                if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
                    raise InputError('export_directory_changed')
            finally:
                os.close(check)
        except BaseException:
            os.unlink(output.name, dir_fd=fd)
            raise
    finally:
        os.close(fd)
    return {'path': str(output), 'bytes': len(data), 'mode': '0600', 'uploaded': False}


def open_export_directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('doctor', help='Print a redacted, read-only snapshot')
    export = commands.add_parser('export', help='Save a private local diagnostic JSON')
    export.add_argument('--output', type=Path)
    args = parser.parse_args()
    value = doctor() if args.command == 'doctor' else export_report(args.output)
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
