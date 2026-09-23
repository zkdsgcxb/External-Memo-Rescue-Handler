"""Ubuntu-specific guest preparation and observations. Guest only."""
import json
import os
from pathlib import Path
import shutil
import subprocess


def enabled():
    return 'ram_rescue_ubuntu=1' in Path('/proc/cmdline').read_text().split()


def prepare():
    from agent import guard
    guard()
    root = Path('/newroot')
    if not enabled() or not (root/'usr/lib/systemd/systemd').is_file():
        raise RuntimeError('Not an extracted Ubuntu systemd root')
    for name in ['workload.py', 'ubuntu_workload.py', 'git_workload.py']:
        (root/'opt/lab').mkdir(parents=True, exist_ok=True)
        shutil.copyfile('/opt/lab/'+name, root/'opt/lab'/name)
    (root/'etc/hostname').write_text('ubuntu-usb-lab\n')
    (root/'etc/fstab').write_text('/dev/mapper/labrescue-ubuntu / ext4 defaults,errors=remount-ro 0 0\n'
                                 '/dev/mapper/labrescue-shared /shared ext4 defaults 0 0\n')
    (root/'etc/cloud/cloud-init.disabled').touch()
    (root/'etc/machine-id').write_text('')
    units = root/'etc/systemd/system'
    units.mkdir(parents=True, exist_ok=True)
    # No NIC/cloud provider. Our guard is the sole owner of the experiment's maps.
    for name in ['systemd-networkd-wait-online.service', 'multipathd.service', 'multipathd.socket',
                 'lvm2-monitor.service', 'lvm2-pvscan@.service', 'serial-getty@ttyS1.service',
                 'serial-getty@ttyS2.service']:
        path = units/name
        path.unlink(missing_ok=True)
        path.symlink_to('/dev/null')
    services = {
        'lab-agent': '/usr/sbin/chroot /run/rescue /usr/bin/python3 /opt/lab/agent.py',
        'lab-guard': '/usr/sbin/chroot /run/rescue /usr/bin/python3 /opt/lab/path_guard.py',
        'lab-shell': '/usr/sbin/chroot /run/rescue /bin/sh -i',
    }
    wants = units/'multi-user.target.wants'
    wants.mkdir(exist_ok=True)
    for name, command in services.items():
        (units/(name+'.service')).write_text('[Unit]\nDescription=USB lab RAM '+name+'\n'
            'After=systemd-remount-fs.service\n[Service]\nType=simple\nExecStart='+command+
            '\nRestart=no\n' + ('StandardInput=tty\nStandardOutput=tty\nStandardError=tty\nTTYPath=/dev/ttyS2\n' if name=='lab-shell' else '') +
            '[Install]\nWantedBy=multi-user.target\n')
        (wants/(name+'.service')).symlink_to('../'+name+'.service')
    (units/'lab-workload.service').write_text('[Unit]\nDescription=USB root append/fsync workload\n'
        'After=multi-user.target\n[Service]\nType=simple\n'
        'ExecStart=/usr/bin/python3 /opt/lab/ubuntu_workload.py\nRestart=no\n')
    (units/'lab-ready.service').write_text('[Unit]\nDescription=USB lab boot marker\n'
        'After=lab-agent.service lab-guard.service lab-shell.service dbus.service systemd-user-sessions.service\n'
        '[Service]\nType=oneshot\nExecStart=/bin/echo LAB_ROOT_READY: Ubuntu systemd root\n'
        'StandardOutput=tty\nTTYPath=/dev/ttyS0\n[Install]\nWantedBy=multi-user.target\n')
    (wants/'lab-ready.service').symlink_to('../lab-ready.service')
    if 'ram_rescue_git=1' in Path('/proc/cmdline').read_text().split():
        (root/'srv/git').mkdir(parents=True, exist_ok=True)
        subprocess.run(['/bin/mount','-o','ro','/dev/vdb',str(root/'srv/git')],check=True)
        with (root/'etc/fstab').open('a') as f:
            f.write('/dev/vdb /srv/git ext4 ro 0 0\n')
        (root/'etc/gitconfig').write_text('[safe]\n\tdirectory = /srv/git/linux.git\n')
        (units/'lab-git-source.service').write_text('[Unit]\nDescription=Independent read-only Git source\n'
            'After=local-fs.target\n[Service]\nType=simple\n'
            'ExecStart=/usr/bin/git daemon --verbose --log-destination=stderr --reuseaddr --export-all --base-path=/srv/git --listen=127.0.0.1 --port=9418 /srv/git\n'
            'Restart=no\n[Install]\nWantedBy=multi-user.target\n')
        (wants/'lab-git-source.service').symlink_to('../lab-git-source.service')
        (units/'lab-git-clone.service').write_text('[Unit]\nDescription=Real Linux Git clone on USB root\n'
            'After=lab-git-source.service\nRequires=lab-git-source.service\n[Service]\nType=oneshot\n'
            'ExecStart=/usr/bin/python3 /opt/lab/git_workload.py\nRemainAfterExit=yes\nRestart=no\nTimeoutStartSec=600\n')



