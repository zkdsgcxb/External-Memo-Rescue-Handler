"""In-process libdevmapper status queries and bounded kernel uevent waiting."""
import ctypes as C
import errno
import fcntl
import os
import select
import socket
import time


# Linux UAPI _IO(0xfd, 18), introduced in 6.16. Noble's userspace header
# predates it. This lab targets the current x86_64 kernel with this interface.
DM_MPATH_PROBE_PATHS = 0xfd12


class PathProbe:
    """One on-demand ioctl; a blocked driver must never spawn more workers.

    Success means the ioctl completed, not that a read or full health check
    succeeded. The kernel can skip paths or ignore non-path read errors.
    """
    def __init__(self):
        self._thread=None
        self._result=None

    @property
    def busy(self):
        return self._thread is not None or self._result is not None

    def start(self,device,token):
        if self.busy:
            raise RuntimeError('Previous path probe has not been consumed')
        # No worker or threading import during ordinary healthy monitoring.
        import threading
        def run():
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
            status={0:'completed',errno.ENOTCONN:'no_paths'}.get(error,'error')
            self._result={'token':token,'errno':error,'elapsed':time.monotonic()-started,'status':status,'source':'ioctl'}
        self._thread=threading.Thread(target=run,name='dm-path-probe',daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._thread=None
            raise

    def poll(self):
        if self._thread is not None:
            if self._thread.is_alive():
                return None
            self._thread.join()
            self._thread=None
        result=self._result
        self._result=None
        return result


class DeviceMapper:
    def __init__(self):
        self.lib=C.CDLL('libdevmapper.so.1.02.1')
        signatures={
            'dm_task_create':(C.c_void_p,[C.c_int]),
            'dm_task_destroy':(None,[C.c_void_p]),
            'dm_task_set_name':(C.c_int,[C.c_void_p,C.c_char_p]),
            'dm_task_run':(C.c_int,[C.c_void_p]),
            'dm_task_get_uuid':(C.c_char_p,[C.c_void_p]),
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
        # DM_DEVICE_STATUS from libdevmapper.h. Tasks own returned string storage.
        task=self.lib.dm_task_create(10)
        if not task:
            raise RuntimeError('Cannot allocate DM status task')
        try:
            if not self.lib.dm_task_set_name(task,name.encode()) or not self.lib.dm_task_run(task):
                raise RuntimeError('Cannot query DM map '+name)
            uuid=self.lib.dm_task_get_uuid(task)
            targets=[];cursor=None
            while True:
                start=C.c_uint64();size=C.c_uint64();kind=C.c_char_p();params=C.c_char_p()
                cursor=self.lib.dm_get_next_target(task,cursor,C.byref(start),C.byref(size),C.byref(kind),C.byref(params))
                if kind.value:
                    targets.append((kind.value.decode(),(params.value or b'').decode()))
                if not cursor:
                    break
            return (uuid or b'').decode(),targets
        finally:
            self.lib.dm_task_destroy(task)


class Events:
    """One bounded batch per wakeup; events are hints, never fault evidence."""
    def __init__(self):
        self.sock=socket.socket(socket.AF_NETLINK,socket.SOCK_DGRAM,socket.NETLINK_KOBJECT_UEVENT if hasattr(socket,'NETLINK_KOBJECT_UEVENT') else 15)
        self.sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,256*1024)
        self.sock.bind((0,1));self.sock.setblocking(False)

    def wait(self,seconds):
        if not select.select([self.sock],[],[],max(0,seconds))[0]:
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
            if peer[0]==0 and b'SUBSYSTEM=block' in data.split(b'\0'):
                relevant=True  # Includes DM path change uevents and disk/partition changes.
        if not relevant:
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
