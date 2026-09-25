#!/usr/bin/python3
"""Current-kernel, VM-only single owner of the stable USB multipath map."""
import os
import re
from pathlib import Path
import subprocess
import sys
import time

from rescue import Recovery
from admission import Admission, readonly
from dm_monitor import DeviceMapper, Events, PathProbe, Schedule
from guard_state import (Owner, Journal, Evidence, Observations, atomic_json,
                         load_json, digest, describe, table_digest, fault_hook)

NAME = 'lab-path'
UUID = 'mpath-RAMRESCUE-LAB'
DEVICE = '/dev/mapper/' + NAME
TERMINAL = {'expired','failed','interrupted','blocked'}


class ControlUncertain(RuntimeError):
    pass


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
        self.admission=Admission(config,recovery)
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
        self.probe=PathProbe(owner_fd=owner.fd)
        self.confirming=False
        self.generation=0
        self.candidate=None
        self.identity_state='enrolled'
        self.journal.write('idle',config_digest=digest(config),snapshot=checked_snapshot(self.mapper),recoveries=0)

    def change(self,*args):
        return dm(*args,lock_fd=self.owner.fd)

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

    def expire(self):
        # This stops new admission and no-path queuing, not all issued writes.
        self.check_map()
        self.change('message',NAME,'0','fail_if_no_path')
        self.journal.write('expired',probe_pending=self.probe.busy)
        self.close_candidate()
        self.event('expired',outcome='admission_stopped',probe_pending=self.probe.busy,
                   reason='queue deadline exceeded; in-flight I/O is not cancelled')

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

    def confirm_path(self):
        result=self.probe.poll()
        if result is None:
            return
        self.confirming=False
        if result['token']!=self.generation:
            raise RuntimeError('Path probe belongs to a different table generation')
        status=self.check_map()
        if (result['status']!='completed' or not self.current_present() or
                not self.current_active(status) or re.search(r'\b\d+:\d+ F \d+\b',status)):
            self.close_candidate()
            self.event('rejected',reason='Post-swap path confirmation failed',kernel_probe=result)
            return
        self.stage('confirming','before_ready',kernel_probe=result)
        if time.monotonic()>=self.deadline:
            self.expire()
            return
        # diskseq/fd checks after the ioctl; no new media scan in this phase.
        self.candidate.revalidate(self.owner.epoch,check_layout=False)
        snapshot=checked_snapshot(self.mapper)
        if (snapshot['active_digest']!=self.journal.record['candidate_table_digest'] or
                snapshot['inactive'] or snapshot['info']['suspended']):
            raise RuntimeError('Committed table no longer matches candidate')
        if time.monotonic()>=self.deadline:
            self.expire()
            return
        self.deadline=None
        self.recoveries+=1
        self.last_rejection=None
        self.identity_state='verified'
        self.stage('ready',snapshot=snapshot,deadline=None,recoveries=self.recoveries)
        self.close_candidate()
        self.event('ready',node=self.current,kernel_probe=result,
                   confirmation='kernel-probe-and-state',outcome='path_restored')

    def discard_staged(self):
        snapshot=checked_snapshot(self.mapper)
        if snapshot['inactive']:
            if snapshot['inactive_digest']!=self.journal.record.get('candidate_table_digest'):
                raise RuntimeError('Refusing to clear an unknown inactive table')
            self.change('clear',NAME)
        if snapshot['info']['suspended']:
            raise RuntimeError('Unexpected suspension requires fail-closed takeover')

    def step(self):
        if self.state in TERMINAL:
            return
        now=time.monotonic()
        if self.deadline is not None and now>=self.deadline:
            self.expire()
            return
        # The ioctl holds a live-table reference. Never start another table
        # transaction or probe until it returns, including after hot-unplug.
        if self.confirming:
            self.confirm_path()
            return
        if self.deadline is None:
            failed_path=re.search(r'\b\d+:\d+ F \d+\b',self.check_map())
            if self.current_present() and not failed_path:
                return
            self.deadline=now+self.config['queue_seconds']
            self.identity_state='missing_or_failed'
            self.stage('waiting',deadline=self.deadline,candidate=None)
            self.event('waiting',old_node=self.current,deadline=self.deadline)
        committed=False
        loaded=False
        mutation_started=False
        try:
            self.close_candidate()
            self.stage('verifying')
            self.candidate=self.admission.verify(self.deadline,self.owner.epoch)
            candidate=self.candidate
            # DM caches table devices by dev_t, not by our fd/diskseq. Until a
            # native binding contract exists, same-number instance reuse is
            # conservatively outside automatic recovery's admission policy.
            if candidate.dev==self.current_dev and candidate.diskseq!=self.current_diskseq:
                raise RuntimeError('New disk instance reuses active dev_t; binding validation required')
            self.identity_state='verified_candidate'
            snapshot=checked_snapshot(self.mapper)
            if snapshot['inactive'] or snapshot['info']['suspended']:
                raise ControlUncertain('Unexpected pre-existing transaction state')
            candidate_table=table(candidate.partition_sectors,f'{os.major(candidate.dev)}:{os.minor(candidate.dev)}')
            self.stage('load_intent','before_load',previous=snapshot,candidate=candidate.to_dict(),
                       candidate_table_digest=table_digest(table_targets(candidate_table)))
            candidate.revalidate(self.owner.epoch,check_layout=False)
            self.event('verified',node=candidate.node)
            # Inactive loading doesn't release queued application I/O.
            mutation_started=True
            self.change('load',NAME,'--table',candidate_table)
            loaded=True
            snapshot=checked_snapshot(self.mapper)
            self.stage('loaded','after_load',snapshot=snapshot)
            candidate.revalidate(self.owner.epoch)
            self.stage('commit_intent','before_commit')
            candidate.revalidate(self.owner.epoch,check_layout=False)
            # One kernel DM_DEV_SUSPEND(resume) transaction internally performs
            # noflush suspend + swap + resume. No userspace suspended window.
            self.change('resume','--noflush','--nolockfs',NAME)
            committed=True
            loaded=False
            self.current=candidate.node
            self.current_sys=candidate.sys_path
            self.current_dev=candidate.dev
            self.current_diskseq=candidate.diskseq
            snapshot=checked_snapshot(self.mapper)
            self.stage('committed','after_commit',snapshot=snapshot)
            if time.monotonic()>=self.deadline:
                self.expire()
                return
            candidate.revalidate(self.owner.epoch,check_layout=False)
            self.stage('probe_intent','before_probe')
            if time.monotonic()>=self.deadline:
                self.expire()
                return
            self.generation+=1
            self.probe.start(DEVICE,self.generation)
            self.confirming=True
            self.stage('probing','probe_started',generation=self.generation)
            self.event('probing',node=self.current,generation=self.generation)
        except Exception as exc:
            if loaded:
                self.discard_staged()
            self.close_candidate()
            if (isinstance(exc,ControlUncertain) or committed or
                    mutation_started and self.journal.record['phase'] in {'load_intent','commit_intent'}):
                # The command may have partially succeeded. Exit into the RAM
                # supervisor; never run another admission on uncertain state.
                self.event('failed',reason=str(exc),outcome='control_uncertain')
                raise
            if time.monotonic()>=self.deadline:
                self.expire()
                return
            reason=str(exc)
            if reason!=self.last_rejection:
                self.event('rejected',reason=reason)
                self.last_rejection=reason


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
    if record['phase'] in {'commit_intent','committed','probe_intent','probing','confirming','ready','expired'}:
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
                  phase=phase,updated_at=time.monotonic(),snapshot=final)
    atomic_json(owner.run/'path-transaction.json',record)
    result={'state':phase,'outcome':'admission_stopped','owner_epoch':owner.epoch,
            'recoveries':record.get('recoveries',0),
            'previous_owner_epoch':previous_epoch,'snapshot':final,
            'reason':'owner ended; no new candidate admitted; in-flight I/O is not cancelled'}
    atomic_json(owner.run/'path-supervisor.json',result)
    if phase!='expired':
        Evidence(owner.run).event({'time':time.monotonic(),**result})
    return result


