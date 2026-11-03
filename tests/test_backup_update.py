from contextlib import closing
from datetime import datetime
from email.utils import formatdate
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import Mock,patch

from segio_archive.archive import verify_archive
from segio_archive.backup_update import load,save,cutoff,repair_local,resolve_selection
from segio_archive.compact_mail import digest_file,mbox_spans,search
from segio_archive.dates import KST
from segio_archive.ssh_client import SSHCollector,SSHConfig
from segio_archive.web import Cancelled
from ssh_fixture import SSHServer
from test_live_archive import create_fixture
from live_fixtures import synthetic_export
import remote_ssh_export as helper


class BackupUpdateTests(unittest.TestCase):
    def fixture(self,root):
        create_fixture(root);server=SSHServer(root);self.addCleanup(server.close)
        config=SSHConfig('127.0.0.1','root','employee',root/'windows',port=server.port,
            db_config='/files/database.json',cell='/files',mail_template='/files/home/{initial}/{user}/mbox',known_hosts=root/'known_hosts')
        return server,config

    def message(self,title,date,body=b'body'):
        return b'From synthetic@example.test Tue Nov 3 10:00:00 2026\nSubject: '+title.encode()+b'\nDate: '+date.encode()+b'\nContent-Type: text/plain\n\n'+body+b'\n'

    def test_server_delta_ranges_exclude_old_dates_and_keep_unknown_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);mail=root/'employee';mail.mkdir()
            raw=mail/'sentbox.mbox'
            raw.write_bytes(self.message('Old','Mon, 01 Jan 2024 00:00:00 +0000')+
                            self.message('Recent',formatdate(usegmt=True))+self.message('Unknown','invalid')+
                            self.message('Delayed','Mon, 01 Jan 2024 00:00:00 +0000').replace(b'Content-Type:',b'Received: by example.test; '+formatdate(usegmt=True).encode()+b'\nContent-Type:'))
            cutoff_value=int(time.time())-15*86400
            sources,transfers,identities=helper.mail_delta({'user':'employee','mail_template':(root/'{user}').as_posix()},lambda *a,**k:None,cutoff_value)
            self.assertEqual(len(sources),3);self.assertEqual(len(identities),1)
            original=raw.read_bytes()
            for item in transfers:
                chunk=original[item['source_offset']:item['source_offset']+item['bytes']]
                self.assertEqual(hashlib.sha256(chunk).hexdigest(),item['sha256'])
                self.assertGreater(item['source_offset'],0)
                self.assertEqual(item['source_bytes'],len(original))
            self.assertTrue(all(s['format']=='rfc822' and s['folder_format']=='mbox' for s in sources))

    def test_full_then_overlapping_delta_keeps_name_old_ids_and_pages_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config=self.fixture(root)
            raw=root/'home/e/employee/mbox/inbox.mbox'
            raw.write_bytes(self.message('Old','Mon, 01 Jan 2024 00:00:00 +0000',b'x'*(2*1024*1024))+
                            self.message('Recent',formatdate(usegmt=True)))
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                first=SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
                self.assertEqual(first,config.output/'SSH-employee')
                page=first/'mail/offline/mail-000000000/index.html';mtime=page.stat().st_mtime_ns
                source_hash=digest_file(first/'mail/raw/000000.mbox')
                with raw.open('ab') as stream:stream.write(b'\n'+self.message('Added',formatdate(usegmt=True)))
                previous_reads=len(server.reads)
                second=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(second,first)
            self.assertEqual(page.stat().st_mtime_ns,mtime)
            self.assertEqual(digest_file(first/'mail/raw/000000.mbox'),source_hash)
            self.assertEqual(search(first,'',kind='mail')['total'],3)
            self.assertEqual(search(first,'Added',kind='mail')['total'],1)
            transferred=sum(length for path,_,length in server.reads[previous_reads:] if path.endswith('inbox.mbox'))
            self.assertLess(transferred,8192)
            self.assertEqual(load(first/'manifest.json')['incremental']['added_mail'],1)
            self.assertEqual(load(first/'manifest.json')['server_cleanup']['state'],'confirmed')
            self.assertTrue(verify_archive(first)['ok'])
            self.assertEqual(list((root/'jobs').iterdir()),[])
            self.assertFalse((config.output/'SSH-employee-2').exists())

    def test_interrupted_merge_keeps_old_archive_and_resumes_same_server_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config=self.fixture(root)
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                result=SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
                before={name:digest_file(result/name) for name in ('manifest.json','catalog.sqlite3','index.html','mail/raw/000000.mbox')}
                with (root/'home/e/employee/mbox/inbox.mbox').open('ab') as stream:stream.write(self.message('Added',formatdate(usegmt=True)))
                with patch('segio_archive.portable_mail.build_portable_mail',side_effect=Cancelled('Synthetic cancellation')):
                    with self.assertRaises(Cancelled):SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
                for name,expected in before.items():self.assertEqual(digest_file(result/name),expected)
                result=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(server.actions.count('prepare'),2)
            self.assertEqual(search(result,'',kind='mail')['total'],4)
            self.assertTrue(verify_archive(result)['ok'])

    def test_old_numeric_checkpoint_migrates_without_downloading_raw_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config=self.fixture(root)
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                with patch('segio_archive.ssh_client.build_compact_catalog',side_effect=Cancelled('Synthetic interruption')):
                    with self.assertRaises(Cancelled):SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
                identity={key:getattr(config,key) for key in ('host','port','ssh_user','username','db_config','cell','mail_template','approvals','mail')}
                token=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:24]
                old=config.output/'.segio-ssh-cache'/token
                (config.output/'.segio-ssh-cache/employee').rename(old)
                (old/'plan.json').unlink();(old/'identity.json').unlink()
                (config.output/'SSH-employee').rename(old/'contents')
                reads=len(server.reads)
                result=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertEqual(result.name,'SSH-employee')
            self.assertEqual(server.reads[reads:],[])
            self.assertFalse(old.exists());self.assertEqual(server.actions.count('prepare'),1)
            self.assertTrue(verify_archive(result)['ok'])

    def test_offline_repair_finishes_raw_checkpoint_and_defers_verified_server_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();server,config=self.fixture(root)
            with patch.object(helper,'run_export',side_effect=synthetic_export):
                with patch('segio_archive.ssh_client.build_compact_catalog',side_effect=Cancelled('Synthetic interruption')):
                    with self.assertRaises(Cancelled):SSHCollector(config,confirm_host=lambda *_:True).run('SYNTHETIC_SSH_PASSWORD')
                old=config.output/'.segio-ssh-cache'/('a'*24)
                (config.output/'.segio-ssh-cache/employee').rename(old)
                (old/'plan.json').unlink();(old/'identity.json').unlink()
                (config.output/'SSH-employee').rename(old/'contents')
                result=repair_local(old/'contents',origin={'host':config.host,'port':config.port,'ssh_user':config.ssh_user})
                self.assertTrue(verify_archive(result)['ok'])
                self.assertFalse((config.output/'.segio-ssh-cache/employee').exists())
                self.assertEqual(len(list((config.output/'.segio-ssh-cache').glob('cleanup-*.json'))),1)
                config.username='other'
                other=SSHCollector(config).run('SYNTHETIC_SSH_PASSWORD')
            self.assertTrue(verify_archive(other)['ok'])
            self.assertEqual(list((root/'jobs').iterdir()),[])

    def test_cutoff_uses_korean_calendar_completion_day_and_folder_picker_accepts_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'SSH-employee';folder.mkdir()
            save(folder/'manifest.json',{'user':'employee','last_backup_completed_at':'2026-11-03T06:25:00Z'})
            self.assertEqual(cutoff(folder),int(datetime(2026,10,19,tzinfo=KST).timestamp()))
            self.assertEqual(resolve_selection(root,'employee'),folder.resolve())
            save(folder/'manifest.json',{'user':'employee','last_backup_completed_at':'2026-11-03T06:25:00Z',
                                        'backup_completed_by_kind':{'approvals':'2026-10-01T00:00:00Z','mail':'2026-11-03T06:25:00Z'}})
            self.assertLess(cutoff(folder,['approvals','mail']),cutoff(folder,['mail']))
            save(folder/'manifest.json',{'user':'employee','last_backup_completed_at':'2026-11-03T06:25:00Z','ssh_export':{'approvals':True,'mail':False}})
            self.assertIsNone(cutoff(folder,['mail']))

    def test_publish_failure_restores_old_directory_and_journal_can_be_reopened(self):
        from segio_archive.backup_update import publish,workspace
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp).resolve();base=output/'SSH-employee';base.mkdir()
            save(base/'manifest.json',{'user':'employee','marker':'ORIGINAL'})
            cache=output/'.segio-ssh-cache/employee';cache.mkdir(parents=True)
            stage=cache/'merged';stage.mkdir();save(stage/'manifest.json',{'user':'employee','marker':'NEW'})
            identity=dict(host='synthetic',port=22,ssh_user='root',username='employee',db_config='config',cell='cell',mail_template='mail',approvals=True,mail=True)
            save(cache/'plan.json',{'base':str(base),'revision':digest_file(base/'manifest.json'),'since':None})
            rename=Path.rename
            def fail(path,target):
                if path==stage:raise PermissionError('Synthetic final rename failure')
                return rename(path,target)
            with patch.object(Path,'rename',fail),self.assertRaises(PermissionError):
                publish(stage,base,cache,base,digest_file(base/'manifest.json'))
            self.assertEqual(load(base/'manifest.json')['marker'],'ORIGINAL')
            workspace(output,'employee',identity)
            self.assertFalse((cache/'publish.json').exists())
            self.assertEqual(load(base/'manifest.json')['marker'],'ORIGINAL')

    def test_delta_retries_mail_that_changes_during_the_first_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);mail=root/'employee';mail.mkdir();raw=mail/'inbox.mbox'
            raw.write_bytes(self.message('One',formatdate(usegmt=True)))
            original=helper.recent_message;changed=[False];events=[]
            def changing(*args):
                if not changed[0]:
                    changed[0]=True
                    with raw.open('ab') as stream:stream.write(self.message('Two',formatdate(usegmt=True)))
                return original(*args)
            with patch.object(helper,'recent_message',side_effect=changing):
                sources,_,_=helper.mail_delta({'user':'employee','mail_template':(root/'{user}').as_posix()},lambda state,**fields:events.append(fields),int(time.time())-86400)
            self.assertEqual(len(sources),2)
            self.assertEqual(events[-1]['attempt'],2)

    def test_refresh_button_prepares_ssh_incremental_and_starts_with_existing_credentials(self):
        from segio_archive.gui import App
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'SSH-employee';folder.mkdir()
            save(folder/'manifest.json',{'user':'employee','viewer':'sqlite-mail/1','portable_mail':{'version':'static-mail/1'},'browser_index':'offline-search/9'})
            (folder/'catalog.sqlite3').touch()
            def variable(value=''):
                box=[value];return types.SimpleNamespace(get=lambda:box[0],set=lambda x:box.__setitem__(0,x))
            fake=types.SimpleNamespace(running=False,select_archive=lambda:folder,user=variable(),folder=variable(),status=variable(),
                                       ssh_password=variable('SYNTHETIC_PASSWORD'),ssh_key=variable(),start=Mock())
            App.refresh_archive(fake)
            self.assertEqual(fake.user.get(),'employee');self.assertEqual(Path(fake.folder.get()),root.resolve())
            fake.start.assert_called_once()

