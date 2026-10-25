from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer


BROWSER = os.environ.get('SEGIO_TEST_BROWSER') or shutil.which('chromium')


def navigate(test, page, url):
    from playwright.sync_api import Error
    try:
        page.goto(url)
    except Error as exc:
        # The managed cloud Chromium blocks all file URLs. Actual file://
        # tests still run with the unrestricted Playwright browser in CI.
        if url.startswith('file:') and 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):
            test.skipTest('Managed browser policy blocks file:// URLs')
        raise


class SearchPageTests(unittest.TestCase):
    def card_fixture(self, root):
        from test_portable_mail import PortableMailTests
        from segio_archive.portable_mail import prepare_archive
        manifest=PortableMailTests().fixture(root)
        catalog={'items':[]}
        for identity,source in (('current','active'),('old','history')):
            directory='documents/'+identity
            files={directory+'/document.html':'<p>결재 본문 '+identity+'</p>',
                   directory+'/database.json':'{"synthetic":true}',
                   directory+'/한글 #첨부.txt':'SYNTHETIC_APPROVAL_ATTACHMENT'}
            for name,text in files.items():
                (root/name).parent.mkdir(parents=True,exist_ok=True)
                (root/name).write_text(text,encoding='utf-8')
                manifest['files'][name]=digest_file(root/name)
            catalog['items'].append({'id':identity,'source':source,'title':'결재 '+identity,
                'path':directory+'/document.html','json':directory+'/database.json',
                'attachments':[{'name':'한글 #첨부.txt','path':directory+'/한글 #첨부.txt'},
                               {'name':'unsafe','path':'../../secret'}]})
        (root/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
        manifest['files']['catalog.json']=digest_file(root/'catalog.json')
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            for item in catalog['items']:
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                           ('approval-'+item['id'],'approval',item['title'],'결재 문서','합성 검색 본문',item['path']))
            db.commit()
        manifest['files']['catalog.sqlite3']=digest_file(root/'catalog.sqlite3')
        manifest.update(user='synthetic',server='cell_000',finished_at='2026-10-25T01:02:03Z',
            state='collected_with_warnings',coverage=[{'kind':'approval','saved':2}],
            warnings=['합성 자료 확인 사항'],snapshot_rows={'approval_document':1})
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        prepare_archive(root)
        return json.loads((root/'manifest.json').read_text(encoding='utf-8'))

    def test_cards_restore_original_db_and_attachment_links_without_reading_original_files(self):
        from segio_archive.portable_mail import prepare_archive
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            manifest=self.card_fixture(root)
            # Simulate static-mail/1 metadata made by an earlier application.
            state_path=root/'mail/offline/mail-000000000/state.json'
            state=json.loads(state_path.read_text(encoding='utf-8'))
            state.pop('attachments')
            state_path.write_text(json.dumps(state),encoding='utf-8')
            manifest['files'][state_path.relative_to(root).as_posix()]=digest_file(state_path)
            manifest['browser_index']='offline-search/3'
            (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
            original_open=Path.open
            def guarded(path,*args,**kwargs):
                if path.suffix=='.mbox' or 'attachments' in path.parts or path.name=='한글 #첨부.txt':
                    self.fail('A presentation update must not read original mail or attachments')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',guarded):
                prepare_archive(root)
                approval=search(root,kind='approval',version='active')
                history=search(root,kind='approval',version='history')
                mail=search(root,kind='mail')
            self.assertEqual(approval['total'],1)
            self.assertEqual(history['items'][0]['presentation']['source'],'history')
            detail=approval['items'][0]['presentation']
            self.assertEqual(detail['json'],'documents/current/database.json')
            self.assertEqual(detail['attachments'],[{'name':'한글 #첨부.txt','path':'documents/current/한글 #첨부.txt'}])
            self.assertEqual(mail['items'][0]['presentation']['raw'],'mail/raw/000000.mbox')
            self.assertEqual(len(mail['items'][0]['presentation']['attachments']),2)
            rows=json.loads((root/'search/part-000000.js').read_text(encoding='utf-8').removeprefix('window.archiveChunk=').removesuffix(';'))
            by_id={row[0]:row for row in rows}
            for item in approval['items']+history['items']+mail['items']:
                self.assertEqual(by_id[item['id']][8],item['presentation'])
            updated=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            for name,value in manifest['files'].items():
                if name!='index.html' and not name.startswith('search/'):
                    self.assertEqual(updated['files'][name],value)
                    self.assertEqual(digest_file(root/name),value)
            self.assertTrue(verify_archive(root)['ok'])

    @unittest.skipUnless(BROWSER,'Real Chromium required')
    def test_card_actions_owner_history_and_mail_headers_match_in_http_and_file_views(self):
        from playwright.sync_api import sync_playwright,expect
        from urllib.parse import unquote
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.card_fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for url in (viewer.url,(root/'index.html').as_uri()):
                            with self.subTest(url=url):
                                page=browser.new_page()
                                navigate(self,page,url)
                                expect(page.locator('.data-row')).to_have_count(4)
                                page.locator('#tab-approval').click()
                                expect(page.locator('.data-row')).to_have_count(2)
                                expect(page.locator('#owner')).to_have_text('대상 ID: synthetic · 서버: cell_000')
                                expect(page.locator('#backup-time')).to_contain_text('KST')
                                page.get_by_text('수집 범위·검증 내역 및 추가 옵션',exact=True).click()
                                expect(page.locator('#coverage')).to_contain_text('합성 자료 확인 사항')
                                page.locator('#version').select_option('history')
                                expect(page.locator('.data-row')).to_have_count(1)
                                expect(page.locator('.record .meta')).to_contain_text('과거 기록 · old')
                                page.locator('#record-tools > summary').click()
                                link=page.get_by_role('link',name='원본 DB 기록',exact=True)
                                self.assertEqual(unquote(link.get_attribute('href')),'documents/old/database.json')
                                with page.expect_popup() as opened:
                                    link.click()
                                popup=opened.value
                                expect(popup.locator('body')).to_contain_text('synthetic')
                                popup.close()
                                attachment=page.get_by_role('link',name='첨부: 한글 #첨부.txt',exact=True)
                                self.assertEqual(unquote(attachment.get_attribute('href')),'documents/old/한글 #첨부.txt')
                                page.locator('#tab-mail').click()
                                expect(page.locator('.data-row')).to_have_count(2)
                                expect(page.locator('#version')).to_be_hidden()
                                expect(page.locator('.record .meta').first).to_contain_text('보낸 사람: sender@example.test')
                                expect(page.get_by_role('link',name='원본 MBOX',exact=True)).to_have_count(2)
                                expect(page.get_by_role('link',name='첨부: ../../CON.txt',exact=True)).to_have_count(1)
                                page.locator('#tab-approval').click()
                                page.locator('#reset').click()
                                expect(page.locator('.data-row')).to_have_count(2)
                                self.assertEqual(page.locator('body').evaluate('node=>getComputedStyle(node).fontSize'),'12px')
                                page.close()
                    finally:browser.close()
            finally:viewer.close()

    def fixture(self, root, approvals=True):
        (root/'index.html').write_text('Old search page', encoding='utf-8')
        (root/'mail').mkdir()
        (root/'mail/original.mbox').write_bytes(b'ORIGINAL_MBOX_MUST_NOT_BE_READ_OR_CHANGED')
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE items (id TEXT PRIMARY KEY,kind TEXT,title TEXT,folders TEXT,body TEXT,html TEXT)')
            if approvals:
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                           ('approval-1','approval','결재 첫 문서','결재 문서','본문 찾기','documents/1/index.html'))
            db.executemany('INSERT INTO items VALUES (?,?,?,?,?,?)',
                [(f'mail-{i:09d}','mail',f'메일 {i:04d}', '받은 편지함',
                  'sender@example.test\nreceiver@example.test','mail/'+f'mail-{i:09d}') for i in range(1101)])
            db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                       ('mail-000001101','mail','</script><img src="https://external.invalid/pixel"> 100%_한글',
                        '보낸 편지함','sender@example.test','mail/mail-000001101'))
            db.commit()
        manifest={'viewer':'sqlite-mail/1','counts':{'mail':1102},'files':{}}
        for name in ('index.html','catalog.sqlite3','mail/original.mbox'):
            manifest['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        return manifest

    def test_existing_backup_is_upgraded_without_reading_mail_or_changing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old=self.fixture(root)
            original_open=Path.open
            def guarded_open(path,*args,**kwargs):
                if path==root/'mail/original.mbox':
                    self.fail('A display update must not read the original MBOX')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',guarded_open):
                viewer=Viewer(root)
                viewer.close()
            updated=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(updated['browser_index'],'offline-search/5')
            self.assertEqual(updated['files']['mail/original.mbox'],old['files']['mail/original.mbox'])
            self.assertEqual(digest_file(root/'catalog.sqlite3'),old['files']['catalog.sqlite3'])
            self.assertTrue(verify_archive(root)['ok'])
            self.assertEqual(len(list((root/'search').glob('part-*.js'))),3)
            before=(root/'index.html').stat().st_mtime_ns
            install_search_page(root)
            self.assertEqual((root/'index.html').stat().st_mtime_ns,before)

    def test_modified_old_index_is_not_overwritten_or_declared_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            (root/'index.html').write_text('modified',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'무결성'):
                install_search_page(root)
            self.assertFalse(verify_archive(root)['ok'])
            self.assertEqual((root/'index.html').read_text(encoding='utf-8'),'modified')

    @unittest.skipUnless(BROWSER,'Chromium is required for real browser archive search')
    def test_direct_file_and_local_viewer_search_tabs_pagination_and_untrusted_titles(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as playwright:
                    browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for url in ((root/'index.html').as_uri(),viewer.url):
                            with self.subTest(url=url):
                                page=browser.new_page()
                                requests=[]
                                page.on('request',lambda request:requests.append(request.url))
                                navigate(self,page,url)
                                page.locator('#tab-approval').click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                page.locator('#tab-mail').click()
                                expect(page.locator('.data-row')).to_have_count(100)
                                expect(page.locator('#summary')).to_contain_text('1,102건')
                                expect(page.locator('#page')).to_have_text('1 / 12')
                                page.locator('#next').click()
                                expect(page.locator('#page')).to_have_text('2 / 12')
                                expect(page.locator('.data-row .item-title').first).to_have_text('메일 0100')
                                page.locator('#q').fill('100%_한글')
                                page.locator('#search-button').click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                expect(page.locator('.data-row')).to_contain_text('</script><img')
                                self.assertEqual(page.locator('.data-row img').count(),0)
                                page.locator('#q').fill('sender@example.test')
                                page.locator('#q').press('Enter')
                                expect(page.locator('#summary')).to_contain_text('1,102건')
                                page.locator('#tab-approval').click()
                                expect(page.locator('#empty')).to_have_text('검색 결과가 없습니다.')
                                page.locator('#reset').click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                page.locator('#q').fill('본문 찾기')
                                page.locator('#q').press('Enter')
                                expect(page.locator('.data-row')).to_have_count(1)
                                self.assertEqual(page.locator('body').evaluate('element=>getComputedStyle(element).fontSize'),'12px')
                                self.assertTrue(all(request.startswith(('file:',viewer.url.rsplit('/',1)[0])) for request in requests))
                                page.close()
                    finally:
                        browser.close()
            finally:
                viewer.close()

    @unittest.skipUnless(BROWSER,'Chromium is required for real browser archive search')
    def test_mail_only_backup_opens_mail_list_without_clicking_a_tab(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root,approvals=False)
            install_search_page(root)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True,executable_path=BROWSER)
                try:
                    page=browser.new_page()
                    navigate(self,page,(root/'index.html').as_uri())
                    expect(page.locator('#tab-all')).to_have_attribute('aria-selected','true')
                    expect(page.locator('.data-row')).to_have_count(100)
                finally:
                    browser.close()
