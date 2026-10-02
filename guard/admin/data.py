"""Read-only enrollment and entry checks for an existing USB data map.

Provisioning and mounting belong to the caller. This policy accepts only our
single-path multipath table; it never adopts a map managed by multipathd.
"""
import os
from pathlib import Path
import re
import stat
import time

from .admission import Admission, identity_layout, readonly
from .identity import FilesystemIdentity
from .dm import DeviceMapper, expected_table, table_digest


DATA_RUN = Path('/run/ram-rescue-data')
CRITICAL_MOUNTS = {'/', '/usr', '/var', '/home', '/workspace', '/boot', '/boot/efi'}
TIMEOUT = Path('/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs')


def validate_config(config):
    """One canonical owner directory per map, irrespective of config location."""
    name = config['map_name']
    if not re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}', name):
        raise ValueError('Data map names must start with rr-data-')
    if not re.fullmatch(r'RAMRESCUE-DATA-[A-Za-z0-9_-]{1,64}', config['map_uuid']):
        raise ValueError('Data map UUID must use the RAMRESCUE-DATA- namespace')
    base = DATA_RUN / name
    if (config['run_dir'] != str(base / 'state') or
            config.get('identity_path') != str(base / 'identity.json')):
        raise ValueError('Data profile requires its canonical RAM owner and identity paths')
    if type(config.get('queue_seconds')) is not int or not 2 <= config['queue_seconds'] <= 8:
        raise ValueError('Data admission budget must be 2..8 seconds')
    if type(config.get('partition_start')) is not int or config['partition_start'] < 0:
        raise ValueError('Data enrollment requires its original partition start sector')


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def mounts():
    # PID 1 sees the host mounts even when this check runs in the RAM chroot.
    entries = [line.split() for line in Path('/proc/1/mountinfo').read_text().splitlines()]
    for entry in entries:
        entry[4] = unescape(entry[4])
    return entries


def physical_disks(device, seen=None):
    """Walk only the selected mount's sysfs dependencies, without reading media."""
    seen = set() if seen is None else seen
    path = (Path('/sys/dev/block') / device).resolve(strict=True)
    if path in seen:
        raise RuntimeError('Cyclic block-device dependency')
    seen = seen | {path}
    if (path / 'partition').exists():
        return {path.parent}
    slaves = list((path / 'slaves').iterdir())
    if slaves:
        return set().union(*(physical_disks((entry / 'dev').read_text().strip(), seen)
                             for entry in slaves))
    return {path}


def check_isolation(node, sys_path, map_sys, identity, runner=readonly):
    dev = (sys_path / 'dev').read_text().strip()
    entries = mounts()
    if any(entry[2] == dev for entry in entries):
        raise RuntimeError('Raw partition is mounted; enroll before mounting through DM')
    for entry in entries:
        if entry[4] in CRITICAL_MOUNTS and (Path('/sys/dev/block') / entry[2]).exists():
            if sys_path.parent in physical_disks(entry[2]):
                raise RuntimeError('Data mode refuses a disk backing a system mount')
    if {path.resolve() for path in (sys_path / 'holders').iterdir()} != {map_sys}:
        raise RuntimeError('Partition must be held exclusively by the selected data map')
    if any((map_sys / 'holders').iterdir()):
        raise RuntimeError('Data mode accepts a filesystem directly on the map, without upper DM layers')
    map_dev = (map_sys / 'dev').read_text().strip()
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        fields = line.split()
        swap = unescape(fields[0])
        if Path(swap).resolve() in {Path(node), Path('/dev') / map_sys.name}:
            raise RuntimeError('Data mode does not accept swap devices')
        if fields[1] == 'file':
            enclosing = [entry for entry in entries if swap.startswith(entry[4].rstrip('/') + '/')]
            if enclosing and max(enclosing, key=lambda entry: len(entry[4]))[2] == map_dev:
                raise RuntimeError('Data mode does not accept a filesystem containing active swap')
    fstab = Path('/proc/1/root/etc/fstab').read_text()
    raw_names = {node, 'UUID=' + identity['fs_uuid'], 'PARTUUID=' + identity['partuuid'],
                 '/dev/disk/by-uuid/' + identity['fs_uuid'],
                 '/dev/disk/by-partuuid/' + identity['partuuid']}
    labels = None
    for line in fstab.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        source = unescape(fields[0])
        if source.startswith(('UUID=', 'PARTUUID=', 'LABEL=', 'PARTLABEL=')):
            tag, value = source.split('=', 1)
            value = value.strip('\"\'')
            source = tag + '=' + value
            if tag in ('UUID', 'PARTUUID'):
                wanted = identity['fs_uuid' if tag == 'UUID' else 'partuuid']
                if value.lower() == wanted.lower():
                    raise RuntimeError('fstab selects the filesystem through its ambiguous UUID')
            else:
                if labels is None:
                    labels = dict(item.split('=', 1) for item in runner(
                        ['/sbin/blkid', '-p', '-o', 'export', node]).splitlines() if '=' in item)
                if value == labels.get('LABEL' if tag == 'LABEL' else 'PART_ENTRY_NAME'):
                    raise RuntimeError('fstab selects the filesystem through its ambiguous label')
        if (source in raw_names or
                source.startswith('/dev/') and Path(source).resolve() == Path(node)):
            raise RuntimeError('fstab still selects the raw partition or its ambiguous UUID')


