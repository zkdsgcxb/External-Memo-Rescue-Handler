"""Read-only enrollment checks bound to a held block-device descriptor.

Recheck device identity and layout before accepting an enrollment. This cold
admin policy never loads DM tables or owns a recovery transaction.
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
from .linux_abi import BLKGETDISKSEQ, BLKGETSIZE64, BLKSSZGET


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


def identity_layout(recovery, node):
    """Read the enrolled content using its filesystem or LVM policy."""
    if hasattr(recovery, 'admission_layout'):
        return recovery.admission_layout(node)
    return layout(node, runner=recovery.run)


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
    """A held descriptor and observations from one enrollment check."""
    def __init__(self, admission, fd, record):
        self._admission = admission
        self.fd = fd
        self._record = record

    @property
    def node(self):
        return self._record['instance']['node']

    @property
    def sys_path(self):
        return self._record['instance']['sys_path']

    @property
    def diskseq(self):
        return self._record['instance']['diskseq']

    @property
    def deadline(self):
        return self._record['deadline']

    def revalidate(self):
        return self._admission.revalidate(self)

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
    def __init__(self, config, recovery, *, clock=time.monotonic):
        self.recovery = recovery
        self.clock = clock
        self.partition_sectors = config['partition_sectors']
        self.partition_start = config.get('partition_start')
        if self.partition_start is not None and (type(self.partition_start) is not int or self.partition_start < 0):
            raise AdmissionError('Enrolled partition start must be a nonnegative integer')
        self.logical_block_size = config['logical_block_size']
        self.layout_digest = digest(config['layout'])
        self.layout_version = config.get('layout_version', self.layout_digest)
        self.enrollment_digest = self._enrollment_digest()
        self._lock = threading.Lock()

    def _enrollment_digest(self):
        enrollment = {
            'identity': self.recovery.c, 'partition_sectors': self.partition_sectors,
            'logical_block_size': self.logical_block_size,
            'layout_digest': self.layout_digest, 'layout_version': self.layout_version}
        if self.partition_start is not None:
            enrollment['partition_start'] = self.partition_start
        return digest(enrollment)

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
        start = _integer(sys_path / 'start')
        if self.partition_start is not None and start != self.partition_start:
            raise AdmissionError('Partition start differs from enrollment')
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
                'partition_start': start,
                'logical_block_size': block_size}

    def _same_instance(self, node, fd, expected):
        # Recheck uniqueness as well as this fd. A newly appeared duplicate
        # serial must not be admitted just because the held instance survives.
        if self.recovery.candidate_node() != node or self._snapshot(node, fd) != expected:
            raise AdmissionError('Candidate changed during verification')

    def _layout(self, node):
        actual = digest(identity_layout(self.recovery, node))
        if actual != self.layout_digest:
            raise AdmissionError('Device layout differs from enrolled metadata')
        return actual

    def verify(self, deadline):
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
            self._layout(node)
            self._budget(deadline)
            if self.recovery.verify() != node:
                raise AdmissionError('Candidate changed during identity verification')
            self._same_instance(node, fd, instance)
            self._budget(deadline)
            self._check_enrollment()
            record = {'instance': instance, 'deadline': deadline}
            candidate = Candidate(self, fd, record)
            fd = None
            return candidate
        finally:
            if fd is not None:
                os.close(fd)
            self._lock.release()

    def revalidate(self, candidate):
        if candidate._admission is not self or candidate.fd is None:
            raise AdmissionError('Candidate has no live fd from this admission policy')
        if not self._lock.acquire(blocking=False):
            raise AdmissionError('Another candidate verification is already running')
        try:
            self._check_enrollment()
            self._budget(candidate.deadline)
            self._same_instance(candidate.node, candidate.fd, candidate._record['instance'])
            self._layout(candidate.node)
            self._same_instance(candidate.node, candidate.fd, candidate._record['instance'])
            self._budget(candidate.deadline)
            self._check_enrollment()
            return candidate
        finally:
            self._lock.release()
