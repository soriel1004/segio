from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, mail_record, search
from segio_archive.local_delete import delete_items, preview, recover, edit_lock, rewrite_mbox, WORK, BLOCK
from segio_archive.portable_mail import prepare_archive, page_path
from segio_archive.viewer import Viewer
from segio_archive.web import Cancelled, CollectionError
import test_search_page as fixtures


class LocalDeleteTests(unittest.TestCase):
    def fixture(self,root,shared=False):
        fixtures.SearchPageTests().card_fixture(root)
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        catalog=json.loads((root/'catalog.json').read_text(encoding='utf-8'))
        for item in catalog['items']:
            item['attachments']=[a for a in item['attachments'] if a['path'] in manifest['files']]
            for attachment in item['attachments']:
                name=attachment['path']
                target=root/Path(name).parent/'attachments'/Path(name).name
                target.parent.mkdir(exist_ok=True)
                (root/name).rename(target)
                attachment['path']=target.relative_to(root).as_posix()
                manifest['files'][attachment['path']]=manifest['files'].pop(name)
        if shared:
            catalog['items'][1]['attachments']=[catalog['items'][0]['attachments'][0]]
        (root/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
        source='mail/raw/000000.mbox'
        (root/'mail/sources.json').write_text(json.dumps({'user':'synthetic','sources':[
            dict(path=source,name='inbox',format='mbox',**manifest['files'][source])]}),encoding='utf-8')
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY,payload TEXT)')
            for item in catalog['items']:
                db.execute('INSERT INTO document_records VALUES (?,?)',
                    ('approval-'+item['id'],(root/item['json']).read_text(encoding='utf-8')))
            db.commit()
        for name in ('catalog.sqlite3','catalog.json','mail/sources.json'):
            manifest['files'][name]=digest_file(root/name)
        manifest['browser_index']='offline-search/6'
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        prepare_archive(root)
        return json.loads((root/'manifest.json').read_text(encoding='utf-8'))

    def bytes_of(self,root,identity):
        record,path=mail_record(root,identity)
        with path.open('rb') as stream:
            stream.seek(record['begin'])
            return stream.read(record['end']-record['begin'])

    def snapshot(self,root):
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        return {name:digest_file(root/name) for name in set(manifest['files'])|{'manifest.json'}}

    def assert_snapshot(self,root,snapshot):
        for name,value in snapshot.items():
            self.assertEqual(digest_file(root/name),value,name)
        self.assertFalse((root/WORK).exists())

    def test_mixed_delete_removes_actual_mail_body_attachments_and_database_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            kept=self.bytes_of(root,'mail-000000001')
            removed=self.bytes_of(root,'mail-000000000')
            old_body=digest_file(root/page_path('mail-000000001'))
            ids=['approval-current','mail-000000000']
            plan=preview(root,ids)
            result=delete_items(root,ids,plan['revision'])
            self.assertEqual((result['approval'],result['mail']),(1,1))
            self.assertFalse((root/'documents/current/document.html').exists())
            self.assertFalse((root/'documents/current/attachments/한글 #첨부.txt').exists())
            self.assertFalse((root/page_path('mail-000000000')).exists())
            self.assertEqual(self.bytes_of(root,'mail-000000001'),kept)
            self.assertNotIn(removed,(root/'mail/raw/000000.mbox').read_bytes())
            self.assertEqual(digest_file(root/page_path('mail-000000001')),old_body)
            self.assertEqual({i['id'] for i in search(root)['items']},{'approval-old','mail-000000001'})
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT id FROM document_records').fetchall(),[('approval-old',)])
            self.assertTrue(verify_archive(root)['ok'])
            before=digest_file(root/page_path('mail-000000001'))
            prepare_archive(root)
            self.assertEqual(digest_file(root/page_path('mail-000000001')),before)
            plan=preview(root,['mail-000000001'])
            delete_items(root,plan['ids'],plan['revision'])
            self.assertFalse((root/'mail/raw/000000.mbox').exists())
            self.assertEqual(json.loads((root/'mail/sources.json').read_text())['sources'],[])
            self.assertTrue(verify_archive(root)['ok'])

    def test_shared_attachment_survives_until_last_referencing_document_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root,shared=True)
            target=root/'documents/current/attachments/한글 #첨부.txt'
            for identity in ('approval-current','approval-old'):
                plan=preview(root,[identity])
                delete_items(root,[identity],plan['revision'])
                self.assertEqual(target.exists(),identity=='approval-current')
                self.assertTrue(verify_archive(root)['ok'])

    def test_cancel_commit_error_and_low_space_leave_the_original_backup_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            before=self.snapshot(root)
            ids=['mail-000000000']
            plan=preview(root,ids)
            cancelled=[False]
            def progress(stage,done,total):
                if stage=='mail' and done:
                    cancelled[0]=True
            def checkpoint():
                if cancelled[0]:raise Cancelled('synthetic cancellation')
            with self.assertRaises(Cancelled):
                delete_items(root,ids,plan['revision'],checkpoint,progress)
            self.assert_snapshot(root,before)
            def fault(name):raise OSError('synthetic install failure')
            with self.assertRaises(OSError):
                delete_items(root,ids,plan['revision'],fault=fault)
            self.assert_snapshot(root,before)
            with patch('segio_archive.local_delete.shutil.disk_usage',return_value=type('Disk',(),{'free':0})()):
                with self.assertRaisesRegex(CollectionError,'공간'):
                    delete_items(root,ids,plan['revision'])
            self.assert_snapshot(root,before)

    def test_crashed_install_is_recovered_on_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            before=self.snapshot(root)
            plan=preview(root,['mail-000000000'])
            code="from pathlib import Path;import os,sys;from segio_archive.local_delete import delete_items;delete_items(Path(sys.argv[1]),['mail-000000000'],sys.argv[2],fault=lambda name:os._exit(23))"
            result=subprocess.run([sys.executable,'-c',code,str(root),plan['revision']],capture_output=True)
            self.assertEqual(result.returncode,23,result.stderr.decode(errors='replace'))
            self.assertTrue((root/WORK).exists())
            recover(root)
            self.assert_snapshot(root,before)
            self.assertTrue(verify_archive(root)['ok'])

    def test_rfc822_source_is_removed_without_touching_other_mailboxes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            manifest=json.loads((root/'manifest.json').read_text())
            sources=json.loads((root/'mail/sources.json').read_text())
            name='mail/raw/000001.eml'
            payload=b'From: synthetic@example.test\nSubject: Single message\n\nSYNTHETIC_EML_BODY\n'
            (root/name).write_bytes(payload)
            manifest['files'][name]=digest_file(root/name)
            sources['sources'].append(dict(path=name,name='sentbox/one.eml',format='rfc822',**manifest['files'][name]))
            (root/'mail/sources.json').write_text(json.dumps(sources))
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',('mail-000000002','mail','Single message','sentbox','synthetic@example.test','mail/mail-000000002'))
                db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',('mail-000000002',name,0,len(payload),'synthetic@example.test','',''))
                db.commit()
            for changed in ('catalog.sqlite3','mail/sources.json'):
                manifest['files'][changed]=digest_file(root/changed)
            manifest.pop('portable_mail')
            manifest['browser_index']=''
            (root/'manifest.json').write_text(json.dumps(manifest))
            prepare_archive(root)
            untouched=digest_file(root/'mail/raw/000000.mbox')
            plan=preview(root,['mail-000000002'])
            delete_items(root,plan['ids'],plan['revision'])
            self.assertFalse((root/name).exists())
            self.assertEqual(digest_file(root/'mail/raw/000000.mbox'),untouched)
            self.assertEqual(search(root)['total'],4)
            self.assertTrue(verify_archive(root)['ok'])

    def test_mbox_rewrite_uses_bounded_reads_and_preserves_kept_frame_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            name='mail/raw/000000.mbox'
            path=root/name
            path.parent.mkdir(parents=True)
            envelope=b'From synthetic@example.test Mon Nov 02 10:00:00 2026\n'
            first=b'Subject: remove\n\n'+b'a'*(2*BLOCK)+b'\n'
            second=b'Subject: keep\n\n'+b'b'*(2*BLOCK)+b'\n'
            path.write_bytes(envelope+first+envelope+second)
            split=len(envelope)+len(first)
            records=[('mail-000000000',name,len(envelope),split),
                     ('mail-000000001',name,split+len(envelope),path.stat().st_size)]
            expected=digest_file(path)
            original_open=Path.open
            test=self
            class Bounded:
                def __init__(self,stream):self.stream=stream
                def __enter__(self):return self
                def __exit__(self,*args):self.stream.close()
                def __getattr__(self,key):return getattr(self.stream,key)
                def read(self,size=-1):
                    test.assertTrue(0<=size<=BLOCK)
                    return self.stream.read(size)
                def readline(self,size=-1):
                    test.assertTrue(0<=size<=BLOCK)
                    return self.stream.readline(size)
            def guarded(candidate,*args,**kwargs):
                stream=original_open(candidate,*args,**kwargs)
                return Bounded(stream) if candidate==path else stream
            reports=[]
            with patch.object(Path,'open',guarded):
                offsets,details=rewrite_mbox(root,root/'stage',name,records,{'mail-000000000'},expected,
                    lambda:None,lambda stage,done,total:reports.append((done,total)))
            self.assertEqual((root/'stage'/name).read_bytes(),envelope+second)
            self.assertEqual(offsets,{'mail-000000001':(len(envelope),len(envelope)+len(second))})
            self.assertEqual(digest_file(root/'stage'/name),details)
            self.assertEqual(digest_file(path),expected)
            self.assertEqual(reports[-1],(expected['bytes'],expected['bytes']))

    def test_os_lock_stale_revision_and_committed_cleanup_are_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            plan=preview(root,['mail-000000000'])
            before=self.snapshot(root)
            code="from pathlib import Path;import sys;from segio_archive.local_delete import delete_items;from segio_archive.web import CollectionError\ntry:delete_items(Path(sys.argv[1]),['mail-000000000'],sys.argv[2])\nexcept CollectionError:sys.exit(7)\nelse:sys.exit(99)"
            with edit_lock(root):
                result=subprocess.run([sys.executable,'-c',code,str(root),plan['revision']],capture_output=True)
                self.assertEqual(result.returncode,7,result.stderr.decode(errors='replace'))
            self.assert_snapshot(root,before)
            manifest=json.loads((root/'manifest.json').read_text())
            manifest['synthetic_revision_change']=True
            (root/'manifest.json').write_text(json.dumps(manifest))
            changed=self.snapshot(root)
            with self.assertRaisesRegex(CollectionError,'변경'):
                delete_items(root,plan['ids'],plan['revision'])
            self.assert_snapshot(root,changed)
            plan=preview(root,plan['ids'])
            import shutil
            original_rmtree=shutil.rmtree
            def interrupted_cleanup(path,*args,**kwargs):
                if Path(path)==root/WORK and json.loads((root/WORK/'journal.json').read_text())['phase']=='committed':
                    raise OSError('synthetic cleanup failure')
                return original_rmtree(path,*args,**kwargs)
            with patch('segio_archive.local_delete.shutil.rmtree',side_effect=interrupted_cleanup):
                result=delete_items(root,plan['ids'],plan['revision'])
            self.assertTrue(result['cleanup_pending'])
            self.assertEqual(search(root)['total'],3)
            recover(root)
            self.assertFalse((root/WORK).exists())
            self.assertEqual(search(root)['total'],3)
            self.assertTrue(verify_archive(root)['ok'])

    def test_symlinked_archive_file_is_rejected_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'archive'
            root.mkdir()
            self.fixture(root)
            outside=root.parent/'outside.txt'
            outside.write_bytes(b'SYNTHETIC_OUTSIDE_FILE')
            path=root/'documents/current/document.html'
            path.unlink()
            try:path.symlink_to(outside)
            except OSError:self.skipTest('Symlink creation is not permitted')
            with self.assertRaises(CollectionError):preview(root,['approval-current'])
            self.assertEqual(outside.read_bytes(),b'SYNTHETIC_OUTSIDE_FILE')

    def test_changed_source_stale_plan_and_unsafe_manifest_do_not_delete_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            plan=preview(root,['mail-000000000'])
            raw=root/'mail/raw/000000.mbox'
            raw.write_bytes(raw.read_bytes()+b'changed')
            before=self.snapshot(root)
            with self.assertRaises(CollectionError):
                delete_items(root,plan['ids'],plan['revision'])
            self.assert_snapshot(root,before)
            manifest=json.loads((root/'manifest.json').read_text())
            manifest['files']['../outside.txt']={'bytes':1,'sha256':'0'*64}
            (root/'manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(CollectionError):preview(root,['approval-current'])

    def request(self,viewer,action,data,headers=None):
        connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
        standard={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{viewer.server.server_port}',
                  'X-Archive-CSRF':viewer.editor.csrf if viewer.editor else ''}
        if headers:standard.update(headers)
        try:
            connection.request('POST','/'+viewer.token+'/api/delete/'+action,json.dumps(data).encode(),standard)
            response=connection.getresponse()
            body=response.read()
            return response.status,json.loads(body) if response.getheader('Content-Type','').startswith('application/json') else {}
        finally:connection.close()

    def test_local_api_requires_edit_mode_origin_csrf_and_one_time_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                self.assertEqual(self.request(viewer,'preview',{'ids':['approval-current']})[0],403)
            finally:viewer.close()
            viewer=Viewer(root,editable=True)
            try:
                before=self.snapshot(root)
                for headers in ({'Origin':'null'},{'Origin':'https://external.invalid'},
                                {'X-Archive-CSRF':'wrong'},{'Host':'external.invalid'}):
                    self.assertEqual(self.request(viewer,'preview',{'ids':['approval-current']},headers)[0],403)
                self.assertEqual(self.request(viewer,'preview',{'ids':['missing']})[0],400)
                self.assert_snapshot(root,before)
                code,plan=self.request(viewer,'preview',{'ids':['approval-current','mail-000000000']})
                self.assertEqual(code,200)
                self.assertEqual((plan['approval'],plan['mail']),(1,1))
                entered,proceed=threading.Event(),threading.Event()
                def paused(*args,**kwargs):
                    entered.set()
                    if not proceed.wait(5):raise RuntimeError('synthetic timeout')
                    return delete_items(*args,**kwargs)
                with patch('segio_archive.editor.local_delete.delete_items',side_effect=paused):
                    code,job=self.request(viewer,'start',{'plan':plan['plan'],'confirmation':'delete-selected'})
                    self.assertEqual(code,200)
                    self.assertTrue(entered.wait(3))
                    saved=root/'saved-index.html'
                    (root/'index.html').rename(saved)
                    connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port,timeout=3)
                    try:
                        connection.request('GET','/'+viewer.token+'/index.html')
                        response=connection.getresponse()
                        self.assertEqual(response.status,200)
                        self.assertIn("script-src 'self'",response.getheader('Content-Security-Policy'))
                        self.assertIn('그룹웨어',response.read().decode('utf-8'))
                        connection.request('GET','/'+viewer.token+'/api/search')
                        response=connection.getresponse()
                        self.assertEqual(response.status,423)
                        response.read()
                    finally:
                        connection.close()
                        saved.rename(root/'index.html')
                        proceed.set()
                    deadline=time.monotonic()+10
                    while viewer.editor.busy and time.monotonic()<deadline:time.sleep(.01)
                self.assertEqual(viewer.editor.status(job['job'])['state'],'done')
                self.assertEqual(self.request(viewer,'start',{'plan':plan['plan'],'confirmation':'delete-selected'})[0],400)
                self.assertEqual(search(root)['total'],2)
                self.assertTrue(verify_archive(root)['ok'])
                pending=viewer.editor.preview(['approval-old'])
                viewer.editor.close()
                with self.assertRaisesRegex(CollectionError,'종료'):
                    viewer.editor.start(pending['plan'])
                self.assertEqual(search(root)['total'],2)
            finally:viewer.close()

    @unittest.skipUnless(fixtures.BROWSER,'Real Chromium required')
    def test_index_selection_preview_cancel_delete_and_readonly_file_notice(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root,editable=True)
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=fixtures.BROWSER)
                    try:
                        page=browser.new_page()
                        page.goto(viewer.url)
                        expect(page.locator('.data-row')).to_have_count(4)
                        button=page.locator('#delete-selected')
                        expect(button).to_be_enabled()
                        expect(button).to_have_text('삭제')
                        expect(page.locator('#select-page')).to_be_hidden()
                        expect(page.locator('.item-select:visible')).to_have_count(0)
                        expect(page.locator('#cancel-selection')).to_be_hidden()
                        self.assertEqual(button.locator('xpath=../..').get_attribute('id'),'page-options')
                        self.assertGreater(button.bounding_box()['x'],page.locator('#apply-page-size').bounding_box()['x'])
                        requests=[]
                        page.on('request',lambda request: requests.append(request.url) if '/api/delete/' in request.url else None)
                        button.click()
                        expect(button).to_have_text('선택한 자료 삭제')
                        expect(page.locator('#select-page')).to_be_visible()
                        expect(button).to_be_disabled()
                        self.assertEqual(requests,[])  # Entering selection mode never starts deletion.
                        page.locator('#select-page').check()
                        page.locator('#cancel-selection').click()
                        expect(page.locator('.item-select:visible')).to_have_count(0)
                        expect(button).to_have_text('삭제')
                        expect(button).to_be_enabled()
                        self.assertEqual(search(root)['total'],4)
                        button.click()
                        expect(page.locator('#select-page')).not_to_be_checked()
                        page.locator('#select-page').check()
                        expect(page.locator('#selection-count')).to_have_text('선택 4건')
                        page.locator('#delete-selected').click()
                        expect(page.locator('#delete-dialog')).to_be_visible()
                        expect(page.locator('#delete-description')).to_contain_text('결재 2건 · 메일 2건')
                        page.locator('#cancel-delete').click()
                        self.assertEqual(search(root)['total'],4)
                        page.locator('#select-page').uncheck()
                        page.locator('.item-select[data-id="approval-current"]').check()
                        page.locator('.item-select[data-id="mail-000000000"]').check()
                        page.locator('#delete-selected').click()
                        expect(page.locator('#delete-description')).to_contain_text('결재 1건 · 메일 1건')
                        page.locator('#confirm-delete').click()
                        expect(page.locator('#tab-all')).to_have_text('전체 (2건)')
                        expect(page.locator('.data-row')).to_have_count(2)
                        expect(page.locator('#delete-message')).to_contain_text('결재 1건 · 메일 1건')
                        expect(button).to_have_text('삭제')
                        expect(page.locator('.item-select:visible')).to_have_count(0)
                        expect(page.locator('#select-page')).to_be_hidden()
                        self.assertTrue(verify_archive(root)['ok'])
                        fixtures.navigate(self,page,(root/'index.html').as_uri())
                        expect(page.locator('#delete-selected')).to_be_enabled()
                        expect(page.locator('#editor-hint')).to_contain_text('폴더')
                    finally:browser.close()
            finally:viewer.close()
