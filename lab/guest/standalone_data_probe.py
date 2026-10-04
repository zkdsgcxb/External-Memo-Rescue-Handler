"""VM-only ordinary Ubuntu data-map acceptance; appended after mount observer."""
import contextlib
import io
import shutil
import stat
import tarfile
import termios
import traceback
import tty

FIXTURE = json.loads(Path('/run/standalone/fixture.json').read_text())


def gate():
    flags = Path('/proc/cmdline').read_text().split()
    if ('ram_rescue_lab=1' not in flags or 'ram_rescue_standalone_test=1' not in flags
            or 'ram_rescue_guard=1' in flags or
            Path('/sys/class/dmi/id/product_name').read_text().strip() != 'RAMRescueLab'):
        raise RuntimeError('Disposable ordinary Ubuntu test gate refused')


def process(pid):
    try:
        path = Path('/proc') / str(pid)
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start_ticks': fields[19], 'state': fields[0],
                'cmdline': (path / 'cmdline').read_bytes().replace(b'\0', b' ').decode()}
    except FileNotFoundError:
        return None


def systemctl(*args):
    return subprocess.check_output(['/usr/bin/systemctl', *args], text=True, timeout=30)


def controller_mount_policy(mountinfo):
    """Verify the kernel's mount view, independent of configured unit strings."""
    rows = [line.split() for line in mountinfo.splitlines()]
    selected = {path: [row[5].split(',') for row in rows if row[4] == path]
                for path in ('/', '/run', '/dev')}
    if not selected['/'] or any('ro' not in options for options in selected['/']):
        raise RuntimeError('The actual data controller root is not read-only')
    if any(not selected[path] or any('rw' not in options for options in selected[path])
           for path in ('/run', '/dev')):
        raise RuntimeError('The actual data controller lacks writable /run or /dev')
    return {'verified': True, 'mount_options': selected}


