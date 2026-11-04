"""Real File System Access writes, with an OPFS directory replacing only the picker."""
import base64
from contextlib import closing
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import search
from segio_archive.local_delete import recover
from segio_archive.web import CollectionError
import test_local_delete as deletion
import test_search_page as fixtures


@unittest.skipUnless(fixtures.BROWSER,'Real Chromium required')
class BrowserDeleteTests(unittest.TestCase):
    @contextmanager
    def opened(self,root,large_manifest=False):
        # HTTP is only the test transport: it permits OPFS automation without
        # clicking an OS dialog. There is no export or deletion HTTP endpoint.
        class Static(SimpleHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                if self.path=='/index.html':
                    data=(root/'index.html').read_bytes().replace(b"offline=location.protocol==='file:'",b'offline=true')
                    self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(data)
                else:super().do_GET()
        from playwright.sync_api import sync_playwright,expect
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Static,directory=str(root)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path=fixtures.BROWSER)
                try:
                    context=browser.new_context();page=context.new_page()
                    page.add_init_script('window.showDirectoryPicker=async()=>window.testRoot;')
                    page.goto('http://127.0.0.1:%s/index.html'%server.server_port)
                    expect(page.locator('.data-row')).to_have_count(search(root)['total'])
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    payload={name:base64.b64encode((root/name).read_bytes()).decode() for name in
                             list(json.loads((root/'manifest.json').read_text())['files'])+['manifest.json']}
                    page.evaluate('''async payload=>{
                        const top=await navigator.storage.getDirectory();window.testRoot=await top.getDirectoryHandle('backup',{create:true});
                        for(const [name,value] of Object.entries(payload)){
                            const parts=name.split('/'),last=parts.pop();let dir=testRoot;
                            for(const part of parts)dir=await dir.getDirectoryHandle(part,{create:true});
                            const writer=await(await dir.getFileHandle(last,{create:true})).createWritable();
                            await writer.write(Uint8Array.from(atob(value),c=>c.charCodeAt(0)));await writer.close();
                        }
                    }''',payload)
                    if large_manifest:
                        page.evaluate('''async()=>{
                            const handle=await testRoot.getFileHandle('manifest.json'),manifest=JSON.parse(await(await handle.getFile()).text());
                            for(let n=0;n<31000;n++)for(const leaf of ['document.html','database.json'])manifest.files['documents/b-00/'+n+'/'+leaf]={bytes:0,sha256:'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'};
                            const writer=await handle.createWritable();await writer.write(JSON.stringify(manifest));await writer.close();
                            window.sqliteReads=0;const original=testRoot;
                            window.testRoot=new Proxy(original,{get(target,key){if(key==='getFileHandle')return async(name,options)=>{if(name==='catalog.sqlite3'){sqliteReads++;throw Error('SQLITE_MUST_BE_DEFERRED')}return target.getFileHandle(name,options)};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value}});
                        }''')
                    page.locator('#delete-selected').click()
                    expect(page.locator('#select-page')).to_be_visible(timeout=15000)
                    yield page,errors
                finally:browser.close()
        finally:server.shutdown();server.server_close();thread.join()

    def test_selection_for_31000_document_pairs_is_ready_without_loading_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);deletion.LocalDeleteTests().fixture(root)
            with self.opened(root,large_manifest=True) as (page,errors):
                self.assertEqual(page.evaluate('sqliteReads'),0)
                self.assertIsNone(page.evaluate('browserEditor.db||null'))
                page.locator('.item-select[data-id="approval-current"]').check()
                self.assertTrue(page.locator('#delete-selected').is_enabled())
                self.assertEqual(errors,[])

    def exported(self,page,target):
        payload=page.evaluate('''async()=>{
            const result={};async function walk(dir,prefix=''){
                for await(const [name,handle] of dir.entries()){
                    if(name.startsWith('.segio-'))continue;
                    if(handle.kind==='directory')await walk(handle,prefix+name+'/');
                    else{const bytes=new Uint8Array(await(await handle.getFile()).arrayBuffer());let text='';for(let n=0;n<bytes.length;n+=16384)text+=String.fromCharCode(...bytes.subarray(n,n+16384));result[prefix+name]=btoa(text)}
                }
            }await walk(testRoot);return result;
        }''')
        for name,value in payload.items():
            path=target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(value))

    def wait_job(self,page):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            status=page.evaluate('browserEditor.status(browserEditor.job.id)')
            if status['state']!='running':return status
            time.sleep(.02)
        self.fail('Browser deletion did not finish')

    def test_browser_deletes_actual_records_and_keeps_shared_mbox_bytes(self):
        from playwright.sync_api import expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();helper=deletion.LocalDeleteTests();helper.fixture(root)
            kept=helper.bytes_of(root,'mail-000000001')
            with self.opened(root) as (page,errors):
                expect(page.locator('#delete-selected')).to_be_disabled()
                page.locator('.item-select[data-id="approval-current"]').check()
                page.locator('.item-select[data-id="mail-000000000"]').check()
                page.locator('#delete-selected').click()
                expect(page.locator('#delete-description')).to_contain_text('결재 1건 · 메일 1건')
                page.locator('#cancel-delete').click()
                page.evaluate("async()=>{const plan=await browserEditor.preview(['approval-current','mail-000000000']);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'})}")
                status=self.wait_job(page);self.assertEqual(status['state'],'done',status)
                target=Path(tmp)/'after';target.mkdir();self.exported(page,target)
                self.assertEqual(errors,[])
            self.assertTrue(verify_archive(target)['ok'])
            self.assertEqual(search(target)['total'],2)
            self.assertEqual(helper.bytes_of(target,'mail-000000001'),kept)
            self.assertFalse((target/'documents/current/document.html').exists())
            self.assertFalse((target/'mail/offline/mail-000000000/index.html').exists())
            self.assertNotIn(b'approval-current',(target/'catalog.sqlite3').read_bytes())
            self.assertNotIn(b'HTML ',(target/'mail/raw/000000.mbox').read_bytes())
            manifest=json.loads((target/'manifest.json').read_text());self.assertEqual(manifest['local_edits']['deleted_mail'],1)
            # The native reader accepts the browser's DB, offsets and hashes.
            from segio_archive.portable_mail import prepare_archive
            prepare_archive(target);self.assertTrue(verify_archive(target)['ok'])

    def test_browser_deletes_bucketed_approval_and_keeps_other_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();deletion.LocalDeleteTests().fixture(root,sharded=True)
            with self.opened(root) as (page,errors):
                page.evaluate("async()=>{const plan=await browserEditor.preview(['approval-current']);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'})}")
                status=self.wait_job(page);self.assertEqual(status['state'],'done',status)
                target=Path(tmp)/'after';target.mkdir();self.exported(page,target)
                self.assertEqual(errors,[])
            self.assertFalse((target/'documents/b-01/current/document.html').exists())
            self.assertFalse((target/'documents/b-01/current/attachments/한글 #첨부.txt').exists())
            self.assertTrue((target/'documents/b-01/old/document.html').exists())
            self.assertTrue(verify_archive(target)['ok'])

    def test_confirmation_button_deletes_and_reloads_the_same_index(self):
        from playwright.sync_api import expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();deletion.LocalDeleteTests().fixture(root)
            target=Path(tmp)/'after';target.mkdir()
            with self.opened(root) as (page,errors):
                dialogs=[];page.on('dialog',lambda dialog:(dialogs.append(dialog.type),dialog.dismiss()))
                observer=page.context.new_page()
                observer.route('**/*',lambda route:route.fulfill(content_type='text/html',body='<!doctype html>'))
                observer.goto(page.url.rsplit('/',1)[0]+'/observer.html')
                observer.evaluate("async()=>{testRoot=await(await navigator.storage.getDirectory()).getDirectoryHandle('backup')}")
                def reloaded(route):
                    self.exported(observer,target)
                    route.fulfill(content_type='text/html; charset=utf-8',body=(target/'index.html').read_bytes().replace(b"offline=location.protocol==='file:'",b'offline=true'))
                page.route('**/index.html',reloaded)
                page.route('**/search/*.js',lambda route:route.fulfill(content_type='text/javascript',body=(target/'search'/route.request.url.rsplit('/',1)[1]).read_bytes()))
                page.locator('.item-select[data-id="approval-current"]').check()
                page.locator('.item-select[data-id="mail-000000000"]').check()
                page.locator('#delete-selected').click();page.locator('#confirm-delete').click()
                expect(page.locator('#tab-all')).to_have_text('전체 (2건)',timeout=15000)
                expect(page.locator('.data-row')).to_have_count(2)
                expect(page.locator('#delete-selected')).to_have_text('삭제')
                self.assertEqual(dialogs,[])
                self.assertTrue(verify_archive(target)['ok']);self.assertEqual(errors,[])

    def test_cancel_and_failed_commit_restore_all_original_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();helper=deletion.LocalDeleteTests();helper.fixture(root);before=helper.snapshot(root)
            with self.opened(root) as (page,errors):
                page.evaluate("""async()=>{const saved=browserEditor.progress.bind(browserEditor);browserEditor.progress=(stage,n,t)=>{saved(stage,n,t);if(stage==='mail')browserEditor.cancelled=true};const plan=await browserEditor.preview(['mail-000000000']);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'})}""")
                status=self.wait_job(page);self.assertEqual(status['state'],'failed');self.assertFalse(status['recovery_required'])
                target=Path(tmp)/'cancelled';target.mkdir();self.exported(page,target);helper.assert_snapshot(target,before)
                page.evaluate("""async()=>{
                    browserEditor=await SegioBrowserEditor.open(testRoot,config);
                    const original=testRoot.getFileHandle.bind(testRoot);let once=true;
                    const proxy=new Proxy(testRoot,{get(target,key){if(key==='getFileHandle')return async(name,options)=>{const handle=await original(name,options);if(name==='catalog.sqlite3')return new Proxy(handle,{get(file,key){if(key==='createWritable')return async options=>{if(once){once=false;throw new DOMException('synthetic','QuotaExceededError')}return file.createWritable(options)};const value=Reflect.get(file,key,file);return typeof value==='function'?value.bind(file):value}});return handle};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value}});
                    browserEditor.root=proxy;const plan=await browserEditor.preview(['approval-current','mail-000000000']);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'});
                }""")
                status=self.wait_job(page);self.assertEqual(status['state'],'failed',status);self.assertFalse(status['recovery_required'],status)
                target=Path(tmp)/'failed';target.mkdir();self.exported(page,target);helper.assert_snapshot(target,before)
                self.assertEqual(errors,[])

    def test_native_edit_refuses_pending_browser_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);deletion.LocalDeleteTests().fixture(root);(root/'.segio-browser-edit').mkdir()
            with self.assertRaisesRegex(CollectionError,'브라우저'):
                recover(root)

    def test_shared_attachments_last_reference_and_entire_mailbox_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();helper=deletion.LocalDeleteTests();helper.fixture(root,shared=True)
            shared=json.loads((root/'catalog.json').read_text())['items'][0]['attachments'][0]['path']
            with self.opened(root) as (page,errors):
                for selected in (['approval-current'],['approval-old'],['mail-000000000','mail-000000001']):
                    page.evaluate("""async ids=>{
                        if(!browserEditor.db){const text=await(await(await testRoot.getFileHandle('index.html')).getFile()).text();const config=JSON.parse(new DOMParser().parseFromString(text,'text/html').querySelector('#archive-config').textContent);browserEditor=await SegioBrowserEditor.open(testRoot,config)}
                        const plan=await browserEditor.preview(ids);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'});
                    }""",selected)
                    status=self.wait_job(page);self.assertEqual(status['state'],'done',status)
                    target=Path(tmp)/str(len(selected))/'-'.join(selected);target.mkdir(parents=True);self.exported(page,target)
                    self.assertTrue(verify_archive(target)['ok'])
                    if selected==['approval-current']:self.assertTrue((target/shared).is_file())
                    else:self.assertFalse((target/shared).exists())
                self.assertFalse((target/'mail/raw/000000.mbox').exists())
                self.assertEqual(search(target)['total'],0)
                self.assertEqual(errors,[])

    def test_interrupted_recovery_can_be_completed_from_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();helper=deletion.LocalDeleteTests();helper.fixture(root);before=helper.snapshot(root)
            with self.opened(root) as (page,errors):
                page.evaluate("""async()=>{
                    const proxy=new Proxy(testRoot,{get(target,key){if(key==='getFileHandle')return async(name,options)=>{const handle=await target.getFileHandle(name,options);if(name==='catalog.sqlite3')return new Proxy(handle,{get(file,key){if(key==='createWritable')return async()=>{throw new DOMException('synthetic','QuotaExceededError')};const value=Reflect.get(file,key,file);return typeof value==='function'?value.bind(file):value}});return handle};const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value}});
                    browserEditor.root=proxy;const plan=await browserEditor.preview(['approval-current','mail-000000000']);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'});
                }""")
                status=self.wait_job(page);self.assertEqual(status['state'],'failed');self.assertTrue(status['recovery_required'])
                self.assertTrue(page.evaluate('async()=>(await SegioBrowserEditor.open(testRoot,config)).recovered'))
                target=Path(tmp)/'recovered';target.mkdir();self.exported(page,target);helper.assert_snapshot(target,before)
                self.assertTrue(page.evaluate("async()=>{const work=await testRoot.getDirectoryHandle('.segio-browser-edit',{create:true});await work.getFileHandle('journal.json',{create:true});return (await SegioBrowserEditor.open(testRoot,config)).recovered}"))
                message=page.evaluate("async()=>{try{await SegioBrowserEditor.open(testRoot,{...config,archive:{...config.archive,browser_edit_id:'wrong-folder'}});return ''}catch(error){return error.message}}")
                self.assertIn('다릅니다',message)
                self.assertTrue(page.evaluate('async()=>Boolean(await SegioBrowserEditor.remembered(config.archive.browser_edit_id))'))
                self.assertFalse(page.evaluate("async()=>{const old=location.href;history.replaceState(null,'','/copied/index.html');try{return Boolean(await SegioBrowserEditor.remembered(config.archive.browser_edit_id))}finally{history.replaceState(null,'',old)}}"))
                self.assertEqual(errors,[])

    def test_mbox_reads_are_bounded_and_kept_large_message_bytes_are_exact(self):
        from segio_archive.compact_mail import digest_file
        from segio_archive.portable_mail import prepare_archive
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'before';root.mkdir();deletion.LocalDeleteTests().fixture(root)
            name='mail/raw/000000.mbox';envelope=b'From synthetic@example.test Mon Nov 02 10:00:00 2026\n'
            first=b'Subject: remove\n\n'+b'a'*(2*1024*1024)+b'\n'
            second=b'Subject: keep\n\n'+b'b'*(2*1024*1024)+b'\n'
            (root/name).write_bytes(envelope+first+envelope+second);split=len(envelope)+len(first)
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                db.execute('UPDATE mail_records SET begin=?,end=? WHERE id=?',(len(envelope),split,'mail-000000000'))
                db.execute('UPDATE mail_records SET begin=?,end=? WHERE id=?',(split+len(envelope),(root/name).stat().st_size,'mail-000000001'));db.commit()
            manifest=json.loads((root/'manifest.json').read_text());manifest['files'][name]=digest_file(root/name)
            sources=json.loads((root/'mail/sources.json').read_text());sources['sources'][0].update(manifest['files'][name]);(root/'mail/sources.json').write_text(json.dumps(sources))
            for changed in ('catalog.sqlite3','mail/sources.json'):manifest['files'][changed]=digest_file(root/changed)
            manifest.pop('portable_mail');manifest['browser_index']='';(root/'manifest.json').write_text(json.dumps(manifest));prepare_archive(root)
            with self.opened(root) as (page,errors):
                page.evaluate("""async()=>{
                    const slice=Blob.prototype.slice;window.maxRawSlice=0;
                    Blob.prototype.slice=function(start=0,end=this.size,...rest){if(this instanceof File&&this.name==='000000.mbox'){maxRawSlice=Math.max(maxRawSlice,end-start);if(end-start>1024*1024)throw Error('unbounded read')}return slice.call(this,start,end,...rest)};
                    const plan=await browserEditor.preview(['mail-000000000']);await browserEditor.request('start',{plan:plan.plan,confirmation:'delete-selected'});
                }""")
                status=self.wait_job(page);self.assertEqual(status['state'],'done',status)
                self.assertEqual(page.evaluate('maxRawSlice'),1024*1024)
                target=Path(tmp)/'after';target.mkdir();self.exported(page,target)
                self.assertEqual((target/name).read_bytes(),envelope+second)
                self.assertTrue(verify_archive(target)['ok']);self.assertEqual(errors,[])

    def test_direct_file_enables_deletion_and_cancelled_picker_keeps_viewing(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);deletion.LocalDeleteTests().fixture(root)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,executable_path=fixtures.BROWSER)
                try:
                    page=browser.new_page();page.add_init_script("window.showDirectoryPicker=async()=>{window.pickerUsed=true;throw new DOMException('cancel','AbortError')}")
                    fixtures.navigate(self,page,(root/'index.html').as_uri())
                    expect(page.locator('#delete-selected')).to_be_enabled();page.locator('#delete-selected').click()
                    expect(page.locator('#delete-message')).to_contain_text('폴더 선택을 취소')
                    self.assertTrue(page.evaluate('window.pickerUsed'));expect(page.locator('.data-row')).to_have_count(4)
                    expect(page.locator('.item-select:visible')).to_have_count(0)
                finally:browser.close()
