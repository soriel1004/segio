from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file,search
from segio_archive.index_maintenance import compact_approval_index
import test_local_delete as localtests


class IndexMaintenanceTests(unittest.TestCase):
    def fixture(self,root):
        localtests.LocalDeleteTests().fixture(root)
        payload={'source':'active','document':{'progress':'complete','wdate':'1700000000','reference':'synthetic','context':'PRIVATE_FULL_BODY'*100000},
                 'related':{'approval_line':[{'uid':'synthetic','division':'reference','sign':'PRIVATE_SIGNATURE'*100000}]}}
        text=json.dumps(payload)
        (root/'documents/current/database.json').write_text(text)
        with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
            db.execute('UPDATE document_records SET payload=? WHERE id=?',(text,'approval-current'));db.commit()
        manifest=json.loads((root/'manifest.json').read_text())
        for name in ('catalog.sqlite3','documents/current/database.json'):manifest['files'][name]=digest_file(root/name)
        (root/'manifest.json').write_text(json.dumps(manifest))
        return manifest

    def test_compaction_keeps_original_json_search_relationships_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest=self.fixture(root);before=localtests.LocalDeleteTests().snapshot(root)
            counts={kind:search(root,kind=kind)['total'] for kind in ('approval','mail')}
            with patch('segio_archive.index_maintenance.THRESHOLD',0):updated=compact_approval_index(root,manifest)
            self.assertLess(updated['files']['catalog.sqlite3']['bytes'],manifest['files']['catalog.sqlite3']['bytes']//10)
            self.assertEqual(digest_file(root/'documents/current/database.json'),before['documents/current/database.json'])
            for kind,total in counts.items():self.assertEqual(search(root,kind=kind)['total'],total)
            self.assertEqual(search(root,kind='approval',approval_folder='reference')['total'],1)
            self.assertTrue(verify_archive(root)['ok'])
            with closing(sqlite3.connect(root/'catalog.sqlite3')) as db:
                self.assertNotIn('PRIVATE_SIGNATURE',db.execute('SELECT payload FROM document_records WHERE id=?',('approval-current',)).fetchone()[0])

    def test_interrupted_install_restores_original_database_and_manifest(self):
        for point in ('catalog.sqlite3','manifest.json'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);manifest=self.fixture(root);before=localtests.LocalDeleteTests().snapshot(root)
                def fault(name):
                    if name==point:raise OSError('synthetic installation interruption')
                with patch('segio_archive.index_maintenance.THRESHOLD',0),self.assertRaises(OSError):
                    compact_approval_index(root,manifest,fault=fault)
                localtests.LocalDeleteTests().assert_snapshot(root,before)
