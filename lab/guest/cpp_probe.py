"""Observe the selected runtime; all test control remains outside Guard cgroups.

The Python modules in this RAM observer are measurement tools, not fallback
controllers. Native controller ownership is checked against /proc/PID/exe.
"""

EXPERIMENT = json.loads(Path('/opt/vmprobe/cpp-experiment.json').read_text())
NATIVE = '/opt/guard-runtime/guard-runtime'


def selected_controller(process):
    if not process:
        return False
    pid = process['pid']
    try:
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
        if EXPERIMENT['implementation'] == 'cpp':
            return (argv[:2] == [NATIVE.encode(), b'run'] and
                    hashlib.sha256(Path(f'/proc/{pid}/exe').read_bytes()).hexdigest()
                    == EXPERIMENT['binary_sha256'])
        return b'/opt/guard/path_guard.py' in argv
    except (FileNotFoundError, ProcessLookupError):
        return False


class CppExperimentProbe(SelectedProbe):
    def activate(self):
        if EXPERIMENT['implementation'] == 'cpp':
            directory = self.ROOT / 'run/systemd/system/ram-rescue-maintain@.service.d'
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'cpp-experiment.conf').write_text(
                '[Service]\nExecStart=\n'
                f'ExecStart={NATIVE} maintain --record /run/ram-rescue-manager/entries/%i.json\n'
                'ExecStopPost=\n'
                f'ExecStopPost={NATIVE} maintain --record /run/ram-rescue-manager/entries/%i.json --takeover\n')
        return super().activate()

    def runtime_integrity(self, require_all=True):
        locations = {'root': Path('/run/ram-rescue-guard/state')}
        locations.update({spec['name']: self.directory(spec['name']) / 'state' for spec in self.specs})
        controllers = {}
        for name, directory in locations.items():
            pidfile = directory / 'path-guard.pid'
            pid = int(pidfile.read_text()) if pidfile.exists() else None
            live = self.process(pid) if pid else None
            if not live or live['state'] in ('Z', 'X'):
                if require_all:
                    raise RuntimeError('Selected controller is absent: ' + name)
                controllers[name] = {'live': False, 'pid': pid}
                continue
            argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')[:-1]
            executable = Path(f'/proc/{pid}/exe')
            digest = hashlib.sha256(executable.read_bytes()).hexdigest()
            if EXPERIMENT['implementation'] == 'cpp':
                expected_mode = b'run' if name == 'root' else b'maintain'
                if argv[:2] != [NATIVE.encode(), expected_mode] or digest != EXPERIMENT['binary_sha256']:
                    raise RuntimeError('Controller is not the selected native executable: ' + name)
                if executable.read_bytes()[:4] != b'\x7fELF':
                    raise RuntimeError('Native controller is not ELF: ' + name)
            else:
                token = b'/opt/guard/path_guard.py' if name == 'root' else b'/opt/manager/maintain.py'
                if token not in argv:
                    raise RuntimeError('Baseline controller is not the selected Python entrypoint: ' + name)
            controllers[name] = {'live': True, 'process': live, 'executable': str(executable.resolve()),
                                 'executable_sha256': digest, 'argv': [part.decode() for part in argv]}
        legacy_controllers = []
        if EXPERIMENT['implementation'] == 'cpp':
            for process_path in Path('/proc').iterdir():
                if not process_path.name.isdigit():
                    continue
                try:
                    arguments = (process_path / 'cmdline').read_bytes().split(b'\0')
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if any(argument in arguments for argument in
                       (b'/opt/guard/path_guard.py', b'/opt/manager/maintain.py')):
                    legacy_controllers.append(int(process_path.name))
            if legacy_controllers:
                raise RuntimeError('Python recovery controller still running: ' + repr(legacy_controllers))
        return {'implementation': EXPERIMENT['implementation'], 'controllers': controllers,
                'legacy_python_controller_pids': legacy_controllers}

    def runtime_hashes(self):
        self.runtime_integrity()
        if EXPERIMENT['implementation'] == 'cpp':
            return {name: {'guard-runtime': EXPERIMENT['binary_sha256']} for name in ('root', 'data')}
        return {name: {filename: hashlib.sha256((directory / filename).read_bytes()).hexdigest()
                       for filename in EXPERIMENT['runtime_payload_sha256']}
                for name, directory in [('root', Path('/opt/guard')), ('data', Path('/opt/manager'))]}

    def manager_status(self):
        value = super().manager_status()
        value['runtime_integrity'] = self.runtime_integrity(require_all=False)
        value['controllers'] = [owner['process'] for owner in value['runtime_integrity']['controllers'].values()
                                if owner['live']]
        return value

    def mount_registered(self):
        result = super().mount_registered()
        result['runtime_integrity'] = self.runtime_integrity()
        return result

    def logs(self):
        result = super().logs()
        result['runtime_integrity'] = self.runtime_integrity(require_all=False)
        return result

    def action(self, action):
        self.gate()
        if action == 'runtime_hashes':
            return self.runtime_hashes()
        if action == 'runtime_integrity':
            return self.runtime_integrity()
        return super().action(action)


UnifiedProbe = CppExperimentProbe
