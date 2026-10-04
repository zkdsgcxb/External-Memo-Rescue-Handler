"""Bounded VM observer for genuine dependency changes; no recovery implementation."""

DEPENDENCIES = json.loads(Path('/run/standalone/dependency-fixture.json').read_text())


class DependencyProbe(StandaloneProbe):
    def dependencies(self):
        gate()
        root = Path('/run/ram-rescue-manager/tools')
        resident = {}
        if root.exists():
            library = root / DEPENDENCIES['library'].lstrip('/')
            resident = {'root': str(root.resolve()),
                        'library_sha256': hashlib.sha256(library.read_bytes()).hexdigest(),
                        'library_inode': library.stat().st_ino,
                        'binary_sha256': hashlib.sha256((root / 'opt/guard-runtime/guard-runtime').read_bytes()).hexdigest(),
                        'manifest_sha256': hashlib.sha256((root / 'opt/guard-runtime/runtime.json').read_bytes()).hexdigest()}
        registry = Path('/etc/ram-rescue-manager/devices')
        controllers = []
        for spec in self.specs:
            unit = 'ram-rescue-maintain@' + spec['name'] + '.service'
            pid = int(systemctl('show', unit, '-p', 'MainPID', '--value').strip())
            if not pid:
                continue
            proc = Path('/proc') / str(pid)
            mappings = [line for line in (proc / 'maps').read_text().splitlines()
                        if line.endswith('/' + Path(DEPENDENCIES['library']).name)]
            if not mappings:
                raise RuntimeError('Active native owner has not mapped the expected real library')
            # map_files addresses the actual mapped inode, including an
            # unlinked file; a pathname-only digest would be weaker evidence.
            mapped = proc / 'map_files' / mappings[0].split()[0]
            with mapped.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            with (proc / 'exe').open('rb') as stream:
                executable = hashlib.file_digest(stream, 'sha256').hexdigest()
            controllers.append({'name': spec['name'], 'process': process(pid),
                                'binary_sha256': executable, 'loaded_library_sha256': digest,
                                'actual_mappings': mappings})
        return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'root_guard_absent': not Path('/run/ram-rescue-guard/config.json').exists(),
                'resident': resident, 'controllers': controllers,
                'registry_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in registry.glob('*.json')},
                'receipt': self.read('/var/lib/ram-rescue-manager/install.json'),
                'package': self.host('/usr/bin/dpkg-query', '-W', '-f=${Version}', 'ram-rescue-handler', check=False)}

    def configure_a(self):
        configured = self.configure()
        fixture = self.install_boot_fixture()
        return {'configured': configured, 'boot_fixture': fixture}

    def retire(self):
        gate()
        stopped = []
        for spec in self.specs:
            unit = 'ram-rescue-maintain@' + spec['name'] + '.service'
            self.host('/usr/bin/systemctl', 'stop', unit)
            # The probe never mounted these maps or launched file workloads.
            # Ordinary removal after stopping their owners is the production
            # manager's required quiescent boundary, not a forced dm removal.
            self.host('/sbin/dmsetup', 'remove', spec['name'])
            stopped.append(unit)
        return {'stopped': stopped, 'dependencies': self.dependencies()}

    def transition(self, label):
        before = self.dependencies()
        retired = self.retire()
        package = 'handler.deb' if label == 'a' else 'handler-b.deb'
        installed = self.host('/usr/bin/dpkg', '--install', '/run/standalone/' + package, timeout=90)
        upgraded = self.manager('upgrade')
        after = self.dependencies()
        return {'before': before, 'retired': retired, 'package_install': installed,
                'upgrade': upgraded, 'after': after}

    def action(self, action):
        choices = {'configure_a': self.configure_a, 'dependencies': self.dependencies,
                   'upgrade_b': lambda: self.transition('b'), 'rollback_a': lambda: self.transition('a'),
                   'retire': self.retire}
        return choices[action]() if action in choices else super().action(action)


def serve_dependencies():
    gate()
    # The observer's read-only helpers follow the actual persistent manager
    # version, not whichever package happened to be packaged into the initrd.
    command = Path('/usr/local/sbin/rescue-guard')
    if command.exists():
        root = command.resolve().parent.parent
        sys.path[:0] = [str(root / 'guard'), str(root / 'ram-rescue-demo/src')]
    probe = DependencyProbe(gate, systemctl, process, FIXTURE['specs'])
    allowed = {'configure_a', 'dependencies', 'upgrade_b', 'rollback_a', 'retire', 'snapshot', 'shutdown', 'logs'}
    tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
    print(json.dumps({'ready': True, 'protocol': 'dependency-upgrade-v1'}), flush=True)
    while line := sys.stdin.buffer.readline(4097):
        if len(line) > 4096:
            raise RuntimeError('Oversized VM action')
        request = json.loads(line)
        if (not isinstance(request, dict) or set(request) != {'id', 'action'}
                or not isinstance(request['action'], str) or request['action'] not in allowed):
            raise RuntimeError('Invalid VM action')
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                value = probe.action(request['action'])
            response = {'id': request['id'], 'ok': True, 'value': value}
        except BaseException:
            response = {'id': request['id'], 'ok': False, 'error': traceback.format_exc()}
        encoded = json.dumps(response)
        if len(encoded) > 4 * 1024**2:
            raise RuntimeError('Oversized VM result')
        print(encoded, flush=True)


if __name__ == '__main__':
    serve_dependencies()
