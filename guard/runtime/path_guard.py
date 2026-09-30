#!/usr/bin/python3
"""Shared single owner of a configured USB multipath map."""
import argparse
import os
import re
from pathlib import Path
import subprocess
import time

from data_recovery import recovery_for_identity
from admission import Admission, readonly
from dm_monitor import DeviceMapper, Events, probe_paths, Schedule
from owned_operation import OwnedOperation
import guard_state as state_store
from guard_state import (Owner, Journal, Evidence, Observations, atomic_json,
                         load_json, digest, describe, table_digest, fault_hook)

NAME = 'lab-path'
UUID = 'mpath-RAMRESCUE-LAB'
DEVICE = '/dev/mapper/' + NAME
RUN = Path('/run')
PROFILE = 'lab'
TERMINAL = {'expired','failed','interrupted','blocked'}


class ControlUncertain(RuntimeError):
    pass


def configure(config):
    """Select one map and RAM state directory before acquiring its owner.

    This has no filesystem side effects. Entrypoints must also call
    validate_environment before creating or changing any host mapping.
    """
    global NAME, UUID, DEVICE, RUN, PROFILE
    profile=config.get('profile','lab')
    if profile not in {'lab','host','host-data'}:
        raise ValueError('Unsupported guard profile')
    if profile in {'host','host-data'}:
        missing=[name for name in ('map_name','map_uuid','run_dir','kernel_release')
                 if not config.get(name)]
        if missing:
            raise ValueError('Host profile requires '+', '.join(missing))
    name=config.get('map_name','lab-path')
    map_uuid=config.get('map_uuid','mpath-RAMRESCUE-LAB')
    run=Path(config.get('run_dir','/run'))
    identity=Path(config.get('identity_path','/etc/rescue/identity.json'))
    if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9+_.-]{1,127}',name)
            or name in {'.','..'}):
        raise ValueError('Invalid multipath map name')
    if (not isinstance(map_uuid,str) or not map_uuid or len(map_uuid.encode())>127
            or any(char.isspace() or char=='\0' for char in map_uuid)):
        raise ValueError('Invalid multipath map UUID')
    if not run.is_absolute() or run==Path('/') or '..' in run.parts:
        raise ValueError('run_dir must be an absolute dedicated state directory')
    if not identity.is_absolute() or '..' in identity.parts:
        raise ValueError('identity_path must be absolute')
    if profile in {'host','host-data'} and not isinstance(config['kernel_release'],str):
        raise ValueError('kernel_release must be a string')
    if profile=='host-data':
        from data_guard import validate_config
        validate_config(config)
    NAME,UUID,DEVICE,RUN,PROFILE=name,map_uuid,'/dev/mapper/'+name,run,profile
    state_store.RUN=run
    state_store.ENABLE_LAB_HOOKS=profile=='lab'


def validate_environment(config):
    profile=config.get('profile','lab')
    if profile in {'host','host-data'}:
        if profile=='host' and 'ram_rescue_guard=1' not in Path('/proc/cmdline').read_text().split():
            raise RuntimeError('Host guard requires explicit ram_rescue_guard=1 boot flag')
        if os.uname().release!=config['kernel_release']:
            raise RuntimeError('Host guard kernel differs from enrolled kernel_release')
    else:
        from agent import guard
        guard()


def notify_ready():
    address=os.environ.get('NOTIFY_SOCKET')
    if address is None:
        return
    import socket
    if address.startswith('@'):
        address='\0'+address[1:]
    with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as notifier:
        notifier.connect(address)
        notifier.sendall(b'READY=1')


def dm(*args,lock_fd=None):
    # A helper retains the owner's flock even if its parent is SIGKILLed.
    # timeout cannot cancel an uninterruptible kernel ioctl; never start a
    # competing owner while such a helper still owns this descriptor.
    return subprocess.check_output(['/sbin/dmsetup','--noudevsync',*args],text=True,
        stderr=subprocess.STDOUT,timeout=5,pass_fds=() if lock_fd is None else (lock_fd,))


def table(sectors,node):
    if not isinstance(sectors,int) or sectors<=0:
        raise ValueError('invalid partition size')
    return f'0 {sectors} multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 {node} 1'


def table_targets(text):
    start,size,kind,params=text.split(maxsplit=3)
    return [[int(start),int(size),kind,params]]