class StandaloneProbe(MountProbe):
    def manager(self, *args, check=True):
        result = self.host('/usr/bin/rescue-guard-admin', 'manager', *args, timeout=90, check=check)
        if result['returncode'] == 0:
            result['value'] = json.loads(result['stdout'])
        return result

    def preflight(self):
        gate()
        root_guard = systemctl('show', 'ram-rescue-guard.service', '-p', 'LoadState,ActiveState,MainPID')
        maps = {(path / 'dm/name').read_text().strip(): {
                    'uuid': (path / 'dm/uuid').read_text().strip(),
                    'slaves': [p.name for p in (path / 'slaves').iterdir()]}
                for path in Path('/sys/class/block').glob('dm-*')}
        return {'flags': Path('/proc/cmdline').read_text().split(), 'root_guard': root_guard,
                'root_config_absent': not Path('/run/ram-rescue-guard/config.json').exists(),
                'protected_ram_absent': not Path('/run/ram-rescue-demo').exists(),
                'kernel': os.uname().release, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                'kernel_no_path_timeout_seconds': int(Path('/sys/module/dm_multipath/parameters/queue_if_no_path_timeout_secs').read_text()),
                'pid1': process(1), 'maps': maps,
                'root_mount': next(line for line in Path('/proc/1/mountinfo').read_text().splitlines() if line.split()[4] == '/')}

    def configure(self):
        gate()
        before = self.preflight()
        if not before['root_config_absent'] or not before['protected_ram_absent'] or 'ram-rescue-path' in before['maps']:
            raise RuntimeError('This scenario requires ordinary Ubuntu without protected root')
        installed = self.host('/usr/bin/dpkg', '--install', '/run/standalone/handler.deb', timeout=90)
        package = Path('/usr/lib/ram-rescue-handler') / FIXTURE['administration_version']
        sys.path[:0] = [str(package / 'guard'), str(package / 'ram-rescue-demo/src')]
        root_enrollment = self.enroll_root()
        integration = self.manager('install')
        enrolled = {}
        for spec in self.specs:
            self.create_map(spec)
            enrolled[spec['name']] = self.manager('register', '--device', '/dev/mapper/' + spec['name'])
        return {'before': before, 'package_install': installed, 'manager_install': integration,
                'administration_manifest': self.read(package / 'administration.json'),
                'installed_entry_sha256': hashlib.sha256(Path('/usr/bin/rescue-guard-admin').read_bytes()).hexdigest(),
                'root_enrollment': root_enrollment, 'registrations': enrolled,
                'environment': self.read('/run/ram-rescue-manager/runtime-environment.json')}

    def enroll_root(self):
        """Exercise the installed entry with actual seed PV and real boot files."""
        release = os.uname().release
        boot = Path('/boot')
        boot.mkdir(exist_ok=True)
        inputs = {'vmlinuz': ('enrollment-vmlinuz', 'vmlinuz-' + release, 'kernel_sha256'),
                  'original-initrd.img': ('enrollment-initrd.img', 'initrd.img-' + release, 'initrd_sha256')}
        for source, name, checksum in inputs.values():
            payload = Path('/run/standalone') / source
            with payload.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != FIXTURE['boot_inputs'][checksum]:
                    raise RuntimeError('Enrollment fixture boot input changed')
            destination = boot / name
            if os.path.lexists(destination):
                raise RuntimeError('Minimal seed unexpectedly already contains enrollment boot input')
            shutil.copyfile(payload, destination)
            destination.chmod(0o644)
        before = self.preflight()
        root = (Path('/sys/dev/block') / before['root_mount'].split()[2]).resolve(strict=True)
        slaves = list((root / 'slaves').iterdir())
        if len(slaves) != 1 or not (slaves[0] / 'partition').is_file():
            raise RuntimeError('Enrollment fixture requires its original ordinary single-PV root')
        command = self.host('/usr/bin/rescue-guard-admin', 'enroll-root', '--name', 'vm-root-enrollment',
                            '--partition', '/dev/' + slaves[0].name, '--usb-serial', 'RAMRESCUE-LAB-001', timeout=90)
        value = json.loads(command['stdout'])
        directory = Path('/var/lib/ram-rescue-enrollments/vm-root-enrollment')
        profile = self.read(directory / 'enrollment.json')
        checksums = {}
        private = all(path.stat().st_uid == 0 and stat.S_IMODE(path.stat().st_mode) == 0o700
                      for path in (directory, directory.parent))
        for path in sorted(directory.iterdir()):
            info = path.lstat()
            private &= stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
            with path.open('rb') as stream:
                checksums[path.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
        if set(checksums) != {'enrollment.json', *inputs}:
            raise RuntimeError('Unexpected enrollment export files')
        private &= all(checksums[name] == FIXTURE['boot_inputs'][spec[2]] == profile['baseline'][spec[2]]
                       for name, spec in inputs.items())
        unchanged = all(hashlib.sha256((boot / spec[1]).read_bytes()).hexdigest() == FIXTURE['boot_inputs'][spec[2]]
                        for spec in inputs.values())
        after = self.preflight()
        unchanged &= all(before[key] == after[key] for key in ('boot_id', 'pid1', 'root_mount', 'maps'))
        archive_hashes = {}
        exporter = subprocess.Popen(['/usr/bin/tar', '-C', str(directory), '-cf', '-', *sorted(checksums)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with tarfile.open(fileobj=exporter.stdout, mode='r|') as archive:
                for member in archive:
                    if member.name not in checksums or not member.isfile() or member.name in archive_hashes:
                        raise RuntimeError('Unexpected enrollment tar member')
                    with archive.extractfile(member) as stream:
                        archive_hashes[member.name] = hashlib.file_digest(stream, 'sha256').hexdigest()
            exporter.stdout.close()
            exporter.wait(timeout=20)
            if exporter.returncode:
                raise RuntimeError('Enrollment tar export failed')
        finally:
            if exporter.poll() is None:
                exporter.kill()
                exporter.wait()
            exporter.stdout.close()
            exporter.stderr.close()
        return {'result': value, 'file_sha256': checksums, 'private_files_verified': bool(private),
                'identity_verified': profile['identity']['usb_serial'] == 'RAMRESCUE-LAB-001'
                    and profile['identity']['vg_name'] == 'labrescue'
                    and profile['identity']['lvs'][profile['guard']['root_lv']]['dm_uuid'] == (root / 'dm/uuid').read_text().strip()
                    and profile['guard']['kernel_release'] == release,
                'boot_and_maps_unchanged': bool(unchanged), 'tar_verified': archive_hashes == checksums}

    def manager_status(self):
        controllers = []
        for spec in self.specs:
            unit = 'ram-rescue-maintain@' + spec['name'] + '.service'
            properties = dict(line.split('=', 1) for line in systemctl('show', unit,
                '-p', 'ActiveState,MainPID,DropInPaths,RootDirectory,NoNewPrivileges,CapabilityBoundingSet').splitlines() if '=' in line)
            pid = int(properties['MainPID'])
            if pid:
                exe = Path('/proc') / str(pid) / 'exe'
                before = process(pid)
                with exe.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                namespace = controller_mount_policy((exe.parent / 'mountinfo').read_text())
                after = process(pid)
                if not before or not after or before['start_ticks'] != after['start_ticks']:
                    raise RuntimeError('Controller changed during runtime observation')
                controllers.append({'name': spec['name'], 'properties': properties, 'process': after,
                                    'binary_sha256': digest, 'root': os.readlink(exe.parent / 'root'),
                                    'namespace': namespace})
        return {'controllers': controllers, 'doctor': self.manager('doctor')['value'],
                'snapshot': self.snapshot(), 'preflight': self.preflight()}

    def diagnostics(self):
        report = self.manager('doctor')['value']
        destination = '/root/standalone-export-' + str(time.monotonic_ns()) + '/report.json'
        exported = self.manager('export', '--output', destination)['value']
        raw = Path(exported['path']).read_text()
        exported_report = json.loads(raw)
        sensitive = [Path('/etc/hostname').read_text().strip(), 'RAMRESCUE-LAB-001']
        for spec in self.specs:
            sensitive.extend(spec[key] for key in ('name', 'serial', 'map_uuid', 'partuuid', 'fs_uuid'))
        leak = [value for value in sensitive if value and value in raw]
        return {'doctor': report, 'export': exported_report, 'metadata': exported,
                'redaction_passed': not leak,
                'leak_count': len(leak), 'file_mode': stat.S_IMODE(Path(exported['path']).stat().st_mode),
                'directory_mode': stat.S_IMODE(Path(exported['path']).parent.stat().st_mode),
                'root_config_absent': not Path('/run/ram-rescue-guard/config.json').exists()}

    def measured_idle(self):
        measured = self.measure_idle()
        totals = []
        for controller in self.manager_status()['controllers']:
            values = {}
            for line in (Path('/proc') / str(controller['process']['pid']) / 'smaps_rollup').read_text().splitlines():
                if ':' in line:
                    key, value = line.split(':', 1)
                    if key in ('Pss', 'Rss', 'Private_Dirty', 'Swap'):
                        values[key] = int(value.split()[0]) * 1024
            totals.append(values)
        measured['controller_memory_snapshot'] = totals
        measured['controller_pss_sum_bytes'] = sum(row.get('Pss', 0) for row in totals)
        tools = os.statvfs('/run/ram-rescue-manager/rootfs')
        measured['private_tmpfs_used_bytes'] = (tools.f_blocks - tools.f_bfree) * tools.f_frsize
        measured['private_tmpfs_limit_bytes'] = tools.f_blocks * tools.f_frsize
        measured['memory_scope_note'] = 'Process PSS, controller cgroup charge and total tool tmpfs use overlap; do not add them.'
        return measured

    def finish(self):
        unmounted = self.prepare_shutdown()
        for spec in self.specs:
            self.host('/usr/bin/systemctl', 'stop', 'ram-rescue-maintain@' + spec['name'] + '.service')
        return {'unmounted': unmounted, 'preflight': self.preflight()}

    def logs(self):
        result = super().logs()
        result['standalone_units'] = self.host('/usr/bin/journalctl', '-b', '--no-pager',
            '-u', 'ram-rescue-manager.service', '-u', 'ram-rescue-maintain@*.service')
        return result

    def failure_details(self):
        """Collect namespace setup evidence only after a controller failed.

        A stopped unit may be started once with systemd debug logging. No unit
        setting is overridden, and this diagnostic retry never counts as an
        acceptance result.
        """
        gate()
        result = {'namespace_mounts': [line for line in Path('/proc/1/mountinfo').read_text().splitlines()
                                     if line.split()[4] in ('/', '/run', '/dev', '/sys', '/proc')
                                     or '/ram-rescue-' in line.split()[4]], 'units': {}}
        previous = systemctl('log-level').strip()
        try:
            systemctl('log-level', 'debug')
            for spec in self.specs:
                unit = 'ram-rescue-maintain@' + spec['name'] + '.service'
                properties = systemctl('show', unit, '-p',
                    'LoadState,ActiveState,MainPID,RootDirectory,RootImage,ProtectSystem,ReadWritePaths,BindPaths,PrivateMounts,Result')
                item = result['units'][unit] = {'properties': properties}
                values = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
                if values.get('LoadState') == 'loaded' and values.get('ActiveState') == 'failed' and values.get('MainPID') == '0':
                    item['diagnostic_retry'] = self.host('/usr/bin/systemctl', 'start', unit, timeout=35, check=False)
            tools = Path('/run/ram-rescue-manager/tools')
            if tools.is_symlink():
                from security_policy import controller_restrictions
                policy = [argument for line in controller_restrictions().splitlines()
                          for argument in ('--property', line)]
                result['namespace_comparison'] = {}
                for name, root in (('alias', tools), ('canonical', tools.resolve(strict=True))):
                    result['namespace_comparison'][name] = self.host('/usr/bin/systemd-run', '--wait', '--pipe', '--collect',
                        '--unit', 'standalone-namespace-' + name, '--property', 'RootDirectory=' + str(root),
                        '--property', 'WorkingDirectory=/', *policy, '/bin/busybox', 'true', timeout=20, check=False)
        finally:
            systemctl('log-level', previous)
        result['debug_journal'] = self.host('/usr/bin/journalctl', '-b', '--no-pager', '-n', '500')
        return result

    def shutdown(self):
        subprocess.Popen(['/usr/bin/systemctl', 'poweroff'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'requested': True}

    def action(self, action):
        self.gate()
        choices = {'preflight': self.preflight, 'configure': self.configure,
                   'manager_status': self.manager_status, 'diagnostics': self.diagnostics,
                   'measured_idle': self.measured_idle, 'finish': self.finish, 'shutdown': self.shutdown,
                   'failure_details': self.failure_details}
        return choices[action]() if action in choices else super().action(action)


def serve():
    """Bounded action protocol on an isolated VM serial port, no eval/shell."""
    gate()
    package = Path('/usr/lib/ram-rescue-handler') / FIXTURE['administration_version']
    if package.is_dir():
        sys.path[:0] = [str(package / 'guard'), str(package / 'ram-rescue-demo/src')]
    probe = StandaloneProbe(gate, systemctl, process, FIXTURE['specs'])
    allowed = {'preflight', 'configure', 'snapshot', 'diagnostics', 'measured_idle',
               'install_mount_plan', 'mount_registered', 'start', 'audit', 'manager_status',
               'readonly_policy', 'logs', 'install_boot_fixture', 'finish', 'shutdown'}
    allowed.add('failure_details')
    tty.setraw(sys.stdin.fileno(), when=termios.TCSANOW)
    print(json.dumps({'ready': True, 'protocol': 'standalone-data-v1'}), flush=True)
    while line := sys.stdin.buffer.readline(4097):
        if len(line) > 4096:
            raise RuntimeError('Oversized VM action')
        request = json.loads(line)
        if (not isinstance(request, dict) or set(request) != {'id', 'action'} or
                not isinstance(request['action'], str) or request['action'] not in allowed):
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
    serve()
