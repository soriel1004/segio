"""Safe diagnostics for local processing, including failures before mail #1."""
import json
from pathlib import Path
import sqlite3

from . import __version__
from .archive import atomic_write
from .progress import LABELS

LOCAL_STAGES={'catalog','mail','index','zip','publish','validate'}


def failure_details(error,stage,event=None):
    value={'protocol':'segio-local-processing/1','collector_version':__version__,
           'stage':stage if stage in LOCAL_STAGES else 'unknown',
           'exception_type':type(error).__name__ if type(error).__module__ in ('builtins','sqlite3') else 'unknown'}
    for field in ('errno','winerror','sqlite_errorcode'):
        number=getattr(error,field,None)
        if type(number) is int and 0<=number<=65535:value[field]=number
    if event and event.get('stage')==stage:
        for key in ('completed','total'):
            number=event.get(key)
            if type(number) is int and number>=0:value[key]=number
        if event.get('unit') in ('messages','bytes','records','files','steps'):value['unit']=event['unit']
    trace=error.__traceback__
    modules={'portable_mail','compact_mail','backup_update','search_page','index_maintenance',
             'archive','ssh_client','mail_import','presentation','sqlite_access'}
    while trace:
        code=trace.tb_frame.f_code;module=Path(code.co_filename).stem
        if module in modules:
            value['site']={'module':module,'function':code.co_name if code.co_name.isidentifier() else 'anonymous','line':trace.tb_lineno}
        trace=trace.tb_next
    return value


def processing_failure(error,stage,root,event=None,log=lambda _:None):
    detail=failure_details(error,stage,event)
    saved=None
    if root is not None:
        path=Path(root)/'processing-failure.json'
        try:
            atomic_write(path,json.dumps(detail,ensure_ascii=False,indent=2).encode('utf-8'))
            saved=path;log('로컬 처리 진단 저장: '+str(path))
        except (OSError,MemoryError):pass
    if isinstance(error,sqlite3.Error):
        code='LOCAL_DATABASE_ERROR';reason='Windows 메일 검색 DB를 열거나 읽지 못했습니다.'
    elif isinstance(error,MemoryError):
        code='LOCAL_MEMORY_LIMIT';reason='Windows 메일 처리 중 메모리가 부족합니다.'
    elif isinstance(error,OSError):
        from .ssh_transfer import TransferFault,fault_details
        code,reason,_,_=fault_details(TransferFault('local_processing',error))
    else:
        code='LOCAL_PROCESSING_ERROR';reason=LABELS.get(stage,'Windows 자료 처리')+' 중 처리 오류가 발생했습니다.'
    position=detail.get('site',{})
    summary=[detail['exception_type']]
    if position:summary.append('%s.%s:%s'%(position['module'],position['function'],position['line']))
    for key in ('errno','winerror','sqlite_errorcode'):
        if key in detail:summary.append('%s=%s'%(key,detail[key]))
    message=reason+' ['+code+']\n진단: '+' · '.join(summary)
    if saved:message+='\n진단 파일: '+str(saved)
    else:message+='\n진단 파일을 저장하지 못했습니다. 이 오류창을 보관하세요.'
    return message+'\n받은 원본과 완료한 페이지는 유지했습니다.'
