import json
import base64
from html import escape
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import export_approval_db as exporter


class DBExportTests(unittest.TestCase):
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
