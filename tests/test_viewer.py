import http.client
import hashlib
import tempfile
import unittest
from pathlib import Path

from segio_archive.viewer import Viewer


class ViewerTests(unittest.TestCase):
    def test_original_mbox_can_be_downloaded_with_length_and_attachment_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'index.html').write_text('test',encoding='utf-8')
            raw=root/'mail/raw'
            raw.mkdir(parents=True)
            payload=b'From synthetic@example.test\nSubject: synthetic\n\n'+b'mail-body\n'*300000
            (raw/'000000.mbox').write_bytes(payload)
            viewer=Viewer(root)
            try:
                path='/'+viewer.token+'/mail/raw/000000.mbox'
                for method in ('HEAD','GET'):
                    connection=http.client.HTTPConnection('127.0.0.1',viewer.server.server_port)
                    try:
                        connection.request(method,path)
                        response=connection.getresponse()
                        self.assertEqual(response.status,200)
                        self.assertEqual(response.getheader('Content-Length'),str(len(payload)))
                        self.assertTrue(response.getheader('Content-Disposition').startswith('attachment;'))
                        digest=hashlib.sha256()
                        count=0
                        while block:=response.read(32768):
                            count+=len(block)
                            digest.update(block)
                        self.assertEqual(count,len(payload) if method=='GET' else 0)
                        if method=='GET':
                            self.assertEqual(digest.hexdigest(),hashlib.sha256(payload).hexdigest())
                    finally:
                        connection.close()
            finally:
                viewer.close()

    def test_local_viewer_rejects_wrong_token_traversal_and_external_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            root.mkdir()
            (root / "index.html").write_text("test", encoding="utf-8")
            (Path(tmp) / "secret").write_text("secret", encoding="utf-8")
            viewer = Viewer(root)
            try:
                def get(path, host=None):
                    connection = http.client.HTTPConnection("127.0.0.1", viewer.server.server_port)
                    connection.request("GET", path, headers={"Host": host} if host else {})
                    response = connection.getresponse()
                    result = response.status, response.getheader("Content-Security-Policy"), response.read()
                    connection.close()
                    return result
                prefix = "/" + viewer.token + "/"
                self.assertEqual(get(prefix + "index.html")[0], 200)
                self.assertIn("default-src 'none'", get(prefix + "index.html")[1])
                self.assertEqual(get("/index.html")[0], 404)
                self.assertEqual(get(prefix + "%2e%2e/secret")[0], 404)
                self.assertEqual(get(prefix + "index.html", "external.invalid")[0], 403)
            finally:
                viewer.close()
