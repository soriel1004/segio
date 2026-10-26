"""Structured worker progress and elapsed time, independent of Tk threads."""
import time

LABELS = {'connect':'서버 SSH 연결', 'environment':'서버 환경 확인',
          'prepare':'서버 추출 도구 준비', 'server':'서버 DB 추출·메일 검사',
          'transfer':'SFTP 원본 전송', 'verify':'서버 원본 변경 검사',
          'catalog':'검색 DB 생성', 'mail':'메일 바로보기 생성', 'index':'인덱스 생성',
          'zip':'ZIP 생성', 'publish':'백업 폴더 저장', 'cleanup':'서버 임시 작업 정리',
          'validate':'기존 백업 확인'}
BACKUP = ('connect','prepare','server','transfer','verify','catalog','mail','index','publish','cleanup')


def duration(seconds):
    seconds=max(0,int(seconds))
    return '%02d:%02d:%02d'%(seconds//3600,seconds//60%60,seconds%60)


def amount(value, unit):
    if unit=='bytes':
        if value>=1024**3:
            return '%.2f GiB'%(value/1024**3)
        return '%.1f MiB'%(value/1024**2)
    return f'{value:,}'+{'messages':'건','records':'건','files':'개','steps':'단계'}.get(unit,'')


class Reporter:
    def __init__(self, callback=None, clock=None, interval=.15):
        self.callback=callback
        self.clock=clock or time.monotonic
        self.interval=interval
        self.previous=None
        self.last=-float('inf')

    def __call__(self, stage, completed=None, total=None, unit='', detail='', force=False):
        if self.callback is None:
            return
        completed=completed if type(completed) is int and completed>=0 else None
        total=total if type(total) is int and total>=0 else None
        event=dict(stage=stage,completed=completed,total=total,unit=unit,detail=detail)
        now=self.clock()
        changed=self.previous is None or any(event[key]!=self.previous[key] for key in ('stage','unit'))
        finished=total is not None and completed is not None and completed>=total
        was_finished=self.previous is not None and self.previous['total'] is not None and self.previous['completed'] is not None and self.previous['completed']>=self.previous['total']
        if force or changed or (finished and not was_finished) or now-self.last>=self.interval:
            self.callback(event)
            self.previous,self.last=event,now


class ProgressState:
    def __init__(self, clock=None):
        self.clock=clock or time.monotonic
        self.started=None

    def begin(self, mode='backup', make_zip=False):
        self.stages=list(BACKUP if mode=='backup' else ('connect','environment') if mode=='check' else ('validate','mail','index'))
        if make_zip and mode=='backup':
            self.stages.insert(self.stages.index('publish'),'zip')
        self.started=self.stage_started=self.clock()
        self.stopped=None
        self.result=''
        self.event=dict(stage=self.stages[0],completed=None,total=None,unit='',detail='')

    def update(self, event):
        if self.started is None or self.stopped is not None or event['stage'] not in self.stages:
            return
        if event['stage']!=self.event['stage']:
            self.stage_started=self.clock()
        self.event=dict(event)

    def finish(self, success, cancelled=False):
        if self.started is not None:
            self.stopped=self.clock()
            self.result='완료' if success else '중단' if cancelled else '오류'

    def view(self):
        now=self.clock() if self.stopped is None else self.stopped
        index=self.stages.index(self.event['stage'])
        completed,total=self.event['completed'],self.event['total']
        known=completed is not None and total is not None
        fraction=min(1,completed/total) if known and total else 1 if known else None
        overall=len(self.stages) if self.result=='완료' else index
        if self.result=='완료':
            fraction=1
        stage=f'현재 단계 {index+1}/{len(self.stages)} · '+LABELS[self.event['stage']]
        if self.result:
            stage+=' · '+self.result
        count=(amount(completed,self.event['unit'])+' / '+amount(total,self.event['unit'])+f' · {fraction*100:.1f}%') if known else '처리량 확인 중'
        if self.result=='완료':
            count='완료'
        elif self.result:
            count+=' · '+self.result
        if self.event['detail']:
            count+=' · '+self.event['detail']
        return dict(stage=stage,overall=100*overall/len(self.stages),current=None if fraction is None else fraction*100,
                    overall_text=f'전체 단계 진행 · {overall}/{len(self.stages)}단계 완료',detail=count,
                    elapsed='전체 경과 '+duration(now-self.started)+' · 현재 단계 '+duration(now-self.stage_started))