def checked_snapshot(mapper):
    snapshot=describe(mapper.snapshot(NAME))
    if (snapshot['uuid']!=UUID or len(snapshot['active'])!=1 or
            snapshot['active'][0][2]!='multipath'):
        raise ControlUncertain('Unexpected stable map identity or target')
    return snapshot


class Guard:
    def __init__(self,config,recovery,owner):
        self.config=config
        self.owner=owner
        self.journal=Journal(owner,NAME,UUID)
        self.evidence=Evidence(owner.run)
        self.observations=Observations()
        self.current=config['initial_node']
        self.current_sys=config['initial_sys_path']
        self.current_dev=os.stat(self.current).st_rdev
        self.current_diskseq=config['initial_diskseq']
        self.deadline=None
        self.state='ready'
        self.recoveries=0
        self.last_rejection=None
        self.mapper=DeviceMapper()
        self.target_version=self.mapper.target_version('multipath')
        if self.target_version<(1,15,0):
            raise RuntimeError('DM_MPATH_PROBE_PATHS is required (multipath target >= 1.15.0)')
        self.operation=OwnedOperation(owner.fd)
        recovery.run=lambda args,timeout=3: readonly(args,timeout,
            owner_fd=self.operation.fence_fd())
        self.admission=Admission(config,recovery)
        self.pending_kind=None
        self.pending_token=None
        self.shutdown_started=False
        self.kernel_probe=None
        self.generation=0
        self.candidate=None
        self.identity_state='enrolled'
        self.journal.write('idle',config_digest=digest(config),snapshot=checked_snapshot(self.mapper),recoveries=0)

    def change(self,*args):
        return dm(*args,lock_fd=self.operation.fence_fd())

    def event(self,state,**details):
        entry={'time':time.monotonic(),'state':state,'recoveries':self.recoveries,
               'multipath_target_version':self.target_version,
               'owner_epoch':self.owner.epoch,
               'observations':{'identity':self.identity_state,
                   'transport':self.observations.transport,
                   'control':self.journal.record['phase'],
                   'upper_errors':self.observations.errors},**details}
        self.state=state
        self.evidence.event(entry)

    def close_candidate(self):
        if self.candidate is not None:
            self.candidate.close()
            self.candidate=None

    def shutdown(self):
        if self.shutdown_started:
            return
        self.shutdown_started=True
        candidate,self.candidate=self.candidate,None
        if not self.operation.busy:
            if candidate is not None:
                candidate.close()
            self.operation.close()
            return
        def cleanup(outcome):
            # verify may produce a new credential after the deadline. It never
            # reaches the controller or authorizes any subsequent operation.
            result=outcome['value'] if outcome['kind']=='verify' else None
            try:
                if result is not None and result is not candidate:
                    result.close()
            finally:
                if candidate is not None:
                    candidate.close()
        self.operation.abandon(cleanup)

    def expire(self):
        # Publish the deadline before any further kernel or helper call.
        # An old operation may be in D state. Its fence prevents a competing
        # controller; takeover disables queuing only after that fence clears.
        phase=self.journal.record['phase']
        pending=self.pending_kind
        self.journal.write('expired',phase_at_expiry=phase,
            operation_pending=pending,probe_pending=pending=='probe',
            queue_disable='deferred_to_takeover')
        self.event('expired',outcome='admission_stopped',operation_pending=pending,
            probe_pending=pending=='probe',queue_disable='deferred_to_takeover',
            reason='admission deadline exceeded; in-flight I/O is not cancelled')
        self.shutdown()

    def check_map(self):
        map_uuid,targets=self.mapper.query(NAME)
        if map_uuid!=UUID or len(targets)!=1 or targets[0][0]!='multipath':
            raise RuntimeError('Unexpected stable map identity or target')
        status=targets[0][1]
        self.observations.sample(self.mapper.last_info,bool(re.search(r'\b\d+:\d+ F \d+\b',status)))
        return status

    def current_present(self):
        path=Path('/sys/class/block')/Path(self.current).name
        try:
            return (path.exists() and str(path.resolve())==self.current_sys and
                    int((path.resolve().parent/'diskseq').read_text())==self.current_diskseq and
                    os.stat(self.current).st_rdev==self.current_dev)
        except (OSError,ValueError):
            return False

    def current_active(self,status):
        expected=f'{os.major(self.current_dev)}:{os.minor(self.current_dev)}'
        return bool(re.search(r'\b'+re.escape(expected)+r' A \d+\b',status))

    def stage(self,phase,hook=None,**details):
        self.journal.write(phase,**details)
        if hook:
            fault_hook(hook,self.journal)

    def submit(self,kind,fn):
        if time.monotonic()>=self.deadline:
            self.expire()
            return
        if self.pending_kind is not None:
            raise RuntimeError('Only one recovery operation may run')
        self.pending_token=self.operation.start(kind,fn)
        self.pending_kind=kind

    def reject(self,reason):
        self.close_candidate()
        if reason!=self.last_rejection:
            self.event('rejected',reason=reason)
            self.last_rejection=reason

    def load_candidate(self):
        candidate=self.candidate
        # Keep the final instance check next to the mutation in the same worker.
        candidate.revalidate(self.owner.epoch,check_layout=False)
        self.mutation_budget()
        self.change('load',NAME,'--table',self.candidate_table)
        return checked_snapshot(self.mapper)

    def commit_candidate(self):
        self.candidate.revalidate(self.owner.epoch,check_layout=False)
        self.mutation_budget()
        # One kernel resume performs noflush suspend + swap + resume. We do
        # not introduce a separate userspace suspended window.
        self.change('resume','--noflush','--nolockfs',NAME)
        return checked_snapshot(self.mapper)

    def mutation_budget(self):
        if self.shutdown_started or time.monotonic()>=self.deadline:
            raise RuntimeError('Admission expired before kernel mutation')

    def confirm_candidate(self):
        self.candidate.revalidate(self.owner.epoch,check_layout=False)
        snapshot=checked_snapshot(self.mapper)
        if (snapshot['active_digest']!=self.journal.record['candidate_table_digest'] or
                snapshot['inactive'] or snapshot['info']['suspended']):
            raise RuntimeError('Committed table no longer matches candidate')
        return snapshot

    def consume(self,result):
        kind=self.pending_kind
        if result['token']!=self.pending_token or result['kind']!=kind:
            raise ControlUncertain('Operation belongs to a different transaction')
        self.pending_kind=self.pending_token=None
        # poll transfers any credential and its cleanup responsibility here.
        if kind=='verify' and result['error'] is None:
            self.candidate=result['value']
        if time.monotonic()>=self.deadline:
            self.expire()
            return
        if result['error'] is not None:
            reason=result['error']['message']
            if kind=='verify':
                self.reject(reason)
                return
            raise ControlUncertain(kind+': '+reason)
        value=result['value']
        if kind=='verify':
            candidate=self.candidate
            # A new diskseq using the active dev_t cannot establish which
            # bdev DM cached; do not admit that ambiguous instance reuse.
            if candidate.dev==self.current_dev and candidate.diskseq!=self.current_diskseq:
                self.reject('New disk instance reuses active dev_t; binding validation required')
                return
            self.identity_state='verified_candidate'
            snapshot=checked_snapshot(self.mapper)
            if snapshot['inactive'] or snapshot['info']['suspended']:
                raise ControlUncertain('Unexpected pre-existing transaction state')
            self.candidate_table=table(candidate.partition_sectors,
                f'{os.major(candidate.dev)}:{os.minor(candidate.dev)}')
            self.stage('load_intent','before_load',previous=snapshot,candidate=candidate.to_dict(),
                candidate_table_digest=table_digest(table_targets(self.candidate_table)),
                commit_started=False)
            self.event('verified',node=candidate.node)
            self.submit('load',self.load_candidate)
        elif kind=='load':
            self.stage('loaded','after_load',snapshot=value)
            fault_hook('before_revalidate',self.journal)
            candidate=self.candidate
            self.submit('revalidate',lambda: candidate.revalidate(self.owner.epoch))
        elif kind=='revalidate':
            self.stage('commit_intent','before_commit',commit_started=True)
            self.submit('commit',self.commit_candidate)
        elif kind=='commit':
            candidate=self.candidate
            self.current=candidate.node
            self.current_sys=candidate.sys_path
            self.current_dev=candidate.dev
            self.current_diskseq=candidate.diskseq
            self.stage('committed','after_commit',snapshot=value)
            self.submit('preprobe',lambda: candidate.revalidate(self.owner.epoch,check_layout=False))
        elif kind=='preprobe':
            self.stage('probe_intent','before_probe')
            self.generation+=1
            generation=self.generation
            self.submit('probe',lambda: probe_paths(DEVICE,generation))
            if self.state not in TERMINAL:
                self.stage('probing','probe_started',generation=self.generation)
                self.event('probing',node=self.current,generation=self.generation)
        elif kind=='probe':
            if value['token']!=self.generation:
                raise ControlUncertain('Path probe belongs to a different table generation')
            status=self.check_map()
            if (value['status']!='completed' or not self.current_present() or
                    not self.current_active(status) or re.search(r'\b\d+:\d+ F \d+\b',status)):
                self.close_candidate()
                self.event('rejected',reason='Post-swap path confirmation failed',kernel_probe=value)
                return
            self.kernel_probe=value
            self.stage('confirming','before_ready',kernel_probe=value)
            self.submit('confirm',self.confirm_candidate)
        elif kind=='confirm':
            self.deadline=None
            self.recoveries+=1
            self.last_rejection=None
            self.identity_state='verified'
            self.stage('ready',snapshot=value,deadline=None,recoveries=self.recoveries)
            self.close_candidate()
            self.event('ready',node=self.current,kernel_probe=self.kernel_probe,
                confirmation='kernel-probe-and-state',outcome='path_restored')
        else:
            raise ControlUncertain('Unknown operation result')

    def step(self):
        if self.state in TERMINAL:
            return
        now=time.monotonic()
        if self.deadline is not None and now>=self.deadline:
            self.expire()
            return
        try:
            if self.pending_kind is not None:
                result=self.operation.poll()
                if result is not None:
                    self.consume(result)
                return
            if self.deadline is None:
                failed_path=re.search(r'\b\d+:\d+ F \d+\b',self.check_map())
                if self.current_present() and not failed_path:
                    return
                self.deadline=now+self.config['queue_seconds']
                self.identity_state='missing_or_failed'
                self.stage('waiting',deadline=self.deadline,candidate=None,commit_started=False)
                self.event('waiting',old_node=self.current,deadline=self.deadline)
            self.close_candidate()
            self.stage('verifying','before_verify')
            deadline,epoch=self.deadline,self.owner.epoch
            self.submit('verify',lambda: self.admission.verify(deadline,epoch))
        except Exception as exc:
            # Even an operation failure may follow a successful kernel change.
            # The RAM supervisor reconciles the recorded intent after all old
            # references have gone; the controller does not start a new job.
            self.event('failed',reason=str(exc),outcome='control_uncertain')
            self.shutdown()
            raise


