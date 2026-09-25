#!/usr/bin/python3
"""Experimental, VM-only single-path multipath manager; not a host installer."""
import json
import os
import re
from pathlib import Path
import subprocess
import time

from rescue import Recovery, command, rows
from dm_monitor import DeviceMapper, Events, PathProbe, Schedule

NAME = 'lab-path'
UUID = 'mpath-RAMRESCUE-LAB'
DEVICE = '/dev/mapper/' + NAME


def dm(*args):
    return subprocess.check_output(['/sbin/dmsetup','--noudevsync',*args],text=True,
                                   stderr=subprocess.STDOUT,timeout=5)


def table(sectors, node):
    if not isinstance(sectors,int) or sectors<=0:
        raise ValueError('invalid partition size')
    # BIO mode supports a partition backend; request mode rejects this topology.
    return f'0 {sectors} multipath 3 queue_if_no_path queue_mode bio 0 1 1 round-robin 0 1 1 {node} 1'


def readonly(args, timeout=3):
    if args[0]=='/sbin/lvm':
        if '--readonly' not in args or '--devices' not in args:
            raise ValueError('guard may only read explicitly selected LVM devices')
        # Inspect the candidate physical PV, not the currently queued multipath device.
        args=[*args,'--config','devices { multipath_component_detection=0 }']
    return command(args,timeout=min(timeout,3))


def layout(node):
    report=rows(readonly(['/sbin/lvm','lvs','--readonly','--devices',node,'--segments',
        '--reportformat','json','--units','s','--nosuffix','-o',
        'lv_name,lv_uuid,vg_uuid,segtype,seg_start,seg_size,seg_pe_ranges']), 'seg')
    # Preserve PE placement while discarding the old Linux node name.
    normalized=[]
    for row in report:
        clean={key:value.strip() for key,value in row.items()}
        clean['seg_pe_ranges']=' '.join(part.rsplit(':',1)[-1] for part in clean['seg_pe_ranges'].split())
        normalized.append(clean)
    return sorted(normalized,key=lambda row:(row['lv_name'],row['seg_start']))


