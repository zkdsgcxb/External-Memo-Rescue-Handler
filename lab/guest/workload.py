#!/usr/bin/python3
"""Application in the USB-backed root: append and fsync, record outcomes in RAM."""
import json
import os
import time

with open('/run/workload.jsonl', 'a', buffering=1) as log:
    seq = 0
    while True:
        seq += 1
        start = time.monotonic()
        event = {'seq': seq, 'start': start}
        try:
            with open('/root/workload.data', 'ab', buffering=0) as data:
                data.write((str(seq)+'\n').encode() + b'x'*4096)
                os.fsync(data.fileno())
            event['ok'] = True
        except OSError as exc:
            event.update(ok=False, errno=exc.errno, error=str(exc))
        event['elapsed'] = time.monotonic()-start
        log.write(json.dumps(event)+'\n')
        time.sleep(0.1)
