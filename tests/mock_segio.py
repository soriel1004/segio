"""Synthetic server for integration tests. Contains no uploaded or production records."""
from __future__ import annotations

import base64
import html
import json
import threading
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/QkAAAAASUVORK5CYII=")
PDF = b"%PDF-1.4\nSynthetic attachment for test\n%%EOF"
PASSWORD = "synthetic-secret-not-production"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests = []
        self.broken_attachment = False
        self.expired = False
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


def page(body):
    return '<!doctype html><html lang="ko"><meta charset="utf-8"><title>SEGIO fixture</title><body>' + body + '</body></html>'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, mime="text/html; charset=utf-8", status=200, headers=None):
        data = data.encode() if isinstance(data, str) else data
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def login(self):
        self.send(page('<form method="post" action="/login" target="zero">'
                       '<input name="uid"><input type="password" name="passwd">'
                       '<input type="hidden" name="csrf" value="fixture-csrf">'
                       '<button>로그인</button></form><iframe name="zero"></iframe>'))

    def do_POST(self):
        self.server.requests.append(("POST", self.path))
        values = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        if self.path != "/login":
            return self.send("mutation forbidden", "text/plain", 405)
        if values.get("uid") == ["alice"] and values.get("passwd") == [PASSWORD] and values.get("csrf") == ["fixture-csrf"]:
            return self.send(page("로그인됨"), headers={"Set-Cookie": "sid=alice-fixture; HttpOnly; SameSite=Lax; Path=/"})
        self.send(page("로그인 실패"), status=401)

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path in {"/", "/icons/app/cms/"}:
            return self.login()
        if "sid=alice-fixture" not in self.headers.get("Cookie", "") or self.server.expired:
            return self.login()
        if u.path == "/sign.png":
            return self.send(PNG, "image/png")
        if u.path.endswith("/index.php"):
            return self.send(page('<a href="my_complete.php">완료함</a><a href="refer_list.php">참조함</a><a href="/_mail/inbox.mbox/">메일</a>'))
        if u.path.endswith(("my_complete.php", "refer_list.php")):
            ref = u.path.endswith("refer_list.php")
            doc = "102" if q.get("page") == ["2"] else "101"
            total = 1 if ref else 2
            next_page = '<a href="my_complete.php?page=2&find=all&find_str=">2</a>' if not ref and doc == "101" else ""
            return self.send(page(f'<div class="data_ltext">문서 : {total}</div><table class="appr_table"><tr><td><a href="view_format.php?no={doc}&view=complete">휴가 신청 {doc}</a></td></tr></table>{next_page}<a href="action.php?mode=delete&no=101">삭제</a>'))
        if u.path.endswith(("view_format.php", "view_format_for_link.php")):
            number = q.get("no", [""])[0]
            if number not in {"101", "102"}:
                return self.send(page("접근 권한이 없습니다."), status=403)
            related = '<a href="view_format_for_link.php?no=102">연계 102</a>' if number == "101" else ""
            return self.send(page(f'''<nav>다른 메뉴</nav><div class="gwcore_zone"><div class="appr_vtitle_wrap"><h2>휴가 신청 {number}</h2></div>
<table><tr><td>기안자 Alice</td><td>결재자 홍길동</td><td>2026-10-20 승인</td></tr></table>
<img src="/sign.png" alt="직인"><p>퇴사 전 보존할 문서 본문 {number}</p>
<div class="comment">홍길동 (manager): 승인합니다</div><a href="download.php?no={number}&status=download">휴가.pdf</a>{related}
<form><input type="hidden" name="csrf" value="never-archive-this"><button>결재하기</button></form>
<script>window.liveScript=true;</script><a href="javascript:alert(1)">위험 링크</a></div>'''))
        if u.path.endswith("download.php"):
            if self.server.broken_attachment:
                return self.send(page("로그인이 필요합니다"))
            return self.send(PDF, "application/pdf", headers={"Content-Disposition": "attachment; filename=leave.pdf"})
        if u.path == "/raw.eml":
            message = EmailMessage()
            message["From"] = "sender@example.test"
            message["To"] = "alice@example.test"
            message["Subject"] = "원문 검증 메일"
            message["Date"] = "Tue, 20 Oct 2026 09:00:00 +0900"
            message.set_content("첨부를 포함한 테스트 메일")
            message.add_attachment(b"synthetic attachment", maintype="application", subtype="octet-stream", filename="../../CON.txt")
            return self.send(message.as_bytes(), "message/rfc822")
        if u.path.startswith("/_mail/"):
            folder = u.path.split("/")[2].split(",")[0]
            is_detail = len(u.path.rstrip("/").split("/")) > 3
            if is_detail:
                raw = '<a href="/raw.eml">원문</a>' if folder == "projects.mbox" else ""
                return self.send(page(f'''<div class="mail_view_top"><h3>메일 {html.escape(folder)} {u.path[-1]}</h3></div>{raw}
<div class="mail_view_div"><table><tr><td>보낸 사람 sender@example.test</td></tr><tr><td class="mail_view_att"><a href="/segio/works/approval/download.php?no=101">mail.pdf</a></td></tr></table>
<table><tbody id="more_rec" style="display:none"><tr><td>숨은 참조 bcc@example.test</td></tr></tbody></table>
<div class="mail_view_context">오프라인 메일 본문<img src="/sign.png"><a href="https://external.invalid/">외부 링크</a></div></div>'''))
            info = [{"mboxdata": 100}, [
                ["inbox", "받은편지함", "/_mail/inbox.mbox/", [1, 2, 100, 0]],
                ["sentbox", "보낸편지함", "/_mail/sentbox.mbox/", [0, 0, 0, 0]]], [
                ["ubox", "프로젝트", "/_mail/projects.mbox/", [0, 1, 100, 0]]]]
            if folder == "sentbox.mbox":
                rows = ""
            else:
                number = "2" if ",02" in u.path else "1"
                rows = f'<a href="/_mail/{folder}/{number}">테스트 메일 {number}</a>'
            next_page = '<a href="/_mail/inbox.mbox,02/?shell=/segio/msg/mail.shell">02</a>' if folder == "inbox.mbox" and ",02" not in u.path else ""
            return self.send(page(f'<script>var Mail_info={json.dumps(info,ensure_ascii=False)}</script><div class="mail_list">{rows}</div>{next_page}<a href="/_mail/sentbox.mbox/">보낸편지함</a>'))
        return self.send("not found", "text/plain", 404)
