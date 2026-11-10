"""Persistent global Dirty pages must slow this job without aborting it."""
import errno
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import remote_ssh_export as helper
from segio_archive.ssh_client import SSHCollector,SSHConfig,PROTOCOL


class Clock:
    def __init__(self):self.now=0;self.after_sleep=lambda:None
    def time(self):return self.now
    def sleep(self,seconds):
        self.now+=seconds
        self.after_sleep()


class IOBudgetTests(unittest.TestCase):
    def memory(self,dirty):return {'MemTotal':8*1024**3,'MemFree':6*1024**3,'Dirty':dirty}

    def test_persistent_pressure_beyond_old_timeout_keeps_writing_exact_bytes_and_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock();dirty=[128*1024**2]
            with patch.object(helper,'time',clock),patch.object(helper,'memory_info',side_effect=lambda:self.memory(dirty[0])):
                budget=helper.WorkBudget(tmp);budget.io_tick=Mock()
                for _ in range(700):budget.document()
                self.assertGreater(clock.now,30)
                block=b'SYNTHETIC_BLOCK'*4096;expected=hashlib.sha256()
                output=Path(tmp)/'approval-pages.bin'
                sync_name='fdatasync' if hasattr(helper.os,'fdatasync') else 'fsync'
                sync=getattr(helper.os,sync_name)
                with output.open('wb') as stream,patch.object(helper.os,sync_name,wraps=sync) as recorded:
                    started=clock.now
                    for _ in range(100):budget.write(stream,block);expected.update(block)
                    self.assertGreaterEqual(clock.now-started,len(block)*100/budget.PRESSURE_RATE-.001)
                    self.assertTrue(budget.io_throttled)
                    dirty[0]=0;clock.sleep(.5);started=clock.now
                    budget.write(stream,block);expected.update(block);budget.finish(stream)
                    self.assertLess(clock.now-started,len(block)/budget.PRESSURE_RATE)
                    self.assertFalse(budget.io_throttled)
                    self.assertTrue(recorded.called)
                    self.assertTrue(all(args[0]==stream.fileno() for args,_ in recorded.call_args_list))
                self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),expected.hexdigest())
                self.assertEqual(budget.io_written,len(block)*101)
                self.assertFalse(budget.io_syncing)

    def test_cancel_during_throttle_stops_before_writing_and_sync_errors_are_not_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock=Clock()
            with patch.object(helper,'time',clock),patch.object(helper,'memory_info',return_value=self.memory(128*1024**2)):
                budget=helper.WorkBudget(tmp)
                clock.after_sleep=lambda:(Path(tmp)/'cancel').touch()
                with (Path(tmp)/'output').open('wb') as stream:
                    with self.assertRaisesRegex(helper.SafeError,'export_cancelled'):budget.write(stream,b'x'*1024**2)
                self.assertEqual((Path(tmp)/'output').stat().st_size,0)
                (Path(tmp)/'cancel').unlink();clock.after_sleep=lambda:None
                sync_name='fdatasync' if hasattr(helper.os,'fdatasync') else 'fsync'
                with (Path(tmp)/'output').open('wb') as stream,patch.object(helper.os,sync_name,side_effect=OSError(errno.EIO,'SYNTHETIC_IO')):
                    budget.write(stream,b'unchanged bytes')
                    with self.assertRaises(OSError) as caught:budget.finish(stream)
                    self.assertEqual(caught.exception.errno,errno.EIO)
                    self.assertFalse(budget.io_syncing)

    def test_wait_ready_displays_throttle_and_sync_without_reporting_collection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=SSHConfig('synthetic','root','employee',Path(tmp))
            events=[];collector=SSHCollector(config,progress=events.append);collector.remote='/synthetic'
            collector.report.interval=0
            collector.heartbeat=Mock()
            base={'protocol':PROTOCOL,'state':'running','stage':'database','records_done':4,'records_total':10,
                  'io_written_bytes':8*1024**2,'io_limit_bytes':1024**2}
            collector.read_json=Mock(side_effect=[dict(base,phase='io_throttle'),dict(base,phase='io_sync'),dict(base,state='ready')])
            with patch.object(config.cancel,'wait',return_value=False):collector.wait_ready()
            self.assertTrue(any('쓰기 부하 조절' in item['detail'] and item['completed']==4 for item in events))
            self.assertTrue(any('디스크에 반영' in item['detail'] for item in events))