def main():
    from agent import guard
    guard()
    config=load_json('/etc/rescue/path-guard.json')
    taking_over=sys.argv[1:]==['--takeover']
    try:
        with Owner() as owner:
            if taking_over:
                takeover(owner,config)
                return
            # A service restart is not permission to erase a terminal outcome.
            if (owner.run/'path-transaction.json').exists():
                raise RuntimeError('Existing transaction requires takeover, not owner restart')
            recovery=Recovery(load_json('/etc/rescue/identity.json'),runner=readonly)
            manager=Guard(config,recovery,owner)
            manager.check_map()
            (owner.run/'path-guard.pid').write_text(str(os.getpid()))
            manager.event('ready',node=manager.current,outcome='initial_mapping')
            events=Events()
            schedule=Schedule(time.monotonic())
            try:
                pending=False
                while manager.state not in TERMINAL:
                    now=time.monotonic()
                    if schedule.due(now,pending) or manager.deadline is not None and now>=manager.deadline:
                        manager.step()
                        schedule.completed(time.monotonic(),manager.deadline is not None)
                        pending=False
                    wake=schedule.next_check
                    if pending:
                        wake=min(wake,schedule.event_after)
                    if manager.deadline is not None:
                        wake=min(wake,manager.deadline)
                    if time.monotonic()<schedule.event_after and pending:
                        time.sleep(max(0,min(wake,schedule.event_after)-time.monotonic()))
                    else:
                        pending=events.wait(wake-time.monotonic()) or pending
            finally:
                events.close()
                manager.close_candidate()
    except Exception as exc:
        if taking_over:
            failure={'state':'blocked','reason':str(exc),
                'outcome':'manual_diagnosis_required','time':time.monotonic()}
            atomic_json('/run/path-supervisor.json',failure)
            # A competing process still owning the lock retains authority over
            # path-state. Do not overwrite its state from a rejected takeover.
            if not isinstance(exc,BlockingIOError):
                Evidence().event(failure)
        raise


if __name__=='__main__':
    main()
