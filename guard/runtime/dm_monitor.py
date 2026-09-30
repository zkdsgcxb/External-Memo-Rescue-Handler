"""In-process libdevmapper status queries and bounded kernel uevent waiting."""
import ctypes as C
import errno
import fcntl
import os
import select
import socket
import time

from linux_abi import DMInfo, DM_MPATH_PROBE_PATHS


def probe_paths(device, token):
    """Blocking kernel probe, called only by the Guard's owned worker.

    Completion does not establish full device health: the kernel can skip
    paths and ignore errors which it does not classify as path failures.
    """
    started=time.monotonic()
    error=0
    fd=None
    try:
        fd=os.open(device,os.O_RDONLY|os.O_NONBLOCK|os.O_CLOEXEC)
        fcntl.ioctl(fd,DM_MPATH_PROBE_PATHS)
    except OSError as exc:
        error=exc.errno or errno.EIO
    finally:
        if fd is not None:
            os.close(fd)
    return {'token':token,'errno':error,'elapsed':time.monotonic()-started,
            'status':{0:'completed',errno.ENOTCONN:'no_paths'}.get(error,'error'),
            'source':'ioctl'}


class DeviceMapper:
    def __init__(self):
        self.lib=C.CDLL('libdevmapper.so.1.02.1')
        signatures={
            'dm_task_create':(C.c_void_p,[C.c_int]),
            'dm_task_destroy':(None,[C.c_void_p]),
            'dm_task_set_name':(C.c_int,[C.c_void_p,C.c_char_p]),
            'dm_task_run':(C.c_int,[C.c_void_p]),
            'dm_task_get_uuid':(C.c_char_p,[C.c_void_p]),
            'dm_task_get_info':(C.c_int,[C.c_void_p,C.POINTER(DMInfo)]),
            'dm_task_query_inactive_table':(C.c_int,[C.c_void_p]),
            'dm_task_get_versions':(C.c_void_p,[C.c_void_p]),
            'dm_get_next_target':(C.c_void_p,[C.c_void_p,C.c_void_p,C.POINTER(C.c_uint64),C.POINTER(C.c_uint64),C.POINTER(C.c_char_p),C.POINTER(C.c_char_p)]),
        }
        for name,(result,args) in signatures.items():
            fn=getattr(self.lib,name);fn.restype=result;fn.argtypes=args

    def target_version(self,name):
        # DM_DEVICE_LIST_VERSIONS and struct dm_versions from libdevmapper.h.
        # Only query once at startup; all returned memory belongs to this task.
        class Version(C.Structure):
            _fields_=[('next',C.c_uint32),('version',C.c_uint32*3)]
        task=self.lib.dm_task_create(16)
        if not task:
            raise RuntimeError('Cannot allocate DM version task')
        try:
            if not self.lib.dm_task_run(task):
                raise RuntimeError('Cannot query kernel DM target versions')
            address=self.lib.dm_task_get_versions(task)
            while address:
                entry=Version.from_address(address)
                if C.string_at(address+C.sizeof(Version)).decode()==name:
                    return tuple(entry.version)
                if not entry.next:
                    break
                address+=entry.next
            raise RuntimeError('Kernel DM target not loaded: '+name)
        finally:
            self.lib.dm_task_destroy(task)

    def query(self,name):
        result=self._read(name,10)
        self.last_info=result['info']
        return result['uuid'],[(target[2],target[3]) for target in result['targets']]

    def snapshot(self,name):
        active=self._read(name,11)  # DM_DEVICE_TABLE
        inactive=self._read(name,11,inactive=True)
        if active['uuid']!=inactive['uuid']:
            raise RuntimeError('DM identity changed while reading tables')
        return {'uuid':active['uuid'],'info':active['info'],
                'active':active['targets'],'inactive':inactive['targets']}

    def _read(self,name,operation,inactive=False):
        # Tasks own all returned strings; copy them before destroying the task.
        task=self.lib.dm_task_create(operation)
        if not task:
            raise RuntimeError('Cannot allocate DM status task')
        try:
            if not self.lib.dm_task_set_name(task,name.encode()):
                raise RuntimeError('Cannot name DM task')
            if inactive and not self.lib.dm_task_query_inactive_table(task):
                raise RuntimeError('Cannot select inactive DM table')
            if not self.lib.dm_task_run(task):
                raise RuntimeError('Cannot query DM map '+name)
            info=DMInfo()
            if not self.lib.dm_task_get_info(task,C.byref(info)) or not info.exists:
                raise RuntimeError('DM map does not exist: '+name)
            uuid=self.lib.dm_task_get_uuid(task)
            targets=[];cursor=None
            while True:
                start=C.c_uint64();size=C.c_uint64();kind=C.c_char_p();params=C.c_char_p()
                cursor=self.lib.dm_get_next_target(task,cursor,C.byref(start),C.byref(size),C.byref(kind),C.byref(params))
                if kind.value:
                    targets.append([start.value,size.value,kind.value.decode(),(params.value or b'').decode()])
                if not cursor:
                    break
            return {'uuid':(uuid or b'').decode(),'targets':targets,
                    'info':{key:getattr(info,key) for key,_ in DMInfo._fields_}}
        finally:
            self.lib.dm_task_destroy(task)


