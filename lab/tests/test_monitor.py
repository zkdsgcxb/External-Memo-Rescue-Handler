"""Scheduling invariants: event storms cannot accelerate recovery retries."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'guest'))
from dm_monitor import Schedule


class ScheduleTests(unittest.TestCase):
    def test_storm_cannot_bypass_backoff(self):
        s=Schedule(0)
        now=0
        for delay in [.1,.2,.4,.8,.8]:
            s.completed(now,True)
            self.assertAlmostEqual(s.next_check,now+delay)
            for fraction in [0,.1,.5,.99]:
                self.assertFalse(s.due(now+delay*fraction,True))
            self.assertTrue(s.due(s.next_check,True))
            now=s.next_check

    def test_healthy_event_coalescing_and_lost_event_fallback(self):
        s=Schedule(0);s.completed(0,False)
        self.assertFalse(s.due(.05,True))
        self.assertTrue(s.due(.1,True))
        self.assertFalse(s.due(.9,False))
        self.assertTrue(s.due(1,False))
        s.completed(1,True);s.completed(2,False);s.completed(3,True)
        self.assertAlmostEqual(s.next_check,3.1)


if __name__=='__main__':unittest.main()
