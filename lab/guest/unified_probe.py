"""VM-only lifecycle checks for the persistent registered-map manager.

This source is appended after data_probe.py in the RAM observer. Block-map
creation below is deliberately a test fixture, not part of the manager.
"""


class UnifiedProbe(DataProbe):
    MANAGER = '/run/data-launcher-src/guard/manage.py'

    def manager(self, *args, check=True):
        result = self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/usr/bin/python3',
                           self.MANAGER, *args, timeout=60, check=check)
        if result['returncode'] == 0:
            result['value'] = json.loads(result['stdout'])
        return result

    def create_map(self, spec):
        from path_guard import table
        link = Path('/dev/disk/by-partuuid') / spec['partuuid']
        deadline = time.monotonic() + 10
        while not link.exists():
            if time.monotonic() > deadline:
                raise RuntimeError('Disposable USB partition did not appear: ' + spec['name'])
            time.sleep(.05)
        node = str(link.resolve(strict=True))
        sectors = int((Path('/sys/class/block') / Path(node).name / 'size').read_text())
        subprocess.run(['/sbin/dmsetup', '--noudevsync', 'create', spec['name'],
                        '--uuid', spec['map_uuid'], '--table', table(sectors, node)], check=True)
        subprocess.run(['/sbin/dmsetup', '--noudevsync', 'mknodes', spec['name']], check=True)
        self.host('/usr/bin/udevadm', 'trigger', '--action=change', '--settle',
                  '/sys/dev/block/' + ':'.join(str(value) for value in
                    (os.major(os.stat('/dev/mapper/' + spec['name']).st_rdev),
                     os.minor(os.stat('/dev/mapper/' + spec['name']).st_rdev))))
        return node

    def configure(self):
        if self.host('/usr/bin/systemctl', 'is-active', 'multipathd.service', check=False)['returncode'] == 0:
            raise RuntimeError('Stock multipathd competes with fixture-owned maps')
        root_adopted = self.manager('register', '--device', '/')
        enrolled = {}
        for spec in self.specs:
            node = self.create_map(spec)
            enrolled[spec['name']] = self.manager('register', '--device', '/dev/mapper/' + spec['name'])
        subprocess.run(['/sbin/dmsetup', '--noudevsync', 'remove', self.specs[1]['name']], check=True)
        return {'root_adopted': root_adopted, 'enrollments': enrolled, 'snapshot': self.snapshot()}

    def activate(self):
        return self.manager('install')

    def mount_registered(self):
        # Mounting is a normal consumer action outside the manager. Wait for
        # the registered controller to publish ready before starting I/O.
        for spec in self.specs:
            name = spec['name']
            deadline = time.monotonic() + 20
            while (self.read(self.directory(name) / 'state/path-state.json') or {}).get('state') != 'ready':
                if time.monotonic() > deadline:
                    raise RuntimeError('Manager did not start registered map: ' + name)
                time.sleep(.1)
            (self.ROOT / 'mnt' / name).mkdir(parents=True)
            self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/mount',
                      '-t', spec['fs_type'], '/dev/mapper/' + name, '/mnt/' + name)
        return {'manager': self.manager_status(), 'snapshot': self.snapshot()}

    def manager_status(self):
        controllers = []
        for path in Path('/proc').iterdir():
            if not path.name.isdigit():
                continue
            live = self.process(int(path.name))
            if live and any(token in live['cmdline'] for token in
                            ('path_guard.py --config ', 'maintain.py --record ')):
                controllers.append(live)
        exclusions = {}
        for spec in self.specs:
            node = Path('/dev/mapper') / spec['name']
            if not node.exists():
                continue
            dev = node.stat().st_rdev
            number = f'{os.major(dev)}:{os.minor(dev)}'
            sysnode = (Path('/sys/dev/block') / number).resolve()
            raw = list((sysnode / 'slaves').iterdir())
            if len(raw) != 1:
                raise RuntimeError('VM map must have exactly one USB partition')
            raw_number = (raw[0] / 'dev').read_text().strip()
            database = Path('/run/udev/data')
            exclusions[spec['name']] = {
                'raw': (database / ('b' + raw_number)).read_text(),
                'map': (database / ('b' + number)).read_text(),
            }
        return {'status': self.manager('status'),
                'units': self.host('/usr/bin/systemctl', 'list-units', '--all', '--no-pager',
                                   '--plain', 'ram-rescue*.service', 'ram-rescue*.path'),
                'root_owner': self.host('/usr/bin/systemctl', 'show', 'ram-rescue-guard.service',
                                        '-p', 'MainPID,ActiveState,SubState'),
                'usb_serials': sorted(path.read_text().strip() for path in
                                      Path('/sys/bus/usb/devices').glob('*/serial')),
                'controllers': controllers,
                'exclusions': exclusions,
                'snapshot': self.snapshot()}

    def create_late_map(self):
        node = self.create_map(self.specs[1])
        return {'node': node, 'manager': self.manager_status()}

    def prepare_shutdown(self):
        for spec in self.specs:
            mount = '/mnt/' + spec['name']
            if any(line.split()[4] == mount for line in Path('/proc/1/mountinfo').read_text().splitlines()):
                self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/umount', mount)
        return self.manager_status()

    def install_boot_fixture(self):
        """Only the VM fixture, not production aftercare, creates DM maps."""
        from path_guard import table
        fixture = self.ROOT / 'root/unified-boot-maps.py'
        statements = [
            'from pathlib import Path', 'import subprocess,time',
            "assert 'ram_rescue_lab=1' in Path('/proc/cmdline').read_text().split()",
            "assert Path('/sys/class/dmi/id/product_name').read_text().strip() == 'RAMRescueLab'",
        ]
        for spec in self.specs:
            link = '/dev/disk/by-partuuid/' + spec['partuuid']
            statements += [
                f'link=Path({link!r})', 'deadline=time.monotonic()+20',
                'while not link.exists():',
                "    if time.monotonic()>deadline: raise RuntimeError('VM test partition absent')",
                '    time.sleep(.05)',
                "node=str(link.resolve(strict=True))",
                f"subprocess.run(['/sbin/dmsetup','--noudevsync','create',{spec['name']!r},'--uuid',{spec['map_uuid']!r},'--table',{table(131072, 'NODE')!r}.replace('NODE',node)],check=True)",
                f"subprocess.run(['/sbin/dmsetup','--noudevsync','mknodes',{spec['name']!r}],check=True)",
            ]
        statements += ["subprocess.run(['/usr/bin/udevadm','trigger','--action=change','--subsystem-match=block','--settle'],check=True)"]
        fixture.write_text('\n'.join(statements) + '\n')
        unit = self.ROOT / 'etc/systemd/system/unified-vm-maps.service'
        unit.write_text('[Unit]\nDescription=VM-only disposable multipath map fixture\n'
            'After=ram-rescue-guard.service\nBefore=ram-rescue-manager.service\n'
            'ConditionKernelCommandLine=ram_rescue_lab=1\n\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            'ExecStart=/usr/bin/python3 /root/unified-boot-maps.py\n\n'
            '[Install]\nWantedBy=multi-user.target\n')
        self.host('/usr/bin/systemctl', 'daemon-reload')
        return self.host('/usr/bin/systemctl', 'enable', 'unified-vm-maps.service')

    def logs(self):
        result = super().logs()
        result['manager_journal'] = self.host('/usr/bin/journalctl', '-b', '--no-pager',
            '-u', 'ram-rescue*.service', '-u', 'ram-rescue*.path')
        return result

    def action(self, action):
        self.gate()
        actions = {'manager_status': self.manager_status, 'activate': self.activate,
                   'mount_registered': self.mount_registered, 'install_boot_fixture': self.install_boot_fixture,
                   'create_late_map': self.create_late_map, 'prepare_shutdown': self.prepare_shutdown}
        return actions[action]() if action in actions else super().action(action)