class Events:
    """One bounded batch per wakeup; events are hints, never fault evidence."""
    def __init__(self):
        self.sock=socket.socket(socket.AF_NETLINK,socket.SOCK_DGRAM,socket.NETLINK_KOBJECT_UEVENT if hasattr(socket,'NETLINK_KOBJECT_UEVENT') else 15)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,256*1024)
        self.sock.bind((0,1));self.sock.setblocking(False)
        self.paths=()

    def watch(self,sys_paths=()):
        """Limit healthy checks to enrolled paths; an empty scope sees all block events.

        Keep the periodic check even with a scope: netlink can lose events.
        Recovery clears the scope because re-enumeration can change USB ports.
        """
        self.paths=tuple(path.removeprefix('/sys').encode() for path in sys_paths)

    def relevant(self,data):
        fields=data.split(b'\0')
        if b'SUBSYSTEM=block' not in fields:
            return False
        if not self.paths:
            return True
        path=next((field[8:] for field in fields if field.startswith(b'DEVPATH=')),None)
        # A malformed block hint warrants reconciliation, never a healthy verdict.
        return path is None or any(path==prefix or path.startswith(prefix+b'/')
                                   for prefix in self.paths)

    def wait(self,seconds,completion_fd=None,*,defer_events=False):
        readers=[] if defer_events else [self.sock]
        if completion_fd is not None:
            readers.append(completion_fd)
        self.operation_ready=False
        ready=select.select(readers,[],[],max(0,seconds))[0]
        if not ready:
            return False
        # The worker owns draining its eventfd; report it separately so
        # completion bypasses event-storm coalescing and retry backoff.
        self.operation_ready=completion_fd is not None and completion_fd in ready
        if self.sock not in ready:
            return False
        relevant=False
        for _ in range(64):
            try:
                data,peer=self.sock.recvfrom(16384)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno==errno.ENOBUFS:
                    return True  # Reconcile once; the periodic check covers lost events.
                raise
            if peer[0]==0 and self.relevant(data):
                relevant=True
        if not relevant and not self.operation_ready:
            time.sleep(min(0.05,max(0,seconds)))
        return relevant

    def close(self):
        self.sock.close()


class Schedule:
    """Serial recovery, bounded event wakeups and monotonic retry deadlines."""
    def __init__(self,now):
        self.next_check=now
        self.event_after=now
        self.delay=0.1

    def due(self,now,event=False):
        return now>=self.next_check or event and now>=self.event_after

    def completed(self,now,recovering):
        self.next_check=now+(self.delay if recovering else 1.0)
        self.event_after=self.next_check if recovering else now+0.1
        self.delay=min(0.8,self.delay*2) if recovering else 0.1
