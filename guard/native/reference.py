#!/usr/bin/env python3
"""Replay the historical read-only Python observer, outside production Guard.

Its implementation and dependencies are kept together at a fixed Git commit;
current builds contain only the C++ recovery runtime.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'lab'))
from historical import run_legacy


if __name__ == '__main__':
    raise SystemExit(run_legacy(__file__))
