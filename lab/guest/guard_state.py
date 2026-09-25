"""Bounded RAM evidence and single-owner coordination for the disposable lab.

The journal survives an owner process, not a reboot. An owner epoch is a fencing
contract among our controllers; it cannot exclude an unrelated root dmsetup.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

RUN = Path('/run')
LOG_LIMIT = 64 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def atomic_json(path, value):
    path=Path(path)
    data=json.dumps(value,sort_keys=True)
    if len(data.encode())>LOG_LIMIT:
        raise ValueError('RAM evidence record exceeds limit')
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(data)
    temporary.replace(path)


def load_json(path):
    path=Path(path)
    if path.stat().st_size>LOG_LIMIT:
        raise ValueError('Oversized RAM evidence record')
    return json.loads(path.read_text())


class Owner:
    def __init__(self, run=RUN):
        self.run=Path(run)
        self.fd=os.open(self.run/'path-owner.lock',os.O_CREAT|os.O_RDWR|os.O_CLOEXEC,0o600)
        try:
            fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.epoch=uuid.uuid4().hex
            self.boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        except BaseException:
            os.close(self.fd)
            self.fd=None
            raise

    def close(self):
        if self.fd is not None:
            # Do not LOCK_UN: a still-running helper shares this open-file
            # description and must retain the fence until its ioctl finishes.
            os.close(self.fd)
            self.fd=None

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()


class Journal:
    def __init__(self,owner,name,map_uuid):
        self.path=owner.run/'path-transaction.json'
        self.record={'schema':1,'owner_epoch':owner.epoch,'boot_id':owner.boot_id,
                     'owner_pid':os.getpid(),'map_name':name,'map_uuid':map_uuid,
                     'phase':'starting','deadline':None,'candidate':None}

    def write(self,phase,**details):
        updated={**self.record,**details,'phase':phase,'updated_at':time.monotonic()}
        atomic_json(self.path,updated)
        self.record=updated


class Evidence:
    def __init__(self,run=RUN):
        self.run=Path(run)

    def event(self,entry):
        # Two files at most; one bounded record can exceed LOG_LIMIT only by
        # being rejected, never by growing an unbounded forensic tail in RAM.
        data=json.dumps(entry)+'\n'
        if len(data.encode())>LOG_LIMIT:
            raise ValueError('Event exceeds RAM evidence limit')
        path=self.run/'path-events.jsonl'
        if path.exists() and path.stat().st_size+len(data.encode())>LOG_LIMIT:
            path.replace(self.run/'path-events.previous.jsonl')
        with path.open('a') as output:
            output.write(data)
        atomic_json(self.run/'path-state.json',entry)


def table_digest(targets):
    """Normalize only kernel-mutated queue policy / selected path-group state.

    This lab admits one group/one path. Every geometry, backend and selector
    argument is retained. Raw tables remain in the journal for diagnosis.
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


def describe(snapshot):
    return {**snapshot,'active_digest':table_digest(snapshot['active']),
            'inactive_digest':table_digest(snapshot['inactive']) if snapshot['inactive'] else None}


def fault_hook(stage,journal):
    """Explicit guest-only phase pause; no healthy-path file polling."""
    config=RUN/'lab-fault-config.json'
    if not config.exists():
        return
    setting=load_json(config)
    if setting.get('stage')!=stage or setting.get('action')!='pause':
        return
    token=setting.get('token')
    atomic_json(RUN/'lab-fault-reached.json',{'stage':stage,'token':token,
                'pid':os.getpid(),'monotonic':time.monotonic(),'transaction':journal.record})
    while True:
        release=RUN/'lab-fault-release.json'
        if release.exists() and load_json(release).get('token')==token:
            return
        # The hook must not extend the controller's admission deadline.
        deadline=journal.record.get('deadline')
        if deadline is not None and time.monotonic()>=deadline:
            return
        time.sleep(.05)


class Observations:
    """Kernel counters are observations, never authority to swap a device."""
    def __init__(self):
        self.previous=None
        self.last_progress=time.monotonic()
        self.transport={'state':'unknown'}
        # No complete application/error feed exists. Explicitly retain this
        # gap instead of treating an empty dmesg search as a clean history.
        self.errors={'state':'incomplete','reason':'application and filesystem history not fully observed'}

    def sample(self,info,failed_path):
        now=time.monotonic()
        try:
            path=Path('/sys/dev/block')/f"{info['major']}:{info['minor']}"/'stat'
            fields=[int(word) for word in path.read_text().split()]
            completed=sum(fields[index] for index in (0,4,11,15) if index<len(fields))
            in_flight=fields[8]
            progressed=self.previous is not None and completed!=self.previous
            if progressed or not in_flight:
                self.last_progress=now
            state=('explicit_failure' if failed_path else 'completion_progress' if progressed
                   else 'no_io' if not in_flight else 'suspected_stall' if now-self.last_progress>=2
                   else 'in_flight')
            self.previous=completed
            self.transport={'state':state,'completed':completed,'in_flight':in_flight,
                            'without_completion_seconds':now-self.last_progress}
        except (OSError,ValueError,KeyError,TypeError):
            self.transport={'state':'explicit_failure' if failed_path else 'unknown'}
        return self.transport
