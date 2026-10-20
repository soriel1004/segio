import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from segio_archive.db_client import PreparedDBCollector as DBCollector, DBCollector as LiveCollector, LIVE_PROTOCOL, PROTOCOL, publish_local, unpack_verified, processing_error
from segio_archive.web import Cancelled, CollectionError, Config, Collector


def make_archive(root, user='employee', contents=None, extra=None):
    contents = contents or {'index.html': b'<p>synthetic offline archive</p>',
                            'documents/1/body.html': '합성 결재 본문'.encode()}
    manifest = {'format':'segio-db-html/1', 'user':user, 'state':'collected_with_warnings',
                'account_deletion_verified':False, 'warnings':[], 'counts':{'approval':1},
                'files':{name:{'bytes':len(body), 'sha256':hashlib.sha256(body).hexdigest()}
                         for name, body in contents.items()}}
    archive = root/'fixture.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr('manifest.json', json.dumps(manifest))
        for name, body in contents.items():
            zipped.writestr(name, body)
        if extra:
            zipped.writestr(extra, b'not-listed')
    return archive


class DBClientTests(unittest.TestCase):
    def test_live_connection_errors_are_specific_and_do_not_expose_response_content(self):
        client=LiveCollector(Config('https://example.invalid/','employee',Path('unused')))
        client.context=Mock()
        client.user_agent='Synthetic Browser UA'
        response=Mock(status=503,headers={'content-type':'application/json'})
        client.context.request.get.return_value=response
        response.body.return_value=json.dumps({'protocol':LIVE_PROTOCOL,'error':'service_not_configured','secret':'NEVER_LOG_THIS'}).encode()
        with self.assertRaisesRegex(CollectionError,'service_not_configured') as caught:
            client.live_call('info',pending_login=True)
        self.assertNotIn('NEVER_LOG_THIS',str(caught.exception))
        self.assertEqual(client.context.request.get.call_args.kwargs['headers'],{'User-Agent':'Synthetic Browser UA'})
        response.status=200
        response.body.return_value=b'{ invalid PHP response with SECRET_SESSION_COOKIE'
        with self.assertRaisesRegex(CollectionError,'BRIDGE_INVALID_JSON') as caught:
            client.live_call('info')
        self.assertNotIn('SECRET_SESSION_COOKIE',str(caught.exception))
        response.status=401
        self.assertIsNone(client.live_call('info',pending_login=True))
        response.dispose.assert_called()

    def test_login_transition_is_retried_and_unexpected_browser_details_are_not_logged(self):
        from playwright.sync_api import Error,TimeoutError as BrowserTimeout
        client=Collector(Config('https://example.invalid/','employee',Path('unused')))
        page,frame=Mock(),Mock()
        page.frames=[frame]
        frame.locator.return_value.count.side_effect=[Error('Frame was detached: SECRET_SESSION_COOKIE'),0]
        self.assertTrue(client.login_present(page))
        self.assertFalse(client.login_present(page))
        client.context=Mock(pages=[page])
        page.is_closed.return_value=False
        frame.locator.return_value.evaluate_all.side_effect=Error('Execution context was destroyed SECRET_SESSION_COOKIE')
        client.register_login_forms()
        for exception,code in ((Error('Frame was detached: SECRET_SESSION_COOKIE'),'LOGIN_FRAME_TRANSITION'),
                               (BrowserTimeout('SECRET_SESSION_COOKIE'),'TIMEOUT'),
                               (Error('net::ERR_CONNECTION_REFUSED https://example.invalid/?pass=SECRET_SESSION_COOKIE'),'ERR_CONNECTION_REFUSED')):
            message=str(processing_error(exception,'SEGIO 로그인'))
            self.assertIn(code,message)
            self.assertIn('SEGIO 로그인',message)
            self.assertNotIn('SECRET_SESSION_COOKIE',message)
            self.assertNotIn('https://',message)

    def test_wrong_user_unlisted_paths_and_windows_unsafe_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root/'unpacked'
            target.mkdir()
            for name in ('../escape', '/absolute', 'x\\escape', 'C:stream', 'aux.txt',
                         'file:ads', 'trailing.', 'a/../escape'):
                with self.subTest(name=name):
                    archive = make_archive(root, contents={'index.html':b'index', name:b'unsafe'})
                    with self.assertRaises(CollectionError):
                        unpack_verified(archive, target, 'employee')
            for options in ({'user':'other'}, {'extra':'unlisted.txt'}):
                archive = make_archive(root, **options)
                with self.assertRaises(CollectionError):
                    unpack_verified(archive, target, 'employee')

    def test_tampered_contents_and_symlinks_do_not_pass_manifest_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = make_archive(root)
            target = root/'unpacked'
            target.mkdir()
            with zipfile.ZipFile(original) as archive:
                manifest = archive.read('manifest.json')
            bad = root/'bad.zip'
            with zipfile.ZipFile(bad, 'w') as archive:
                archive.writestr('manifest.json', manifest)
                archive.writestr('index.html', b'TAMPERED')
                archive.writestr('documents/1/body.html', '합성 결재 본문'.encode())
            with self.assertRaises(CollectionError):
                unpack_verified(bad, target, 'employee')
            with zipfile.ZipFile(bad, 'w') as archive:
                archive.writestr('manifest.json', manifest)
                link = zipfile.ZipInfo('index.html')
                link.external_attr = (stat.S_IFLNK | 0o777)<<16
                archive.writestr(link, b'/etc/passwd')
                archive.writestr('documents/1/body.html', b'body')
            with self.assertRaises(CollectionError):
                unpack_verified(bad, target, 'employee')

    def test_valid_zip_is_extracted_and_old_short_names_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_archive(root)
            target = root/'unpacked'
            target.mkdir()
            unpack_verified(archive, target, 'employee')
            (root/'segio-employee.zip').write_bytes(b'keep old zip')
            (root/'segio-employee-2').mkdir()
            result = publish_local(target, archive, root, 'employee')
            self.assertEqual(result.name, 'segio-employee-3')
            self.assertTrue((result/'index.html').is_file())
            self.assertTrue((root/'segio-employee-3.zip').is_file())
            self.assertEqual((root/'segio-employee.zip').read_bytes(), b'keep old zip')

    def test_bridge_identity_and_protocol_required_no_target_id_sent(self):
        config = Config('https://example.invalid/', 'employee', Path('unused'))
        client = DBCollector(config)
        response = Mock(status=200, headers={'content-type':'application/json'})
        client.context = Mock()
        client.context.request.get.return_value = response
        for payload in ({'protocol':PROTOCOL, 'username':'other', 'archive':{}},
                        {'protocol':'unexpected', 'username':'employee'},
                        {'protocol':PROTOCOL, 'username':'employee', 'archive':None}):
            response.body.return_value = json.dumps(payload).encode()
            with self.assertRaises(CollectionError):
                client.info()
        self.assertEqual(client.context.request.get.call_args.args[0],
                         'https://example.invalid/segio/archive/bridge.php?action=info')
        self.assertEqual(client.context.request.get.call_args.kwargs['max_redirects'], 0)

    def test_failed_or_cancelled_transfer_leaves_no_archive_or_partial_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = make_archive(root)
            body = archive.read_bytes()
            archive.unlink()
            config = Config('https://example.invalid/', 'employee', root)
            client = DBCollector(config)
            client.context, client.page = Mock(), Mock()
            client.page.evaluate.return_value = 'Synthetic User Agent'
            record = {'bytes':len(body), 'sha256':'0'*64, 'version':'a'*32}
            incoming = Mock(status=200)
            incoming.headers.get_content_type.return_value = 'application/zip'
            incoming.headers.get.return_value = str(len(body))
            incoming.read.side_effect = [body, b'']
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=incoming)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch('segio_archive.db_client.download_opener', return_value=opener):
                with self.assertRaises(CollectionError):
                    client.receive(record)
                self.assertEqual(list(root.iterdir()), [])
                config.cancel.set()
                incoming.read.side_effect = [body, b'']
                with self.assertRaises(Cancelled):
                    client.receive(record)
                self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