def takeover(owner,config):
    """Terminate an interrupted owner; never promote its old candidate."""
    record=load_json(owner.run/'path-transaction.json')
    if (record.get('schema')!=1 or record.get('boot_id')!=owner.boot_id or
            record.get('map_name')!=NAME or record.get('map_uuid')!=UUID or
            record.get('config_digest')!=digest(config)):
        raise RuntimeError('Untrusted transaction journal; manual diagnosis required')
    mapper=DeviceMapper()
    snapshot=checked_snapshot(mapper)
    known={item.get('active_digest') for item in (record.get('previous',{}),record.get('snapshot',{}))}
    # Only a commit intent authorizes that the candidate might already be live.
    phase=record.get('phase_at_expiry',record['phase']) if record['phase']=='expired' else record['phase']
    if phase in {'commit_intent','committed','probe_intent','probing','confirming','ready'}:
        known.add(record.get('candidate_table_digest'))
    if snapshot['active_digest'] not in known:
        raise RuntimeError('Unknown active table; refusing automatic resume')
    if snapshot['inactive']:
        if snapshot['inactive_digest']!=record.get('candidate_table_digest'):
            raise RuntimeError('Unknown inactive table; refusing automatic clear')
        dm('clear',NAME,lock_fd=owner.fd)
    if snapshot['info']['suspended']:
        # Clear first: resume must not install a stale inactive candidate.
        dm('resume','--noflush','--nolockfs',NAME,lock_fd=owner.fd)
    dm('message',NAME,'0','fail_if_no_path',lock_fd=owner.fd)
    final=checked_snapshot(mapper)
    if final['inactive'] or final['info']['suspended']:
        raise RuntimeError('Map still has an unfinished control transaction')
    previous_epoch=record['owner_epoch']
    phase='expired' if record['phase']=='expired' else 'interrupted'
    record.update(owner_epoch=owner.epoch,previous_owner_epoch=previous_epoch,
                  phase=phase,updated_at=time.monotonic(),snapshot=final,
                  queue_disable='completed_by_takeover')
    atomic_json(owner.run/'path-transaction.json',record)
    result={'state':phase,'outcome':'admission_stopped','owner_epoch':owner.epoch,
            'recoveries':record.get('recoveries',0),
            'previous_owner_epoch':previous_epoch,'snapshot':final,
            'reason':'owner ended; no new candidate admitted; in-flight I/O is not cancelled'}
    atomic_json(owner.run/'path-supervisor.json',result)
    if phase!='expired':
        Evidence(owner.run).event({'time':time.monotonic(),**result})
    return result


