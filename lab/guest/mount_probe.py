"""VM-only mount graph and held-file-descriptor acceptance checks."""

sys.path.insert(0, '/run/data-launcher-src/guard')


class MountProbe(UnifiedProbe):
    EXT4 = '/mnt/rr-data-vm-ext4'
    VFAT = EXT4 + '/nested-vfat'
    CHILDREN = {'child-ext4': '/mnt/ext4-bind', 'child-vfat': '/mnt/vfat-bind'}

    def configure(self):
        configured = super().configure()
        self.create_map(self.specs[1])
        return configured

    def mount_paths(self):
        return [self.EXT4, self.VFAT, '/mnt/rr-data-vm-vfat', *self.CHILDREN.values()]

    def snapshot(self):
        value = super().snapshot()
        paths = self.mount_paths()
        value['mount_graph'] = {path: next((line for line in Path('/proc/1/mountinfo').read_text().splitlines()
                                         if line.split()[4] == path and ' - autofs ' not in line), None)
                                for path in paths}
        value['children'] = {}
        for name, mount in self.CHILDREN.items():
            worker = self.read(Path('/run') / (name + '-worker.json'))
            records = self.records(name)
            value['children'][name] = {'worker': worker, 'mount': mount,
                'process': self.process(worker['pid']) if worker else None,
                'acks': len(records), 'errors': [row for row in records if not row['ok']]}
        return value

    def install_mount_plan(self):
        from mounts import render_units
        records = {spec['name']: self.read(self.ROOT / 'etc/ram-rescue-manager/devices' /
                                          (spec['name'] + '.json')) for spec in self.specs}
        plan = {'schema': 1, 'mounts': [
            {'map': self.specs[0]['name'], 'where': self.EXT4, 'automount': True},
            {'map': self.specs[1]['name'], 'where': self.VFAT},
            {'bind': self.VFAT, 'where': '/mnt/rr-data-vm-vfat'},
            {'bind': self.EXT4 + '/work', 'where': self.CHILDREN['child-ext4']},
            {'bind': self.VFAT + '/work', 'where': self.CHILDREN['child-vfat']},
        ]}
        units = render_units(plan, records)
        for name, content in units.items():
            (self.ROOT / 'etc/systemd/system' / name).write_text(content)
        (self.ROOT / 'root/mount-plan.json').write_text(json.dumps(plan))
        self.host('/usr/bin/systemctl', 'daemon-reload')
        verified = self.host('/usr/bin/systemd-analyze', 'verify', '--man=no',
                             *['/etc/systemd/system/' + name for name in units])
        self.host('/usr/bin/systemctl', 'start', 'mnt-rr\\x2ddata\\x2dvm\\x2dext4.automount')
        return {'units': units, 'plan': plan, 'verify': verified, 'snapshot': self.snapshot()}

    def mount_registered(self):
        from mounts import unit_name
        # The first directory lookup intentionally triggers the native automount.
        (self.ROOT / self.EXT4.lstrip('/') / 'work').mkdir(exist_ok=True)
        self.host('/usr/bin/systemctl', 'start', unit_name(self.VFAT))
        (self.ROOT / self.VFAT.lstrip('/') / 'work').mkdir(exist_ok=True)
        for path in ['/mnt/rr-data-vm-vfat', *self.CHILDREN.values()]:
            self.host('/usr/bin/systemctl', 'start', unit_name(path))
        return self.snapshot()

    def start(self):
        super().start()
        for name, mount in self.CHILDREN.items():
            with (Path('/run') / (name + '-stderr.log')).open('ab', buffering=0) as log:
                child = subprocess.Popen(
                    ['/bin/chroot', str(self.ROOT), '/usr/bin/python3',
                     '/root/data-guard-workload.py', name, mount],
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            (Path('/run') / (name + '-worker.json')).write_text(json.dumps(self.process(child.pid)))
        return self.snapshot()

    def audit(self):
        for name in self.CHILDREN:
            worker = self.read(Path('/run') / (name + '-worker.json'))
            current = self.process(worker['pid'])
            if not current or current['start_ticks'] != worker['start_ticks'] or current['state'] in ('Z', 'X'):
                raise RuntimeError('Original child workload no longer alive: ' + name)
            os.kill(worker['pid'], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all(not (proc := self.process(self.read(Path('/run') / (name + '-worker.json'))['pid']))
                   or proc['state'] == 'Z' for name in self.CHILDREN):
                break
            time.sleep(.02)
        else:
            raise RuntimeError('Child workloads did not stop normally')
        result = super().audit()
        result['children'] = {}
        for name, mount in self.CHILDREN.items():
            records = self.records(name)
            if not records or any(not row['ok'] or row['seq'] != i for i, row in enumerate(records, 1)):
                raise RuntimeError('Child ACK stream failed: ' + name)
            expected = b''.join((str(row['seq']) + '\n').encode().ljust(4096, b'x') for row in records)
            actual = (self.ROOT / mount.lstrip('/') / 'held-fd.data').read_bytes()
            result['children'][name] = {'acks': len(records), 'hash_matches': actual == expected,
                'max_write_and_direct_read_seconds': max(row['elapsed'] for row in records)}
        return result

    def readonly_policy(self):
        from mounts import unit_name
        # A controlled read-only mount represents a filesystem already degraded
        # to RO. This does not claim to reproduce ext4 journal corruption.
        self.host('/usr/bin/systemd-run', '--wait', '--pipe', '/bin/mount', '-o', 'remount,ro', self.VFAT)
        before = self.snapshot()
        self.host('/usr/bin/systemctl', 'start', unit_name(self.VFAT))
        return {'before': before, 'after': self.snapshot()}

    def prepare_shutdown(self):
        from mounts import unit_name
        for path in reversed(self.mount_paths()):
            self.host('/usr/bin/systemctl', 'stop', unit_name(path))
        self.host('/usr/bin/systemctl', 'stop', unit_name(self.EXT4, 'automount'))
        return self.snapshot()

    def terminal_mount_start(self):
        from mounts import unit_name
        retained = self.snapshot()
        # Requires does not evict an existing mount merely because its service
        # exits. Close consumers normally before testing a NEW mount request.
        for path in (self.CHILDREN['child-vfat'], '/mnt/rr-data-vm-vfat', self.VFAT):
            self.host('/usr/bin/systemctl', 'stop', unit_name(path))
        result = self.host('/usr/bin/systemctl', 'start', unit_name(self.VFAT),
                           timeout=40, check=False)
        return {'retained_before_unmount': retained, 'start': result, 'snapshot': self.snapshot()}

    def logs(self):
        logs = super().logs()
        logs['mount_journal'] = self.host('/usr/bin/journalctl', '-b', '--no-pager',
                                        '-u', '*.mount', '-u', '*.automount')
        return logs

    def action(self, action):
        self.gate()
        actions = {'install_mount_plan': self.install_mount_plan, 'readonly_policy': self.readonly_policy,
                   'terminal_mount_start': self.terminal_mount_start}
        return actions[action]() if action in actions else super().action(action)
