"""One local deletion job with preview, progress and cancellation."""
import secrets
import threading
import time

from . import local_delete
from .web import Cancelled, CollectionError


class Editor:
    def __init__(self,root,archive_lock):
        self.root,self.archive_lock=root,archive_lock
        self.csrf=secrets.token_urlsafe(32)
        self.mutex=threading.Lock()
        self.plans={}
        self.job=None
        self.thread=None
        self.cancel_event=threading.Event()
        self.recovery_required=False
        self.index_bytes=None
        self.closed=False

    @property
    def busy(self):
        with self.mutex:
            return self.job is not None and self.job['state']=='running'

    def preview(self,ids):
        if self.closed or self.busy or self.recovery_required or not self.archive_lock.acquire(blocking=False):
            raise CollectionError('백업본 정리 또는 복구 중입니다.')
        try:
            value=local_delete.preview(self.root,ids)
        finally:
            self.archive_lock.release()
        token=secrets.token_urlsafe(24)
        with self.mutex:
            self.plans={token:(value,time.monotonic()+300)}
        return dict(plan=token,approval=value['approval'],mail=value['mail'],titles=value['titles'],
                    temporary_bytes=value['temporary_bytes'],raw_files=value['raw_files'])

    def start(self,token):
        with self.mutex:
            if self.closed:
                raise CollectionError('관리 화면이 종료되었습니다. 보관함을 다시 열어주세요.')
            if self.job is not None and self.job['state']=='running':
                raise CollectionError('삭제 처리가 진행 중입니다.')
            plan=self.plans.pop(token,None) if isinstance(token,str) else None
            if plan is None or plan[1]<time.monotonic():
                raise CollectionError('삭제 범위를 다시 확인하세요.')
            self.index_bytes=(self.root/'index.html').read_bytes()
            self.cancel_event=threading.Event()
            self.job=dict(id=secrets.token_urlsafe(24),state='running',stage='prepare',completed=0,total=None,
                          started=time.monotonic(),ended=None,message='삭제 준비 중')
            self.thread=threading.Thread(target=self.run,args=(plan[0],),daemon=False)
            self.thread.start()
            return self.job['id']

    def checkpoint(self):
        if self.cancel_event.is_set():
            raise Cancelled('삭제를 중단했습니다. 기존 자료를 유지했습니다.')

    def run(self,plan):
        acquired=False
        reported=[0.0,'']
        def progress(stage,done,total):
            now=time.monotonic()
            if stage!=reported[1] or now-reported[0]>=.2 or done==total:
                with self.mutex:
                    self.job.update(stage=stage,completed=done,total=total)
                reported[:]=[now,stage]
        try:
            while not acquired:
                self.checkpoint()
                acquired=self.archive_lock.acquire(timeout=.1)
            result=local_delete.delete_items(self.root,plan['ids'],plan['revision'],self.checkpoint,progress)
            message='백업본에서 결재 %s건·메일 %s건을 삭제했습니다.'%(result['approval'],result['mail'])
            if result['cleanup_pending']:
                message+=' 임시 자료 정리가 남았습니다. 프로그램에서 보관함을 다시 열어주세요.'
            with self.mutex:
                self.job.update(state='done',result=result,message=message)
        except Cancelled as exc:
            with self.mutex:
                self.job.update(state='cancelled',message=str(exc))
        except Exception as exc:
            self.recovery_required=(self.root/local_delete.WORK).exists()
            message=str(exc) if isinstance(exc,CollectionError) else '삭제 처리에 실패했습니다. 프로그램에서 보관함을 다시 열어 파일 무결성을 확인하세요.'
            with self.mutex:
                self.job.update(state='failed',message=message,recovery_required=self.recovery_required)
        finally:
            with self.mutex:
                self.job['ended']=time.monotonic()
            if acquired:
                self.archive_lock.release()

    def status(self,identity):
        with self.mutex:
            if self.job is None or identity!=self.job['id']:
                raise CollectionError('삭제 작업을 찾지 못했습니다.')
            value={key:val for key,val in self.job.items() if key not in ('started','ended')}
            value['elapsed']=int((self.job['ended'] or time.monotonic())-self.job['started'])
            value['can_cancel']=value['state']=='running' and value['stage']!='commit'
            return value

    def cancel(self,identity):
        with self.mutex:
            if self.job is None or identity!=self.job['id'] or self.job['state']!='running' or self.job['stage']=='commit':
                raise CollectionError('이미 완료 처리 중이거나 끝난 작업입니다.')
            self.cancel_event.set()

    def close(self):
        with self.mutex:
            self.closed=True
            self.cancel_event.set()
            thread=self.thread
        if thread:
            thread.join()
