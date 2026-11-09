import json
import queue
import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch,call

from segio_archive.local_processing import failure_details,processing_failure
from segio_archive.power import keep_awake
from segio_archive.web import Cancelled


class LocalProcessingTests(unittest.TestCase):
    def test_pre_mail_database_error_has_safe_details_without_message_or_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            try:sqlite3.connect(':memory:').execute('SELECT * FROM PRIVATE_MAIL_SUBJECT')
            except sqlite3.Error as error:
                message=processing_failure(error,'mail',root,{'stage':'mail','detail':'PRIVATE_SENDER','completed':None})
            data=json.loads((root/'processing-failure.json').read_text())
            self.assertEqual(data['stage'],'mail');self.assertEqual(data['exception_type'],'OperationalError')
            self.assertEqual(data['sqlite_errorcode'],sqlite3.SQLITE_ERROR)
            self.assertNotIn('PRIVATE_',json.dumps(data));self.assertNotIn('PRIVATE_',message)
            self.assertIn('LOCAL_DATABASE_ERROR',message);self.assertIn(str(root/'processing-failure.json'),message)
            self.assertNotIn('completed',data)

    def test_diagnostic_write_failure_keeps_original_reason_and_safe_popup(self):
        error=MemoryError('PRIVATE_RAW_CONTENT')
        with patch('segio_archive.local_processing.atomic_write',side_effect=OSError(28,'PRIVATE_PATH')):
            message=processing_failure(error,'mail',Path('/synthetic'))
        self.assertIn('LOCAL_MEMORY_LIMIT',message);self.assertIn('저장하지 못했습니다',message)
        self.assertNotIn('PRIVATE_',message)
        self.assertEqual(failure_details(error,'mail',{'stage':'mail','completed':7,'total':30,'unit':'messages'})['completed'],7)

    def test_windows_power_guard_resets_on_error_and_allows_screen_lock(self):
        import ctypes
        setter=Mock(return_value=1)
        with patch('segio_archive.power.os',SimpleNamespace(name='nt')),patch.object(ctypes,'windll',SimpleNamespace(kernel32=SimpleNamespace(SetThreadExecutionState=setter)),create=True):
            with self.assertRaises(Cancelled):
                with keep_awake():raise Cancelled('Synthetic cancellation')
        self.assertEqual(setter.call_args_list,[call(0x80000001),call(0x80000000)])
        setter.reset_mock();setter.return_value=0
        with patch('segio_archive.power.os',SimpleNamespace(name='nt')),patch.object(ctypes,'windll',SimpleNamespace(kernel32=SimpleNamespace(SetThreadExecutionState=setter)),create=True):
            with keep_awake():pass
        setter.assert_called_once_with(0x80000001)

    def test_local_repair_worker_records_failure_before_first_message(self):
        from segio_archive.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fake=SimpleNamespace(events=queue.Queue(),cancel_event=SimpleNamespace(is_set=lambda:False))
            def broken(*args,**kwargs):
                kwargs['progress']({'stage':'mail','completed':None,'total':None,'unit':'','detail':'PRIVATE_SUBJECT'})
                raise sqlite3.OperationalError('PRIVATE_DATABASE_PATH')
            with patch('segio_archive.backup_update.repair_local',side_effect=broken):App.archive_worker(fake,root)
            events=[]
            while not fake.events.empty():events.append(fake.events.get())
            self.assertTrue(any(kind=='archive_error' and 'LOCAL_DATABASE_ERROR' in message for kind,message in events))
            self.assertEqual(json.loads((root/'processing-failure.json').read_text())['stage'],'mail')
            self.assertNotIn('PRIVATE_',str([(kind,message) for kind,message in events if kind!='progress']))
            self.assertNotIn('PRIVATE_',(root/'processing-failure.json').read_text())
