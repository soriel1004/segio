import http.client
import tempfile
import unittest
from pathlib import Path

from segio_archive.viewer import Viewer


class ViewerTests(unittest.TestCase):
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
