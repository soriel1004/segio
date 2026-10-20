"""Local-only archive viewer, also usable where file:// is disabled by browser policy."""
from __future__ import annotations

import mimetypes
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


class Viewer:
    def __init__(self, folder: Path):
        self.folder = folder.resolve()
        if not (self.folder / "index.html").is_file():
            raise ValueError("보관함 index.html이 없습니다.")
        self.token = secrets.token_urlsafe(24)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.serve(False)

            def do_HEAD(self):
                self.serve(True)

            def serve(self, head):
                if self.headers.get("Host") != f"127.0.0.1:{owner.server.server_port}":
                    return self.send_error(403)
                prefix = "/" + owner.token + "/"
                name = unquote(urlsplit(self.path).path)
                if not name.startswith(prefix):
                    return self.send_error(404)
                target = (owner.folder / name[len(prefix):]).resolve()
                if not target.is_relative_to(owner.folder) or not target.is_file():
                    return self.send_error(404)
                mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", mime + ("; charset=utf-8" if mime == "text/html" else ""))
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                relative = target.relative_to(owner.folder)
                if ("attachments" in relative.parts or target.suffix.lower() == ".eml" or
                        relative.parts[:2] == ('mail', 'raw')):
                    from urllib.parse import quote
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(target.name))
                else:
                    script = " script-src 'unsafe-inline';" if target == owner.folder / "index.html" else ""
                    self.send_header("Content-Security-Policy", "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none';" + script)
                self.end_headers()
                if not head:
                    try:
                        with target.open('rb') as incoming:
                            shutil.copyfileobj(incoming, self.wfile, length=1024*1024)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/{self.token}/index.html"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
