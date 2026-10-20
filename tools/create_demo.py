"""Build an offline example using synthetic documents only, never the real SEGIO site."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from mock_segio import PASSWORD, Server
from segio_archive.web import Collector, Config
from segio_archive.viewer import Viewer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "demo")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    server = Server()
    try:
        cfg = Config(server.url + "/icons/app/cms/?html=/home/login.html&shell=/index.shell:483",
                     "alice", args.output, headless=True, allow_http=True,
                     browser_executable=shutil.which("chromium"))
        folder = Collector(cfg, print).run(PASSWORD)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest["counts"] != {"approval": 2, "mail": 3}:
            raise RuntimeError("Demo collection failed")
    finally:
        server.close()
    # Original server is stopped before testing the offline viewer.
    if args.screenshot:
        from playwright.sync_api import sync_playwright
        viewer = Viewer(folder)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, executable_path=shutil.which("chromium"))
                page = browser.new_page(viewport={"width": 1280, "height": 1000}, device_scale_factor=1)
                page.goto(viewer.url)
                page.screenshot(path=str(args.screenshot), full_page=True)
                browser.close()
        finally:
            viewer.close()
    (args.output / "읽어주세요.txt").write_text(
        "이 폴더는 합성 시험 자료입니다. 실제 직원의 문서·메일은 포함되어 있지 않습니다.\n"
        "하위 alice_... 폴더의 index.html을 Edge 등으로 열어 검색·열람을 확인하세요.\n"
        "파일 접근이 제한된 PC에서는 프로그램의 보관함 열기를 이용하세요.\n", encoding="utf-8")
    print(folder)


if __name__ == "__main__":
    main()
