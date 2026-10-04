"""Production policy and optional detailed sampler for the roadmap A/B trial."""


class RoadmapPerformanceProbe(ProductionIntegrationProbe):
    @staticmethod
    def read_namespace_policy(pid, writable):
        unit = Path('/proc/1/root/run/systemd/system/ram-rescue-guard.service').read_text()
        if 'ProtectSystem=strict' in unit.splitlines():
            return ProductionIntegrationProbe.read_namespace_policy(pid, writable)
        # The baseline's exact unit hash is checked by integration_integrity.
        # It did not request a read-only root; do not silently harden it for A/B.
        rows = [line.split() for line in Path(f'/proc/{pid}/mountinfo').read_text().splitlines()]
        selected = {path: [row[5].split(',') for row in rows if row[4] == path]
                    for path in ['/', *writable]}
        if not selected['/'] or any('rw' not in options for options in selected['/']):
            raise RuntimeError('Historical service no longer has its original writable root')
        return {'verified': True, 'historical_unrestricted_root': True, 'mount_options': selected}

    def rescue_logging_integrity(self):
        # Both versions run their own unchanged logger unit. Older releases
        # intentionally lack the new restrictions; never retrofit those limits
        # into the baseline or call it a measurement of the old package.
        import configparser
        source = Path('/opt/vmprobe/ram-rescue-log.service')
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(source.read_text())
        policy = config['Service']
        state = self.host('/usr/bin/systemctl', 'show', 'ram-rescue-log.service',
            '-p', 'ActiveState,MainPID,CapabilityBoundingSet,NoNewPrivileges,ProtectSystem,DropInPaths')['stdout']
        fields = dict(line.split('=', 1) for line in state.splitlines() if '=' in line)
        expected = {'ActiveState': 'active', 'DropInPaths': '',
                    'NoNewPrivileges': policy.get('NoNewPrivileges', 'no'),
                    'ProtectSystem': policy.get('ProtectSystem', 'no')}
        if 'CapabilityBoundingSet' in policy:
            expected['CapabilityBoundingSet'] = policy['CapabilityBoundingSet'].lower()
        if any(fields.get(key) != value for key, value in expected.items()):
            raise RuntimeError('Logger policy differs from selected release: ' + state)
        for name, digest in EXPERIMENT['rescue_unit_sha256'].items():
            path = self.ROOT / 'run/systemd/system' / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError('Selected release logging unit changed: ' + name)
        return {'verified': True, 'service': fields, 'expected': expected,
                'namespace': self.read_namespace_policy(int(fields['MainPID']), ['/var/log']),
                'log_bytes': Path('/var/log/kernel-live.log').stat().st_size}

    def mount_registered(self):
        # Authentication has its own acceptance matrix; avoid extra auth fixture
        # allocations immediately before the performance warm-up.
        result = CppExperimentProbe.mount_registered(self)
        result['production_integration'] = self.integration_integrity()
        result['rescue_logging'] = self.rescue_logging_integrity()
        return result

    def logs(self):
        result = CppExperimentProbe.logs(self)
        result['production_integration'] = self.integration_integrity()
        result['effective_policy'] = self.policy_snapshot()
        result['rescue_logging'] = self.rescue_logging_integrity()
        return result

    def policy_snapshot(self):
        integrity = self.integration_integrity()
        names = ['ram-rescue-guard.service'] + [
            'ram-rescue-maintain@' + spec['name'] + '.service' for spec in self.specs]
        units = {name: self.host('/usr/bin/systemctl', 'show', name, '-p',
            'MainPID,ControlGroup,NoNewPrivileges,CapabilityBoundingSet,RestrictAddressFamilies,'
            'SystemCallFilter,SystemCallArchitectures,RestrictNamespaces,ProtectSystem,ReadWritePaths,'
            'MemoryDenyWriteExecute,PrivateDevices,CPUQuotaPerSecUSec,CPUQuotaPeriodUSec')
            for name in names}
        controllers = {}
        for name, owner in integrity['actual_controllers']['controllers'].items():
            if not owner['live']:
                continue
            pid = owner['process']['pid']
            status = {}
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                key, _, value = line.partition(':')
                if key in {'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'NoNewPrivs', 'Seccomp', 'Seccomp_filters'}:
                    status[key] = value.strip()
            controllers[name] = {'pid': pid, 'status': status,
                                 'mountinfo': Path(f'/proc/{pid}/mountinfo').read_text()}
        root = Path('/proc/1/root/sys/fs/cgroup')
        root_pid = int(Path('/run/ram-rescue-guard/state/path-guard.pid').read_text())
        relative = Path(f'/proc/{root_pid}/cgroup').read_text().strip().split('0::', 1)[1]
        quotas = {'root': (root / relative.lstrip('/') / 'cpu.max').read_text().strip(),
                  'data_slice': (root / 'ramrescuedata.slice/cpu.max').read_text().strip()}
        if set(quotas.values()) != {'4000 20000'}:
            raise RuntimeError('Production root/data budgets differ from the 20%/20ms trial contract')
        return {'integration': integrity, 'units': units, 'controllers': controllers, 'cpu_max': quotas}

    def measure(self, name):
        kind = {'unrelated': 'unrelated', 'relevant': 'relevant'}.get(name)
        return sample_guard_groups(name, 20, kind, process_samples=True, pss_period=.2)

    def start_sampler(self):
        output = Path('/run/performance-recovery.json')
        if output.exists():
            raise RuntimeError('Recovery measurement already exists')
        code = ("import sys,json;sys.path.insert(0,'/opt/vmprobe');import probe;"
                "result=probe.sample_guard_groups('recovery',16,process_samples=True,pss_period=.2);"
                "open('/run/performance-recovery.json','w').write(json.dumps(result))")
        log = open('/run/performance-sampler.log', 'ab', buffering=0)
        try:
            worker = subprocess.Popen([sys.executable, '-c', code], stdout=log, stderr=log,
                                      stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            log.close()
        return {'pid': worker.pid}

    def action(self, action):
        self.gate()
        if action in ('quota20', 'effective_policy'):
            # Read actual shipped limits. Setting a runtime property would add a
            # drop-in and invalidate the "real production unit" comparison.
            return self.policy_snapshot()
        if action.startswith('quota'):
            raise RuntimeError('This comparison must retain the shipped 20% quotas')
        return super().action(action)


UnifiedProbe = RoadmapPerformanceProbe
