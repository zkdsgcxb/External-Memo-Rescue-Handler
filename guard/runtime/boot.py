#!/usr/bin/python3
"""Activate an enrolled existing LVM root through the stable map. No formatting."""
import argparse
import json
import os
from pathlib import Path
import time

from admission import Admission, readonly
from dm_monitor import DeviceMapper
from guard_state import Owner, atomic_json, table_digest
import path_guard
from rescue import Recovery, command


def dm_name(vg,lv):
    return vg.replace('-','--')+'-'+lv.replace('-','--')


def activate(enrollment):
    if enrollment.get('schema')!=1:
        raise RuntimeError('Unsupported enrollment schema')
    identity=enrollment['identity']
    config=dict(enrollment['guard'])
    if config.get('profile')!='host':
        raise RuntimeError('Existing-disk boot requires an explicit host profile')
    path_guard.configure(config)
    path_guard.validate_environment(config)
    if not isinstance(config.get('queue_seconds'),int) or not 2<=config['queue_seconds']<=60:
        raise RuntimeError('Invalid queue budget')
    if config['root_lv'] not in identity['lvs']:
        raise RuntimeError('Root LV is not enrolled')
    expected_root='/dev/mapper/'+dm_name(identity['vg_name'],config['root_lv'])
    roots=[arg[5:] for arg in Path('/proc/cmdline').read_text().split() if arg.startswith('root=')]
    if roots!=[expected_root]:
        raise RuntimeError('Boot root argument does not match the enrolled root LV')
    mapper=DeviceMapper()
    if mapper.target_version('multipath')<(1,15,0):
        raise RuntimeError('Current kernel path probe interface is required')
    if any(row['segtype']!='linear' for row in config['layout']):
        raise RuntimeError('Only the enrolled linear layout can boot here')
    state=Path(config['run_dir'])
    state.mkdir(mode=0o700,parents=True,exist_ok=True)
    record=state/'boot.json'
    recovery=Recovery(identity,runner=readonly)
    deadline=time.monotonic()+30
    while True:
        try:
            recovery.candidate_node()
            break
        except (OSError,RuntimeError):
            if time.monotonic()>=deadline:
                raise
            time.sleep(.1)
    with Owner(state) as owner:
        if record.exists() or (state/'path-transaction.json').exists():
            raise RuntimeError('This boot already has a protection transaction')
        # A misordered initramfs must fail, never splice a live root stack.
        vg_prefix='LVM-'+identity['vg_uuid']
        for entry in Path('/sys/class/block').glob('dm-*'):
            name=(entry/'dm/name').read_text().strip()
            uuid=(entry/'dm/uuid').read_text().strip()
            if name==config['map_name'] or uuid.startswith(vg_prefix):
                raise RuntimeError('An enrolled mapping was activated before protection')
        recovery.run=lambda args,timeout=3: readonly(args,timeout,owner_fd=owner.fd)
        def record_phase(phase,**extra):
            atomic_json(record,{'phase':phase,'boot_id':owner.boot_id,
                'time':time.monotonic(),'map_name':config['map_name'],**extra})
        record_phase('verifying')
        with Admission(config,recovery).verify(deadline,owner.epoch) as candidate:
            candidate.revalidate(owner.epoch)
            Path('/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs').write_text(
                str(config['queue_seconds']+2)+'\n')
            record_phase('create_intent',candidate=candidate.to_dict())
            new_table=path_guard.table(candidate.partition_sectors,
                f'{os.major(candidate.dev)}:{os.minor(candidate.dev)}')
            path_guard.dm('create',config['map_name'],'--uuid',config['map_uuid'],
                '--table',new_table,lock_fd=owner.fd)
            path_guard.dm('mknodes',config['map_name'],lock_fd=owner.fd)
            snapshot=path_guard.checked_snapshot(mapper)
            if (snapshot['active_digest']!=table_digest(path_guard.table_targets(new_table))
                    or snapshot['inactive'] or snapshot['info']['suspended']):
                raise RuntimeError('Stable map is not ready for initial activation')
            config.update(initial_node=candidate.node,initial_sys_path=candidate.sys_path,
                          initial_diskseq=candidate.diskseq)
            candidate.revalidate(owner.epoch)
            record_phase('activate_intent',snapshot=snapshot)
            # The manual rescue runtime normally disables udev integration.
            # Initial boot activation needs the real DM/LVM rules and cookie
            # completion so systemd can discover each LV's /dev/mapper alias.
            # This chroot shares /run, /dev and the initramfs IPC namespace.
            command(['/sbin/lvm','lvchange','-ay','--devices',path_guard.DEVICE,
                '--config','activation { udev_rules=1 udev_sync=1 }',
                *[identity['vg_name']+'/'+lv for lv in sorted(identity['lvs'])]],
                timeout=15,pass_fds=(owner.fd,))
            stable_dev=os.stat(path_guard.DEVICE).st_rdev
            stable_sys=(Path('/sys/dev/block')/f'{os.major(stable_dev)}:{os.minor(stable_dev)}').resolve()
            for name in identity['lvs']:
                mapping=recovery.mapping(name)
                if [p.resolve() for p in (mapping/'slaves').iterdir()] != [stable_sys]:
                    raise RuntimeError('Enrolled LV does not depend solely on the stable map')
            candidate.revalidate(owner.epoch,check_layout=False)
        # This is preparation, not the service READY notification. The service
        # acquires a fresh owner and validates the instance again after pivot.
        atomic_json('/run/ram-rescue-guard/config.json',config)
        record_phase('prepared',snapshot=path_guard.checked_snapshot(mapper))
    print('RAM rescue stable root mapping prepared',flush=True)
    return config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    activate(json.loads(args.config.read_text()))


if __name__=='__main__':
    main()
