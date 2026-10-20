from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="SEGIO 결재 문서·메일 오프라인 보관")
    sub = parser.add_subparsers(dest="command")
    collect = sub.add_parser("collect", help="서버에서 문서와 메일 수집")
    collect.add_argument("--server", required=True)
    collect.add_argument("--user", required=True)
    collect.add_argument("--output", type=Path, default=Path("archives"))
    collect.add_argument("--allow-http", action="store_true")
    collect.add_argument("--no-mail", action="store_true")
    collect.add_argument("--no-approvals", action="store_true")
    collect.add_argument("--no-pdf", action="store_true")
    collect.add_argument("--headless", action="store_true")
    collect.add_argument("--manual-login", action="store_true")
    collect.add_argument("--max-pages", type=int, default=300)
    verify = sub.add_parser("verify", help="수집 파일 SHA-256 검사")
    verify.add_argument("folder", type=Path)
    view = sub.add_parser("view", help="서버 연결 없이 로컬 보관함 열기")
    view.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.command == "view":
        import webbrowser
        import threading
        from .viewer import Viewer
        viewer = Viewer(args.folder)
        webbrowser.open(viewer.url)
        print("보관함을 열었습니다. 종료하려면 Ctrl+C를 누르세요.")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            viewer.close()
        return 0
    if args.command == "verify":
        from .archive import verify_archive
        result = verify_archive(args.folder)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 2
    if args.command == "collect":
        from .web import Collector, Config
        try:
            config = Config(server=args.server, username=args.user, output=args.output,
                            allow_http=args.allow_http, approvals=not args.no_approvals,
                            mail=not args.no_mail, pdf=not args.no_pdf,
                            headless=args.headless, manual_login=args.manual_login,
                            max_pages=args.max_pages)
            if not config.approvals and not config.mail:
                parser.error("결재 또는 메일 중 하나를 선택하세요.")
            password = "" if args.manual_login else getpass.getpass("암호 (저장하지 않음): ")
            path = Collector(config, print).run(password)
            password = ""
            status = json.loads((path / "manifest.json").read_text(encoding="utf-8"))["state"]
            return 0 if status == "collected" else 2
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            print(f"시작 실패: {type(exc).__name__}", file=sys.stderr)
            return 2
    from .gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
