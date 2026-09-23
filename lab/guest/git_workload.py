#!/usr/bin/python3
"""Run real Git in Ubuntu; keep progress and process evidence in RAM."""
import json
import os
from pathlib import Path
import subprocess
import time

STATE = Path('/run/git-clone-state.json')


def write(state):
    temp=STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state))
    temp.replace(STATE)


with open('/run/git-clone.log','w') as log:
    child=subprocess.Popen(['/usr/bin/git','clone','--progress','--depth','1','--branch','v6.8',
        'git://127.0.0.1/linux.git','/root/linux'],stdout=log,stderr=subprocess.STDOUT,
        env={**os.environ,'GIT_PROGRESS_DELAY':'0'})
    state={'pid':child.pid,'start_ticks':Path(f'/proc/{child.pid}/stat').read_text().split()[21],
           'started':time.monotonic(),'exit_code':None}
    write(state)
    state.update(exit_code=child.wait(),finished=time.monotonic())
    write(state)
    raise SystemExit(state['exit_code'])