class Guard:
    def __init__(self, config, recovery):
        self.config=config
        self.recovery=recovery
        self.current=config['initial_node']
        self.current_sys=config['initial_sys_path']
        self.deadline=None
        self.state='ready'
        self.recoveries=0
        self.last_rejection=None
        self.suspended=False
        self.mapper=DeviceMapper()
        self.target_version=self.mapper.target_version('multipath')
        if self.target_version<(1,15,0):
            raise RuntimeError('DM_MPATH_PROBE_PATHS is required (multipath target >= 1.15.0)')
        self.probe=PathProbe()
        self.confirming=False
        self.generation=0
        self.current_dev=None

    def event(self, state, **details):
        entry={'time':time.monotonic(),'state':state,'recoveries':self.recoveries,
               'multipath_target_version':self.target_version,**details}
        self.state=state
        with open('/run/path-events.jsonl','a') as log:
            log.write(json.dumps(entry)+'\n')
        tmp=Path('/run/path-state.json.tmp')
        tmp.write_text(json.dumps(entry))
        tmp.replace('/run/path-state.json')

    def expire(self):
        # Terminal state: do not silently re-enable writes after errors have escaped.
        if self.suspended:
            dm('resume',NAME)
            self.suspended=False
        dm('message',NAME,'0','fail_if_no_path')
        self.event('expired',probe_pending=self.probe.busy,
                   reason='queue deadline exceeded; no-path queue disabled; in-flight I/O may still be blocked')

    def check_map(self):
        map_uuid,targets=self.mapper.query(NAME)
        if map_uuid!=UUID or len(targets)!=1 or targets[0][0]!='multipath':
            raise RuntimeError('Unexpected stable map identity or target')
        return targets[0][1]

    def current_present(self):
        path=Path('/sys/class/block')/Path(self.current).name
        return path.exists() and str(path.resolve())==self.current_sys

    def current_active(self,status):
        try:
            dev=os.stat(self.current).st_rdev
        except OSError:
            return False
        expected=f'{os.major(dev)}:{os.minor(dev)}'
        return dev==self.current_dev and bool(re.search(r'\b'+re.escape(expected)+r' A \d+\b',status))

    def confirm_path(self):
        result=self.probe.poll()
        if result is None:
            return
        self.confirming=False
        if result['token']!=self.generation:
            raise RuntimeError('Path probe belongs to a different table generation')
        status=self.check_map()
        # Zero is not proof of a successful read. Reconcile the mapping and
        # enrolled instance again; never substitute this for media admission.
        if (result['status']!='completed' or
                not self.current_present() or not self.current_active(status) or
                re.search(r'\b\d+:\d+ F \d+\b',status)):
            self.event('rejected',reason='Post-swap path confirmation failed',kernel_probe=result)
            return
        if time.monotonic()>=self.deadline:
            self.expire()
            return
        self.deadline=None
        self.recoveries+=1
        self.last_rejection=None
        self.event('ready',node=self.current,kernel_probe=result,
                   confirmation='kernel-probe-and-state')

    def step(self):
        if self.state=='expired':
            return
        now=time.monotonic()
        if self.deadline is not None and now>=self.deadline:
            self.expire()
            return
        # An ioctl holds a live-table reference until it finishes. Never load
        # or suspend another table while it is outstanding, even after unplug.
        if self.confirming:
            self.confirm_path()
            return
        # No pre-unplug notification from host: first notice the old sysfs object disappearing.
        if self.deadline is None:
            failed_path=re.search(r'\b\d+:\d+ F \d+\b',self.check_map())
            if self.current_present() and not failed_path:
                return
            self.deadline=now+self.config['queue_seconds']
            self.event('waiting',old_node=self.current,deadline=self.deadline)
        try:
            # Cheap sysfs readiness gate; no blkid/LVM until one partition exists.
            if not Path(self.recovery.candidate_node()).exists():
                raise RuntimeError("Candidate device node is not ready")
            node=self.recovery.verify()
            sys_path=Path('/sys/class/block')/Path(node).name
            if int((sys_path/'size').read_text())!=self.config['partition_sectors']:
                raise RuntimeError('Partition size differs')
            if layout(node)!=self.config['layout']:
                raise RuntimeError('LV layout differs from enrolled metadata')
            fd=os.open(node,os.O_RDONLY|os.O_NONBLOCK)
            try:
                dev=os.fstat(fd).st_rdev
                resolved=str(sys_path.resolve())
                if self.recovery.verify()!=node or str(sys_path.resolve())!=resolved or os.stat(node).st_rdev!=dev:
                    raise RuntimeError('Candidate changed during verification')
                if time.monotonic()>=self.deadline:
                    self.expire()
                    return
                self.check_map()
                self.event('verified',node=node)
                # Loading an inactive table does not dispatch queued I/O to this path.
                dm('load',NAME,'--table',table(self.config['partition_sectors'],f'{os.major(dev)}:{os.minor(dev)}'))
                if time.monotonic()>=self.deadline or str(sys_path.resolve())!=resolved:
                    dm('clear',NAME)
                    raise RuntimeError('Candidate expired or disappeared before table swap')
                # Short atomic table swap AFTER failure, without flushing queued requests.
                dm('suspend','--noflush','--nolockfs',NAME)
                self.suspended=True
                try:
                    dm('resume',NAME)
                    self.suspended=False
                except Exception:
                    # Keep the experiment diagnosable; never silently mark recovery successful.
                    self.event('swap_failed')
                    raise
                self.current=node
                self.current_sys=resolved
                self.current_dev=dev
                if time.monotonic()>=self.deadline:
                    self.expire()
                    return
                self.generation+=1
                self.probe.start(DEVICE,self.generation)
                self.confirming=True
                self.event('probing',node=node,generation=self.generation)
            finally:
                os.close(fd)
        except Exception as exc:
            reason=str(exc)
            if reason!=self.last_rejection:
                self.event('rejected',reason=reason)
                self.last_rejection=reason


def main():
    from agent import guard
    guard()
    config=json.loads(Path('/etc/rescue/path-guard.json').read_text())
    recovery=Recovery(json.loads(Path('/etc/rescue/identity.json').read_text()),runner=readonly)
    manager=Guard(config,recovery)
    manager.check_map()
    Path('/run/path-guard.pid').write_text(str(os.getpid()))
    manager.event('ready',node=manager.current)
    events=Events()
    schedule=Schedule(time.monotonic())
    try:
        pending=False
        while manager.state!='expired':
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
            # Do not drain an event storm in a busy loop. Every batch is coalesced.
            if time.monotonic()<schedule.event_after and pending:
                time.sleep(max(0,min(wake,schedule.event_after)-time.monotonic()))
            else:
                pending=events.wait(wake-time.monotonic()) or pending
    finally:
        events.close()


if __name__=='__main__':
    main()
