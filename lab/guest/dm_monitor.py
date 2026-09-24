"""In-process libdevmapper status queries and bounded kernel uevent waiting."""
import ctypes as C
import errno
import select
import socket
import time


class DeviceMapper:
    def __init__(self):
        self.lib=C.CDLL('libdevmapper.so.1.02.1')
        signatures={
            'dm_task_create':(C.c_void_p,[C.c_int]),
            'dm_task_destroy':(None,[C.c_void_p]),
            'dm_task_set_name':(C.c_int,[C.c_void_p,C.c_char_p]),
            'dm_task_run':(C.c_int,[C.c_void_p]),
            'dm_task_get_uuid':(C.c_char_p,[C.c_void_p]),
            'dm_get_next_target':(C.c_void_p,[C.c_void_p,C.c_void_p,C.POINTER(C.c_uint64),C.POINTER(C.c_uint64),C.POINTER(C.c_char_p),C.POINTER(C.c_char_p)]),
        }
        for name,(result,args) in signatures.items():
            fn=getattr(self.lib,name);fn.restype=result;fn.argtypes=args

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
