"""Linux UAPI and libdevmapper ABI used by cold enrollment checks.

The validated targets use the generic ioctl encoding and the LP64 ABI. Reject
other layouts before touching a device; an architecture name alone does not
prove that Python itself is a 64-bit process (for example, x86 x32).
"""
import ctypes as C
import os
import sys


SUPPORTED_MACHINES = ('x86_64', 'aarch64', 'riscv64')


def validate_abi(machine, *, system, pointer_bytes, size_t_bytes, int_bytes,
                 byteorder):
    if (system != 'Linux' or machine not in SUPPORTED_MACHINES
            or pointer_bytes != 8 or size_t_bytes != 8 or int_bytes != 4
            or byteorder != 'little'):
        raise RuntimeError(
            'Guard requires a validated Linux LP64 little-endian ABI '
            '(x86_64, aarch64, riscv64); '
            f'got {system}/{machine}, pointer={pointer_bytes}, '
            f'size_t={size_t_bytes}, int={int_bytes}, {byteorder}-endian')
    return machine


MACHINE = validate_abi(
    os.uname().machine, system=os.uname().sysname,
    pointer_bytes=C.sizeof(C.c_void_p), size_t_bytes=C.sizeof(C.c_size_t),
    int_bytes=C.sizeof(C.c_int), byteorder=sys.byteorder)


def _ioctl(direction, kind, number, size=0):
    # include/uapi/asm-generic/ioctl.h. All validated targets use this layout.
    return direction << 30 | size << 16 | kind << 8 | number


# BLKGETSIZE64 encodes sizeof(size_t), although its output is always __u64.
# BLKSSZGET is the historical _IO request and returns an int through its arg.
BLKGETSIZE64 = _ioctl(2, 0x12, 114, C.sizeof(C.c_size_t))
BLKSSZGET = _ioctl(0, 0x12, 104)
BLKGETDISKSEQ = _ioctl(2, 0x12, 128, C.sizeof(C.c_uint64))


class DMInfo(C.Structure):
    """Public libdevmapper dm_info, including internal_suspend."""
    _fields_ = [
        ('exists', C.c_int), ('suspended', C.c_int),
        ('live_table', C.c_int), ('inactive_table', C.c_int),
        ('open_count', C.c_int32), ('event_nr', C.c_uint32),
        ('major', C.c_uint32), ('minor', C.c_uint32),
        ('read_only', C.c_int), ('target_count', C.c_int32),
        ('deferred_remove', C.c_int), ('internal_suspend', C.c_int),
    ]


if (C.sizeof(DMInfo) != 48 or C.alignment(DMInfo) != 4
        or any(getattr(DMInfo, name).offset != index * 4
               for index, (name, _) in enumerate(DMInfo._fields_))):
    raise RuntimeError('Unexpected libdevmapper dm_info layout')