def check_environment(config):
    if os.uname().release != config['kernel_release']:
        raise RuntimeError('Data guard kernel differs from enrolled kernel_release')
    # This is a compatibility check, not a claim that the kernel timeout can
    # cancel a lower driver request. Never change the root map's global policy.
    if int(TIMEOUT.read_text()) < config['queue_seconds'] + 2:
        raise RuntimeError('Existing kernel no-path timeout is shorter than the data budget plus margin')
    for process in Path('/proc').glob('[0-9]*/comm'):
        try:
            name = process.read_text().strip()
        except FileNotFoundError:
            continue
        if name == 'multipathd':
            raise RuntimeError('multipathd is running; competing map controllers are unsupported')


def check_map(config, node, mapper):
    snapshot = mapper.snapshot(config['map_name'])
    info = snapshot['info']
    held = os.stat(node)
    if not stat.S_ISBLK(held.st_mode):
        raise RuntimeError('Data candidate must be a block partition')
    expected = expected_table(config['partition_sectors'],
                              f'{os.major(held.st_rdev)}:{os.minor(held.st_rdev)}')
    if (snapshot['uuid'] != config['map_uuid'] or snapshot['inactive'] or
            any(info[key] for key in ('suspended', 'internal_suspend', 'deferred_remove', 'read_only')) or
            table_digest(snapshot['active']) != table_digest(expected)):
        raise RuntimeError('Existing map is not the exclusive ready single-path data table')
    # table_digest deliberately tolerates the kernel removing queue_if_no_path;
    # adoption additionally requires that queuing is currently enabled.
    words = snapshot['active'][0][3].split()
    if 'queue_if_no_path' not in words[1:1 + int(words[0])]:
        raise RuntimeError('Data map must already enable queue_if_no_path')
    map_sys = (Path('/sys/dev/block') / f"{info['major']}:{info['minor']}").resolve(strict=True)
    sys_path = (Path('/sys/class/block') / Path(node).name).resolve(strict=True)
    if {entry.resolve() for entry in (map_sys / 'slaves').iterdir()} != {sys_path}:
        raise RuntimeError('Data map backing partition differs')
    return sys_path, map_sys


def usb_identity(node):
    node = str(Path(node).resolve(strict=True))
    if not stat.S_ISBLK(os.stat(node).st_mode):
        raise RuntimeError('Expected an existing USB block partition')
    sys_path = (Path('/sys/class/block') / Path(node).name).resolve(strict=True)
    if not (sys_path / 'partition').is_file():
        raise RuntimeError('Only partitioned USB data filesystems are supported')
    usb = next((parent for parent in sys_path.parent.parents
                if (parent / 'idVendor').exists()), None)
    if usb is None or not (usb / 'serial').is_file():
        raise RuntimeError('USB transport with a nonempty device serial is required')
    props = dict(line.split('=', 1) for line in
                 readonly(['/sbin/blkid', '-p', '-o', 'export', node]).splitlines() if '=' in line)
    identity = {'kind': 'filesystem', 'vid': (usb / 'idVendor').read_text().strip().lower(),
                'pid': (usb / 'idProduct').read_text().strip().lower(),
                'usb_serial': (usb / 'serial').read_text().strip(),
                'sectors': int((sys_path.parent / 'size').read_text()),
                'partition_number': int((sys_path / 'partition').read_text()),
                'partuuid': props.get('PART_ENTRY_UUID', ''),
                'fs_type': props.get('TYPE', ''), 'fs_uuid': props.get('UUID', '')}
    return node, sys_path, identity


def collect(map_name, partition):
    """Attest an existing map without creating, mounting, or changing it."""
    if not re.fullmatch(r'rr-data-[A-Za-z0-9_-]{1,64}', map_name):
        raise ValueError('Data map names must start with rr-data-')
    node, sys_path, identity = usb_identity(partition)
    recovery = FilesystemIdentity(identity, runner=readonly)
    mapper = DeviceMapper()
    if mapper.target_version('multipath') < (1, 15, 0):
        raise RuntimeError('DM_MPATH_PROBE_PATHS requires multipath target >= 1.15.0')
    config = {'schema': 1, 'profile': 'host-data', 'map_name': map_name,
              'map_uuid': mapper.snapshot(map_name)['uuid'],
              'kernel_release': os.uname().release, 'queue_seconds': 8,
              'run_dir': str(DATA_RUN / map_name / 'state'),
              'identity_path': str(DATA_RUN / map_name / 'identity.json'),
              'partition_sectors': int((sys_path / 'size').read_text()),
              'partition_start': int((sys_path / 'start').read_text()),
              'logical_block_size': int((sys_path.parent / 'queue/logical_block_size').read_text()),
              'layout': identity_layout(recovery, node)}
    validate_config(config)
    check_environment(config)
    _, map_sys = check_map(config, node, mapper)
    check_isolation(node, sys_path, map_sys, identity)
    with Admission(config, recovery).verify(time.monotonic() + 15) as candidate:
        candidate.revalidate()
        check_map(config, candidate.node, mapper)
        config.update(initial_node=candidate.node, initial_sys_path=candidate.sys_path,
                      initial_diskseq=candidate.diskseq)
    return {'schema': 1, 'identity': identity, 'guard': config}
