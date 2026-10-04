"""VM observer adapter to current read-only DM administration APIs.

No recovery loop, mapping mutation or historical controller is loaded here.
The integration fixture owns creation of its disposable test maps explicitly.
"""
import sys
from pathlib import Path

sys.path.insert(0, '/run/data-launcher-src/guard')
from admin.dm import DeviceMapper as ReadOnlyMapper, expected_table


def gate():
    if ('ram_rescue_lab=1' not in Path('/proc/cmdline').read_text().split() or
            Path('/sys/class/dmi/id/product_name').read_text().strip() != 'RAMRescueLab'):
        raise RuntimeError('VM-only DM observer refused')


class DeviceMapper(ReadOnlyMapper):
    def __init__(self):
        gate()
        super().__init__()

    def query(self, name):
        value = self._read(name, 10)  # DM_DEVICE_STATUS, a read-only query
        return value['uuid'], [(target[2], target[3]) for target in value['targets']]


def checked_snapshot(mapper, config):
    value = mapper.snapshot(config['map_name'])
    if (value['uuid'] != config['map_uuid'] or len(value['active']) != 1 or
            value['active'][0][2] != 'multipath'):
        raise RuntimeError('Unexpected stable map identity or target')
    return value


def table(sectors, device):
    return ' '.join(str(part) for part in expected_table(sectors, device)[0])
