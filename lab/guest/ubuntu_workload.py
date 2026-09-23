#!/usr/bin/python3
"""A regular systemd service running from the real Ubuntu root disk."""
import os
from pathlib import Path
import runpy

Path('/run/workload.pid').write_text(str(os.getpid()))
runpy.run_path('/opt/lab/workload.py', run_name='__main__')
