from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.approval_folders import describe, draft_date
from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file, search
from segio_archive.portable_mail import prepare_archive
from segio_archive.viewer import Viewer
import test_portable_mail as portable_fixture
from test_search_page import navigate

BROWSER=portable_fixture.BROWSER


class ApprovalFolderTests(unittest.TestCase):
    def fixture(self,root):
        manifest=portable_fixture.PortableMailTests().fixture(root)
        catalog={'items':[]}
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('CREATE TABLE document_records (id TEXT PRIMARY KEY,payload TEXT)')
            for number,state in enumerate(['complete']*205+['cancel','progress','standby','unexpected','complete']):
                identity=str(number+1)
                directory='documents/'+identity
                source='history' if number==209 else 'active'
                document={'progress':state,'wdate':'1792550396','reference':'employee@@직원' if number==0 else 'employee2@@다른직원',
                          'doc_recv_uid':'employee' if number==1 else '','uid':'other'}
                related={}
                if number==206:
                    related={'approval_send_box':[{'recv_user':'employee','recv_uid':'','doc_uid':'other'}]}
                if number==208:
                    related={'approval_send_box':[{'recv_user':'employee2','doc_uid':'employee'}]}
                if number==209:
                    related={'approval_line_backup':[{'uid':'employee','division':'reference'}]}
                payload=json.dumps({'source':source,'document':document,'related':related},ensure_ascii=False)
                (root/directory).mkdir(parents=True)
                (root/directory/'document.html').write_text('<meta charset="utf-8"><p>합성 결재 본문</p>',encoding='utf-8')
                (root/directory/'database.json').write_text(payload,encoding='utf-8')
                for name in (directory+'/document.html',directory+'/database.json'):
                    manifest['files'][name]=digest_file(root/name)
                item={'id':identity,'title':'기안 %03d'%number,'source':source,
                      'path':directory+'/document.html','json':directory+'/database.json','attachments':[]}
                catalog['items'].append(item)
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',
                           ('approval-'+identity,'approval',item['title'],'결재 문서','공통 합성 내용',item['path']))
                db.execute('INSERT INTO document_records VALUES (?,?)',('approval-'+identity,payload))
            db.execute("UPDATE mail_records SET date='Thu, 22 Oct 2026 15:39:00 +0900' WHERE id='mail-000000000'")
            db.execute("UPDATE mail_records SET date='original malformed date' WHERE id='mail-000000001'")
            db.commit()
        (root/'catalog.json').write_text(json.dumps(catalog,ensure_ascii=False),encoding='utf-8')
        for name in ('catalog.sqlite3','catalog.json'):
            manifest['files'][name]=digest_file(root/name)
        manifest.update(user='employee',server='cell_000',finished_at='2026-10-26T00:00:00Z')
        (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        prepare_archive(root)
        return json.loads((root/'manifest.json').read_text(encoding='utf-8'))

    def test_classification_uses_exact_personal_relations_and_never_guesses_unknown_states(self):
        info=describe({'document':{'progress':'complete','reference':'worker2@@다른이름','doc_recv_uid':'worker2'},
                       'related':{'approval_send_box':[{'doc_uid':'worker','recv_user':'worker2'}]}},'worker')
        self.assertEqual(info['approval_folders'],['complete'])
        self.assertEqual(describe({'document':{'progress':'unknown','reference':'worker@@직원'}},'worker')['approval_folders'],['unknown','reference'])
        self.assertEqual(describe({'document':{'progress':'standby'}},'worker')['approval_label'],'임시문서')
        self.assertEqual(describe({'document':{'progress':'after'}},'worker')['approval_label'],'상태 미확인')
        self.assertEqual(draft_date('1792550396'),'2026-10-21 11:39:56 KST')
        self.assertEqual(draft_date(None),'')

    def test_counts_filter_dates_and_existing_upgrade_preserve_all_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old=self.fixture(root)
            old['browser_index']='offline-search/4'
            (root/'manifest.json').write_text(json.dumps(old),encoding='utf-8')
            original_open=Path.open
            def guarded(path,*args,**kwargs):
                if path.suffix=='.mbox' or 'attachments' in path.parts:
                    self.fail('A table/status upgrade must not reread MBOX or attachments')
                return original_open(path,*args,**kwargs)
            with patch.object(Path,'open',guarded):
                prepare_archive(root)
                self.assertEqual(search(root)['total'],212)
                for folder,count in {'complete':206,'cancel':1,'reference':2,'receive':2,'progress':1,'draft':1,'unknown':1}.items():
                    self.assertEqual(search(root,kind='approval',approval_folder=folder)['total'],count)
                self.assertEqual(search(root,'완료문서','approval')['total'],206)
                self.assertEqual(search(root,kind='approval',approval_folder='complete',version='history')['total'],1)
                self.assertEqual(search(root,kind='approval',approval_folder="' OR 1=1 --")['total'],0)
                self.assertEqual(search(root,kind='mail')['items'][0]['presentation']['date_display'],'2026-10-22 15:39 +0900')
                self.assertEqual(search(root,kind='mail')['items'][1]['presentation']['date_display'],'original malformed date')
            updated=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
            for name,value in old['files'].items():
                if name!='index.html' and not name.startswith('search/'):
                    self.assertEqual(updated['files'][name],value)
                    self.assertEqual(digest_file(root/name),value)
            self.assertTrue(verify_archive(root)['ok'])

    @unittest.skipUnless(BROWSER,'Real Chromium required')
    def test_table_scope_status_search_dates_pagination_and_reload_in_http_and_file(self):
        from playwright.sync_api import sync_playwright,expect
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.fixture(root)
            viewer=Viewer(root)
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True,executable_path=BROWSER)
                    try:
                        for url in (viewer.url,(root/'index.html').as_uri()):
                            with self.subTest(url=url):
                                page=browser.new_page()
                                navigate(self,page,url)
                                expect(page.locator('.data-row')).to_have_count(100)
                                expect(page.locator('#tab-all')).to_have_text('전체 (212건)')
                                expect(page.locator('#date-label')).to_have_text('기안일자 / 메일 헤더 날짜')
                                expect(page.locator('#approval-folders')).to_be_hidden()
                                expect(page.locator('#mail-folders')).to_be_hidden()
                                page.locator('#kind').select_option('approval')
                                expect(page.locator('#tab-approval')).to_have_attribute('aria-selected','true')
                                page.get_by_role('button',name='반려문서 (1건)',exact=True).click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                expect(page.locator('.item-date')).to_have_text('2026-10-21 11:39:56 KST')
                                expect(page.locator('.mailbox-name')).to_have_text('반려문서')
                                page.locator('#q').fill('공통')
                                page.locator('#search-button').click()
                                expect(page.locator('.data-row')).to_have_count(1)
                                page.get_by_role('button',name='완료문서 (206건)',exact=True).click()
                                expect(page.locator('#summary')).to_contain_text('206건')
                                page.locator('#next').click()
                                expect(page.locator('#page')).to_have_text('2 / 3')
                                expect(page.locator('.data-row')).to_have_count(100)
                                page.locator('#next').click()
                                expect(page.locator('.data-row')).to_have_count(6)
                                page.get_by_role('button',name='참조문서 (2건)',exact=True).click()
                                expect(page.locator('.data-row')).to_have_count(2)
                                page.reload()
                                expect(page.locator('.data-row')).to_have_count(2)
                                expect(page.get_by_role('button',name='참조문서 (2건)',exact=True)).to_have_attribute('aria-pressed','true')
                                page.locator('#kind').select_option('mail')
                                expect(page.locator('.data-row')).to_have_count(2)
                                expect(page.locator('#date-label')).to_have_text('메일 헤더 날짜')
                                expect(page.locator('.item-date').first).to_have_text('2026-10-22 15:39 +0900')
                                page.get_by_role('button',name='보낸편지함 (0건)',exact=True).click()
                                expect(page.locator('#empty')).to_have_text('이 종류의 백업 자료가 없습니다.')
                                page.get_by_role('button',name='받은편지함 (2건)',exact=True).click()
                                page.locator('#q').fill('sender@example.test')
                                page.locator('#q').press('Enter')
                                expect(page.locator('.data-row')).to_have_count(2)
                                page.locator('#kind').select_option('all')
                                expect(page.locator('.data-row')).to_have_count(2)
                                page.locator('#tab-approval').click()
                                expect(page.locator('#empty')).to_have_text('검색 결과가 없습니다.')
                                page.locator('#reset').click()
                                expect(page.locator('#summary')).to_contain_text('210건')
                                page.locator('#tab-all').click()
                                expect(page.locator('#summary')).to_contain_text('212건')
                                self.assertEqual(page.locator('body').evaluate('node=>getComputedStyle(node).fontSize'),'12px')
                                page.close()
                    finally:browser.close()
            finally:viewer.close()
