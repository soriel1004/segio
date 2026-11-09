from contextlib import closing
from email.message import EmailMessage
import hashlib
import http.client
import json
import os
import shutil
from pathlib import Path
import sqlite3
import tempfile
import subprocess
import sys
import io
import errno
import types
from contextlib import redirect_stdout
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import build_compact_catalog,mbox_spans,search,mail_record
from segio_archive.ssh_client import SSHConfig,SSHCollector,WORK_SCRIPT,WORK_PROTOCOL,ssh_failure_message
from segio_archive.viewer import Viewer
from segio_archive.web import Cancelled,CollectionError
from live_fixtures import synthetic_export
from test_live_archive import create_fixture
import remote_ssh_export as helper
from ssh_fixture import SSHServer


class SSHArchiveTests(unittest.TestCase):
    def test_corrupt_resumed_prefix_repairs_only_that_file_and_keeps_verified_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            payload=b'From a@example.test\nSubject: Resumed prefix\n\n'+bytes(range(256))*(8192+1)
            (root/'home/e/employee/mbox/large.mbox').write_bytes(payload)
            collector=SSHCollector(config,confirm_host=lambda *_:True);transfer=collector.transfer
            target_path=[];verified_stats={};resumed=[];call=collector.io
            def seed(folder):
                entries=collector.read_json(collector.remote+'/transfer.json')['files']
                for entry in entries:
                    target=folder/entry['path'];target.parent.mkdir(parents=True,exist_ok=True)
                    source=server.path(os.fsdecode(bytes.fromhex(entry['source_hex'])))
                    offset=entry.get('source_offset',0)
                    if entry['bytes']==len(payload):
                        target.write_bytes(b'CORRUPT_PREFIX'+payload[14:123457]);target_path.append(target)
                    else:
                        target.write_bytes(source.read_bytes()[offset:offset+entry['bytes']])
                        if entry['path'].startswith(('documents/','mail/raw/')):
                            verified_stats[target]=(target.stat().st_mtime_ns,target.read_bytes())
                return transfer(folder)
            def observed(operation,callback,*args,**kwargs):
                if operation=='remote_read' and getattr(callback,'__name__','')=='seek' and collector.transfer_context.get('file_bytes')==len(payload):
                    resumed.append(args[0])
                return call(operation,callback,*args,**kwargs)
            collector.transfer=seed;collector.io=observed
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                result=collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(resumed,[123457,0]);self.assertTrue(collector.sequential_reads)
            self.assertEqual(server.actions.count('prepare'),1)
            self.assertEqual(target_path[0].read_bytes(),payload)
            for target,(modified,data) in verified_stats.items():
                # Publishing renames the same staging folder; files are not rewritten.
                self.assertEqual(target.stat().st_mtime_ns,modified);self.assertEqual(target.read_bytes(),data)
            self.assertTrue(verify_archive(result)['ok'])

    def test_hash_mismatch_repairs_transient_write_but_stops_persistent_corruption(self):
        for persistent in (False,True):
            with self.subTest(persistent=persistent),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();server,config,_=self.fixture(root)
                payload=b'From a@example.test\nSubject: Integrity\n\n'+b'x'*400000
                (root/'home/e/employee/mbox/large.mbox').write_bytes(payload)
                collector=SSHCollector(config,confirm_host=lambda *_:True);call=collector.io;corruptions=[];disconnected=[False]
                def observed(operation,callback,*args,**kwargs):
                    if persistent and operation=='remote_verify' and collector.transfer_context.get('hash_mismatches')==1 and not disconnected[0]:
                        disconnected[0]=True;collector.client.get_transport().close()
                        def dropped():raise EOFError('PRIVATE_RAW_ERROR')
                        return call(operation,dropped)
                    if operation=='local_write' and collector.transfer_context.get('file_bytes')==len(payload) and collector.transfer_context.get('file_completed_bytes')==0 and (persistent or not corruptions):
                        block=args[0];args=(bytes([block[0]^1])+block[1:],)+args[1:];corruptions.append(True)
                    return call(operation,callback,*args,**kwargs)
                collector.io=observed
                with patch.object(helper,'run_export',side_effect=synthetic_export):
                    if persistent:
                        with self.assertRaisesRegex(CollectionError,'SSH_FILE_HASH_MISMATCH') as caught:
                            collector.run('SYNTHETIC_SSH_PASSWORD')
                        self.assertEqual(len(corruptions),3)
                        self.assertTrue(disconnected[0])
                        diagnostic=json.loads(collector.transfer_failure_path.read_text())
                        self.assertEqual(diagnostic['hash_mismatches'],3)
                        self.assertEqual(diagnostic['code'],'SSH_FILE_HASH_MISMATCH')
                        self.assertNotIn('SYNTHETIC_SSH_PASSWORD',json.dumps(diagnostic))
                        self.assertNotIn('large.mbox',json.dumps(diagnostic));self.assertNotIn('large.mbox',str(caught.exception))
                        self.assertNotIn('cleanup',server.actions)
                    else:
                        result=collector.run('SYNTHETIC_SSH_PASSWORD')
                        self.assertEqual(len(corruptions),1)
                        self.assertIn(payload,[p.read_bytes() for p in (result/'mail/raw').iterdir()])
                        self.assertTrue(verify_archive(result)['ok'])
                self.assertEqual(server.actions.count('prepare'),1)

    def test_hash_repair_rejects_changed_source_before_resetting_cached_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            source=root/'home/e/employee/mbox/large.mbox'
            payload=b'From a@example.test\nSubject: Changed source\n\n'+b'x'*400000;source.write_bytes(payload)
            collector=SSHCollector(config,confirm_host=lambda *_:True);call=collector.io;changed=[False]
            def observed(operation,callback,*args,**kwargs):
                if collector.transfer_context.get('file_bytes')==len(payload):
                    if operation=='local_write' and not changed[0]:
                        block=args[0];args=(bytes([block[0]^1])+block[1:],)+args[1:]
                    if operation=='local_hash' and not changed[0]:
                        changed[0]=True;source.write_bytes(payload[:-1]+b'z')
                        stamp=source.stat().st_mtime+5;os.utime(source,(stamp,stamp))
                return call(operation,callback,*args,**kwargs)
            collector.io=observed
            with patch.object(helper,'run_export',side_effect=synthetic_export),self.assertRaisesRegex(CollectionError,'서버 메일이 변경'):
                collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(changed[0]);self.assertEqual(server.actions.count('prepare'),1)
            self.assertNotIn('cleanup',server.actions)
            self.assertTrue(any(p.stat().st_size==len(payload) for p in (config.output/'SSH-employee/mail/raw').iterdir()))

    def test_disconnect_at_99_percent_reconnects_and_resumes_same_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            payload=b'From synthetic@example.test\nSubject: Near complete\n\n'+b'x'*(4*1024*1024)
            (root/'home/e/employee/mbox/large.mbox').write_bytes(payload)
            logs=[];collector=SSHCollector(config,log=logs.append,confirm_host=lambda *_:True)
            call=collector.io;failed=[False];positions=[]
            def observed(operation,callback,*args,**kwargs):
                context=collector.transfer_context
                if operation=='remote_read' and context.get('file_bytes')==len(payload):
                    if not failed[0] and context.get('file_completed_bytes',0)>=len(payload)*.99:
                        failed[0]=True;collector.client.get_transport().close()
                        def broken():raise ConnectionResetError(errno.ECONNRESET,'PRIVATE_REMOTE_PATH')
                        return call(operation,broken)
                    if failed[0] and getattr(callback,'__name__','')=='seek':positions.append(args[0])
                return call(operation,callback,*args,**kwargs)
            collector.io=observed
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                result=collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(failed[0]);self.assertTrue(positions)
            self.assertGreaterEqual(min(positions),len(payload)*.99)
            self.assertEqual(server.actions.count('prepare'),1)
            self.assertTrue(collector.sequential_reads);self.assertIsNone(collector.reconnect_transfer)
            self.assertTrue(verify_archive(result)['ok'])
            self.assertIn(payload,[p.read_bytes() for p in (result/'mail/raw').iterdir()])
            self.assertNotIn('SYNTHETIC_SSH_PASSWORD','\n'.join(logs));self.assertNotIn('PRIVATE_REMOTE_PATH','\n'.join(logs))

    def test_short_read_recovers_only_after_source_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            payload=b'From a@example.test\nSubject: Short read\n\n'+b'x'*(2*1024*1024)
            (root/'home/e/employee/mbox/large.mbox').write_bytes(payload)
            collector=SSHCollector(config,confirm_host=lambda *_:True);call=collector.io;failed=[False]
            def observed(operation,callback,*args,**kwargs):
                if operation=='remote_read' and getattr(callback,'__name__','')=='next' and collector.transfer_context.get('file_completed_bytes',0)>32768 and not failed[0]:
                    failed[0]=True
                    return b''
                return call(operation,callback,*args,**kwargs)
            collector.io=observed
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                result=collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(failed[0]);self.assertTrue(collector.sequential_reads)
            self.assertEqual(server.actions.count('prepare'),1)
            self.assertGreaterEqual(server.actions.count('verify'),2)
            self.assertIn(payload,[p.read_bytes() for p in (result/'mail/raw').iterdir()])
            self.assertTrue(verify_archive(result)['ok'])

    def test_local_disk_error_is_not_retried_and_safe_diagnostic_keeps_partial_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            payload=b'From synthetic@example.test\nSubject: Disk fixture\n\n'+b'x'*(2*1024*1024)
            (root/'home/e/employee/mbox/large.mbox').write_bytes(payload)
            collector=SSHCollector(config,confirm_host=lambda *_:True);call=collector.io
            def observed(operation,callback,*args,**kwargs):
                if operation=='local_write' and collector.transfer_context.get('file_bytes')==len(payload) and collector.transfer_context.get('file_completed_bytes',0)>0:
                    def full():raise OSError(errno.ENOSPC,'PRIVATE_LOCAL_PATH_AND_CONTENT')
                    return call(operation,full)
                return call(operation,callback,*args,**kwargs)
            collector.io=observed
            with patch.object(helper,'run_export',side_effect=synthetic_export),self.assertRaisesRegex(CollectionError,'WINDOWS_DISK_FULL'):
                collector.run('SYNTHETIC_SSH_PASSWORD')
            diagnostic=json.loads(collector.transfer_failure_path.read_text())
            self.assertEqual(diagnostic['operation'],'local_write');self.assertEqual(diagnostic['errno'],errno.ENOSPC)
            self.assertEqual(diagnostic['retry'],0);self.assertIsNone(collector.reconnect_transfer)
            self.assertTrue(any(0<p.stat().st_size<len(payload) for p in (config.output/'SSH-employee/mail/raw').iterdir()))
            self.assertNotIn('PRIVATE_LOCAL_PATH_AND_CONTENT',json.dumps(diagnostic))
            self.assertNotIn('SYNTHETIC_SSH_PASSWORD',json.dumps(diagnostic))

    def test_reconnect_still_rejects_changed_server_host_key(self):
        import paramiko
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            (root/'home/e/employee/mbox/large.mbox').write_bytes(b'From a@example.test\nSubject: Key\n\n'+b'x'*(2*1024*1024))
            confirmations=[];collector=SSHCollector(config,confirm_host=lambda *args:confirmations.append(args) or True)
            call=collector.io;failed=[False]
            def observed(operation,callback,*args,**kwargs):
                if operation=='remote_read' and collector.transfer_context.get('file_completed_bytes',0)>32768 and not failed[0]:
                    failed[0]=True;server.key=paramiko.RSAKey.generate(2048);collector.client.get_transport().close()
                    def broken():raise EOFError('PRIVATE_RAW_ERROR')
                    return call(operation,broken)
                return call(operation,callback,*args,**kwargs)
            collector.io=observed
            with patch.object(helper,'run_export',side_effect=synthetic_export),self.assertRaisesRegex(CollectionError,'SSH_HOST_KEY_CHANGED'):
                collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(len(confirmations),1);self.assertEqual(server.actions.count('prepare'),1)

    def test_network_retries_are_bounded_and_windows_security_errors_stay_distinct(self):
        from segio_archive.ssh_transfer import TransferFault,fault_details
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=SSHConfig('synthetic','root','employee',root)
            collector=SSHCollector(config);collector.remote='/backup/.segio-ssh-'+'a'*32
            collector.transfer_failure_path=root/'transfer-failure.json'
            collector.reconnect_transfer=Mock();collector.read_json=Mock(return_value={'protocol':helper.PROTOCOL,'state':'ready'})
            collector.execute=Mock()
            collector.transfer_once=Mock(side_effect=TransferFault('remote_read',TimeoutError(errno.ETIMEDOUT,'PRIVATE_ERROR')))
            with patch.object(config.cancel,'wait',return_value=False),self.assertRaisesRegex(CollectionError,'SSH_TRANSFER_INTERRUPTED'):
                collector.transfer(root)
            self.assertEqual(collector.transfer_once.call_count,4);self.assertEqual(collector.reconnect_transfer.call_count,3)
            self.assertEqual(json.loads(collector.transfer_failure_path.read_text())['retry'],3)
            for number,code in ((225,'WINDOWS_SECURITY_BLOCKED'),(226,'WINDOWS_SECURITY_BLOCKED'),(32,'WINDOWS_FILE_LOCKED')):
                error=OSError(errno.EACCES,'PRIVATE_ERROR');error.winerror=number
                actual,_,retry,details=fault_details(TransferFault('local_write',error))
                self.assertEqual(actual,code);self.assertFalse(retry);self.assertEqual(details['winerror'],number)

    def test_work_budget_stops_disconnected_cancelled_and_memory_starved_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);budget=helper.WorkBudget(str(root))
            with patch.object(helper,'memory_info',return_value={'MemTotal':8*1024**3,'MemFree':4*1024**3}):
                (root/'lease').write_bytes(b'active');os.utime(root/'lease',(1,1))
                with self.assertRaisesRegex(helper.SafeError,'export_client_disconnected'):budget.check()
                (root/'lease').unlink();(root/'cancel').touch()
                with self.assertRaisesRegex(helper.SafeError,'export_cancelled'):budget.check()
                (root/'cancel').unlink()
            with patch.object(helper,'memory_info',return_value={'MemTotal':8*1024**3,'MemFree':1}):
                with self.assertRaisesRegex(helper.SafeError,'server_memory_pressure'):budget.check()
            budget.started=0
            with self.assertRaisesRegex(helper.SafeError,'db_query_budget'):budget.check()
            for code in ('export_client_disconnected','export_cancelled','server_memory_pressure','db_query_budget'):
                actual,detail=helper.failure_details(helper.SafeError(code),'database')
                self.assertEqual(actual,code);self.assertIn('['+code+']',ssh_failure_message(actual,detail))

    @unittest.skipUnless(os.name=='posix','POSIX process limits and lock')
    def test_server_work_is_serialized_and_limits_stay_in_child_process(self):
        import resource
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);first=helper.WorkBudget(str(root/'one'));second=helper.WorkBudget(str(root/'two'))
            first.enter(False)
            try:
                with self.assertRaisesRegex(helper.SafeError,'export_busy'):second.enter(False)
            finally:first.close();second.close()
            second.enter(False);second.close()
            before=resource.getrlimit(resource.RLIMIT_AS)
            code="import sys,os,resource,json;sys.path.insert(0,sys.argv[1]);import remote_ssh_export as h;b=h.WorkBudget(sys.argv[2]);b.enter();print(json.dumps({'cap':resource.getrlimit(resource.RLIMIT_AS)[0],'priority':os.getpriority(os.PRIO_PROCESS,0)}));b.close()"
            result=json.loads(subprocess.check_output([sys.executable,'-c',code,str(Path(helper.__file__).parent),str(root/'child')]))
            self.assertLessEqual(result['cap'],512*1024**2);self.assertGreaterEqual(result['priority'],10)
            self.assertEqual(resource.getrlimit(resource.RLIMIT_AS),before)

    def fixture(self,root):
        settings,original=create_fixture(root)
        server=SSHServer(root)
        self.addCleanup(server.close)
        config=SSHConfig('127.0.0.1','root','employee',root/'windows',port=server.port,
            db_config='/files/database.json',cell='/files',
            mail_template='/files/home/{initial}/{user}/mbox',known_hosts=root/'known_hosts')
        return server,config,original

    def test_connection_check_needs_no_employee_id_and_never_prepares_or_uploads_a_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            executable=root/'psql'
            executable.write_bytes(b'fixture executable access only')
            executable.chmod(0o700)
            config.psql='/files/psql'
            config.username=''
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            result=collector.check_connection('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(result['ready'])
            self.assertIsNone(result['checks']['mail_readable'])
            self.assertTrue(result['checks']['database_config_readable'])
            config.username='employee'
            self.assertTrue(collector.check_connection('SYNTHETIC_SSH_PASSWORD')['checks']['mail_readable'])
            config.psql='/files/missing-psql'
            failed=collector.check_connection('SYNTHETIC_SSH_PASSWORD')
            self.assertFalse(failed['ready'])
            self.assertFalse(failed['checks']['psql_executable'])
            self.assertEqual(server.probes,3)

            self.assertEqual(server.actions,[])
            self.assertEqual(list((root/'jobs').iterdir()),[])
            self.assertIsNone(collector.client)
            self.assertIsNone(collector.sftp)
            self.assertNotIn('SYNTHETIC_SSH_PASSWORD',json.dumps(result))
            with self.assertRaisesRegex(CollectionError,'SSH_AUTH_FAILED'):
                collector.check_connection('WRONG_PRIVATE_PASSWORD')
            original_connect=collector.connect
            def connected_then_cancelled(password):
                original_connect(password)
                config.cancel.set()
            with patch.object(collector,'connect',side_effect=connected_then_cancelled):
                with self.assertRaises(Cancelled):
                    collector.check_connection('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(server.probes,3)
            self.assertEqual(server.actions,[])

    @unittest.skipUnless(hasattr(os,'statvfs'),'POSIX workspace probe')
    def test_workspace_probe_creates_private_parent_and_reports_low_space_without_private_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'backup space'
            result=json.loads(subprocess.check_output([sys.executable,'-c',WORK_SCRIPT,str(path)]))
            self.assertEqual(result['protocol'],WORK_PROTOCOL);self.assertTrue(result['ok'])
            self.assertEqual(result['base'],str(path.resolve()));self.assertEqual(path.stat().st_mode&0o777,0o700)
            output=io.StringIO()
            with patch.object(sys,'argv',['probe',str(path)]),patch.object(os,'statvfs',return_value=types.SimpleNamespace(f_bavail=0,f_frsize=4096,f_bsize=4096)),redirect_stdout(output):
                with self.assertRaises(SystemExit) as caught:exec(compile(WORK_SCRIPT,'workspace','exec'),{})
            self.assertEqual(caught.exception.code,1)
            failure=json.loads(output.getvalue());self.assertEqual(failure['error'],'server_disk_full')
            self.assertNotIn(str(path),output.getvalue())

    def test_ssh_db_and_whole_mbox_export_is_searchable_without_web_or_mail_copy_on_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,original=self.fixture(root)
            fingerprints=[]
            progress=[]
            with patch.object(helper,'run_export',side_effect=synthetic_export), \
                 patch('segio_archive.browser.launch_browser',side_effect=AssertionError('SSH must not launch a browser')):
                result=SSHCollector(config,confirm_host=lambda *args:fingerprints.append(args) or True,progress=progress.append).run('SYNTHETIC_SSH_PASSWORD')
            stages=list(dict.fromkeys(event['stage'] for event in progress))
            self.assertEqual(stages,['connect','prepare','server','transfer','verify','catalog','mail','index','publish','cleanup'])
            transferred=[event for event in progress if event['stage']=='transfer' and event['total'] is not None]
            self.assertEqual(transferred[0]['completed'],0)
            self.assertEqual(transferred[-1]['completed'],transferred[-1]['total'])
            self.assertGreater(transferred[-1]['total'],len(original))
            mail=[event for event in progress if event['stage']=='mail']
            self.assertEqual(mail[-1]['completed'],3)
            self.assertEqual(mail[-1]['total'],3)
            index=[event for event in progress if event['stage']=='index']
            self.assertEqual(index[-1]['completed'],4)
            self.assertEqual(index[-1]['total'],4)
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(),original)
            self.assertEqual(search(result,'회의','mail')['total'],1)
            self.assertEqual(search(result,'현재 DB','approval')['total'],1)
            self.assertFalse((config.output/'segio-employee.zip').exists())
            self.assertTrue(config.known_hosts.is_file())
            self.assertEqual(len(fingerprints),1)
            self.assertTrue(fingerprints[0][2].startswith('SHA256:'))
            self.assertEqual(server.actions,['prepare','verify','cleanup'])
            self.assertEqual(list((root/'jobs').glob('.segio-ssh-*')),[])
            self.assertFalse((result/'mail/messages').exists())
            for path in result.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'SYNTHETIC_SSH_PASSWORD',path.read_bytes())
            viewer=Viewer(result)
            self.addCleanup(viewer.close)
            connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
            def get(path):
                connection.request('GET','/'+viewer.token+'/'+path)
                response=connection.getresponse()
                return response.status,response.getheader('Content-Disposition'),response.read()
            try:
                status,_,payload=get('api/search?q='+quote('첨부 확인')+'&kind=mail')
                self.assertEqual(status,200)
                item=json.loads(payload)['items'][0]
                status,_,page=get(item['html'])
                self.assertEqual(status,200)
                self.assertIn('원본 첨부 보존 시험',page.decode())
                self.assertTrue(item['html'].endswith('/index.html'))
                from bs4 import BeautifulSoup
                from urllib.parse import urljoin
                attachment_url=BeautifulSoup(page,'html.parser').select_one('a.attachment')['href']
                status,disposition,attachment=get(urljoin(item['html'],attachment_url))
                self.assertEqual(status,200)
                self.assertEqual(attachment,b'SYNTHETIC_ATTACHMENT')
                self.assertTrue(disposition.startswith('attachment;'))
                status,_,eml=get('mail/'+item['id']+'/original.eml')
                self.assertEqual(status,200)
                self.assertIn(b'Content-Type: multipart/mixed',eml)
            finally:
                connection.close()
            executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')
            if executable:
                from playwright.sync_api import sync_playwright,expect
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=executable)
                    try:
                        page=browser.new_page()
                        requests=[]
                        page.on('request',lambda request:requests.append(request.url))
                        page.goto(viewer.url)
                        expect(page.locator('#tab-all')).to_have_attribute('aria-selected','true')
                        expect(page.locator('.data-row')).to_have_count(4)
                        page.locator('#tab-approval').click()
                        expect(page.locator('#tab-approval')).to_have_attribute('aria-selected','true')
                        expect(page.locator('.data-row')).to_have_count(1)
                        self.assertIn('현재 DB',page.locator('.data-row').inner_text())
                        self.assertEqual(page.locator('body').evaluate('element=>getComputedStyle(element).fontSize'),'12px')
                        page.locator('#tab-mail').click()
                        expect(page.locator('.data-row')).to_have_count(3)
                        page.locator('#q').fill('회의')
                        page.locator('#search-button').click()
                        expect(page.locator('.data-row')).to_have_count(1)
                        self.assertIn('회의',page.locator('.data-row').inner_text())
                        self.assertTrue(all(url.startswith(viewer.url.rsplit('/',1)[0]) for url in requests))
                        page.locator('#q').fill('없는 검색어')
                        page.locator('#q').press('Enter')
                        expect(page.locator('.data-row')).to_have_count(0)
                        expect(page.locator('#empty')).to_have_text('검색 결과가 없습니다.')
                        page.locator('#reset').click()
                        expect(page.locator('.data-row')).to_have_count(3)
                    finally:
                        browser.close()

    def test_cancelled_sftp_transfer_resumes_same_snapshot_at_nonzero_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,original=self.fixture(root)
            source=root/'home/e/employee/mbox/large.mbox'
            payload=b'From test@example.test Tue Oct 20 10:00:00 2026\nSubject: Large fixture\n\n'+b'x'*(3*1024*1024)
            source.write_bytes(payload)
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            checkpoint=collector.checkpoint
            def stop_when_partial():
                if config.output.exists():
                    for partial in (config.output/'SSH-employee/mail/raw').glob('*'):
                        if 1024*1024<=partial.stat().st_size<len(payload):
                            config.cancel.set()
                checkpoint()
            collector.checkpoint=stop_when_partial
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                with self.assertRaises(Cancelled):
                    collector.run('SYNTHETIC_SSH_PASSWORD')
                self.assertEqual(server.actions.count('prepare'),1)
                self.assertTrue(list(config.output.glob('.segio-ssh-cache/*/session.json')))
                config.cancel=threading.Event()
                reads=len(server.reads)
                result=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual(server.actions.count('prepare'),1)
            resumed_offsets=[offset for path,offset,_ in server.reads[reads:] if path.endswith('large.mbox')]
            self.assertEqual(min(resumed_offsets),1024*1024)
            self.assertIn(payload,[p.read_bytes() for p in (result/'mail/raw').iterdir()])

    def test_unknown_host_and_wrong_password_do_not_start_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            with self.assertRaisesRegex(CollectionError,'SSH_HOST_KEY_UNCONFIRMED'):
                SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            with self.assertRaisesRegex(CollectionError,'SSH_AUTH_FAILED') as caught:
                SSHCollector(config,confirm_host=lambda *_:True).run('PRIVATE_WRONG_PASSWORD')
            self.assertNotIn('PRIVATE_WRONG_PASSWORD',str(caught.exception))
            self.assertEqual(server.actions,[])

    def test_large_approval_attachment_is_referenced_not_copied_into_server_job(self):
        import export_approval_db as exporter
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,original=self.fixture(root)
            upload=root/'data/approval/upload';upload.mkdir(parents=True)
            source=upload/'large.bin';payload=b'SYNTHETIC_APPROVAL_ATTACHMENT'*120000;source.write_bytes(payload)
            model=json.loads((Path(exporter.HERE)/'approval_db_columns.json').read_text())
            rows={name:[] for name in model}
            rows['approval_document']=[{'no':'1','uid':'employee','title':'Direct attachment','context':'<p>direct</p>','wdate':'1700000000'}]
            rows['approval_member_info']=[{'uid':'employee','rname':'Synthetic'}]
            rows['approval_attach_info']=[{'no':'1','doc_no':'1','fname':'large.bin','fpath':'large.bin'}]
            counts={name:len(values) for name,values in rows.items()}
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            transfer=collector.transfer
            def inspect_then_transfer(folder):
                job=next((root/'jobs').glob('.segio-ssh-*'))
                self.assertLess(sum(path.stat().st_size for path in job.rglob('*') if path.is_file()),len(payload))
                self.assertFalse(list(job.rglob('*.zip')))
                self.assertFalse(list(job.rglob('attachments/*.bin')))
                self.assertTrue((job/'approval-pages.bin').is_file())
                self.assertFalse(list(job.rglob('document.html')))
                self.assertFalse(list(job.rglob('database.json')))
                entries=json.loads((job/'transfer.json').read_text())['files']
                pages=[item for item in entries if item['path'].startswith(exporter.document_directory('1')+'/') and '/attachments/' not in item['path']]
                self.assertEqual(len(pages),2)
                self.assertEqual([item['source_offset'] for item in pages],sorted(item['source_offset'] for item in pages))
                data=(job/'approval-pages.bin').read_bytes()
                for item in pages:
                    self.assertEqual(hashlib.sha256(data[item['source_offset']:item['source_offset']+item['bytes']]).hexdigest(),item['sha256'])
                transfer(folder)
            collector.transfer=inspect_then_transfer
            with patch.object(exporter,'query_database',return_value=(rows,counts)):
                result=collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(server.workspace_requests,['/backup'])
            self.assertEqual((result/(exporter.document_directory('1')+'/attachments/1.bin')).read_bytes(),payload)
            self.assertEqual(source.read_bytes(),payload)
            self.assertEqual((result/'mail/raw/000000.mbox').read_bytes(),original)
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual(json.loads((result/'manifest.json').read_text())['server_cleanup']['state'],'confirmed')
            self.assertEqual(list((root/'jobs').iterdir()),[])

    def test_cleanup_failure_keeps_receipt_and_retries_partial_job_on_next_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,original=self.fixture(root)
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            execute=collector.execute
            def command(action,**kwargs):
                if action=='cleanup':raise CollectionError('Synthetic lost cleanup helper')
                return execute(action,**kwargs)
            collector.execute=command
            def partial(remote,**kwargs):
                collector.sftp.remove(remote+'/remote_ssh_export.py')
                raise OSError(13,'Synthetic cleanup denial')
            collector.remove_job=partial
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                first=collector.run('SYNTHETIC_SSH_PASSWORD')
                old=collector.remote
                self.assertEqual(json.loads((first/'manifest.json').read_text())['server_cleanup']['state'],'pending')
                self.assertEqual(len(list((config.output/'.segio-ssh-cache').glob('cleanup-*.json'))),1)
                if os.name!='nt':(server.path(old)/'source-link').symlink_to(root/'home/e/employee/mbox/inbox.mbox')
                config.username='other'  # A later backup of another employee still reclaims this completed job.
                next_collector=SSHCollector(config)
                initialize=next_collector.initialize
                def check_old_gone(cache):
                    self.assertFalse(server.path(old).exists())
                    initialize(cache)
                next_collector.initialize=check_old_gone
                second=next_collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(verify_archive(first)['ok']);self.assertTrue(verify_archive(second)['ok'])
            self.assertEqual(json.loads((first/'manifest.json').read_text())['server_cleanup']['state'],'confirmed')
            self.assertEqual(list((config.output/'.segio-ssh-cache').glob('cleanup-*.json')),[])
            self.assertEqual(list((root/'jobs').iterdir()),[])
            self.assertEqual((root/'home/e/employee/mbox/inbox.mbox').read_bytes(),original)

    def test_failed_legacy_tmp_job_restarts_in_backup_and_only_failed_job_is_retired(self):
        from export_schema_from_segio_config import SafeError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,original=self.fixture(root)
            config.work_base='/tmp'
            with patch.object(helper,'run_export',side_effect=SafeError('space','server_disk_full')):
                with self.assertRaisesRegex(CollectionError,'server_disk_full'):
                    SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
            old=next((root/'jobs').glob('.segio-ssh-*'))
            unrelated=root/'jobs'/('.segio-ssh-'+'f'*32);unrelated.mkdir(mode=0o700)
            (unrelated/'keep.txt').write_text('OTHER_JOB_MUST_SURVIVE')
            config.work_base='/backup'
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                result=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertFalse(old.exists());self.assertTrue((unrelated/'keep.txt').exists())
            self.assertTrue(verify_archive(result)['ok'])
            self.assertEqual(server.workspace_requests,['/tmp','/backup'])
            self.assertEqual((root/'home/e/employee/mbox/inbox.mbox').read_bytes(),original)

    def test_ready_job_refreshes_verifier_and_reuses_already_downloaded_files(self):
        from export_schema_from_segio_config import SafeError
        from segio_archive import __version__
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config,_=self.fixture(root)
            first=SSHCollector(config,confirm_host=lambda *_:True)
            with patch.object(helper,'run_export',side_effect=synthetic_export),patch.object(helper,'verify_sources',side_effect=SafeError('invalid_metadata')):
                with self.assertRaisesRegex(CollectionError,'invalid_metadata'):
                    first.run('SYNTHETIC_SSH_PASSWORD')
            job=server.path(first.remote)
            self.assertEqual(json.loads((job/'status.json').read_text())['state'],'ready')
            (job/'remote_ssh_export.py').write_bytes(b'OLD_VERIFIER_TO_BE_REPLACED')
            reads=len(server.reads)
            result=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(server.actions.count('prepare'),1)
            self.assertEqual(server.reads[reads:],[])
            self.assertEqual(result.name,'SSH-employee')
            self.assertEqual(json.loads((result/'manifest.json').read_text())['collector_version'],__version__)
            self.assertTrue(verify_archive(result)['ok'])

    def test_ssh_error_message_preserves_code_stage_and_only_safe_diagnostic_fields(self):
        message=ssh_failure_message('invalid_metadata',{'stage':'verification','exception_type':'SafeError',
            'errno':None,'site':{'function':'read_json','line':120,'private':'PRIVATE_SECRET'},
            'message':'PRIVATE_SECRET'},'local')
        self.assertIn('[invalid_metadata]',message)
        self.assertIn('서버 원본 변경 검사',message)
        self.assertIn('read_json:120',message)
        self.assertNotIn('last_failure',message)
        self.assertNotIn('PRIVATE_SECRET',message)
        self.assertNotIn('PRIVATE_SECRET',ssh_failure_message('PRIVATE_SECRET',{'stage':'PRIVATE_SECRET','exception_type':'PRIVATE_SECRET'},'safe phase'))

    def test_changed_host_key_is_rejected_before_credentials_or_new_job(self):
        import paramiko
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
            original=list(server.actions)
            server.key=paramiko.RSAKey.generate(2048)
            with self.assertRaisesRegex(CollectionError,'SSH_HOST_KEY_CHANGED'):
                SSHCollector(config,confirm_host=lambda *_:self.fail('Changed host must not be auto-trusted')).run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(server.actions,original)

    def test_mail_only_empty_files_and_optional_zip(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            (root/'home/e/employee/mbox/empty.mbox').touch()
            config.approvals=False
            config.make_zip=True
            with patch.object(helper,'run_export') as database:
                result=SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
                database.assert_not_called()
            self.assertTrue(verify_archive(result)['ok'])
            with zipfile.ZipFile(result.with_suffix('.zip')) as zipped:
                self.assertIn('catalog.sqlite3',zipped.namelist())
            self.assertTrue(any(p.stat().st_size==0 for p in (result/'mail/raw').iterdir()))

    def test_changed_mail_is_not_published_as_completed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            server,config,_=self.fixture(root)
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            execute=collector.execute
            def mutate(action,detach=False):
                if action=='verify':
                    with (root/'home/e/employee/mbox/inbox.mbox').open('ab') as stream:
                        stream.write(b'new incoming mail')
                return execute(action,detach)
            collector.execute=mutate
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                with self.assertRaisesRegex(CollectionError,'메일이 변경'):
                    collector.run('SYNTHETIC_SSH_PASSWORD')
            self.assertFalse((config.output/'segio-employee').exists())
            self.assertTrue(list(config.output.glob('.segio-ssh-cache/*/session.json')))


class CompactMailTests(unittest.TestCase):
    def test_large_generated_approval_verification_list_is_not_limited_to_64k(self):
        from export_schema_from_segio_config import SafeError
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp).resolve();root=base/('.segio-ssh-'+'a'*32);root.mkdir(mode=0o700)
            source=base/'synthetic-attachment';source.write_bytes(b'SYNTHETIC_SOURCE')
            helper.write_json(str(root/'request.json'),{'protocol':helper.PROTOCOL,'user':'employee','mail':False,'approvals':True})
            helper.write_json(str(root/'status.json'),{'protocol':helper.PROTOCOL,'state':'ready'})
            entry={'source_hex':helper.source_hex(str(source)),'identity':list(helper.fingerprint(str(source)))}
            helper.write_json(str(root/'approval-check.json'),[entry]*700)
            self.assertGreater((root/'approval-check.json').stat().st_size,65536)
            helper.verify_sources(str(root))
            self.assertTrue(json.loads((root/'verified.json').read_text())['ok'])
            (root/'verified.json').unlink();source.write_bytes(b'CHANGED_SOURCE')
            with self.assertRaises(SafeError):helper.verify_sources(str(root))
            self.assertFalse((root/'verified.json').exists())

    def test_ssh_hash_snapshot_retries_new_files_and_reuses_stable_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            mailbox=root/'home/e/employee/mbox'
            mailbox.mkdir(parents=True)
            for name in ('a.mbox','b.mbox'):
                (mailbox/name).write_bytes(b'From fixture\nSubject: '+name.encode()+b'\n\nbody\n')
            request={'user':'employee','mail_template':(root/'home/{initial}/{user}/mbox').as_posix()}
            calls,progress=[],[]
            original=helper.hash_file
            def changing(path,*args,**kwargs):
                calls.append(Path(path).name)
                value=original(path,*args,**kwargs)
                if not (mailbox/'z.mbox').exists():
                    (mailbox/'z.mbox').write_bytes(b'From fixture\nSubject: New arrival\n\nbody\n')
                return value
            with patch.object(helper,'hash_file',side_effect=changing):
                sources,transfers,identities=helper.mail_snapshot(request,lambda state,**fields:progress.append(fields))
            self.assertEqual({source['name'] for source in sources},{'a.mbox','b.mbox','z.mbox'})
            self.assertEqual(calls.count('a.mbox'),1)
            self.assertEqual(calls.count('b.mbox'),1)
            self.assertEqual(len(transfers),3)
            self.assertEqual(len(identities),3)
            self.assertEqual(progress[-1]['attempt'],2)
            self.assertEqual(progress[-1]['completed_bytes'],sum(source['bytes'] for source in sources))
            self.assertEqual(progress[-1]['files_done'],3)
            self.assertEqual(sorted(p.name for p in mailbox.iterdir()),['a.mbox','b.mbox','z.mbox'])

    def test_ssh_hash_snapshot_refuses_mail_that_changes_on_every_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            mailbox=root/'home/e/employee/mbox'
            mailbox.mkdir(parents=True)
            (mailbox/'inbox.mbox').write_bytes(b'From fixture\nSubject: Growing\n\nbody\n')
            request={'user':'employee','mail_template':(root/'home/{initial}/{user}/mbox').as_posix()}
            original=helper.hash_file
            calls=[]
            def changing(path,*args,**kwargs):
                calls.append(path)
                value=original(path,*args,**kwargs)
                with open(path,'ab') as stream:stream.write(b'another arrival\n')
                return value
            with patch.object(helper,'hash_file',side_effect=changing):
                with self.assertRaisesRegex(helper.SafeError,'mail_changed_retry'):
                    helper.mail_snapshot(request,lambda *args,**kwargs:None)
            self.assertEqual(len(calls),3)

    def test_mbox_delimiter_split_between_chunks_is_indexed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'large.mbox'
            first=b'From first@example.test\nSubject: First\n\n'
            filler=b'x'*(1024*1024-3-len(first))
            second=b'\nFrom second@example.test\nSubject: Second\n\nbody\n'
            path.write_bytes(first+filler+second)
            spans=list(mbox_spans(path))
            self.assertEqual(len(spans),2)
            with path.open('rb') as stream:
                stream.seek(spans[1][0])
                self.assertTrue(stream.read(30).startswith(b'Subject: Second'))

    def test_sqlite_mail_offsets_above_fifty_gib_are_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            (root/'mail/raw').mkdir(parents=True)
            source=root/'mail/raw/000000.mbox'
            offset=50*1024**3+4096
            payload=b'Subject: Sparse offset\n\nbody\n'
            with source.open('wb') as stream:
                if os.name=='nt':
                    import ctypes,msvcrt
                    from ctypes import wintypes
                    control=ctypes.windll.kernel32.DeviceIoControl
                    control.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,
                                      wintypes.LPVOID,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),wintypes.LPVOID]
                    returned=wintypes.DWORD()
                    if not control(msvcrt.get_osfhandle(stream.fileno()),0x900c4,None,0,None,0,ctypes.byref(returned),None):
                        self.skipTest('Sparse file support required for offset-only test')
                stream.seek(offset)
                stream.write(payload)
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                db.execute('CREATE TABLE items (id TEXT,title TEXT)')
                db.execute('CREATE TABLE mail_records (id TEXT,source TEXT,begin INTEGER,end INTEGER,sender TEXT,receiver TEXT,date TEXT)')
                db.execute('INSERT INTO items VALUES (?,?)',('mail-000000000','Sparse offset'))
                db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',('mail-000000000','mail/raw/000000.mbox',offset,offset+len(payload),'','',''))
                db.commit()
            record,path=mail_record(root,'mail-000000000')
            self.assertEqual(record['begin'],offset)
            with path.open('rb') as stream:
                stream.seek(record['begin'])
                self.assertEqual(stream.read(record['end']-record['begin']),payload)
            server=SSHServer(root)
            self.addCleanup(server.close)
            config=SSHConfig('127.0.0.1','root','employee',root/'unused',port=server.port,known_hosts=root/'known_hosts')
            collector=SSHCollector(config,confirm_host=lambda *_:True)
            try:
                collector.connect('SYNTHETIC_SSH_PASSWORD')
                with collector.sftp.open('/files/mail/raw/000000.mbox','rb') as stream:
                    stream.seek(offset)
                    self.assertEqual(stream.read(len(payload)),payload)
                    self.assertEqual(list(stream.readv([(offset,len(payload))],max_concurrent_prefetch_requests=8)),[payload])
            finally:
                if collector.sftp:
                    collector.sftp.close()
                if collector.client:
                    collector.client.close()


if __name__=='__main__':
    unittest.main()
