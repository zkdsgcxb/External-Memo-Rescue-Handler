"""One-shot volume discovery. No enrollment, media probes or activation.

Kernel metadata and trusted receipts are observations, not permission to enable
protection. The Python injection points are for fixtures, never CLI arguments.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import time
import unicodedata

import diagnostics as doctor
from admin.data import CRITICAL_MOUNTS, unescape
from admin.dm import expected_table, table_digest
from admin.identity import FILESYSTEM_TYPES
from admin.registry import validate_record

MAX_NODES = 256
MAX_MOUNTS = 4096
TIMEOUT = '/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs'
PACKAGE_MANIFEST = str(Path(__file__).resolve().parent.parent / 'runtime/manifest.json')


class ConfigurationSnapshot:
    """Compare only the bounded trusted inputs used by this observation.

    Absence is distinct from unreadability. Retain digests instead of another
    copy of each registration, and sample each source only once more at the end.
    This detects observed changes, not an atomic snapshot or change-and-revert.
    """
    def __init__(self, read):
        self.read, self.sources = read, []

    def observe(self, scope, label, function, *, missing=None, required=False):
        if len(self.sources) >= doctor.MAX_DEVICES + 6:
            raise doctor.InputError('configuration_source_limit')

        def sample():
            try:
                value = function()
            except FileNotFoundError:
                return ('absent', None), None
            return ('present', hashlib.sha256(doctor.encode(value)).digest()), value

        before, value = self.read(label, sample, missing=(('unreadable', None), None))
        self.sources.append((scope, label, sample, before, required))
        return value if before[0] == 'present' else missing

    def recheck(self):
        failed = set()
        for scope, label, sample, before, required in self.sources:
            after, _ = self.read(label, sample, missing=(('unreadable', None), None))
            if (before != after or 'unreadable' in (before[0], after[0]) or
                    (required and before[0] != 'present')):
                failed.add(scope)
        return failed

    def fingerprints(self):
        """Stable private inputs for planning; never use diagnostic tokens."""
        return sorted(({'scope': scope, 'source': label, 'state': before[0],
                        'sha256': before[1].hex() if before[1] else None}
                       for scope, label, _, before, _ in self.sources),
                      key=lambda row: (row['scope'], row['source']))


def safe_text(value):
    """Device labels are untrusted terminal text, including bidi controls."""
    return ''.join(character if not unicodedata.category(character).startswith('C')
                   else '\\u%04x' % ord(character) for character in str(value)[:512])


class Metadata:
    """Read bounded kernel/udev metadata; never open a block device."""
    def __init__(self, root=Path('/')):
        self.root = Path(root)

    def path(self, name):
        return self.root / name.lstrip('/')

    def text(self, name, limit=8192, *, optional=False):
        path = self.path(name)
        try:
            # sysfs class/slave links are expected; do not follow them elsewhere.
            if name.startswith('/sys/') and not path.resolve(strict=True).is_relative_to(self.path('/sys')):
                raise ValueError('metadata_path_escape')
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            if optional:
                return None
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('metadata_not_regular')
            chunks = bytearray()
            while len(chunks) <= limit:
                chunk = os.read(fd, min(8192, limit + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            if len(chunks) > limit:
                raise ValueError('metadata_limit')
            return chunks.decode(errors='replace').strip()
        finally:
            os.close(fd)

    def names(self, name, limit=MAX_NODES):
        with os.scandir(self.path(name)) as entries:
            result = []
            for entry in entries:
                if len(result) >= limit:
                    raise ValueError('metadata_count_limit')
                result.append(entry.name)
        return sorted(result)

    def inventory(self):
        nodes = {}
        for name in self.names('/sys/class/block'):
            if not re.fullmatch(r'[A-Za-z0-9_.!+-]{1,128}', name):
                raise ValueError('invalid_block_name')
            base = '/sys/class/block/' + name
            resolved = self.path(base).resolve(strict=True)
            if not resolved.is_relative_to(self.path('/sys')):
                raise ValueError('metadata_path_escape')
            partition = self.text(base + '/partition', optional=True)
            disk = resolved.parent if partition else resolved
            disk_name = '/' + str(disk.relative_to(self.root))
            usb = next((path for path in disk.parents
                        if path.is_relative_to(self.path('/sys')) and (path / 'idVendor').is_file()), None)
            usb_path = '/' + str(usb.relative_to(self.root)) if usb else None
            device = self.text(base + '/dev')
            if not re.fullmatch(r'[0-9]+:[0-9]+', device):
                raise ValueError('invalid_device_number')
            nodes[name] = {
                'name': name, 'dev': device, 'path': str(resolved),
                'partition': int(partition) if partition else None,
                'parent': disk.name if partition else None,
                'size': int(self.text(base + '/size')) * 512,
                'slaves': self.names(base + '/slaves') if not partition else [],
                'dm_name': self.text(base + '/dm/name', optional=True),
                'dm_uuid': self.text(base + '/dm/uuid', optional=True),
                'diskseq': self.text(disk_name + '/diskseq', optional=True),
                'usb': usb is not None,
                'serial': self.text(usb_path + '/serial', optional=True) if usb else None,
                'model': self.text(disk_name + '/device/model', optional=True) or '',
            }
        return nodes

    def mounts(self):
        content = self.text('/proc/1/mountinfo', 2 * 1024 * 1024)
        rows = content.splitlines()
        if len(rows) > MAX_MOUNTS:
            raise ValueError('mount_count_limit')
        result = []
        for row in rows:
            fields = row.split()
            separator = fields.index('-')
            if separator < 6 or len(fields) < separator + 4:
                raise ValueError('invalid_mountinfo')
            result.append({'dev': fields[2], 'where': unescape(fields[4]),
                           'fstype': fields[separator + 1], 'options': fields[5]})
        return result

    def filesystem(self, device):
        # Existing udev database only; cached properties cannot admit a medium.
        content = self.text('/run/udev/data/b' + device, 65536, optional=True) or ''
        fields = dict(line[2:].split('=', 1) for line in content.splitlines()
                      if line.startswith('E:') and '=' in line)
        return {'type': safe_text(fields.get('ID_FS_TYPE', 'unknown')),
                'source': 'udev_cache', 'identity': 'not_checked'}

    def swaps(self):
        rows = self.text('/proc/swaps', 256 * 1024).splitlines()[1:]
        result = []
        for row in rows:
            fields = row.split()
            if len(fields) < 5:
                raise ValueError('invalid_swaps')
            result.append({'path': unescape(fields[0]), 'kind': fields[1]})
        return result


def above(nodes, selected):
    """Selected partition and its consumers, not other partitions on its disk."""
    result = {selected}
    for _ in range(len(nodes)):
        expanded = result | {name for name, node in nodes.items() if result.intersection(node['slaves'])}
        if expanded == result:
            return result
        result = expanded
    raise ValueError('invalid_dependency_graph')


def check_graph(nodes):
    completed = set()
    def visit(name, stack):
        if name in stack or len(stack) >= 32:
            raise ValueError('cyclic_or_deep_dependency_graph')
        if name not in nodes:
            raise ValueError('incomplete_dependency_graph')
        if name in completed:
            return
        for child in nodes[name]['slaves']:
            visit(child, stack | {name})
        completed.add(name)
    for name in nodes:
        visit(name, set())


def check(code, result, source):
    return {'code': code, 'result': result, 'source': source}


def map_topology(config, node, snapshot, nodes):
    """Check the registered map/backing only, not medium or LV identities."""
    if len(node['slaves']) != 1:
        return 'fail'
    backing = nodes[node['slaves'][0]]
    if not backing['partition'] or not backing['usb']:
        return 'fail'
    if backing['diskseq'] is None:
        return 'unknown'
    sectors = config.get('partition_sectors')
    if type(sectors) is not int:
        return 'unknown'
    info = snapshot['info']
    if (snapshot['uuid'] != config['map_uuid'] or snapshot['inactive'] or
            any(info.get(key) != 0 for key in ('suspended', 'internal_suspend', 'deferred_remove', 'read_only')) or
            info.get('live_table') != 1 or backing['size'] != sectors * 512 or
            table_digest(snapshot['active']) != table_digest(expected_table(sectors, backing['dev']))):
        return 'fail'
    words = snapshot['active'][0][3].split()
    return 'pass' if 'queue_if_no_path' in words[1:1 + int(words[0])] else 'fail'


def query_mapper():
    """Never let libdevmapper prepare an absent control device/module."""
    if not Path('/sys/module/dm_multipath').is_dir():
        raise ValueError('multipath_not_loaded')
    if not stat.S_ISCHR(os.stat('/dev/mapper/control').st_mode):
        raise ValueError('dm_control_unavailable')
    from admin.dm import DeviceMapper
    return DeviceMapper()


def collect(*, metadata=None, reader=None, service_reader=doctor.systemd_properties,
            map_reader=None, process_reader=doctor.process_info, target_reader=None, inputs=None):
    metadata, reader = metadata or Metadata(), reader or doctor.Reader()
    mapper = None
    enrolled_uuids = {}

    def mapping(name):
        nonlocal mapper
        if mapper is None:
            mapper = query_mapper()
        return mapper.snapshot_by_uuid(name, enrolled_uuids[name])

    def target():
        nonlocal mapper
        if mapper is None:
            mapper = query_mapper()
        return mapper.target_version('multipath')

    observer = doctor.Collector(reader, service_reader, map_reader or mapping,
                                process_reader, lambda packages: {})
    read = observer.read
    configuration = ConfigurationSnapshot(read)
    observe = configuration.observe
    inventory = read('block_inventory', metadata.inventory)
    mount_rows = read('mountinfo', metadata.mounts)
    nodes, mounts = inventory or {}, mount_rows or []
    swaps = read('swaps', metadata.swaps)
    graph_ok = inventory is not None and read('block_graph', lambda: (check_graph(nodes), True)[1], missing=False)
    boot = read('boot_id', lambda: reader.read('/proc/sys/kernel/random/boot_id', 128).decode().strip())
    kernel = read('kernel_release', lambda: metadata.text('/proc/sys/kernel/osrelease'))
    timeout = read('queue_timeout', lambda: int(metadata.text(TIMEOUT)))
    version = read('multipath_target', target_reader or target)
    environment_checks = [
        check('multipath_interface', 'unknown' if version is None else
              'pass' if tuple(version) >= (1, 15, 0) else 'fail', 'loaded_dm_target'),
        check('queue_timeout', 'unknown' if timeout is None else
              'pass' if timeout >= 10 else 'fail', 'module_parameter'),
    ]
    competing = [read(unit, lambda unit=unit: service_reader(unit), missing={})
                 for unit in ('multipathd.service', 'multipathd.socket')]
    environment_checks.append(check('multipathd_units',
        'fail' if any(value.get('ActiveState') in ('active', 'activating', 'reloading') for value in competing)
        else 'unknown' if any(value.get('ActiveState') not in ('inactive', 'failed') for value in competing)
        else 'pass', 'systemd_units_only'))
    environment_checks.append(check('manual_multipathd_processes', 'not_checked', 'none'))
    package = observe('package', 'command_package_manifest', lambda: reader.json(PACKAGE_MANIFEST), missing={})
    package_kernel = package.get('kernel_release') if package.get('schema') == 1 and package.get('kind') == 'data-runtime' else None
    environment_checks.append(check('package_kernel', 'unknown' if not package_kernel or not kernel else
                                    'pass' if package_kernel == kernel else 'fail', 'package_manifest_uname'))

    profiles = []
    root = observe('relationships', doctor.ROOT_CONFIG, lambda: reader.json(doctor.ROOT_CONFIG))
    if root is not None:
        config = read('root_config', lambda: doctor.config_fields(root, root=True))
        if config:
            profiles.append((root, 'ram-rescue-guard.service', 'root'))
    names = observe('relationships', 'registration_list', lambda: reader.names(doctor.REGISTRY), missing=[])
    for filename in names:
        if not re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}\.json', filename):
            observer.issues.append({'source': 'registry', 'code': 'invalid_registration_filename'})
            continue
        path = doctor.REGISTRY + '/' + filename
        record = observe('relationships', path, lambda path=path: reader.json(path), required=True)
        profile = read('registration', lambda: validate_record(record)) if record is not None else None
        if profile and profile['guard']['profile'] == 'host-data' and profile['guard']['map_name'] + '.json' == filename:
            config = profile['guard']
            profiles.append((config, 'ram-rescue-maintain@' + config['map_name'] + '.service', 'manager'))
        elif profile:
            observer.issues.append({'source': 'registry', 'code': 'registration_name_mismatch'})
    enrolled_uuids.update((config['map_name'], config['map_uuid']) for config, _, _ in profiles)

    receipts, candidates = {}, {}
    def manager_candidate(receipt):
        value = observer.manager_candidate(receipt)
        # The receipt already binds the program path. Its per-report display
        # token must not enter the reusable configuration fingerprint.
        value['summary'].pop('program', None)
        return value

    for role, path in (('root', doctor.ROOT_RECEIPT), ('manager', doctor.MANAGER_RECEIPT)):
        receipt = observe(role, path, lambda path=path: reader.json(path))
        receipts[role] = {'state': doctor.choice(receipt.get('state'), doctor.RECEIPT_STATES[role]) if receipt else 'not_observed',
                          'integrity': 'not_checked', 'source': 'installation_receipt'}
        if receipt and receipt.get('state') == 'installed':
            if role == 'root':
                build = doctor.object_value(receipt.get('build'))
                candidates[role] = {'native_runtime': doctor.object_value(build.get('native_runtime')),
                                    'kernel_release': receipt.get('kernel_release')}
            else:
                candidate = observe(role, 'manager_candidate',
                    lambda receipt=receipt: manager_candidate(receipt), required=True)
                if candidate:
                    candidates[role] = candidate

    groups = []
    snapshots = {}
    original_map_reader = observer.map_reader
    def observed_map(name):
        value = original_map_reader(name)
        snapshots[name] = value
        return value
    observer.map_reader = observed_map
    for config, unit, role in profiles:
        # Only trusted enrolled names can reach a DM query; foreign tables may
        # carry secrets and are outside this read-only product slice.
        matches = [node for node in nodes.values() if node['dm_name'] == config['map_name']
                   and node['dm_uuid'] == config['map_uuid']]
        observed = None
        topology = 'not_checked'
        if len(matches) == 1:
            observed = read('controller', lambda: observer.device(config, unit, boot), missing={})
            topology = read('registered_map_topology', lambda: map_topology(
                config, matches[0], snapshots[config['map_name']], nodes), missing='unknown')
        candidate = candidates.get(role, {})
        expected = doctor.checksum(candidate.get('native_runtime', {}).get('binary_sha256'))
        actual = (observed or {}).get('running_binary_sha256')
        restart = 'unknown'
        if (observed or {}).get('owner_matches') and expected and actual:
            restart = 'required' if expected != actual else 'binary_matches_dependencies_not_checked'
        groups.append({'map_name': config['map_name'], 'map_uuid': config['map_uuid'],
                       'node': matches[0]['name'] if len(matches) == 1 else None,
                       'role': role, 'kernel_release': config.get('kernel_release'),
                       'evidence': {'preparation': receipts[role], 'restart': restart,
                                    'current_runtime': (observed or {}).get('state', 'not_observed'),
                                    'current_boot_owner': (observed or {}).get('owner_matches', False),
                                    'topology': topology, 'topology_scope': 'registered_map_and_backing',
                                    'media_identity': 'not_checked',
                                    'recovery_experiment': 'not_evaluated'},
                       'observation': observed})

    volumes = []
    if graph_ok:
        partitions = [name for name, node in nodes.items() if node['usb'] and (node['partition'] or
                      not any(other['parent'] == name for other in nodes.values()))]
        system_disks = set()
        if len(partitions) > doctor.MAX_DEVICES:
            observer.issues.append({'source': 'inventory', 'code': 'volume_count_limit'})
        for name in partitions:
            consumers = above(nodes, name)
            devices = {nodes[item]['dev'] for item in consumers}
            if any(row['dev'] in devices and row['where'] in CRITICAL_MOUNTS for row in mounts):
                system_disks.add(nodes[name]['parent'] or name)
        for name in partitions[:doctor.MAX_DEVICES]:
            node = nodes[name]
            consumers = above(nodes, name)
            attached = [group for group in groups if group['node'] in consumers]
            locations = [row for row in mounts if row['dev'] in {nodes[item]['dev'] for item in consumers}]
            root_volume = any(row['where'] == '/' for row in locations)
            filesystem = read('udev_cache', lambda: metadata.filesystem(node['dev']), missing={'type': 'unknown'})
            checks = list(environment_checks)
            checks.append(check('media_identity', 'not_checked', 'none'))
            checks.append(check('mount_policy', 'not_checked', 'none'))
            if not root_volume and not any(group['role'] == 'root' for group in attached) and filesystem['type'] not in FILESYSTEM_TYPES | {'unknown'}:
                checks.append(check('cached_filesystem_unsupported', 'fail', 'udev_cache'))
            if not node['partition']:
                checks.append(check('partition_required', 'fail', 'sysfs'))
            if not node['serial']:
                checks.append(check('usb_serial_missing', 'fail', 'sysfs'))
            if node['diskseq'] is None:
                checks.append(check('device_instance', 'unknown', 'sysfs'))
            if not root_volume and (node['parent'] or name) in system_disks and not any(g['role'] == 'root' for g in attached):
                checks.append(check('system_disk_sibling', 'fail', 'mountinfo_sysfs'))
            if len(attached) > 1:
                checks.append(check('multiple_protection_groups', 'fail', 'registration_sysfs'))
            if any(row['dev'] == node['dev'] for row in locations):
                checks.append(check('raw_partition_mounted', 'fail', 'mountinfo'))
            if not attached:
                if len(consumers) > 1:
                    checks.append(check('unmanaged_upper_layers', 'unknown' if root_volume else 'fail', 'sysfs'))
                else:
                    checks.append(check('mapping_not_enrolled', 'not_checked', 'registration_sysfs'))
            for group in attached:
                if group['role'] == 'manager' and above(nodes, group['node']) != {group['node']}:
                    checks.append(check('data_upper_layers', 'fail', 'sysfs'))
                checks.append(check('enrollment_kernel', 'unknown' if not kernel or not group['kernel_release'] else
                    'pass' if kernel == group['kernel_release'] else 'fail', 'registration_uname'))
                if group['evidence']['topology'] == 'fail':
                    checks.append(check('registered_map_topology', 'fail', 'dm_sysfs'))
            if not root_volume:
                in_use = False
                swap_unknown = swaps is None
                known_paths = {'/dev/' + item for item in nodes} | {
                    '/dev/mapper/' + item['dm_name'] for item in nodes.values() if item['dm_name']}
                for swap in swaps or []:
                    if swap['kind'] == 'partition':
                        swap_unknown |= swap['path'] not in known_paths
                        in_use |= swap['path'] in {'/dev/' + item for item in consumers} | {
                            '/dev/mapper/' + nodes[item]['dm_name'] for item in consumers if nodes[item]['dm_name']}
                    else:
                        enclosing = [row for row in mounts if swap['path'].startswith(row['where'].rstrip('/') + '/')]
                        swap_unknown |= not enclosing
                        in_use |= bool(enclosing and max(enclosing, key=lambda row: len(row['where'])) in locations)
                checks.append(check('swap_in_use', 'fail' if in_use else 'unknown' if swap_unknown else 'pass', 'proc_swaps_mountinfo'))
            volumes.append({'number': len(volumes) + 1, 'device': '/dev/' + name,
                'model': safe_text(node['model']), 'capacity_bytes': node['size'],
                'role': 'system' if root_volume else 'data',
                'members': [safe_text(nodes[item]['dm_name'] or item) for item in sorted(consumers)],
                'mounts': [safe_text(row['where']) for row in locations],
                'filesystem': filesystem,
                'groups': [group['map_name'] for group in attached], 'checks': checks,
                'assessment': 'blocked' if any(row['result'] == 'fail' for row in checks) else
                              'recognized_existing' if attached else 'needs_checks'})
    for group in groups:
        if not any(group['map_name'] in volume['groups'] for volume in volumes):
            if len(volumes) >= doctor.MAX_DEVICES:
                observer.issues.append({'source': 'inventory', 'code': 'volume_count_limit'})
                break
            volumes.append({'number': len(volumes) + 1, 'device': None, 'model': '', 'capacity_bytes': None,
                'role': 'system' if group['role'] == 'root' else 'data', 'members': [group['map_name']],
                'mounts': [], 'filesystem': {'type': 'unknown'}, 'groups': [group['map_name']],
                'checks': [check('registered_map_not_resolved', 'unknown', 'registration_sysfs')],
                'assessment': 'unknown'})
    stable = inventory is not None and mount_rows is not None and read('snapshot_recheck',
        lambda: nodes == metadata.inventory() and mounts == metadata.mounts() and swaps == metadata.swaps(), missing=False)
    for group in groups:
        if group['map_name'] in snapshots:
            unchanged = read('map_recheck', lambda: original_map_reader(group['map_name']) == snapshots[group['map_name']], missing=False)
            if not unchanged:
                stable = False
    changed_configuration = configuration.recheck()
    if changed_configuration:
        stable = False
        observer.issues.append({'source': 'configuration', 'code': 'trusted_configuration_changed_or_unavailable'})
        for role in ('root', 'manager'):
            if role in changed_configuration:
                receipts[role]['state'] = 'unknown'
        if 'package' in changed_configuration:
            package_kernel = None
            for row in environment_checks:
                if row['code'] == 'package_kernel':
                    row['result'] = 'unknown'
        for volume in volumes:
            volume['checks'].append(check('trusted_configuration', 'unknown', 'configuration_recheck'))
            if 'relationships' in changed_configuration:
                for row in volume['checks']:
                    if row['source'].startswith('registration_'):
                        row['result'] = 'unknown'
    if not stable or not graph_ok:
        observer.issues.append({'source': 'snapshot', 'code': 'snapshot_changed_or_incomplete'})
        for volume in volumes:
            volume['assessment'] = 'unknown'
        for group in groups:
            group['evidence'].update(current_runtime='unknown', current_boot_owner=False, restart='unknown', topology='unknown')
            if group['observation']:
                group['observation'].update(state='unknown', owner_matches=False, evidence_from_this_boot=False)
                group['observation']['mapping']['identity_matches'] = None
    report = {'schema': 1, 'operation': 'discover', 'captured_at_unix': int(time.time()),
            'snapshot_id': observer.token(str(time.time_ns())),
            'scope': {'mutations': 'none', 'media_probe': 'not_run', 'report': 'local_not_redacted'},
            'environment': {'kernel_release': kernel, 'package_kernel_release': package_kernel, 'multipath_target': version,
                            'queue_timeout_seconds': timeout, 'checks': environment_checks,
                            'public_support_policy': 'target_decided_combination_unvalidated'},
            'preparation': receipts, 'volumes': volumes, 'groups': groups,
            'issues': observer.issues, 'snapshot_stable': stable,
            'configuration_stable': {scope: scope not in changed_configuration
                                     for scope in ('relationships', 'root', 'manager', 'package')}}
    if len(doctor.encode(report)) > doctor.EXPORT_LIMIT:
        raise doctor.InputError('discovery_output_limit')
    if inputs is not None:
        # Internal in-process handoff, not a caller-supplied trust/CLI option.
        inputs.update(nodes=nodes, mounts=mounts, swaps=swaps, boot_id=boot,
                      environment=report['environment'], configuration=configuration.fingerprints())
    return report


REASONS = {
    'trusted_configuration': '可信配置发生变化或无法复读，关联与当前结论已失效，请重新查询',
    'multipath_interface': '已加载的 DM multipath 接口版本',
    'queue_timeout': '当前全局无路径排队超时至少 10 秒',
    'multipathd_units': '系统 multipathd 服务／socket 未在运行',
    'manual_multipathd_processes': '手工启动的其他 multipathd 尚未排查',
    'package_kernel': '运行内核与当前管理代码附带的运行包记录一致',
    'mount_policy': 'fstab／自动挂载冲突尚未完整核对',
    'cached_filesystem_unsupported': 'udev 缓存显示数据文件系统不在现有支持范围内，介质尚未核验',
    'swap_in_use': '所选数据卷未承载活动 swap（已检查列出的路径）',
    'registered_map_topology': '已登记 DM 表或底层依赖与登记不一致',
    'data_upper_layers': '已登记数据映射存在不支持的上层映射',
    'media_identity': '尚未读取介质完成身份核验',
    'partition_required': '当前接入要求已有分区，不支持整盘文件系统',
    'usb_serial_missing': '未取得接入所需的 USB 序列号',
    'device_instance': '缺少 diskseq，无法完整排除设备实例替换',
    'system_disk_sibling': '与系统挂载共用物理盘，当前数据入口不支持接管此分区',
    'multiple_protection_groups': '同一分区关联多个保护对象，需要检查',
    'raw_partition_mounted': '裸分区正在挂载，不能在线改接',
    'unmanaged_upper_layers': '存在尚未纳入本项目的上层映射',
    'mapping_not_enrolled': '尚无已登记保护关系，仍需完整接入检查',
    'enrollment_kernel': '运行内核与登记内核一致',
    'registered_map_not_resolved': '登记存在，但未定位当前 USB 保护映射；重启未必解决',
}
RESULTS = {'pass': '通过', 'fail': '阻碍', 'unknown': '未知', 'not_checked': '未检查'}
ASSESSMENTS = {'blocked': '存在接入阻碍', 'recognized_existing': '识别到已有保护关系',
               'needs_checks': '还需接入检查', 'unknown': '信息不足或采样变化'}
RUNTIME_STATES = {'ready': '本次控制器归属与就绪记录吻合', 'recovering': '恢复进行中',
                  'failed': '运行失败或恢复终止', 'refused': '接入被拒绝',
                  'installed_not_running': '配置存在但当前未运行',
                  'not_observed': '未取得当前实例证据', 'unknown': '未知'}
PREPARATION_STATES = {'installed': '按安装收据已准备', 'not_observed': '未取得准备记录',
    'preparing': '准备未完成', 'installing': '安装未完成', 'upgrading': '升级未完成',
    'removed': '根保护入口已撤除', 'uninstalled': '管理集成已撤除',
    'failed_rolled_back': '安装失败并记录回退', 'upgrade_failed': '升级失败', 'unknown': '未知'}


def format_text(report, selection=None):
    lines = ['USB 存储保护 · 只读发现与预检',
             '仅观察，不登记、不启用；当前检查不证明 I/O、文件系统或应用完整。',
             '运行内核：' + safe_text(report['environment']['kernel_release']),
             '首版目标：Ubuntu 24.04 / x86_64 / 官方 HWE；具体组合未验收，内核号不自动放行。']
    for role, receipt in report['preparation'].items():
        lines.append(('根盘' if role == 'root' else '管理集成') + '准备记录：' + PREPARATION_STATES[receipt['state']] + '（未复核产物完整性）')
    volumes = report['volumes']
    if selection is not None:
        volumes = [row for row in volumes if row['number'] == selection]
        if not volumes:
            raise ValueError('编号不在本次快照中')
    for volume in volumes:
        size = '容量未知' if volume['capacity_bytes'] is None else '%.2f GiB' % (volume['capacity_bytes'] / 1024**3)
        lines.append('\n%d. %s · %s · %s · %s' % (volume['number'], volume['device'] or '离线／未定位对象',
            volume['model'] or '型号未知', size, '系统卷' if volume['role'] == 'system' else '数据卷'))
        lines.append('   关联范围：' + '、'.join(volume['members']))
        lines.append('   挂载位置：' + ('、'.join(volume['mounts']) or '未观察到'))
        lines.append('   ' + ASSESSMENTS[volume['assessment']])
        for row in volume['checks']:
            if selection is not None or row['result'] != 'pass':
                lines.append('   [%s] %s' % (RESULTS[row['result']], REASONS[row['code']]))
        for group in report['groups']:
            if group['map_name'] not in volume['groups']:
                continue
            evidence = group['evidence']
            lines.append('   当前运行核对：' + RUNTIME_STATES[evidence['current_runtime']])
            lines.append('   已登记 DM 表与底层依赖：' + RESULTS[evidence['topology']] + '；介质身份：未检查')
            lines.append('   候选切换：' + {'required': '收据记录的候选 ELF 与当前实例不同；若要切换需冷启动，候选完整性尚待核对',
                'binary_matches_dependencies_not_checked': 'ELF 相同，依赖尚未核对',
                'unknown': '尚不能确定是否待重启'}[evidence['restart']])
            lines.append('   恢复实验：本次未执行，也未据历史报告推定当前生效')
    if not volumes:
        lines.append('未列出对象；读取失败时不能据此认定没有设备。')
    if report['issues']:
        lines.append('\n存在 %d 项读取异常／信息缺失，请使用 doctor 进一步诊断。' % len(report['issues']))
    lines.append('\n这是本地设备快照，包含挂载路径；分享诊断请使用现有脱敏 export。')
    return '\n'.join(lines)


def show(*, json_output=False):
    report = collect()
    if json_output:
        print(doctor.encode(report).decode())
        return
    print(format_text(report))
    if sys.stdin.isatty() and report['volumes']:
        try:
            value = input('\n输入编号查看只读详情，直接回车退出：').strip()
            if value:
                print(format_text(report, int(value)))
        except (ValueError, EOFError, KeyboardInterrupt):
            print('\n已退出；未执行任何启用操作。')
