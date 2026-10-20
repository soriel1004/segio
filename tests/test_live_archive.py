import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.db_client import DBCollector
from segio_archive.mail_import import build_local_catalog
from segio_archive.viewer import Viewer
from segio_archive.web import Config
from test_login_bridge import NativePHPFixture, PASSWORD, PHP
from live_fixtures import synthetic_export

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
import live_export_worker as worker
import export_approval_db as exporter


def create_fixture(root):
    root=Path(root).resolve()
    (root/'jobs').mkdir(exist_ok=True)
    mail=root/'home/e/employee/mbox'
    mail.mkdir(parents=True,exist_ok=True)
    content=(b'From synthetic@example.test Tue Oct 20 10:00:00 2026\n'
             b'Subject: =?utf-8?b?7ZqM7J2Y?=\r\nFrom: synthetic@example.test\r\n'
             b'To: employee@example.test\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'+
             '회의 본문\r\n'.encode()+b'\nFrom synthetic@example.test Tue Oct 20 11:00:00 2026\n'
             b'Subject: =?euc-kr?b?vcPH6A==?=\nContent-Type: text/html; charset=utf-8\n\n'
             b'<p>mail two</p><script>window.UNSAFE=1</script><img src="https://external.invalid/pixel">\n')
    from email.message import EmailMessage
    message=EmailMessage()
    message['Subject']='첨부 확인'
    message['From']='synthetic@example.test'
    message.set_content('원본 첨부 보존 시험')
    message.add_attachment(b'SYNTHETIC_ATTACHMENT',maintype='application',subtype='octet-stream',filename='../../CON.txt')
    content+=b'\nFrom synthetic@example.test Tue Oct 20 12:00:00 2026\n'+message.as_bytes()
    (mail/'inbox.mbox').write_bytes(content)
    (mail/'native.index').write_bytes(b'\x00SYNTHETIC_NATIVE_INDEX\x00')
    other=root/'home/o/other/mbox'
    other.mkdir(parents=True,exist_ok=True)
    (other/'inbox.mbox').write_bytes(b'From other@example.test Tue Oct 20 12:00:00 2026\nSubject: OTHER_USER_MUST_NOT_EXPORT\n\nprivate\n')
    database=root/'database.json'
    database.write_text(json.dumps({'employee':{'uid':'employee','title':'현재 DB 첫 문서'},
                                    'other':{'uid':'other','title':'OTHER_USER_MUST_NOT_EXPORT'}},ensure_ascii=False),encoding='utf-8')
    settings={'root':str(root),'db_config':str(database),'cell':str(root),
              'mail_template':(root/'home/{initial}/{user}/mbox').as_posix(),
              'csrf_secret':'b'*64,'python':sys.executable,'worker':str(root/'worker.py')}
    (root/'config.json').write_text(json.dumps(settings),encoding='utf-8')
    launcher=f'''import sys
sys.path.insert(0,{str(ROOT/'tools')!r})
sys.path.insert(0,{str(ROOT/'tests')!r})
import live_export_worker as worker
from live_fixtures import synthetic_export
worker.run_export=synthetic_export
raise SystemExit(worker.main())
'''
    (root/'worker.py').write_text(launcher,encoding='utf-8')
    return settings,content


