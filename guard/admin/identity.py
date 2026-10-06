"""Read-only device identity policies used during cold enrollment."""
import re

from pathlib import Path

from rescue import RescueDiagnostics, Refuse
from .admission import readonly


FILESYSTEM_TYPES = frozenset({'ext4', 'exfat', 'vfat'})


class LVMIdentity:
    """Expose only the existing manual tool's read-only discovery operations."""

    def __init__(self, config, sysroot=Path('/sys'), devroot=Path('/dev'), runner=readonly):
        self._source = RescueDiagnostics(config, sysroot, devroot, runner)
        self.c = config
        self.sys = self._source.sys
        self.dev = self._source.dev
        self.run = runner

    def candidate_node(self):
        return self._source.candidate_node()

    def verify(self):
        return self._source.verify()

    def mapping(self, target):
        return self._source.mapping(target)


class FilesystemIdentity(LVMIdentity):
    """Reuse USB discovery, then attest one plain filesystem with blkid."""

    def __init__(self, config, *args, **kwargs):
        if config.get('kind') != 'filesystem':
            raise Refuse('Filesystem identity must explicitly select kind=filesystem')
        if not isinstance(config.get('fs_type'), str) or config['fs_type'] not in FILESYSTEM_TYPES:
            raise Refuse('Unsupported filesystem type; expected ext4, exfat, or vfat')
        for key in ('usb_serial', 'fs_uuid', 'partuuid'):
            if not isinstance(config.get(key), str) or not config[key].strip():
                raise Refuse(f'Filesystem enrollment needs a nonempty {key}')
        for key in ('vid', 'pid'):
            if not isinstance(config.get(key), str) or not re.fullmatch('[0-9a-f]{4}', config[key]):
                raise Refuse(f'Filesystem enrollment needs a lowercase USB {key}')
        for key in ('sectors', 'partition_number'):
            if type(config.get(key)) is not int or config[key] <= 0:
                raise Refuse(f'Filesystem enrollment needs a positive integer {key}')
        super().__init__(config, *args, **kwargs)

    def admission_layout(self, node):
        """Probe stable identity fields; labels and allocation state may change."""
        props = dict(line.split('=', 1) for line in
                     self.run(['/sbin/blkid', '-p', '-o', 'export', node]).splitlines()
                     if '=' in line)
        expected = {'TYPE': self.c['fs_type'], 'UUID': self.c['fs_uuid'],
                    'PART_ENTRY_UUID': self.c['partuuid']}
        for key, value in expected.items():
            if props.get(key) != value:
                raise Refuse(f'{key} does not match the enrolled filesystem. No changes made.')
        return {'kind': 'filesystem', 'fs_type': props['TYPE'],
                'fs_uuid': props['UUID'], 'partuuid': props['PART_ENTRY_UUID']}

    def verify(self):
        node = self.candidate_node()
        self.admission_layout(node)
        return node
