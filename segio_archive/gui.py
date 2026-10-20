from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archive import verify_archive, STATES
from .browser import BrowserUnavailable
from .web import Collector, CollectionError, Config, DEFAULT_SERVER

DB_MODE = "DB·MBOX 바로 가져오기"
WEB_MODE = "웹 화면 수집 (결재·메일)"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SEGIO 퇴사자 문서·메일 보관")
        self.geometry("890x820")
        self.minsize(800, 700)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.last_archive = None
        self.viewer = None
        self.server = tk.StringVar(value=DEFAULT_SERVER)
        self.user = tk.StringVar()
        self.password = tk.StringVar()
        self.folder = tk.StringVar(value=str(Path.home() / "Documents" / "SEGIO보관함"))
        self.approvals = tk.BooleanVar(value=True)
        self.mail = tk.BooleanVar(value=True)
        self.pdf = tk.BooleanVar(value=False)
        self.method = tk.StringVar(value=DB_MODE)
        self.http = tk.BooleanVar(value=False)
        self.manual = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="서버에 연결하기 전입니다.")
        self.build()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def build(self):
        panel = ttk.Frame(self, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="퇴사자의 문서와 메일을 계정 없이 열람", font=("Malgun Gothic", 18, "bold")).pack(anchor="w")
        ttk.Label(panel, text="계정이 살아 있을 때 수집하세요. 계정·문서·메일을 삭제하는 기능은 없습니다.", wraplength=770).pack(anchor="w", pady=(8, 16))
        method_row = ttk.Frame(panel)
        method_row.pack(fill="x", pady=(0, 8))
        ttk.Label(method_row, text="가져오기 방식", width=12).pack(side="left")
        method_box = ttk.Combobox(method_row, textvariable=self.method,
                                 values=[DB_MODE, WEB_MODE], state="readonly", width=35)
        method_box.pack(side="left")
        method_box.bind("<<ComboboxSelected>>", self.change_method)
        ttk.Label(panel, text="직원 SEGIO ID·암호로 로그인하면 현재 DB 자료와 서버 MBOX 원본을 가져옵니다.\nWindows에 저장한 뒤 결재 내용과 메일 제목을 검색할 수 있습니다. 서버 연결 기능은 한 번 설치해야 합니다.",
                  wraplength=800).pack(anchor="w", pady=(0, 8))
        form = ttk.Frame(panel)
        form.pack(fill="x")
        for i, (label, variable, show) in enumerate([
            ("서버 주소", self.server, ""), ("사용자 ID", self.user, ""),
            ("암호", self.password, "•"), ("저장 폴더", self.folder, ""),
        ]):
            ttk.Label(form, text=label, width=12).grid(row=i, column=0, sticky="w", pady=6)
            ttk.Entry(form, textvariable=variable, show=show).grid(row=i, column=1, sticky="ew", pady=6)
            if i == 3:
                ttk.Button(form, text="선택", command=self.choose_folder).grid(row=i, column=2, padx=(8, 0))
        form.columnconfigure(1, weight=1)
        ttk.Label(panel, text="사내 PC에서는 주소의 lssgroup.co.kr 부분을 192.168.0.100 으로 바꿀 수 있습니다.").pack(anchor="w", pady=(4, 12))
        choices = ttk.Frame(panel)
        choices.pack(fill="x")
        self.collection_choices = []
        for label, var in [("결재 문서", self.approvals), ("메일", self.mail), ("결재 PDF 생성", self.pdf)]:
            widget = ttk.Checkbutton(choices, text=label, variable=var, state="disabled" if var is self.pdf else "normal")
            widget.pack(side="left", padx=(0, 18))
            self.collection_choices.append(widget)
        ttk.Checkbutton(panel, text="암호화되지 않은 HTTP 연결 허용 (ID·암호와 문서가 전송 중 노출될 수 있음)", variable=self.http).pack(anchor="w", pady=(12, 4))
        ttk.Checkbutton(panel, text="열린 브라우저에서 직접 로그인 (자동 로그인에 실패하는 경우)", variable=self.manual).pack(anchor="w")
        ttk.Label(panel, text="암호는 파일에 저장하지 않습니다.\n보관 폴더 자체는 암호화되지 않으므로 접근 권한을 관리하세요.", wraplength=770, foreground="#76551b").pack(anchor="w", pady=12)
        actions = ttk.Frame(panel)
        actions.pack(fill="x", pady=5)
        self.start_button = ttk.Button(actions, text="로그인하고 수집 시작", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중단", state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="보관함 열기", command=self.open_archive).pack(side="left", padx=8)
        ttk.Button(actions, text="파일 무결성 검증", command=self.verify).pack(side="left")
        ttk.Label(panel, textvariable=self.status, wraplength=770).pack(anchor="w", pady=(14, 6))
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.log_text = tk.Text(panel, height=10, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(panel, text="수집 후 오프라인 열람·누락 대조를 수행하세요. 계정 삭제 영향 시험과 서버 전체 백업은 별도입니다.", wraplength=770).pack(anchor="w", pady=(10, 0))

    def change_method(self, event=None):
        is_db = self.method.get() == DB_MODE
        self.approvals.set(True)
        self.mail.set(True)
        self.pdf.set(not is_db)
        for index, widget in enumerate(self.collection_choices):
            widget.config(state="disabled" if is_db and index==2 else "normal")

    def choose_folder(self):
        value = filedialog.askdirectory(title="백업을 저장할 상위 폴더")
        if value:
            self.folder.set(value)

    def start(self):
        if self.running:
            return
        if not self.approvals.get() and not self.mail.get():
            return messagebox.showerror("선택 필요", "결재 문서 또는 메일을 선택하세요.")
        self.cancel_event = threading.Event()
        is_db = self.method.get() == DB_MODE
        try:
            config = Config(server=self.server.get().strip(), username=self.user.get().strip(),
                            output=Path(self.folder.get()), approvals=self.approvals.get(),
                            mail=self.mail.get(), pdf=False if is_db else self.pdf.get(), allow_http=self.http.get(),
                            manual_login=self.manual.get(), cancel=self.cancel_event)
            config.validate()
            if not self.password.get() and not config.manual_login:
                raise ValueError("암호를 입력하거나 브라우저 직접 로그인을 선택하세요.")
        except Exception as exc:
            return messagebox.showerror("입력 확인", str(exc))
        password = self.password.get()
        self.password.set("")
        self.running = True
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        self.progress.start()
        self.status.set("수집 중입니다. 프로그램이 연 브라우저 창을 닫지 마세요.")
        threading.Thread(target=self.worker, args=(config, password, is_db), daemon=True).start()

    def worker(self, config, password, is_db=False):
        try:
            if is_db:
                from .db_client import DBCollector
                collector_class = DBCollector
            else:
                collector_class = Collector
            path = collector_class(config, lambda s: self.events.put(("log", s))).run(password)
            self.events.put(("done", path))
        except (CollectionError, BrowserUnavailable) as exc:
            self.events.put(("error", str(exc)))
        except Exception as exc:
            self.events.put(("error", f"시작 실패 ({type(exc).__name__}). 저장 폴더 권한과 설치 상태를 확인하세요."))
        finally:
            password = ""

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "log":
                    self.log_text.config(state="normal")
                    self.log_text.insert("end", value + "\n")
                    self.log_text.see("end")
                    self.log_text.config(state="disabled")
                elif event in {"done", "error"}:
                    self.running = False
                    self.progress.stop()
                    self.start_button.config(state="normal")
                    self.stop_button.config(state="disabled")
                    if event == "done":
                        self.last_archive = value
                        manifest = json.loads((value / "manifest.json").read_text(encoding="utf-8"))
                        self.status.set(f"{STATES.get(manifest['state'], manifest['state'])} · {value}")
                        if manifest["state"] == "failed":
                            detail = "\n".join(manifest["warnings"][1:][-3:]) or "수집 로그를 확인하세요."
                            messagebox.showerror("수집 실패", detail)
                        else:
                            messagebox.showinfo("수집 결과", "저장한 보관함에서 수집 범위·실패 내역을 확인하세요.\n이 결과만으로 계정을 삭제해도 된다는 의미는 아닙니다.")
                    else:
                        self.status.set(value)
                        messagebox.showerror("수집 실패", value)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def stop(self):
        self.cancel_event.set()
        self.status.set("현재 요청이 끝나면 중단합니다. 검증 전 임시 다운로드 파일은 제거합니다.")

    def select_archive(self):
        value = filedialog.askdirectory(title="index.html과 manifest.json이 있는 보관 폴더",
                                        initialdir=str(self.last_archive or self.folder.get()))
        return Path(value) if value else None

    def open_archive(self):
        path = self.select_archive()
        if path:
            if (path / "index.html").is_file():
                from .viewer import Viewer
                if self.viewer:
                    self.viewer.close()
                self.viewer = Viewer(path)
                webbrowser.open(self.viewer.url)
            else:
                messagebox.showerror("보관함 확인", "선택한 폴더에 index.html이 없습니다.")

    def verify(self):
        path = self.select_archive()
        if not path:
            return
        # Only the Tk thread opens dialogs.
        try:
            result = verify_archive(path)
            detail = "\n".join(x["file"] for x in result["errors"][:10])
            messagebox.showinfo("파일 무결성 검사", f"검사 {result['checked']}개 / 오류 {len(result['errors'])}개\n{detail}\n파일 검사는 서버 자료 누락이나 계정 삭제 안전성을 검증하지 않습니다.")
        except Exception:
            messagebox.showerror("검증 실패", "manifest.json과 보관 파일을 확인하세요.")

    def close(self):
        if self.running:
            messagebox.showinfo("수집 중", "‘중단’을 누르고 현재 요청이 끝난 뒤 창을 닫아주세요.")
            return
        if self.viewer:
            self.viewer.close()
        self.destroy()


def main():
    App().mainloop()
