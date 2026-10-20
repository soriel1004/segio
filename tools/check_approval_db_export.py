"""Integration check for the CentOS DB archive against disposable PostgreSQL.

Python 3 developer tool; optional pgserver==0.1.4 and Playwright for browser check.
"""
import base64
import json
from html import escape
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
from urllib.parse import quote_plus

import pgserver
import export_approval_db as exporter


def main():
    binary = Path(pgserver.__file__).parent / "pginstall/bin/psql"
    model = json.loads((Path(__file__).parent / "approval_db_columns.json").read_text())
    secret = "SYNTHETIC:DB\\PASSWORD_NEVER_EXPORT"
    with tempfile.TemporaryDirectory(prefix="segio-bulk-check-") as tmp:
        root = Path(tmp)
        cell = root / "cell000"
        (cell / "etc").mkdir(parents=True)
        upload = cell / "data/approval/upload"
        images = cell / "data/approval/app_images"
        upload.mkdir(parents=True)
        images.mkdir(parents=True)
        (upload / "stored.hwp").write_bytes(b"SYNTHETIC_ATTACHMENT_CONTENT")
        (images / "pixel.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="))
        (upload / "outside.png").symlink_to(root / "outside.png")
        (root / "outside.png").write_bytes((images / "pixel.png").read_bytes())
        with pgserver.get_server(root / "pgdata", cleanup_mode="delete") as server:
            def run(sql, db=None):
                result = subprocess.run([str(binary), "-XAt", "-w", "-d", server.get_uri(db),
                                         "-v", "ON_ERROR_STOP=1"], input=sql,
                                        capture_output=True, text=True, timeout=60)
                if result.returncode:
                    raise RuntimeError("Disposable fixture SQL failed; details omitted")
                return result.stdout

            def insert(table, **values):
                def literal(v):
                    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"
                return "INSERT INTO %s (%s) VALUES (%s);" % (table, ",".join('"'+k+'"' for k in values), ",".join(literal(v) for v in values.values()))

            run("CREATE DATABASE cell_000; CREATE ROLE segio_bulk_fixture LOGIN SUPERUSER PASSWORD '%s';" % secret)
            statements = []
            for table, columns in model.items():
                def dtype(column):
                    numeric = column in ("id", "no", "p_no") or (column == "doc_no" and table in ("approval_send_box", "approval_send_box_backup", "approval_favorite"))
                    return "integer" if numeric else "text"
                statements.append("CREATE TABLE %s (%s);" % (table, ",".join('"%s" %s' % (c, dtype(c)) for c in columns)))
            statements.append("ALTER TABLE approval_member_info ADD secret_password text;")
            statements.append(insert("approval_member_info", no=1, uid="target", rname="김퇴사", secret_password=secret))
            statements.append(insert("approval_member_info", no=2, uid="other", rname="다른직원"))
            statements.append(insert("approval_member_info", no=3, uid="commenter", rname="의견 작성자 이름"))
            jpeg = (Path(__file__).parents[1] / "tests/fixtures/signature.jpg").read_bytes()
            encoded_signature = base64.b64encode(jpeg).decode("ascii")
            for no in range(1, 18):
                values = dict(no=no, uid="target" if no == 1 else "other", title="합성 문서 %d" % no, context="<p>본문 %d</p>" % no, fno="7", wdate="1700000000")
                if no == 1:
                    encoded_context = quote_plus(escape('<table><tr><td>유니코드 본문 A+B</td></tr></table><img src="/data/approval/app_images/pixel.png" onerror="window.UNSAFE=1"><img src="https://external.invalid/leak"><script>window.UNSAFE=1</script><p style="background-image:url(https://external.invalid/css);color:red">빈 문자열/NULL 보존</p>'))
                    values.update(title="한글 결재 </script><script>window.UNSAFE=1</script>",
                                  context=encoded_context,
                                  doc_id="", reference=None)
                if no == 3:
                    values["reference"] = "someone@@이름,target@@김퇴사"
                if no == 4:
                    values["reference"] = "target2@@동명이인,target"
                if no == 8:
                    values["auto_pass"] = "target@@dept@@김퇴사"
                if no == 9:
                    values["doc_recv_uid"] = "target"
                statements.append(insert("approval_document", **values))
            statements.extend([
                insert("approval_line", no=1, p_no=2, uid="target", rname="김퇴사 당시 이름", state="approve", order_no=1, sign=encoded_signature),
                insert("approval_line", no=2, p_no=7, uid="other", target_id="target", rname="대결자", state="agency"),
                insert("approval_send_box", no=1, doc_no=5, recv_user="someone,target"),
                insert("approval_send_box", no=2, doc_no=6, recv_uid="target2"),
                insert("approval_comment_info", no=1, doc_no="10", uid="target", context="합성 의견"),
                insert("approval_comment_info", no=2, doc_no="1", uid="commenter", context="동료의 의견"),
                insert("approval_favorite", no=1, doc_no=11, uid="target"),
                insert("approval_doc_backup", no=1, p_no=12, uid="target", context="저장된 수정 기록"),
                insert("approval_line_backup", id=140, no=14, p_no=14, uid="target", rname="과거 참여자"),
                insert("approval_comment_info_backup", id=150, no=15, doc_no="15", uid="target", context="과거 댓글 참여"),
                insert("approval_doc_backup_backup", id=160, no=16, p_no=16, uid="target", context="과거 수정 참여"),
                insert("approval_send_box_backup", id=170, no=17, doc_no=17, recv_user="target"),
                insert("approval_document_backup", id=200, no=20, uid="target", title="삭제 후 남은 백업", context="<p>과거 문서</p>", backupdate=1700000000),
                insert("approval_line_backup", id=200, no=20, p_no=20, uid="target", rname="과거 이름", backupdate=1700000000),
                insert("approval_document_backup", id=201, no=20, uid="target", title="두 번째 과거 기록", context="<p>두 번째 이력</p>", backupdate=1700000100),
                insert("approval_line_backup", id=201, no=20, p_no=20, uid="target", rname="변경된 과거 이름", backupdate=1700000100),
                insert("approval_format_info", no=7, title="양식", context="보존할 원본 서식"),
                insert("approval_attach_info", no=1, doc_no="1", fname="첨부 원본.hwp", fpath="stored.hwp"),
                insert("approval_attach_info", no=2, doc_no="2", fname="누락.hwp", fpath="missing.hwp"),
                insert("approval_attach_info", no=3, doc_no="1", fname="설정.conf", fpath="../etc/db.conf"),
                insert("approval_attach_info", no=4, doc_no="1", fname="외부 파일.txt", fpath="/etc/passwd"),
                insert("approval_attach_info", no=5, doc_no="4", fname="범위 밖.hwp", fpath="stored.hwp"),
            ])
            run("\n".join(statements), "cell_000")
            hba = root / "pgdata/pg_hba.conf"
            hba.write_text("local cell_000 segio_bulk_fixture password\n" + hba.read_text())
            run("SELECT pg_reload_conf();")
            config = cell / "etc/db.conf"
            config.write_text("DBMS=postgresql\nHOST=%s\nDBNAME=cell_000\nUSER=segio_bulk_fixture\nPW=%s\nOPTION=\n" % (server.get_postmaster_info().socket_dir, secret))
            originals = {p: p.read_bytes() for p in (hba, root / "pgdata/pg_ident.conf", config)}
            before = run("SELECT count(*) FROM approval_document;", "cell_000")
            folder, archive = exporter.run_export(str(config), "target", str(root), binary=str(binary))
            folder = Path(folder)
            manifest = json.loads((folder / "manifest.json").read_text())
            assert manifest["snapshot_rows"]["approval_document"] == 14
            assert manifest["snapshot_rows"]["approval_document_backup"] == 2
            assert manifest["snapshot_rows"]["approval_attach_info"] == 4
            assert manifest["account_deletion_verified"] is False
            assert manifest["state"] == "collected_with_warnings"
            from segio_archive.archive import verify_archive
            assert verify_archive(folder)["ok"]
            for path in folder.rglob("*"):
                if path.is_file():
                    assert secret.encode() not in path.read_bytes()
                    assert path.stat().st_mode & 0o777 == 0o600
            assert not (folder / "private-query").exists()
            raw = json.loads((folder / "documents/1/database.json").read_text())
            assert raw["document"]["doc_id"] == "" and raw["document"]["reference"] is None
            assert raw["document"]["context"] == encoded_context
            signature_raw = json.loads((folder / "documents/2/database.json").read_text())
            assert signature_raw["related"]["approval_line"][0]["sign"] == encoded_signature
            assert manifest["display_decoding"]["base64_signature"] == 1
            assert manifest["display_decoding"]["urlencoded_html"] == 1
            assert "\n" not in str(raw["document"]["no"])
            assert (folder / "documents/1/attachments/1.hwp").read_bytes() == b"SYNTHETIC_ATTACHMENT_CONTENT"
            assert not (folder / "documents/4").exists() and not (folder / "documents/6").exists()
            assert (folder / "documents/20-history-200/document.html").is_file()
            assert all(p.read_bytes() == data for p, data in originals.items())
            assert run("SELECT count(*) FROM approval_document;", "cell_000") == before
            bundle = exporter.Bundle(str(folder), str(cell), "target")
            assert bundle.local_file("data/approval/upload/outside.png", upload=True) is None
            assert bundle.local_file("../etc/db.conf", upload=True) is None
            unpacked = root / "windows-copy"
            with zipfile.ZipFile(archive) as z:
                assert z.testzip() is None
                z.extractall(unpacked)
            assert verify_archive(unpacked)["ok"]
            from playwright.sync_api import sync_playwright
            from segio_archive.viewer import Viewer
            viewer = Viewer(unpacked)
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
                page = browser.new_page()
                external = []
                local_prefix = viewer.url.rsplit("/", 1)[0] + "/"
                page.on("request", lambda r: external.append(r.url) if r.url.startswith(("http:", "https:")) and not r.url.startswith(local_prefix) else None)
                page.goto(viewer.url)
                assert page.locator("article").count() == 16
                page.locator("#q").fill("김퇴사 당시 이름")
                assert page.locator("article").count() == 1
                assert page.evaluate("typeof window.UNSAFE") == "undefined"
                page.locator("#q").fill("유니코드 본문")
                assert page.locator("article").count() == 1
                page.goto(viewer.url.replace("index.html", "documents/1/document.html"))
                assert "유니코드 본문 A+B" in page.locator("body").inner_text()
                assert "의견 작성자 이름" in page.locator("body").inner_text()
                assert page.evaluate("typeof window.UNSAFE") == "undefined"
                assert not external
                assert page.locator("img").first.get_attribute("src").startswith("data:image/png;base64,")
                page.goto(viewer.url.replace("index.html", "documents/2/document.html"))
                signatures = page.locator('img[src^="data:image/jpeg;base64,"]')
                assert signatures.count() == 1
                assert signatures.first.evaluate("img => img.complete && img.naturalWidth === 1")
                assert not external
                browser.close()
            viewer.close()
    print("PASS: exact employee scope, active/history, snapshot counts, NULL/Unicode, attachment hashes, "
          "missing/unsafe files, unchanged DB/auth, private credentials, Windows-copy ZIP, offline search and HTML isolation")


if __name__ == "__main__":
    main()