def acquire_owner(taking_over):
    if not taking_over:
        return Owner(RUN)
    announced=False
    while True:
        try:
            return Owner(RUN)
        except BlockingIOError:
            # A killed controller can leave a D-state helper inheriting its
            # flock. Wait in RAM; never compete with or repeatedly signal it.
            if not announced:
                atomic_json(RUN/'path-supervisor.json',{
                    'state':'waiting_for_owner','time':time.monotonic(),
                    'pid':os.getpid(),'outcome':'old_operation_not_finished'})
                announced=True
            time.sleep(1)


def run_owned(config, owner, taking_over=False):
    """Run one configured controller while the caller keeps its owner fence.

    Entrypoints must configure and validate the environment before calling.
    Registration preparation can therefore retain its lock into this loop.
    """
    if taking_over:
        takeover(owner,config)
        return
    # A service restart is not permission to erase a terminal outcome.
    if (owner.run/'path-transaction.json').exists():
        raise RuntimeError('Existing transaction requires takeover, not owner restart')
    recovery=recovery_for_identity(
        load_json(config.get('identity_path','/etc/rescue/identity.json')),runner=readonly)
    if PROFILE=='host-data':
        from data_guard import validate_runtime
        recovery.run=lambda args,timeout=3: readonly(args,timeout,owner_fd=owner.fd)
        validate_runtime(config,recovery)
    manager=Guard(config,recovery,owner)
    events=None
    try:
        status=manager.check_map()
        if not manager.current_present() or not manager.current_active(status):
            raise RuntimeError('Initial enrolled path is absent or not active')
        (owner.run/'path-guard.pid').write_text(str(os.getpid()))
        manager.event('ready',node=manager.current,outcome='initial_mapping')
        try:
            notify_ready()
        except Exception as exc:
            manager.event('failed',reason=str(exc),outcome='startup_notification_failed')
            raise
        events=Events()
        schedule=Schedule(time.monotonic())
        pending=False
        completed=False
        while manager.state not in TERMINAL:
            now=time.monotonic()
            if completed or schedule.due(now,pending) or manager.deadline is not None and now>=manager.deadline:
                manager.step()
                schedule.completed(time.monotonic(),manager.deadline is not None)
                pending=False
                completed=False
            if manager.state in TERMINAL:
                break
            wake=schedule.next_check
            if pending:
                wake=min(wake,schedule.event_after)
            if manager.deadline is not None:
                wake=min(wake,manager.deadline)
            pending=events.wait(wake-time.monotonic(),manager.operation.fileno(),
                defer_events=pending and time.monotonic()<schedule.event_after) or pending
            completed=events.operation_ready
    finally:
        if events is not None:
            events.close()
        manager.shutdown()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('/etc/rescue/path-guard.json'))
    parser.add_argument('--takeover',action='store_true')
    args=parser.parse_args(argv)
    config=load_json(args.config)
    configure(config)
    validate_environment(config)
    taking_over=args.takeover
    try:
        with acquire_owner(taking_over) as owner:
            run_owned(config, owner, taking_over)
    except Exception as exc:
        if taking_over:
            failure={'state':'blocked','reason':str(exc),
                'outcome':'manual_diagnosis_required','time':time.monotonic()}
            atomic_json(RUN/'path-supervisor.json',failure)
            # A competing process still owning the lock retains authority over
            # path-state. Do not overwrite its state from a rejected takeover.
            if not isinstance(exc,BlockingIOError):
                Evidence(RUN).event(failure)
        raise


if __name__=='__main__':
    main()
