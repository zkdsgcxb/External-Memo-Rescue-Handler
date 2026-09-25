"""Read-only admission of one enrolled USB partition for a DM transaction.

A credential binds observations to a held block-device fd and one owner epoch.
It reduces userspace TOCTOU exposure; it does not prove which kernel object a
later DM table load acquired, nor authenticate a byte-for-byte cloned disk.
"""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import struct
import threading
import time

from rescue import command, rows


# Current x86_64 Linux UAPI; this project requires its kernel 7.0 baseline.
BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268
BLKGETDISKSEQ = 0x80081280


class AdmissionError(RuntimeError):
    pass


def readonly(args, timeout=3):
    if args[0] == '/sbin/lvm':
        if '--readonly' not in args or '--devices' not in args:
            raise ValueError('guard may only read explicitly selected LVM devices')
        args = [*args, '--config', 'devices { multipath_component_detection=0 }']
    return command(args, timeout=min(timeout, 3))


def layout(node, runner=readonly):
    report = rows(runner(['/sbin/lvm', 'lvs', '--readonly', '--devices', node,
                         '--segments', '--reportformat', 'json', '--units', 's',
                         '--nosuffix', '-o',
                         'lv_name,lv_uuid,vg_uuid,segtype,seg_start,seg_size,seg_pe_ranges']), 'seg')
    normalized = []
    for row in report:
        clean = {key: value.strip() for key, value in row.items()}
        # Keep PE placement; Linux node names change during reenumeration.
        clean['seg_pe_ranges'] = ' '.join(
            part.rsplit(':', 1)[-1] for part in clean['seg_pe_ranges'].split())
        normalized.append(clean)
    return sorted(normalized, key=lambda row: (row['lv_name'], row['seg_start']))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def _integer(path):
    return int(path.read_text().strip())


def _ioctl_number(fd, request, fmt):
    value = bytearray(struct.calcsize(fmt))
    fcntl.ioctl(fd, request, value, True)
    return struct.unpack(fmt, value)[0]


class Candidate:
    """Live fd plus a JSON-safe statement of what this owner verified."""
    def __init__(self, admission, fd, record):
        self._admission = admission
        self.fd = fd
        self._record = record

    @property
    def node(self):
        return self._record['instance']['node']

    @property
    def dev(self):
        return self._record['instance']['dev']

    @property
    def sys_path(self):
        return self._record['instance']['sys_path']

    @property
    def diskseq(self):
        return self._record['instance']['diskseq']

    @property
    def partition_sectors(self):
        return self._record['instance']['partition_sectors']

    @property
    def logical_block_size(self):
        return self._record['instance']['logical_block_size']

    @property
    def layout_digest(self):
        return self._record['layout_digest']

    @property
    def verified_at(self):
        return self._record['verified_at']

    @property
    def deadline(self):
        return self._record['deadline']

    @property
    def owner_epoch(self):
        return self._record['owner_epoch']

    def to_dict(self):
        # No fd is transferable through the transaction journal. Also prevent
        # callers from mutating the credential by changing the returned object.
        return json.loads(json.dumps(self._record))

    def revalidate(self, owner_epoch, check_layout=True):
        return self._admission.revalidate(self, owner_epoch, check_layout)

    def close(self):
        if self.fd is not None:
            fd, self.fd = self.fd, None
            os.close(fd)

    def __enter__(self):
        if self.fd is None:
            raise AdmissionError('Candidate fd is closed')
        return self

    def __exit__(self, *_):
        self.close()