class LiveWorkerTests(unittest.TestCase):
    def test_unencoded_cp949_rfc822_subject_is_searchable_and_raw_bytes_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            create_fixture(root)
            raw=(b'Delivered-To: employee@example.test\r\nX-Original-To: employee@example.test\r\nSubject: '+
                 '한글 원문'.encode('cp949')+b'\r\nFrom: synthetic@example.test\r\n\r\nbody\r\n')
            (root/'home/e/employee/mbox/plain.eml').write_bytes(raw)
            job_id='c'*32
            job=root/'jobs'/job_id
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            worker.run_job(str(root/'config.json'),job_id)
            from segio_archive.db_client import unpack_verified
            result=root/'result'
            result.mkdir()
            unpack_verified(job/'archive.zip',result,'employee')
            manifest=build_local_catalog(result,'employee')
            self.assertEqual(manifest['counts']['mail'],4)
            self.assertEqual((result/'mail/raw/000002.eml').read_bytes(),raw)
            with closing(sqlite3.connect(result/'catalog.sqlite3')) as database:
                self.assertEqual(database.execute('SELECT count(*) FROM items WHERE title=?',('한글 원문',)).fetchone()[0],1)
            self.assertTrue(verify_archive(result)['ok'])

    def test_mail_only_and_changed_mail_does_not_report_completed_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings,_=create_fixture(root)
            job_id='d'*32
            job=root/'jobs'/job_id
            job.mkdir()
            worker.write_json(str(job/'request.json'),{'username':'employee','approvals':False,'mail':True,'created_at':1})
            with patch.object(worker,'run_export') as database:
                worker.run_job(str(root/'config.json'),job_id)
                database.assert_not_called()
            self.assertEqual(worker.read_json(str(job/'status.json'))['state'],'ready')
            target=root/'changed'
            target.mkdir()
            calls=0
            def changed():
                nonlocal calls
                calls+=1
                if calls==3:
                    with (root/'home/e/employee/mbox/inbox.mbox').open('ab') as stream:
                        stream.write(b'new incoming mail')
            with self.assertRaisesRegex(worker.SafeError,'mail_changed_retry'):
                worker.copy_mail(settings['mail_template'],'employee',str(target),{'files':{},'counts':{},'coverage':[]},changed)

    def test_current_database_and_whole_user_mail_are_exported_without_other_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings,original=create_fixture(root)
            for number in range(2):
                job_id=f'{number+1:032x}'
                job=root/'jobs'/job_id
                job.mkdir()
                worker.write_json(str(job/'request.json'),{'username':'employee','approvals':True,'mail':True,'created_at':number})
                records=json.loads((root/'database.json').read_text(encoding='utf-8'))
                records['employee']['title']=f'현재 DB 버전 {number}'
                (root/'database.json').write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8')
                with patch.object(worker,'run_export',side_effect=synthetic_export):
                    worker.run_job(str(root/'config.json'),job_id)
                status=worker.read_json(str(job/'status.json'))
                self.assertEqual(status['state'],'ready',status)
                import zipfile
                folder=root/f'result{number}'
                from segio_archive.db_client import unpack_verified
                folder.mkdir()
                unpack_verified(job/'archive.zip',folder,'employee')
                result=build_local_catalog(folder,'employee')
                self.assertEqual(result['counts']['mail'],3)
                self.assertEqual((folder/'mail/raw/000000.mbox').read_bytes(),original)
                self.assertEqual((root/'home/e/employee/mbox/inbox.mbox').read_bytes(),original)
                self.assertTrue(verify_archive(folder)['ok'])
                with closing(sqlite3.connect(folder/'catalog.sqlite3')) as connection:
                    self.assertEqual(connection.execute("SELECT title FROM items WHERE kind='approval'").fetchone()[0],f'현재 DB 버전 {number}')
                    titles=[r[0] for r in connection.execute("SELECT title FROM items WHERE kind='mail'")]
                    self.assertIn('회의',titles)
                    self.assertIn('시험',titles)
                self.assertEqual((folder/'mail/raw/000001.bin').read_bytes(),b'\x00SYNTHETIC_NATIVE_INDEX\x00')
                self.assertEqual(next((folder/'mail/messages/00000002/attachments').iterdir()).read_bytes(),b'SYNTHETIC_ATTACHMENT')
                for file in folder.rglob('*'):
                    if file.is_file():
                        self.assertNotIn(b'OTHER_USER_MUST_NOT_EXPORT',file.read_bytes())
                page=(folder/'mail/messages/00000001/document.html').read_text(encoding='utf-8')
                self.assertNotIn('window.UNSAFE',page)
                self.assertNotIn('https://external',page)


    def test_mail_paths_and_live_database_scope_reject_unsafe_entitlements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            settings,_=create_fixture(root)
            self.assertEqual(worker.mail_path(settings['mail_template'],'employee'),str(root.resolve()/'home/e/employee/mbox'))
            for user in ('.','..','../other','guest'):
                with self.assertRaises(worker.SafeError):
                    worker.mail_path(settings['mail_template'],user)
            with self.assertRaises(worker.SafeError):
                worker.mail_path(str(root/'home'),'employee')
            if sys.platform!='win32':
                (root/'home/e/employee/mbox/escape').symlink_to(root/'database.json')
                with self.assertRaises(worker.SafeError):
                    worker.inventory(str(root/'home/e/employee/mbox'))
        sql,_=exporter.selection_sql('employee',live_scope=True)
        self.assertNotIn('approval_favorite',sql)
        self.assertNotIn('approval_comment_info',sql)
        self.assertIn('approval_line',sql)
        # Windows ctime is creation time, unlike the POSIX change marker.
        # Different stat/fstat representations must not reject unchanged mail.
        from types import SimpleNamespace
        before=SimpleNamespace(st_dev=1,st_ino=2,st_size=30,st_mtime=4,st_mtime_ns=4000000000,st_ctime=5,st_ctime_ns=5000000000)
        opened=SimpleNamespace(**{**vars(before),'st_ctime':5.01,'st_ctime_ns':5010000000})
        with patch.object(worker.os,'name','nt'):
            self.assertEqual(worker.file_identity(before),worker.file_identity(opened))
        with patch.object(worker.os,'name','posix'):
            self.assertNotEqual(worker.file_identity(before),worker.file_identity(opened))


