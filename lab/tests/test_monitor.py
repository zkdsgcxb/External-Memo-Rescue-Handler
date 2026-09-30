"""Scheduling invariants: event storms cannot accelerate recovery retries."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'guest'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'guard/runtime'))
from dm_monitor import Events, Schedule


class EventScopeTests(unittest.TestCase):
    def setUp(self):
        self.events=Events.__new__(Events)
        self.disk='/sys/devices/pci/usb/target/block/sdb'
        self.dm='/sys/devices/virtual/block/dm-0'
        self.events.watch((self.disk,self.dm))

    def packet(self,path,subsystem='block'):
        return f'change@{path}\0DEVPATH={path}\0SUBSYSTEM={subsystem}\0'.encode()

    def test_disk_partition_and_stable_dm_events_are_relevant(self):
        for path in (self.disk,self.disk+'/sdb1',self.dm):
            self.assertTrue(self.events.relevant(self.packet(path.removeprefix('/sys'))))

    def test_unrelated_disks_and_prefix_collisions_do_not_scan_maps(self):
        for path in (self.disk+'1',self.dm+'1','/sys/devices/virtual/block/loop0'):
            self.assertFalse(self.events.relevant(self.packet(path.removeprefix('/sys'))))
        self.assertFalse(self.events.relevant(self.packet(self.disk.removeprefix('/sys'),'usb')))

    def test_recovery_accepts_new_port_and_malformed_block_hint_reconciles(self):
        packet=self.packet('/devices/different-port/block/sdc/sdc1')
        self.assertFalse(self.events.relevant(packet))
        self.events.watch()
        self.assertTrue(self.events.relevant(packet))
        self.events.watch((self.disk,self.dm))
        self.assertTrue(self.events.relevant(b'SUBSYSTEM=block\0'))


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