def systemctl(*args):
    return subprocess.check_output(['/bin/chroot', '/proc/1/root', '/usr/bin/systemctl', *args],
                                   text=True, stderr=subprocess.STDOUT, timeout=15)


def status():
    from agent import guard
    guard()
    if not enabled():
        raise ValueError('Ubuntu mode required')
    services = {}
    for name in ['systemd-journald', 'dbus', 'lab-agent', 'lab-guard', 'lab-shell', 'lab-workload']:
        props = dict(line.split('=', 1) for line in systemctl('show', name+'.service',
            '-p', 'ActiveState,SubState,MainPID,NRestarts').splitlines() if '=' in line)
        pid = int(props['MainPID'])
        props['start_ticks'] = Path(f'/proc/{pid}/stat').read_text().split()[21] if pid else None
        services[name] = props
    root = Path('/proc/1/root')
    return {'os_release':(root/'etc/os-release').read_text(),
            'pid1_comm':Path('/proc/1/comm').read_text().strip(),
            'pid1_start_ticks':Path('/proc/1/stat').read_text().split()[21],
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'services':services, 'failed_units':systemctl('--failed', '--no-legend', '--plain'),
            'multi_user':systemctl('show', 'multi-user.target', '-p', 'ActiveState', '--value').strip(),
            'installed_packages':(root/'var/lib/dpkg/status').read_text().count('Status: install ok installed')}


class ServiceWorker:
    """Observe a workload managed by guest systemd instead of owning it via Popen."""
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        path = Path(f'/proc/{self.pid}/stat')
        return None if path.exists() and path.read_text().split()[2] != 'Z' else 1


def git_status(runtime=Path('/run')):
    state = runtime/'git-clone-state.json'
    result = json.loads(state.read_text()) if state.exists() else {}
    log = runtime/'git-clone.log'
    result['progress_tail'] = log.read_text(errors='replace')[-6000:] if log.exists() else ''
    if 'pid' in result and result.get('exit_code') is None:
        pid = result['pid']
        stat = Path(f'/proc/{pid}/stat')
        result['same_process_alive'] = stat.exists() and stat.read_text().split()[21]==result['start_ticks']
    return result


def git_validate():
    prefix = ['/bin/chroot','/proc/1/root','/usr/bin/git','-C','/root/linux']
    def git(*args):
        return subprocess.check_output([*prefix,*args],text=True,stderr=subprocess.STDOUT,timeout=180).strip()
    source = json.loads(Path('/proc/1/root/srv/git/source.json').read_text())
    head = git('rev-parse','HEAD')
    fsck = git('fsck','--full')
    clean = git('status','--porcelain','--untracked-files=no')
    return {'head':head,'expected_head':source['commit'],'head_matches':head==source['commit'],
            'fsck_passed':True,'fsck_output':fsck,'worktree_clean':not clean,
            'tracked_files':len(git('ls-files').splitlines()),'source':source}


def git_diagnostics():
    return {'service':systemctl('show','lab-git-source.service','-p','ActiveState,SubState,MainPID,ExecMainStatus'),
            'journal':subprocess.check_output(['/bin/chroot','/proc/1/root','/usr/bin/journalctl',
                '-u','lab-git-source.service','-u','lab-git-clone.service','--no-pager','-n','50'],
                text=True,stderr=subprocess.STDOUT,timeout=15)}