@unittest.skipIf(sys.platform=='win32', 'POSIX one-time installer')
class LiveInstallationTests(unittest.TestCase):
    def test_installation_never_prepares_employee_data_or_copies_db_password(self):
        import types
        import install_live_archive as installer
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cell=root/'cell000'
            (cell/'folder/html/segio').mkdir(parents=True)
            (cell/'etc').mkdir()
            config=cell/'etc/db.conf'
            secret=b'SYNTHETIC_DB_PASSWORD_NEVER_COPY'
            config.write_bytes(secret)
            account=types.SimpleNamespace(pw_uid=1234,pw_gid=os.getgid())
            private=root/'private'
            with patch.object(installer.os,'geteuid',return_value=0), \
                 patch.object(installer.os,'chown') as ownership, \
                 patch.object(installer.pwd,'getpwnam',return_value=account), \
                 patch.object(worker,'run_export') as export:
                installer.install('synthetic-web',str(config),str(private))
                export.assert_not_called()
                ownership.assert_any_call(str(private/'jobs'),1234,os.getgid())
            self.assertEqual(config.read_bytes(),secret)
            self.assertEqual(list((private/'jobs').iterdir()),[])
            self.assertEqual((private/'jobs').stat().st_mode&0o777,0o700)
            self.assertEqual((private/'code').stat().st_mode&0o777,0o750)
            settings=json.loads((private/'config.json').read_text(encoding='utf-8'))
            self.assertEqual(settings['mail_template'],str(cell/'home/{initial}/{user}/mbox'))
            self.assertEqual(len(settings['csrf_secret']),64)
            for path in private.rglob('*'):
                if path.is_file():
                    self.assertNotIn(secret,path.read_bytes())
                    self.assertEqual(path.stat().st_mode&0o777,0o640)
            endpoint=cell/'folder/html/segio/archive/live.php'
            self.assertNotIn('__PRIVATE_ROOT__',endpoint.read_text(encoding='utf-8'))
            self.assertEqual(endpoint.stat().st_mode&0o777,0o644)


@unittest.skipUnless(PHP,'PHP CLI required')
class LiveBridgeTests(NativePHPFixture, unittest.TestCase):
    """Real native-auth fixture, detached worker and streaming client."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.live_root=cls.root/'live'
        cls.live_root.mkdir()
        cls.settings,cls.original=create_fixture(cls.live_root)
        php=(ROOT/'tools/live_archive_bridge.php').read_text(encoding='utf-8')
        literal=cls.live_root.as_posix().replace("'","\\'")
        (cls.root/'live.php').write_text(php.replace('__PRIVATE_ROOT__',literal),encoding='utf-8')
        router=(cls.root/'router.php').read_text(encoding='utf-8')
        router=router.replace("$path==='/segio/archive/bridge.php'","($path==='/segio/archive/bridge.php' || $path==='/segio/archive/live.php')")
        router=router.replace("include __DIR__.'/bridge.php';","include __DIR__.($path==='/segio/archive/live.php'?'/live.php':'/bridge.php');")
        (cls.root/'router.php').write_text(router,encoding='utf-8')

    def test_live_login_db_mail_download_and_subject_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Config(self.url+'/', 'employee', Path(tmp),mail=True,pdf=False,
                          allow_http=True,headless=True,timeout_ms=10000,
                          browser_executable=os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium'))
            path=DBCollector(config).run(PASSWORD)
            self.assertTrue(verify_archive(path)['ok'])
            self.assertEqual((path/'mail/raw/000000.mbox').read_bytes(),self.original)
            self.assertFalse([p for p in (self.live_root/'jobs').iterdir() if p.is_dir()])
            original_manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
            records=json.loads((self.live_root/'database.json').read_text(encoding='utf-8'))
            records['employee']['title']='요청 후 변경된 최신 DB'
            (self.live_root/'database.json').write_text(json.dumps(records,ensure_ascii=False),encoding='utf-8')
            second=DBCollector(config).run(PASSWORD)
            latest=json.loads((second/'manifest.json').read_text(encoding='utf-8'))
            self.assertNotEqual(original_manifest['live_export']['job'],latest['live_export']['job'])
            self.assertEqual(second.name,'segio-employee-2')
            with closing(sqlite3.connect(second/'catalog.sqlite3')) as database:
                self.assertEqual(database.execute("SELECT title FROM items WHERE kind='approval'").fetchone()[0],'요청 후 변경된 최신 DB')
            viewer=Viewer(path)
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=config.browser_executable)
                    context=browser.new_context()
                    requests=[]
                    context.on('request',lambda request:requests.append(request.url))
                    page=context.new_page()
                    page.goto(viewer.url)
                    page.locator('#q').fill('회의')
                    self.assertEqual(page.locator('article').count(),1)
                    self.assertIn('회의',page.locator('article h2').inner_text())
                    self.assertTrue(all(url.startswith(viewer.url.rsplit('/',1)[0]) for url in requests))
                    browser.close()
            finally:
                viewer.close()

    def test_start_requires_csrf_and_rejects_other_account_job_access(self):
        opener=self.logged_in()
        import urllib.error
        import urllib.parse
        endpoint=self.url+'/segio/archive/live.php'
        with self.assertRaises(urllib.error.HTTPError) as caught:
            opener.open(endpoint+'?action=start',data=b'approvals=1&mail=1')
        self.assertEqual(caught.exception.code,403)
        job_id='f'*32
        job=self.live_root/'jobs'/job_id
        job.mkdir(exist_ok=True)
        worker.write_json(str(job/'request.json'),{'username':'employee','created_at':0})
        other=self.logged_in('other')
        for action in ('status','download'):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                other.open(endpoint+f'?action={action}&job={job_id}')
            self.assertEqual(caught.exception.code,404)
        shutil.rmtree(job)


if __name__=='__main__':
    unittest.main()