class Admission:
    def __init__(self, config, recovery, *, clock=time.monotonic, boot_id=None):
        self.recovery = recovery
        self.clock = clock
        self.boot_id = boot_id or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.partition_sectors = config['partition_sectors']
        self.logical_block_size = config['logical_block_size']
        self.layout_digest = digest(config['layout'])
        self.layout_version = config.get('layout_version', self.layout_digest)
        self.enrollment_digest = self._enrollment_digest()
        self._lock = threading.Lock()

    def _enrollment_digest(self):
        return digest({
            'identity': self.recovery.c, 'partition_sectors': self.partition_sectors,
            'logical_block_size': self.logical_block_size,
            'layout_digest': self.layout_digest, 'layout_version': self.layout_version})

    def _check_enrollment(self):
        if self._enrollment_digest() != self.enrollment_digest:
            raise AdmissionError('Enrollment changed; a new admission policy is required')

    def _budget(self, deadline):
        if (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                or not math.isfinite(deadline) or self.clock() >= deadline):
            raise AdmissionError('Candidate verification deadline expired')

    def _snapshot(self, node, fd):
        """Only fd ioctls and sysfs; never enumerate unrelated device media."""
        held = os.fstat(fd)
        current = os.stat(node)
        if not stat.S_ISBLK(held.st_mode) or not stat.S_ISBLK(current.st_mode):
            raise AdmissionError('Candidate must be a block device')
        if held.st_rdev != current.st_rdev:
            raise AdmissionError('Candidate node no longer refers to the held device')
        sys_path = (self.recovery.sys / 'class/block' / Path(node).name).resolve(strict=True)
        if not (sys_path / 'partition').is_file():
            raise AdmissionError('Candidate is not the enrolled partition')
        disk = sys_path.parent
        if (sys_path / 'dev').read_text().strip() != f'{os.major(held.st_rdev)}:{os.minor(held.st_rdev)}':
            raise AdmissionError('Candidate sysfs device number differs')
        diskseq = _integer(disk / 'diskseq')
        if diskseq <= 0 or _ioctl_number(fd, BLKGETDISKSEQ, '=Q') != diskseq:
            raise AdmissionError('Candidate disk instance differs from held fd')
        sectors = _integer(sys_path / 'size')
        if sectors != self.partition_sectors or _ioctl_number(fd, BLKGETSIZE64, '=Q') != sectors * 512:
            raise AdmissionError('Partition size differs')
        block_size = _integer(disk / 'queue/logical_block_size')
        if (block_size != self.logical_block_size or block_size <= 0
                or _ioctl_number(fd, BLKSSZGET, '=I') != block_size):
            raise AdmissionError('Candidate logical block size differs')
        if _integer(sys_path / 'partition') != self.recovery.c['partition_number']:
            raise AdmissionError('Candidate partition number differs')
        disk_sectors = _integer(disk / 'size')
        if disk_sectors != self.recovery.c['sectors']:
            raise AdmissionError('Disk capacity differs')
        return {'node': node, 'dev': held.st_rdev, 'sys_path': str(sys_path),
                'disk_sys_path': str(disk), 'diskseq': diskseq,
                'disk_sectors': disk_sectors, 'partition_sectors': sectors,
                'partition_number': _integer(sys_path / 'partition'),
                'partition_start': _integer(sys_path / 'start'),
                'logical_block_size': block_size}

    def _same_instance(self, node, fd, expected):
        # Recheck uniqueness as well as this fd. A newly appeared duplicate
        # serial must not be admitted just because the held instance survives.
        if self.recovery.candidate_node() != node or self._snapshot(node, fd) != expected:
            raise AdmissionError('Candidate changed during verification')

    def _layout(self, node):
        actual = digest(layout(node, runner=self.recovery.run))
        if actual != self.layout_digest:
            raise AdmissionError('LV layout differs from enrolled metadata')
        return actual

    def verify(self, deadline, owner_epoch):
        if not self._lock.acquire(blocking=False):
            raise AdmissionError('Another candidate verification is already running')
        fd = None
        try:
            self._check_enrollment()
            self._budget(deadline)
            # No blkid/LVM until exactly one enrolled disk and partition exist.
            node = self.recovery.candidate_node()
            fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            instance = self._snapshot(node, fd)
            if self.recovery.verify() != node:
                raise AdmissionError('Candidate changed during identity verification')
            self._budget(deadline)
            self._same_instance(node, fd, instance)
            layout_digest = self._layout(node)
            self._budget(deadline)
            if self.recovery.verify() != node:
                raise AdmissionError('Candidate changed during identity verification')
            self._same_instance(node, fd, instance)
            self._budget(deadline)
            self._check_enrollment()
            record = {'schema': 1, 'boot_id': self.boot_id,
                      'enrollment_digest': self.enrollment_digest,
                      'layout_version': self.layout_version, 'layout_digest': layout_digest,
                      'instance': instance, 'verified_at': self.clock(),
                      'deadline': deadline, 'owner_epoch': owner_epoch}
            candidate = Candidate(self, fd, record)
            fd = None
            return candidate
        finally:
            if fd is not None:
                os.close(fd)
            self._lock.release()

    def revalidate(self, candidate, owner_epoch, check_layout=True):
        if candidate._admission is not self or candidate.fd is None:
            raise AdmissionError('Candidate has no live fd from this admission policy')
        if candidate.owner_epoch != owner_epoch:
            raise AdmissionError('Candidate belongs to a different owner epoch')
        if not self._lock.acquire(blocking=False):
            raise AdmissionError('Another candidate verification is already running')
        try:
            self._check_enrollment()
            self._budget(candidate.deadline)
            self._same_instance(candidate.node, candidate.fd, candidate._record['instance'])
            if check_layout:
                self._layout(candidate.node)
                self._same_instance(candidate.node, candidate.fd, candidate._record['instance'])
            self._budget(candidate.deadline)
            self._check_enrollment()
            return candidate
        finally:
            self._lock.release()
