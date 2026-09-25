#!/usr/bin/python3
"""Application in the USB-backed root: append and fsync, record outcomes in RAM."""
import json
import errno
import os
import time


def write_all(stream, payload):
    """FileIO.write can return a short count without raising an exception."""
    pending = memoryview(payload)
    while pending:
        written = stream.write(pending)
        if type(written) is not int or not 0 < written <= len(pending):
            raise OSError(errno.EIO, 'Write did not make valid progress')
        pending = pending[written:]


def main():
    with open('/run/workload.jsonl', 'a', buffering=1) as log:
        seq = 0
        while True:
            seq += 1
            start = time.monotonic()
            event = {'seq': seq, 'start': start}
            try:
                with open('/root/workload.data', 'ab', buffering=0) as data:
                    write_all(data, (str(seq)+'\n').encode() + b'x'*4096)
                    os.fsync(data.fileno())
                # File creation also needs its containing directory persisted.
                # Acknowledge only after both persistence operations return.
                parent = os.open('/root', os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                event['ok'] = True
            except OSError as exc:
                event.update(ok=False, errno=exc.errno, error=str(exc))
            event['elapsed'] = time.monotonic()-start
            log.write(json.dumps(event)+'\n')
            time.sleep(0.1)


if __name__ == '__main__':
    main()
