import json
import base64
import os
import re
import errno
import subprocess
from html import escape
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_approval_db as exporter


class DBExportTests(unittest.TestCase):
    def test_streamed_metadata_preserves_large_offsets_and_cancel_keeps_previous_json(self):
        import live_export_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);store=exporter.MetadataStore(str(root/'metadata.sqlite3'))
            try:
                files=store.mapping('files');records=store.sequence('records')
                files['mail/raw/한글.mbox']={'bytes':50*1024**3,'sha256':'0'*64}
                records.append({'source_offset':50*1024**3+123,'text':'한글','optional':None})
                path=root/'state.json';worker.write_json(str(path),{'files':files,'records':records})
                saved=path.read_bytes();decoded=json.loads(saved)
                self.assertEqual(decoded['files']['mail/raw/한글.mbox']['bytes'],50*1024**3)
                self.assertEqual(decoded['records'][0]['source_offset'],50*1024**3+123)
                self.assertEqual(decoded['records'][0]['text'],'한글')
                def stop():raise worker.SafeError('export_cancelled')
                with self.assertRaisesRegex(worker.SafeError,'export_cancelled'):
                    worker.write_json(str(path),{'files':files,'records':records},checkpoint=stop)
                self.assertEqual(path.read_bytes(),saved)
            finally:store.close()

    def test_many_file_manifests_and_ssh_ledgers_finish_under_128_mib(self):
        runtimes=[sys.executable]
        if os.environ.get('SEGIO_TEST_PYTHON26'):runtimes.append(os.environ['SEGIO_TEST_PYTHON26'])
        for executable in runtimes:
            with self.subTest(runtime=executable),tempfile.TemporaryDirectory() as tmp:
                result=subprocess.run([executable,str(Path(__file__).parent/'large_metadata_fixture.py'),tmp],
                                      capture_output=True,text=True,timeout=180)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout)['derived_files'],62000)

    def test_large_related_records_and_history_render_under_128_mib_limit(self):
        runtimes=[sys.executable]
        if os.environ.get('SEGIO_TEST_PYTHON26'):runtimes.append(os.environ['SEGIO_TEST_PYTHON26'])
        for executable in runtimes:
            with self.subTest(runtime=executable),tempfile.TemporaryDirectory() as tmp:
                target=Path(tmp)
                if os.name=='posix':
                    actual=target/'real';actual.mkdir();alias=target/'alias';alias.symlink_to(actual,target_is_directory=True);target=alias
                result=subprocess.run([executable,str(Path(__file__).parent/'large_approval_fixture.py'),str(target)],
                                      capture_output=True,text=True,timeout=120)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertTrue(json.loads(result.stdout)['ok'])

    def test_streamed_json_preserves_utf8_controls_null_and_unicode_chunk_boundaries(self):
        value={'body':'한글'+('x'*16380)+'\U0001f600\n"\\끝','empty':'','null':None}
        chunks=list(exporter.byte_chunks(exporter.json_chunks(value)))
        data=b''.join(chunks)
        self.assertEqual(json.loads(data.decode('utf-8')),value)
        self.assertIn('한글'.encode(),data)
        self.assertTrue(all(len(chunk)<=65536 for chunk in chunks))

    def test_disk_snapshot_does_not_materialize_all_document_bodies(self):
        import tracemalloc
        model=json.loads((Path(exporter.HERE)/'approval_db_columns.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);work=root/'work';work.mkdir();counts={table:0 for table in model};counts['approval_document']=1000
            def execute(config,folder,commands,*args):
                (work/'counts.csv').write_text('table_name,row_count\n'+''.join(table.encode().hex()+','+str(count).encode().hex()+'\n' for table,count in counts.items()))
                for table,columns in model.items():
                    with (work/(table+'.csv')).open('w') as stream:
                        stream.write(','.join(columns)+'\n')
                        if table=='approval_document':
                            for number in range(1000):
                                row={'no':str(number+1),'context':'<p>'+'BODY'*2048+'</p>','title':'Synthetic','uid':'employee'}
                                stream.write(','.join(str(row[key]).encode().hex() if key in row else exporter.NULL for key in columns)+'\n')
            tracemalloc.start()
            try:
                with patch.object(exporter,'psql_readonly',side_effect=execute):
                    rows,expected=exporter.query_database('unused','employee',str(work),disk_backed=True)
                self.assertIsInstance(rows['approval_document'],exporter.DiskTable)
                self.assertEqual(len(rows['approval_document']),1000)
                self.assertEqual(rows['approval_document'][999]['no'],'1000')
                self.assertLess(tracemalloc.get_traced_memory()[1],4*1024*1024)
                cell=root/'cell';(cell/'data').mkdir(parents=True)
                generator=exporter.iter_render_bundle(exporter.Bundle(str(root/'bundle'),str(cell),'employee'),rows,expected,True)
                self.assertEqual(next(generator)['id'],'1');generator.close();rows.close()
            finally:tracemalloc.stop()

    def test_bucketed_documents_and_history_survive_flat_directory_limit_and_keep_links(self):
        from bs4 import BeautifulSoup
        import live_export_worker as worker
        from segio_archive.ssh_client import ssh_failure_message
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();cell=root/'cell';(cell/'data/approval/upload').mkdir(parents=True)
            (cell/'data/approval/upload/file.txt').write_bytes(b'EXACT_ATTACHMENT')
            model=json.loads((Path(exporter.HERE)/'approval_db_columns.json').read_text())
            rows={table:[] for table in model}
            rows['approval_document']=[{'no':str(n),'uid':'employee','title':'Synthetic','context':'<p>body</p>'} for n in range(1,351)]
            rows['approval_document_backup']=[{'no':'1','id':str(n),'uid':'employee','title':'Historical','context':'<p>old</p>'} for n in range(1,101)]
            rows['approval_member_info']=[{'uid':'employee','rname':'Synthetic'}]
            rows['approval_attach_info']=[{'no':'1','doc_no':'1','fname':'file.txt','fpath':'file.txt'}]
            bundle=exporter.Bundle(str(root/'archive'),str(cell),'employee')
            documents=root/'archive/documents';mkdir=os.mkdir
            def limited(path,mode=0o777,**kwargs):
                path=Path(path)
                if path.parent==documents and documents.exists() and len(list(documents.iterdir()))>=300:
                    raise OSError(errno.EMLINK,'Synthetic directory fanout limit')
                return mkdir(path,mode,**kwargs)
            with patch.object(exporter.os,'mkdir',side_effect=limited):
                items=exporter.render_bundle(bundle,rows,{table:len(value) for table,value in rows.items()},allow_empty=True)
            self.assertEqual(len(items),450)
            self.assertLessEqual(len(list(documents.iterdir())),256)
            self.assertEqual(len({item['path'] for item in items}),450)
            for item in (items[0],items[-1]):
                page=root/'archive'/item['path'];soup=BeautifulSoup(page.read_text(),'html.parser')
                self.assertEqual((page.parent/soup.select_one('.navigation a')['href']).resolve(),root/'archive/index.html')
                link=soup.select_one('li a')
                self.assertEqual((page.parent/link['href']).resolve().read_bytes(),b'EXACT_ATTACHMENT')
            error=OSError(errno.EMLINK,'PRIVATE_FILE_PATH')
            code,diagnostic=worker.failure_details(error,'database')
            self.assertEqual(code,'server_directory_limit')
            self.assertNotIn('PRIVATE_FILE_PATH',ssh_failure_message(code,diagnostic))

    def test_snapshot_queries_materialize_employee_scope_once_per_copy_and_keep_read_only(self):
        model=json.loads((Path(exporter.HERE)/'approval_db_columns.json').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sql=[]
            def execute(config,work,commands,*args):
                sql.extend(commands)
                exporter_header='table_name,row_count\n'+''.join(table.encode().hex()+',30\n' for table in sorted(model))
                (root/'counts.csv').write_text(exporter_header)
                for table in model:(root/(table+'.csv')).write_text(','.join(model[table])+'\n')
            # Empty synthetic row counts are encoded hex: 30 decodes to "0".
            with patch.object(exporter,'psql_readonly',side_effect=execute):
                rows,counts=exporter.query_database('unused','employee',str(root),live_scope=True)
            copies=[command for command in sql if command.startswith('\\copy')]
            self.assertEqual(len(copies),len(model)+1)
            self.assertTrue(all(command.count('WITH segio_selected AS (')==1 for command in copies))
            self.assertTrue(all('FROM segio_selected' in command for command in copies))
            self.assertTrue(any('SERIALIZABLE READ ONLY' in command for command in sql))
            self.assertIn('SET LOCAL statement_timeout=300000;',sql)
            self.assertFalse(any(re.search(r'(?i)\b(CREATE|INSERT|UPDATE|DELETE|ALTER|DROP)\b',command) for command in sql))
            self.assertEqual(counts,{table:0 for table in model})

    def test_localized_sqlstates_and_query_phase_cross_safe_error_boundary_without_raw_data(self):
        import live_export_worker as worker
        from segio_archive.ssh_client import ssh_failure_message
        secret='PRIVATE_PASSWORD_QUERY_AND_DOCUMENT'
        cases=[('57014','db_query_timeout'),('53200','db_resource_limit'),('22021','db_encoding_failed'),('42601','db_query_failed')]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=root/'db.conf'
            config.write_text('DBMS=pgsql\nDBNAME=cell_000\nUSER=fixture\nPW='+secret+'\n')
            for state,expected in cases:
                process=Mock(returncode=1)
                process.communicate.return_value=(b'SEGIO_DB_PHASE=counts\nSEGIO_DB_PHASE=approval_document\n',
                    ('오류:  '+state+': '+secret+'\nCONTEXT: '+secret).encode())
                with self.subTest(sqlstate=state),patch.object(exporter.subprocess,'Popen',return_value=process) as started:
                    try:exporter.psql_readonly(str(config),str(root),['SELECT 1;'])
                    except exporter.SafeError as exc:code,diagnostic=worker.failure_details(exc,'database')
                    else:self.fail('The query failure was not propagated')
                    self.assertEqual(code,expected)
                    self.assertEqual(diagnostic['sqlstate'],state)
                    self.assertEqual(diagnostic['query_phase'],'approval_document')
                    message=ssh_failure_message(code,diagnostic,'database')
                    self.assertIn('SQLSTATE='+state,message);self.assertIn('query=approval_document',message)
                    self.assertNotIn(secret,message+json.dumps(diagnostic))
                    self.assertIn('statement_timeout=300000',started.call_args.kwargs['env']['PGOPTIONS'])
                    self.assertFalse((root/'pgpass').exists())
        self.assertEqual(exporter.database_sqlstates(b'ERROR: bad value "PRIVATE: 57014:"\nCONTEXT: 28P01: PRIVATE\n'),[])

    @unittest.skipIf(os.name == 'nt', 'Legacy POSIX filename encodings')
    def test_korean_native_paths_under_ascii_locale_and_ambiguous_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            upload=root/'cell/data/approval/upload'
            upload.mkdir(parents=True)
            name='한글+첨부.txt'
            native=os.fsencode(upload)+b'/'+name.encode('cp949')
            with open(native,'wb') as stream:stream.write(b'SYNTHETIC_EXACT_BYTES')
            bundle=exporter.Bundle(str(root/'archive'),str(root/'cell'),'employee')
            with patch.object(exporter.sys,'getfilesystemencoding',return_value='ascii'):
                result=bundle.attachment({'no':'1','fname':name,'fpath':name},'1')
                self.assertEqual((root/'archive'/result['path']).read_bytes(),b'SYNTHETIC_EXACT_BYTES')
                self.assertEqual(bundle.local_file(name.encode('cp949'),upload=True),native)
                from urllib.parse import quote_from_bytes
                encoded='data/approval/upload/'+quote_from_bytes(name.encode('cp949'))
                self.assertEqual(bundle.local_file(encoded,upload=True),native)
                (upload/name).write_bytes(b'OTHER_FILE_SAME_DISPLAYED_NAME')
                self.assertIsNone(bundle.local_file(name,upload=True))
                self.assertIsNone(bundle.local_file('../../../../private.txt',upload=True))

    @unittest.skipIf(os.name == 'nt', 'Legacy POSIX filename encodings')
    def test_real_export_roundtrip_with_legacy_runtime_when_available(self):
        from segio_archive.db_client import unpack_verified
        from segio_archive.archive import verify_archive
        runtimes=[sys.executable]
        if os.environ.get('SEGIO_TEST_PYTHON26'):
            runtimes.append(os.environ['SEGIO_TEST_PYTHON26'])
        for executable in runtimes:
            with self.subTest(runtime=executable), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                environment=dict(os.environ,LC_ALL='C',PYTHONCOERCECLOCALE='0',PYTHONUTF8='0')
                result=subprocess.run([executable,str(Path(__file__).parent/'legacy_export_fixture.py'),str(root)],
                                      env=environment,capture_output=True,check=True)
                summary=json.loads(result.stdout)
                self.assertTrue(summary['ok'])
                self.assertEqual(summary['attachments'],2)
                target=root/'verified'
                target.mkdir()
                unpack_verified(root/'output/segio-employee.zip',target,'employee')
                self.assertTrue(verify_archive(target)['ok'])
                for number in range(2):
                    self.assertEqual((target/(exporter.document_directory('1')+'/attachments/%d.txt'%(number+1))).read_bytes(),
                                     b'SYNTHETIC_ATTACHMENT_KEPT_EXACTLY_'+str(number).encode('ascii'))

    def test_encoding_diagnostic_contains_no_document_fragments(self):
        import live_export_worker as worker
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'private.csv'
            source.write_text('context\n'+(b'PRIVATE_DB_TEXT_'+bytes([255])).hex()+'\n')
            try:
                exporter.read_rows(str(source))
            except UnicodeError as exc:
                code,diagnostic=worker.failure_details(exc,'database')
            else:
                self.fail('Invalid UTF-8 must not be silently rewritten')
            self.assertEqual(code,'db_encoding_failed')
            self.assertEqual(diagnostic['exception_type'],'UnicodeDecodeError')
            self.assertEqual(diagnostic['encoding'],'utf-8')
            self.assertEqual(diagnostic['site']['function'],'read_rows')
            self.assertIsInstance(diagnostic['site']['line'],int)
            self.assertNotIn('PRIVATE',json.dumps(diagnostic))
            self.assertNotIn(str(source),json.dumps(diagnostic))

    def test_sqlstate_errors_are_specific_and_credentials_never_escape(self):
        cases = ((b'28P01', 'db_authentication_failed'), (b'42501', 'db_permission_denied'),
                 (b'42P01', 'db_schema_mismatch'), (b'42703', 'db_schema_mismatch'),
                 (b'57014', 'db_query_timeout'), (b'53100', 'server_disk_full'))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root/'db.conf'
            secret = 'PRIVATE_PASSWORD_AND_SQL_VALUE'
            config.write_text('DBMS=pgsql\nDBNAME=cell_000\nUSER=fixture\nPW='+secret+'\n')
            for state, expected in cases:
                process = Mock(returncode=1)
                process.communicate.return_value = (b'', b'ERROR:  '+state+b': '+secret.encode())
                def launch(*args, **kwargs):
                    passfile = Path(kwargs['env']['PGPASSFILE'])
                    self.assertTrue(passfile.is_file())
                    if sys.platform != 'win32':
                        self.assertEqual(passfile.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn(secret, repr(args))
                    return process
                with self.subTest(state=state), patch.object(exporter.subprocess, 'Popen', side_effect=launch):
                    with self.assertRaises(exporter.SafeError) as caught:
                        exporter.psql_readonly(str(config), str(root), ['SELECT 1;'])
                    self.assertEqual(caught.exception.code, expected)
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertFalse((root/'pgpass').exists())

    def test_authenticated_employee_without_approval_profile_can_archive_empty_approvals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root/'cell'
            (cell/'data').mkdir(parents=True)
            model = json.loads((Path(exporter.HERE)/'approval_db_columns.json').read_text())
            rows = {table: [] for table in model}
            counts = {table: 0 for table in model}
            with patch.object(exporter, 'query_database', return_value=(rows, counts)):
                # Administrator ID spelling checks remain strict in CLI mode.
                with self.assertRaisesRegex(exporter.SafeError, 'Target user'):
                    exporter.run_export('unused', 'employee', str(root), cell=str(cell))
                folder, archive = exporter.run_export('unused', 'employee', str(root), cell=str(cell), live_scope=True)
            manifest = json.loads((Path(folder)/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['counts']['approval'], 0)
            self.assertTrue(any('0개' in warning for warning in manifest['warnings']))
            self.assertTrue(Path(archive).is_file())

    def test_encoded_body_is_decoded_then_sanitized_without_changing_literal_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = exporter.Bundle(tmp, tmp, "employee")
            original_html = '<p>한글 A+B 100% &lt;예시&gt;</p><script>evil()</script><img src="https://external.invalid/x" onerror="evil()">'
            stored = quote_plus(escape(original_html))
            self.assertTrue(stored.startswith("%26lt%3Bp%26gt"))
            rendered, plain = bundle.freeze(stored)
            self.assertIn("한글 A+B 100% &lt;예시&gt;", rendered)
            self.assertIn("한글 A+B 100% <예시>", plain)
            for unsafe in ("<script", "onerror", "evil()", "https:"):
                self.assertNotIn(unsafe, rendered)
            ordinary, ordinary_text = bundle.freeze('<p>literal &lt;em&gt; A+B /?x=%26lt</p>')
            self.assertIn("&lt;em&gt; A+B /?x=%26lt", ordinary)
            self.assertEqual(bundle.display_decoding["urlencoded_html"], 1)
            self.assertEqual(bundle.display_decoding["escaped_html"], 1)
            literal, literal_text = bundle.freeze("&lt;script&gt;code example&lt;/script&gt;")
            self.assertIn("&lt;script&gt;code example&lt;/script&gt;", literal)

    def test_raw_jpeg_signature_becomes_an_image_and_arbitrary_base64_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = exporter.Bundle(tmp, tmp, "employee")
            jpeg = (Path(__file__).parent / "fixtures/signature.jpg").read_bytes()
            stored = base64.b64encode(jpeg).decode()
            self.assertTrue(stored.startswith("/9j/"))
            rendered, plain = bundle.freeze(stored, signature=True)
            self.assertIn('src="data:image/jpeg;base64,', rendered)
            self.assertEqual(plain, "")
            self.assertEqual(bundle.display_decoding["base64_signature"], 1)
            bad = base64.b64encode(b"<svg onload='evil()'></svg>").decode()
            self.assertIsNone(exporter.signature_data_uri(bad))
            self.assertIsNone(exporter.signature_data_uri("/9j/invalid!"))

    def test_attachment_traversal_symlinks_and_changed_files_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root / "cell"
            upload = cell / "data/approval/upload"
            upload.mkdir(parents=True)
            (upload / "ok.txt").write_text("selected attachment")
            outside = root / "private.txt"
            outside.write_text("SYNTHETIC_SECRET_MUST_NOT_COPY")
            references = [str(outside), "../../../../private.txt"]
            if sys.platform != "win32":
                (upload / "symlink.txt").symlink_to(outside)
                references.append("symlink.txt")
            folder = root / "archive"
            folder.mkdir()
            bundle = exporter.Bundle(str(folder), str(cell), "employee")
            for reference in references:
                self.assertIsNone(bundle.attachment({"no": "1", "fname": "original.txt", "fpath": reference}, "10"))
            with patch.object(bundle, "same_file", return_value=False):
                self.assertIsNone(bundle.attachment({"no": "2", "fname": "original.txt", "fpath": "ok.txt"}, "10"))
            self.assertFalse(list(folder.rglob("*.txt")))
            self.assertEqual(bundle.files, {})
            self.assertTrue(any(r["kind"] == "attachment_copy_failed_or_changed" for r in bundle.issues))

    def test_dynamic_inline_image_maps_only_the_verified_local_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            images = root / "cell/data/approval/app_images"
            images.mkdir(parents=True)
            (images / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE")
            bundle = exporter.Bundle(str(root / "archive"), str(root / "cell"), "employee")
            rendered, plain = bundle.freeze('''<img src="/segio/works/approval/img_insert.php?mode=app_images&amp;fname=pic.png" onerror="evil()"><script>evil()</script><style>@import url(https://external.invalid)</style><iframe src="file:///etc/passwd"></iframe><a href="javascript:evil()">text</a><p style="background:url(https://external.invalid);color:red">body</p>''')
            self.assertIn("data:image/png;base64,", rendered)
            for value in ("onerror", "evil()", "javascript:", "file:", "https:", "<script", "<iframe", "<style"):
                self.assertNotIn(value, rendered)
            self.assertIn("color:red", rendered)
            self.assertIsNone(bundle.image("/segio/works/approval/img_insert.php?mode=app_images&fname=../../etc/db.conf"))

    @unittest.skipIf(sys.platform == "win32", "Uses a POSIX fake psql executable")
    def test_client_failure_and_incomplete_success_do_not_leave_credentials_or_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cell = root / "cell"
            (cell / "data").mkdir(parents=True)
            (cell / "etc").mkdir()
            config = cell / "etc/db.conf"
            secret = "SYNTHETIC_PASSWORD_MUST_NOT_PRINT"
            config.write_text("DBMS=postgresql\nDBNAME=cell_000\nUSER=fixture\nPW=" + secret + "\n")
            original = config.read_bytes()
            client = root / "psql"
            for exit_code in (2, 0):
                client.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '" + secret + "' >&2\nexit " + str(exit_code) + "\n")
                client.chmod(0o700)
                with self.assertRaises(exporter.SafeError) as caught:
                    exporter.run_export(str(config), "employee", str(root), binary=str(client))
                self.assertNotIn(secret, str(caught.exception))
                self.assertFalse(list(root.glob("segio-db-*")))
                self.assertEqual(config.read_bytes(), original)

    def test_csv_transport_preserves_unicode_null_empty_and_literal_null_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "transport.csv"
            values = [None, "", "__SEGIO_NULL__", "문서\n둘째 줄", "a,b\\c"]
            p.write_text("value\n" + "\n".join(exporter.NULL if v is None else v.encode().hex() if v else '""' for v in values) + "\n")
            self.assertEqual([r["value"] for r in exporter.read_rows(str(p))], values)


if __name__ == "__main__":
    unittest.main()
