from contextlib import closing
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
from segio_archive.dates import approval_timestamp, text_timestamp
from segio_archive.pagination import validate_page_size
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer
import test_search_page as fixtures


class ListOptionTests(unittest.TestCase):
    def fixture(self,root):
        manifest=fixtures.SearchPageTests().fixture(root)
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY,payload TEXT)')
            stamp=int(datetime(2026,10,26,15,30,tzinfo=timezone.utc).timestamp())
            db.execute('INSERT INTO document_records VALUES (?,?)',('approval-1',json.dumps(
                {'source':'active','document':{'progress':'complete','wdate':str(stamp)}})))
            db.execute('CREATE TABLE mail_records (id TEXT PRIMARY KEY,source TEXT,begin INTEGER,end INTEGER,sender TEXT,receiver TEXT,date TEXT)')
            for number in range(1102):
                date=(datetime(2026,10,1,tzinfo=timezone.utc)+timedelta(minutes=number)).strftime('%a, %d %b %Y %H:%M:%S %z')
                if number==1098:
                    date='Thu, 01 Oct 2026 18:19:00 +0000'
                if number in (3,4):
                    date='' if number==3 else 'unparseable date'
                if number==1100:
                    date='Tue, 27 Oct 2026 00:00:00 +0900'
                if number==1101:
                    date='Mon, 26 Oct 2026 16:00:00 +0000'
                db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',
                           (f'mail-{number:09d}','mail/original.mbox',0,1,'sender@example.test','receiver@example.test',date))
            db.commit()
        manifest['files']['catalog.sqlite3']=digest_file(root/'catalog.sqlite3')
        manifest['browser_index']='offline-search/5'
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        install_search_page(root)
        return manifest

    def test_dates_compare_timezones_and_unknown_dates_have_no_artificial_timestamp(self):
        self.assertGreater(text_timestamp('Mon, 26 Oct 2026 16:00:00 +0000'),
                           text_timestamp('Tue, 27 Oct 2026 00:00:00 +0900'))
        self.assertEqual(text_timestamp('2026-10-27 00:30:00 KST'),
                         text_timestamp('Mon, 26 Oct 2026 15:30:00 +0000'))
        self.assertIsNone(text_timestamp('unparseable date'))
        self.assertIsNone(approval_timestamp('0'))
        self.assertIsNone(approval_timestamp('9'*100))
        for size in (1,25,50,75,100,37,1000,' 37 '):
            self.assertEqual(validate_page_size(size),int(size))
        for size in (0,-1,1001,1.5,'1.5','1e2','',None,True,"1 OR 1=1"):
            with self.subTest(size=size),self.assertRaises(ValueError):
                validate_page_size(size)

    def test_global_date_order_chunk_boundaries_and_variable_pages_preserve_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            original=self.fixture(root)
            original_open=Path.open
            def guarded(path,*args,**kwargs):
                if path==root/'mail/original.mbox':
                    self.fail('Sorting and changing page size must not read the MBOX')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',guarded):
                manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
                manifest['browser_index']='offline-search/5'
                (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
                install_search_page(root)
                rows=[]
                for path in sorted((root/'search').glob('part-*.js')):
                    rows.extend(json.loads(path.read_text(encoding='utf-8').removeprefix('window.archiveChunk=').removesuffix(';')))
                ids=[row[0] for row in rows]
                self.assertEqual(ids[:5],['mail-000001101','approval-1','mail-000001100','mail-000001098','mail-000001099'])
                self.assertEqual(ids[-2:],['mail-000000003','mail-000000004'])
                for size in (25,50,75,100,37,1000):
                    pages=[]
                    for page in range((1103+size-1)//size):
                        result=search(root,page=page,page_size=size)
                        self.assertEqual(result['total'],1103)
                        self.assertLessEqual(len(result['items']),size)
                        pages.extend(item['id'] for item in result['items'])
                    self.assertEqual(pages,ids)
                self.assertEqual(search(root,page=1102,page_size=1)['items'][0]['id'],ids[-1])
                self.assertEqual(search(root,kind='mail',page_size=25)['items'][0]['id'],'mail-000001101')
            self.assertEqual(digest_file(root/'catalog.sqlite3'),original['files']['catalog.sqlite3'])
            self.assertEqual(digest_file(root/'mail/original.mbox'),original['files']['mail/original.mbox'])
            self.assertTrue(verify_archive(root)['ok'])
            viewer=Viewer(root)
            try:
                connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
                try:
                    for size in ('0','1001','1.5','-1'):
                        connection.request('GET','/'+viewer.token+'/api/search?page_size='+size)
                        response=connection.getresponse()
                        self.assertEqual(response.status,400)
                        response.read()
                finally:connection.close()
            finally:viewer.close()

    @unittest.skipUnless(fixtures.BROWSER,'Real Chromium required')
    def test_browser_presets_manual_entry_search_and_reload_in_http_and_file(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=fixtures.BROWSER)
                    try:
                        for url in (viewer.url,(root/'index.html').as_uri()):
                            with self.subTest(url=url):
                                page=browser.new_page()
                                fixtures.navigate(self,page,url)
                                expect(page.locator('.data-row')).to_have_count(100)
                                self.assertTrue(page.locator('.data-row a').first.get_attribute('href').endswith('mail-000001101'))
                                self.assertEqual(page.locator('#page-size-preset option').evaluate_all('(options)=>options.map(option=>option.value)'),['25','50','75','100','custom'])
                                for size in (25,50,75,100):
                                    page.locator('#page-size-preset').select_option(str(size))
                                    expect(page.locator('.data-row')).to_have_count(size)
                                    expect(page.locator('#page')).to_have_text(f'1 / {(1103+size-1)//size}')
                                    page.locator('#next').click()
                                    expect(page.locator('.data-row td').first).to_have_text(str(size+1))
                                    page.locator('#prev').click()
                                    expect(page.locator('.data-row td').first).to_have_text('1')
                                page.locator('#page-size-preset').select_option('custom')
                                page.locator('#page-size').fill('37')
                                page.locator('#apply-page-size').click()
                                expect(page.locator('.data-row')).to_have_count(37)
                                expect(page.locator('#page')).to_have_text('1 / 30')
                                page.locator('#next').click()
                                expect(page.locator('.data-row td').first).to_have_text('38')
                                page.locator('#tab-mail').click()
                                expect(page.locator('.data-row')).to_have_count(37)
                                expect(page.locator('#page')).to_have_text('1 / 30')
                                page.get_by_role('button',name='보낸편지함 (1건)',exact=True).click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                page.locator('#reset').click()
                                page.locator('#tab-all').click()
                                page.reload()
                                expect(page.locator('.data-row')).to_have_count(37)
                                expect(page.locator('#page-size')).to_have_value('37')
                                expect(page.locator('#page-size-preset')).to_have_value('custom')
                                for invalid in ('0','-1','1.5','1001',''):
                                    page.locator('#page-size').fill(invalid)
                                    page.locator('#apply-page-size').click()
                                    expect(page.locator('#page-size-error')).to_contain_text('정수')
                                    expect(page.locator('.data-row')).to_have_count(37)
                                page.locator('#page-size').fill('25')
                                page.locator('#page-size').press('Enter')
                                expect(page.locator('.data-row')).to_have_count(25)
                                expect(page.locator('#page-size-error')).to_have_text('')
                                page.locator('#q').fill('100%_한글')
                                page.locator('#search-button').click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                self.assertEqual(page.locator('.data-row img').count(),0)
                                page.close()
                    finally:browser.close()
            finally:viewer.close()
