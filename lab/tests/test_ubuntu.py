"""Regression for asynchronous systemd Git startup, before the PID is published."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('guest_ubuntu',BASE/'guest/ubuntu.py')
ubuntu=importlib.util.module_from_spec(spec)
spec.loader.exec_module(ubuntu)


class GitObservationTests(unittest.TestCase):
    def test_log_can_precede_pid_record(self):
        (BASE/'work').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=BASE/'work') as folder:
            runtime=Path(folder)
            self.assertNotIn('pid',ubuntu.git_status(runtime))
            (runtime/'git-clone.log').write_text("Cloning into '/root/linux'...\n")
            self.assertNotIn('pid',ubuntu.git_status(runtime))
            pid=os.getpid()
            record={'pid':pid,'start_ticks':Path(f'/proc/{pid}/stat').read_text().split()[21],
                    'exit_code':None}
            state=runtime/'git-clone-state.json'
            state.write_text(json.dumps(record))
            self.assertTrue(ubuntu.git_status(runtime)['same_process_alive'])
            record['start_ticks']='-1'
            state.write_text(json.dumps(record))
            self.assertFalse(ubuntu.git_status(runtime)['same_process_alive'])
            record['exit_code']=0
            state.write_text(json.dumps(record))
            self.assertNotIn('same_process_alive',ubuntu.git_status(runtime))


if __name__=='__main__':
    unittest.main()
