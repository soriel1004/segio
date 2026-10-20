from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from segio_archive.archive import Archive, safe_name, verify_archive
from segio_archive.web import Collector, Config, CollectionError, readonly_url
from segio_archive.viewer import Viewer
from mock_segio import PASSWORD, Server


class ArchiveTests(unittest.TestCase):
    def test_integrity_detects_missing_and_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = Archive(Path(tmp), "https://example.test", "alice")
            a.write("file.txt", b"original")
            a.finish("collected")
            self.assertTrue(verify_archive(a.path)["ok"])
            (a.path / "file.txt").write_bytes(b"changed")
            self.assertFalse(verify_archive(a.path)["ok"])
            (a.path / "file.txt").unlink()
            self.assertEqual(verify_archive(a.path)["errors"][0]["reason"], "missing_or_unsafe")

    def test_http_is_explicit_and_mutations_are_rejected(self):
        with self.assertRaises(CollectionError):
            Config("http://example.test", "alice", Path("unused")).validate()
        for url in ["https://x/action.php?mode=delete", "https://x/a.php?mode=delete_backup_document", "https://x/logout.php", "javascript:alert(1)"]:
            self.assertFalse(readonly_url(url), url)
        self.assertNotIn("/", safe_name("../../CON.txt"))

    def test_login_query_from_user_is_preserved_and_html_unescaped(self):
        cfg = Config("http://example.test/icons/app/cms/?html=/home/login.html&amp;shell=/index.shell:483", "alice", Path("unused"), allow_http=True)
        cfg.validate()
        self.assertEqual(cfg.server, "http://example.test/icons/app/cms/?html=/home/login.html&shell=/index.shell:483")
        with self.assertRaises(CollectionError):
            Config("https://example.test/login?password=secret", "alice", Path("unused")).validate()


class BrowserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.temp = tempfile.TemporaryDirectory()
        cls.logs = []
        cls.config = Config(cls.server.url + "/icons/app/cms/?html=/home/login.html&shell=/index.shell:483", "alice", Path(cls.temp.name),
                            headless=True, allow_http=True, timeout_ms=5000,
                            browser_executable=os.environ.get("SEGIO_TEST_BROWSER") or shutil.which("chromium"))
        cls.path = Collector(cls.config, cls.logs.append).run(PASSWORD)
        cls.manifest = json.loads((cls.path / "manifest.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        cls.temp.cleanup()

    def test_full_workflow_with_pagination_deduplication_and_email(self):
        self.assertEqual(self.manifest["state"], "collected_with_warnings", self.logs)
        self.assertEqual(self.manifest["counts"], {"approval": 2, "mail": 3}, self.logs)
        self.assertTrue(verify_archive(self.path)["ok"])
        con = sqlite3.connect(self.path / "catalog.sqlite3")
        try:
            rows = con.execute("SELECT folders,html,pdf FROM items WHERE kind='approval'").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertTrue(any("참조함" in r[0] for r in rows))
            for _, document, pdf in rows:
                self.assertTrue(pdf)
                self.assertTrue((self.path / pdf).read_bytes().startswith(b"%PDF-"))
                body = (self.path / document).read_text(encoding="utf-8")
                self.assertIn("결재자 홍길동", body)
                self.assertIn("data:image/png;base64,", body)
                self.assertNotIn("<script", body)
                self.assertNotIn("javascript:", body)
                self.assertNotIn("never-archive-this", body)
            self.assertEqual(con.execute("SELECT count(*) FROM items WHERE kind='mail' AND eml<>''").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM items WHERE status='rendered_only'").fetchone()[0], 2)
            for (body,) in con.execute("SELECT body FROM items WHERE kind='mail'"):
                self.assertIn("bcc@example.test", body)
        finally:
            con.close()
        coverage = self.manifest["coverage"]
        self.assertTrue(all(c["status"] == "enumerated" for c in coverage), coverage)
        self.assertFalse(self.manifest["account_deletion_verified"])
        for method, path in self.server.requests:
            self.assertNotIn("delete", path)
            self.assertNotIn("no=999", path)
            if method == "POST":
                self.assertEqual(path, "/login")

    def test_no_credentials_saved_and_no_path_traversal(self):
        for p in self.path.rglob("*"):
            if p.is_file():
                self.assertNotIn(PASSWORD.encode(), p.read_bytes(), p.name)
                self.assertNotIn(b"alice-fixture", p.read_bytes(), p.name)
        self.assertFalse((self.path.parent / "CON.txt").exists())
        self.assertTrue(any("CON.txt" in p.name for p in self.path.rglob("*") if p.is_file()))

    def test_offline_viewer_search_works_without_network(self):
        before = len(self.server.requests)
        viewer = Viewer(self.path)
        try:
          with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=os.environ.get("SEGIO_TEST_BROWSER") or shutil.which("chromium"))
            context = browser.new_context()
            # The archive viewer is local; every non-local request is aborted.
            context.route("**/*", lambda r: r.continue_() if r.request.url.startswith(viewer.url.rsplit('/',1)[0] + '/') else r.abort())
            page = context.new_page()
            page.goto(viewer.url)
            self.assertEqual(page.locator("article").count(), 5)
            page.locator("#q").fill("홍길동")
            self.assertEqual(page.locator("article").count(), 2)
            href = page.locator("article a").first.get_attribute("href")
            page.goto(viewer.url.rsplit('/',1)[0] + '/' + href)
            self.assertIn("승인합니다", page.locator("body").inner_text())
            self.assertFalse(page.locator("script").count())
            browser.close()
        finally:
            viewer.close()
        self.assertEqual(len(self.server.requests), before)

    def test_wrong_password_does_not_create_false_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(self.server.url, "alice", Path(tmp), approvals=True, mail=False,
                         headless=True, allow_http=True, pdf=False, timeout_ms=5000,
                         browser_executable=os.environ.get("SEGIO_TEST_BROWSER") or shutil.which("chromium"))
            path = Collector(cfg).run("wrong")
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "failed")
            self.assertEqual(manifest["counts"], {})

    def test_missing_attachment_is_reported_and_not_saved_as_html(self):
        self.server.broken_attachment = True
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = Config(self.server.url, "alice", Path(tmp), mail=False, pdf=False,
                             headless=True, allow_http=True, timeout_ms=5000,
                             browser_executable=os.environ.get("SEGIO_TEST_BROWSER") or shutil.which("chromium"))
                path = Collector(cfg).run(PASSWORD)
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["state"], "collected_with_warnings")
                self.assertTrue(any("첨부파일 다운로드 실패" in w for w in manifest["warnings"]))
                self.assertFalse(list(path.rglob("attachments/*")))
        finally:
            self.server.broken_attachment = False

    def test_page_limit_preserves_count_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(self.server.url, "alice", Path(tmp), mail=False, pdf=False,
                         headless=True, allow_http=True, max_pages=1, timeout_ms=5000,
                         browser_executable=os.environ.get("SEGIO_TEST_BROWSER") or shutil.which("chromium"))
            path = Collector(cfg).run(PASSWORD)
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            completed = next(c for c in manifest["coverage"] if c.get("folder") == "완료함")
            self.assertEqual(completed["expected"], 2)
            self.assertEqual(completed["discovered"], 1)
            self.assertIn(completed["status"], {"limited", "count_mismatch"})
            self.assertEqual(manifest["state"], "collected_with_warnings")

    def test_cancel_keeps_a_reviewable_partial_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(self.server.url, "alice", Path(tmp), mail=False, pdf=False,
                         headless=True, allow_http=True,
                         browser_executable=os.environ.get("SEGIO_TEST_BROWSER") or shutil.which("chromium"))
            cfg.cancel.set()
            path = Collector(cfg).run(PASSWORD)
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "cancelled")
            self.assertTrue((path / "index.html").is_file())


if __name__ == "__main__":
    unittest.main()
