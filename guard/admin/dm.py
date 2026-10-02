"""Read-only libdevmapper snapshots for existing-map enrollment checks."""
import ctypes as C

from .admission import digest
from .linux_abi import DMInfo


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


def table_digest(targets):
    """Normalize only kernel-mutated queue policy / selected path-group state.

    Enrollment accepts one group/one path. Every geometry, backend and
    selector argument is retained in the comparison.
    """
    result=[]
    for start,size,kind,params in targets:
        words=params.split()
        if kind=='multipath':
            count=int(words[0]);features=words[1:count+1]
            features=[word for word in features if word!='queue_if_no_path']
            tail=words[count+1:]
            handler_count=int(tail[0])
            group_index=handler_count+1
            # Only the enrolled single-group topology permits 0/1 selection.
            if tail[group_index]!='1' or tail[group_index+1] not in ('0','1'):
                raise RuntimeError('Unexpected multipath topology')
            tail[group_index+1]='1'
            words=[str(len(features)),*features,*tail]
        result.append([start,size,kind,' '.join(words)])
    return digest(result)


def expected_table(sectors, device):
    """Describe the existing table required for adoption; never install it."""
    if type(sectors) is not int or sectors <= 0:
        raise ValueError("Invalid partition size")
    return [[0, sectors, "multipath",
             f"3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 {device} 1"]]
