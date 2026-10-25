from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .archive import verify_archive, STATES
from .web import CollectionError
from . import __version__

APP_TITLE = "그룹웨어 문서 및 메일 백업 및 검색"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE + " · v" + __version__)
        self.geometry("890x820")
        self.minsize(800, 700)
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self.local_conversion = False
        self.connection_check = False
        self.last_archive = None
        self.viewer = None
        self.user = tk.StringVar()
        self.ssh_host = tk.StringVar(value='192.168.0.100')
        self.ssh_port = tk.StringVar(value='22')
        self.ssh_user = tk.StringVar(value='root')
        self.ssh_password = tk.StringVar()
        self.ssh_key = tk.StringVar()
        self.resume = tk.BooleanVar(value=True)
        self.make_zip = tk.BooleanVar(value=False)
        self.folder = tk.StringVar(value=str(Path.home() / "Documents" / "SEGIO보관함"))
        self.approvals = tk.BooleanVar(value=True)
        self.mail = tk.BooleanVar(value=True)
        self.pdf = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="서버에 연결하기 전입니다.")
        self.build()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def build(self):
        panel = ttk.Frame(self, padding=24)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=APP_TITLE, font=("Malgun Gothic", 18, "bold")).pack(anchor="w")
        ttk.Label(panel, text="서버의 원본을 보존하며 Windows에 백업합니다. 계정·문서·메일을 삭제하지 않습니다.", wraplength=770).pack(anchor="w", pady=(6, 10))
        ttk.Label(panel,text='서버 관리자 SSH 계정으로 접속하고 백업할 직원의 SEGIO ID를 지정합니다.\nDB·MBOX를 직접 가져오며 끊긴 전송은 이어받습니다.',wraplength=800).pack(anchor='w',pady=(0,6))
        self.ssh_form = ttk.Frame(panel)
        self.ssh_form.pack(fill='x')
        for i,(label,variable,show) in enumerate([
            ('SSH 서버 주소',self.ssh_host,''),('SSH 포트',self.ssh_port,''),
            ('서버 SSH 계정',self.ssh_user,''),('SSH 암호 / 키 암호',self.ssh_password,'•'),
            ('백업할 SEGIO ID',self.user,''),('개인키 파일 (선택)',self.ssh_key,'')]):
            ttk.Label(self.ssh_form,text=label,width=20).grid(row=i,column=0,sticky='w',pady=3)
            ttk.Entry(self.ssh_form,textvariable=variable,show=show).grid(row=i,column=1,sticky='ew',pady=3)
            if variable is self.ssh_key:
                ttk.Button(self.ssh_form,text='선택',command=self.choose_key).grid(row=i,column=2,padx=(8,0))
        self.ssh_form.columnconfigure(1,weight=1)
        folder_row = ttk.Frame(panel)
        folder_row.pack(fill='x',pady=5)
        ttk.Label(folder_row,text='Windows 저장 폴더',width=20).pack(side='left')
        ttk.Entry(folder_row,textvariable=self.folder).pack(side='left',fill='x',expand=True)
        ttk.Button(folder_row,text='선택',command=self.choose_folder).pack(side='left',padx=(8,0))
        choices = ttk.Frame(panel)
        choices.pack(fill="x")
        for label, var in [("결재 문서", self.approvals), ("메일", self.mail), ("결재 PDF 생성", self.pdf)]:
            widget = ttk.Checkbutton(choices, text=label, variable=var, state="disabled" if var is self.pdf else "normal")
            widget.pack(side="left", padx=(0, 18))
        self.ssh_options = ttk.Frame(panel)
        self.ssh_options.pack(fill='x',pady=(8,0))
        ttk.Checkbutton(self.ssh_options,text='이전 전송 이어받기',variable=self.resume).pack(side='left',padx=(0,14))
        ttk.Checkbutton(self.ssh_options,text='ZIP도 생성 (추가 공간·시간 필요)',variable=self.make_zip).pack(side='left')
        ttk.Label(panel, text="암호와 개인키 내용은 보관 파일에 저장하지 않습니다. 보관 폴더 접근 권한을 관리하세요.", wraplength=770, foreground="#76551b").pack(anchor="w", pady=8)
        actions = ttk.Frame(panel)
        actions.pack(fill="x", pady=5)
        self.start_button = ttk.Button(actions, text="로그인하고 수집 시작", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중단", state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=8)
        self.check_button=ttk.Button(actions,text='SSH 연결 확인',command=self.check_connection)
        self.check_button.pack(side='left',padx=(0,8))
        ttk.Button(actions, text="보관함 열기", command=self.open_archive).pack(side="left", padx=8)
        ttk.Button(actions, text="파일 무결성 검증", command=self.verify).pack(side="left")
        ttk.Button(actions, text="기존 백업 갱신", command=self.open_archive).pack(side="left", padx=8)
        ttk.Label(panel, textvariable=self.status, wraplength=770).pack(anchor="w", pady=(14, 6))
        self.progress = ttk.Progressbar(panel, mode="indeterminate")
        self.progress.pack(fill="x", pady=(0, 8))
        self.log_text = tk.Text(panel, height=7, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True)
        ttk.Label(panel, text="수집 후 오프라인 열람·누락 대조를 수행하세요. 계정 삭제 영향 시험과 서버 전체 백업은 별도입니다.", wraplength=770).pack(anchor="w", pady=(10, 0))

    def choose_folder(self):
        value = filedialog.askdirectory(title="백업을 저장할 상위 폴더")
        if value:
            self.folder.set(value)

    def choose_key(self):
        value=filedialog.askopenfilename(title='서버 SSH 개인키 파일')
        if value:
            self.ssh_key.set(value)

    def confirm_host(self,host,algorithm,fingerprint):
        ready,result=threading.Event(),{}
        self.events.put(('host_key',(host,algorithm,fingerprint,ready,result)))
        while not ready.wait(.2):
            if self.cancel_event.is_set():
                return False
        return result.get('accepted',False)

    def ssh_config(self):
        from .ssh_client import SSHConfig
        return SSHConfig(host=self.ssh_host.get().strip(),port=int(self.ssh_port.get()),
            ssh_user=self.ssh_user.get().strip(),username=self.user.get().strip(),output=Path(self.folder.get()),
            key_file=self.ssh_key.get().strip(),approvals=self.approvals.get(),mail=self.mail.get(),
            make_zip=self.make_zip.get(),resume=self.resume.get(),cancel=self.cancel_event)

    def check_connection(self):
        if self.running:
            return
        self.cancel_event=threading.Event()
        try:
            config=self.ssh_config()
            config.validate(for_backup=False)
            password=self.ssh_password.get()
            if not password and not config.key_file:
                raise ValueError('서버 SSH 암호 또는 개인키 파일을 입력하세요.')
        except Exception as exc:
            return messagebox.showerror('입력 확인',str(exc))
        self.running=self.connection_check=True
        self.local_conversion=False
        self.start_button.config(state='disabled')
        self.check_button.config(state='disabled')
        self.stop_button.config(state='normal')
        self.progress.start()
        self.status.set('SSH 로그인·SFTP와 서버 파일 접근을 확인합니다.')
        threading.Thread(target=self.connection_worker,args=(config,password),daemon=True).start()

    def connection_worker(self,config,password):
        from .ssh_client import SSHCollector
        try:
            result=SSHCollector(config,confirm_host=self.confirm_host).check_connection(password)
            self.events.put(('connection_ready',result))
        except CollectionError as exc:
            self.events.put(('connection_error',str(exc)))
        except Exception as exc:
            self.events.put(('connection_error','SSH 연결 확인 중 오류가 발생했습니다. ['+type(exc).__name__+']'))
        finally:
            password=''

    def start(self):
        if self.running:
            return
        self.local_conversion = False
        self.connection_check = False
        if not self.approvals.get() and not self.mail.get():
            return messagebox.showerror("선택 필요", "결재 문서 또는 메일을 선택하세요.")
        self.cancel_event = threading.Event()
        try:
            config=self.ssh_config()
            password=self.ssh_password.get()
            if not password and not config.key_file:
                raise ValueError('서버 SSH 암호 또는 개인키 파일을 입력하세요.')
            config.validate()
        except Exception as exc:
            return messagebox.showerror("입력 확인", str(exc))
        self.ssh_password.set('')
        self.running = True
        self.start_button.config(state="disabled")
        self.check_button.config(state='disabled')
        self.stop_button.config(state="normal")
        self.progress.start()
        self.status.set('SSH로 수집 중입니다. 처음 접속하면 서버 지문을 확인해 주세요.')
        threading.Thread(target=self.worker, args=(config, password), daemon=True).start()

    def worker(self, config, password):
        try:
            from .ssh_client import SSHCollector
            collector=SSHCollector(config,lambda s:self.events.put(('log',s)),self.confirm_host)
            path = collector.run(password)
            self.events.put(("done", path))
        except CollectionError as exc:
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
                elif event == 'host_key':
                    host,algorithm,fingerprint,ready,result=value
                    try:
                        result['accepted']=messagebox.askyesno('SSH 서버 지문 확인',
                            f'서버: {host}\n종류: {algorithm}\n지문: {fingerprint}\n\n서버 관리자에게 확인한 지문과 일치하면 저장하고 연결합니다. 계속할까요?',parent=self)
                    finally:
                        ready.set()
                elif event in {"done", "error", "archive_ready", "archive_error", 'connection_ready','connection_error'}:
                    self.running = False
                    self.progress.stop()
                    self.start_button.config(state="normal")
                    self.check_button.config(state='normal')
                    self.stop_button.config(state="disabled")
                    self.connection_check=False
                    if event=='connection_ready':
                        labels={'python_compatible':'Python 버전','runtime_modules':'Python 필수 모듈','database_config_readable':'DB 설정 파일 읽기',
                            'cell_readable':'자료 폴더 읽기','psql_executable':'psql 실행','work_directory_writable':'임시 폴더 쓰기',
                            'mail_readable':'직원 메일 경로 읽기'}
                        lines=['SSH 로그인 및 SFTP 연결: 정상','서버 Python: '+'.'.join(map(str,value['python_version']))]
                        lines.extend(label+': '+('정상' if value['checks'].get(key) else '확인 필요')
                                     for key,label in labels.items() if value['checks'].get(key) is not None)
                        if value['checks'].get('mail_readable') is None:
                            lines.append('메일 경로: 메일 선택·직원 ID 입력 후 확인합니다.')
                        lines.append('DB 접속·자료 추출은 수집 시작 시 확인합니다.')
                        self.status.set('SSH 연결 확인 완료' if value['ready'] else 'SSH 연결 정상 · 파일 접근 항목을 확인하세요.')
                        (messagebox.showinfo if value['ready'] else messagebox.showwarning)('SSH 연결 확인','\n'.join(lines))
                    elif event=='connection_error':
                        self.status.set(value)
                        messagebox.showerror('SSH 연결 확인',value)
                    elif event == 'archive_ready':
                        self.last_archive=value
                        self.status.set('메일 바로보기 준비 완료 · 이제 프로그램을 종료해도 index.html에서 열람할 수 있습니다.')
                        webbrowser.open((value/'index.html').as_uri())
                    elif event == 'archive_error':
                        self.status.set(value)
                        messagebox.showerror('백업 갱신 확인',value)
                    elif event == "done":
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
        if self.connection_check:
            self.status.set('SSH 연결 확인을 중단합니다.')
            return
        if self.local_conversion:
            self.status.set('메일 변환을 중단합니다. 완료한 메일은 다음 갱신에서 재사용합니다.')
            return
        self.status.set('현재 전송을 중단합니다. SSH 임시 자료는 다음 실행의 이어받기를 위해 보존합니다.')

    def select_archive(self):
        value = filedialog.askdirectory(title="index.html과 manifest.json이 있는 보관 폴더",
                                        initialdir=str(self.last_archive or self.folder.get()))
        return Path(value) if value else None

    def open_archive(self):
        if self.running:
            return
        path = self.select_archive()
        if path:
            if (path / "index.html").is_file():
                from .viewer import Viewer
                try:
                    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
                    if manifest.get('viewer')=='sqlite-mail/1':
                        self.local_conversion=True
                        self.connection_check=False
                        self.running=True
                        self.cancel_event=threading.Event()
                        self.start_button.config(state='disabled')
                        self.check_button.config(state='disabled')
                        self.stop_button.config(state='normal')
                        self.progress.start()
                        self.status.set('기존 백업의 메일 본문·첨부를 바로보기 파일로 변환합니다. 서버 접속 없이 진행합니다.')
                        threading.Thread(target=self.archive_worker,args=(path,),daemon=True).start()
                        return
                    self.status.set('보관함 목록·검색 화면을 준비합니다. 서버에서 자료를 다시 받지 않습니다.')
                    self.update_idletasks()
                    viewer = Viewer(path)
                    if self.viewer:
                        self.viewer.close()
                    self.viewer = viewer
                    webbrowser.open(self.viewer.url)
                    self.status.set('보관함을 열었습니다. 결재·메일 탭과 검색 버튼을 사용하세요.')
                except Exception as exc:
                    self.status.set('보관함을 열지 못했습니다.')
                    messagebox.showerror('보관함 확인', str(exc))
            else:
                messagebox.showerror("보관함 확인", "선택한 폴더에 index.html이 없습니다.")

    def archive_worker(self,path):
        from .portable_mail import prepare_archive
        from .web import Cancelled
        def checkpoint():
            if self.cancel_event.is_set():
                raise Cancelled('메일 변환을 중단했습니다. 기존 백업 갱신을 다시 누르면 완료한 메일을 재사용합니다.')
        try:
            prepare_archive(path,checkpoint,lambda message:self.events.put(('log',message)))
            self.events.put(('archive_ready',path))
        except Cancelled as exc:
            self.events.put(('archive_error',str(exc)))
        except Exception as exc:
            self.events.put(('archive_error','메일 바로보기 변환에 실패했습니다. 저장 공간과 파일 무결성을 확인하세요. ['+type(exc).__name__+']'))

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
