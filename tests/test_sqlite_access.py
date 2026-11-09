from contextlib import closing
import json
import os
from pathlib import Path,PureWindowsPath
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from urllib.parse import urlsplit,unquote
from unittest.mock import patch

from segio_archive.sqlite_access import readonly_uri,connect_readonly


class SQLiteAccessTests(unittest.TestCase):
    def make_archive(self,root):
        from test_portable_mail import PortableMailTests
        from segio_archive.compact_mail import digest_file
        manifest=PortableMailTests().fixture(root)
        name='mail/raw/000000.mbox'
        (root/'mail/sources.json').write_text(json.dumps({'sources':[
            dict(path=name,name='inbox',format='mbox',**manifest['files'][name])]}),encoding='utf-8')
        manifest['files']['mail/sources.json']=digest_file(root/'mail/sources.json')
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def check_archive(self,root,original):
        from segio_archive.portable_mail import prepare_archive,page_path
        from segio_archive.compact_mail import search,mail_record,digest_file
        from segio_archive.archive import verify_archive
        from segio_archive.local_delete import preview,delete_items
        from segio_archive.index_maintenance import compact_approval_index
        prepare_archive(root)
        self.assertIn('한글 표 내용',(root/page_path('mail-000000000')).read_text(encoding='utf-8'))
        self.assertEqual(digest_file(root/'mail/raw/000000.mbox'),original['files']['mail/raw/000000.mbox'])
        self.assertEqual(search(root,kind='mail')['total'],2)
        self.assertEqual(mail_record(root,'mail-000000000')[0]['id'],'mail-000000000')
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        with patch('segio_archive.index_maintenance.THRESHOLD',0):compact_approval_index(root,manifest)
        plan=preview(root,['mail-000000001'])
        delete_items(root,plan['ids'],plan['revision'])
        self.assertEqual(search(root,kind='mail')['total'],1)
        self.assertTrue(verify_archive(root)['ok'])

    def test_archive_workflow_fixture_renders_searches_compacts_and_deletes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'한글 #100%';root.mkdir()
            self.check_archive(root,self.make_archive(root))

    def test_unc_has_empty_uri_authority_and_escapes_filename_data(self):
        for server in ('192.0.2.1','synthetic-server','localhost'):
            with self.subTest(server=server):
                path=PureWindowsPath('//'+server+'/Backup/한글 #100%/catalog.sqlite3')
                uri=readonly_uri(path);parts=urlsplit(uri)
                self.assertEqual(parts.netloc,'');self.assertEqual(parts.query,'mode=ro')
                self.assertEqual(unquote(parts.path),'//'+server+'/Backup/한글 #100%/catalog.sqlite3')
                self.assertIn('%23',uri);self.assertIn('%25',uri)
        local=PureWindowsPath('C:/한글 #100%/catalog.sqlite3')
        self.assertEqual(readonly_uri(local),local.as_uri()+'?mode=ro')

    def test_old_unc_uri_reproduces_reported_sqlite_error_one(self):
        with closing(sqlite3.connect(':memory:')) as db:
            options={row[0] for row in db.execute('PRAGMA compile_options')}
        if 'ALLOW_URI_AUTHORITY' in options:self.skipTest('SQLite build allows URI authorities')
        uri=PureWindowsPath('//synthetic-server.invalid/backup/catalog.sqlite3').as_uri()+'?mode=ro'
        with self.assertRaises(sqlite3.OperationalError) as caught:sqlite3.connect(uri,uri=True)
        self.assertEqual(caught.exception.sqlite_errorcode,sqlite3.SQLITE_ERROR)
        self.assertIn('invalid uri authority',str(caught.exception))

    def test_real_readonly_open_handles_unc_style_path_and_never_creates_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()/'한글 #100%.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE payload (value TEXT)');db.execute('INSERT INTO payload VALUES (?)',('한글 데이터',));db.commit()
            alias=PureWindowsPath('\\\\?\\'+str(path)) if os.name=='nt' else PureWindowsPath('//'+path.as_posix().lstrip('/'))
            for candidate in (path,alias):
                with closing(connect_readonly(candidate)) as db:
                    self.assertEqual(db.execute('SELECT value FROM payload').fetchone()[0],'한글 데이터')
                    with self.assertRaises(sqlite3.OperationalError) as caught:db.execute("INSERT INTO payload VALUES ('changed')")
                    self.assertEqual(caught.exception.sqlite_errorcode,sqlite3.SQLITE_READONLY)
            missing=path.with_name('missing.sqlite3')
            with self.assertRaises(sqlite3.OperationalError):connect_readonly(missing)
            self.assertFalse(missing.exists())

    @unittest.skipUnless(os.name=='nt' and os.environ.get('GITHUB_ACTIONS')=='true','Real SMB test uses an ephemeral Windows CI share')
    def test_windows_smb_mail_render_search_index_compaction_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            share=Path(tmp).resolve();local=share/'한글 #100%';local.mkdir()
            original=self.make_archive(local)
            name='segio-test-'+uuid.uuid4().hex
            env=dict(os.environ,SEGIO_TEST_SHARE_PATH=str(share),SEGIO_TEST_SHARE_NAME=name)
            create="$ErrorActionPreference='Stop'; $segioService=Get-Service LanmanServer; if($segioService.Status -ne 'Running'){Start-Service LanmanServer}; $segioIdentity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name; New-SmbShare -Name $env:SEGIO_TEST_SHARE_NAME -Path $env:SEGIO_TEST_SHARE_PATH -FullAccess $segioIdentity | Out-Null"
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',create],env=env,capture_output=True,timeout=30)
            if result.returncode:
                print('::notice::Synthetic Windows SMB share unavailable; UNC URI and extended-path tests still run.')
                self.skipTest('Cannot create synthetic Windows SMB share (exit=%s)'%result.returncode)
            try:
                root=Path('\\\\localhost\\'+name)/local.name
                self.check_archive(root,original)
                print('::notice::Synthetic Windows SMB mail rendering, search, index compaction, deletion and integrity checks passed.')
            finally:
                cleanup="Remove-SmbShare -Name $env:SEGIO_TEST_SHARE_NAME -Force -Confirm:$false -ErrorAction Stop"
                result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',cleanup],env=env,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,'Synthetic SMB share cleanup failed')
