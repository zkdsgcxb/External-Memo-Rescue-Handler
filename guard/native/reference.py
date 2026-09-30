#!/usr/bin/env python3
"""Read-only Python reference for the optional C++ health observer.

This is an experiment, not a Guard replacement. Both observers keep the
initial admitted incarnation fixed and cannot authorize a reconnect.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))
from dm_monitor import DeviceMapper, Events, Schedule


def observe(args):
    mapper = DeviceMapper()
    if mapper.target_version('multipath') < (1, 15, 0):
        raise RuntimeError('Multipath target >= 1.15.0 is required')
    device = os.stat(args.node).st_rdev
    expected = f'{os.major(device)}:{os.minor(device)}'
    path = Path('/sys/class/block') / Path(args.node).name
    events = Events()
    started = time.monotonic()
    schedule = Schedule(started)
    pending = fault = False
    try:
        while time.monotonic() < started + args.seconds:
            now = time.monotonic()
            if schedule.due(now, pending):
                uuid, targets = mapper.query(args.map)
                if uuid != args.uuid or len(targets) != 1 or targets[0][0] != 'multipath':
                    raise RuntimeError('Unexpected stable map identity or target')
                status = targets[0][1]
                failed = bool(re.search(r'\b\d+:\d+ F \d+\b', status))
                active = bool(re.search(r'\b' + re.escape(expected) + r' A \d+\b', status))
                try:
                    resolved = path.resolve(strict=True)
                    present = (str(resolved) == args.sys_path and
                               int((resolved.parent / 'diskseq').read_text()) == args.diskseq and
                               os.stat(args.node).st_rdev == device)
                except (OSError, ValueError):
                    present = False
                healthy = present and not failed and active
                fault |= not healthy
                print(json.dumps({'state': 'ready' if healthy else 'path-unavailable',
                                  'time': now - started, 'present': present,
                                  'current_active': active, 'failed_path': failed}), flush=True)
                schedule.completed(time.monotonic(), False)
                pending = False
            wake = min(schedule.next_check, started + args.seconds)
            if pending:
                wake = min(wake, schedule.event_after)
            pending = events.wait(wake - time.monotonic(),
                                  defer_events=pending and time.monotonic() < schedule.event_after) or pending
    finally:
        events.close()
    return 2 if fault else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('map', 'uuid', 'node', 'sys-path'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--diskseq', type=int, required=True)
    parser.add_argument('--seconds', type=float, default=30)
    args = parser.parse_args()
    if not 0 < args.seconds <= 3600 or args.diskseq <= 0:
        parser.error('Positive diskseq and duration in (0, 3600] are required')
    try:
        return observe(args)
    except Exception as exc:
        print(json.dumps({'state': 'control-uncertain', 'reason': str(exc)}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
