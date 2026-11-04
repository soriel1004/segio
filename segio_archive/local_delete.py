"""Delete selected local records, rewriting shared MBOX files transactionally."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import stat

from .archive import atomic_write, utc_now
from .compact_mail import digest_file, mbox_spans
from .db_client import safe_member
from .portable_mail import VERSION as MAIL_VERSION, page_path
from .web import Cancelled, CollectionError

WORK = '.segio-edit'
PROTOCOL = 'segio-local-delete/1'
BLOCK = 1024*1024


@contextmanager
def edit_lock(root):
    path=local(root,'.segio-edit.lock')
    with path.open('a+b') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_nlink>1:
            raise CollectionError('백업본 편집 잠금 파일을 확인하세요.')
        if os.fstat(stream.fileno()).st_size==0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise CollectionError('다른 창에서 백업본을 편집 중입니다.') from None
        try:
            yield
        finally:
            if os.name=='nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def local(root, name):
    safe_member(name)
    path=root
    for part in Path(name).parts:
        path=path/part
        if path.is_symlink() or getattr(path,'is_junction',lambda:False)():
            raise CollectionError('심볼릭 링크가 있는 백업본은 편집하지 않습니다.')
    if not path.resolve().is_relative_to(root):
        raise CollectionError('백업 폴더를 벗어난 경로입니다.')
    return path


def write_json(path, value):
    atomic_write(path,json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))


def checked(root, name, files):
    path=local(root,name)
    if name not in files or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise CollectionError('삭제 대상 파일 또는 검증 정보를 확인하지 못했습니다.')
    if digest_file(path)!=files[name]:
        raise CollectionError('백업 파일이 변경되었습니다. 파일 무결성을 먼저 확인하세요.')
    return path


def selected(db, ids):
    if not isinstance(ids,list) or not 1<=len(ids)<=1000 or any(not isinstance(i,str) or len(i)>150 for i in ids) or len(set(ids))!=len(ids):
        raise CollectionError('현재 페이지에서 삭제할 자료를 1~1,000건 선택하세요.')
    rows=[]
    for start in range(0,len(ids),500):
        batch=ids[start:start+500]
        rows.extend(db.execute('SELECT id,kind,title,html FROM items WHERE id IN ('+','.join('?' for _ in batch)+')',batch).fetchall())
    if len(rows)!=len(ids) or any(row[1] not in ('approval','mail') for row in rows):
        raise CollectionError('선택한 자료가 바뀌었습니다. 목록을 새로고침하세요.')
    return rows


def preview(root, ids):
    root=Path(root).resolve()
    if (root/'.segio-browser-edit').exists():
        raise CollectionError('인덱스에서 브라우저 삭제 또는 복구를 완료한 뒤 다시 시도하세요.')
    if (root/WORK).exists():
        raise CollectionError('삭제 처리 또는 복구가 진행 중입니다.')
    manifest=json.loads(local(root,'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('viewer')!='sqlite-mail/1' or manifest.get('portable_mail',{}).get('version')!=MAIL_VERSION:
        raise CollectionError('기존 백업 갱신을 먼저 실행하세요.')
    files=manifest['files']
    if len({name.casefold() for name in files})!=len(files):
        raise CollectionError('대소문자만 다른 파일 경로가 있어 편집하지 않습니다.')
    for name in files:
        local(root,name)
    checked(root,'index.html',files)
    checked(root,'catalog.sqlite3',files)
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        rows=selected(db,ids)
        sources=set()
        retained_sources=set()
        for identity,kind,_,_ in rows:
            if kind=='mail':
                row=db.execute('SELECT source FROM mail_records WHERE id=?',(identity,)).fetchone()
                if row is None or not row[0].startswith('mail/raw/') or row[0] not in files:
                    raise CollectionError('메일 원본 연결 정보를 확인하지 못했습니다.')
                sources.add(row[0])
        for name in sources:
            if any(identity not in ids for identity, in db.execute('SELECT id FROM mail_records WHERE source=?',(name,))):
                retained_sources.add(name)
    temporary=sum(files[name]['bytes'] for name in retained_sources)+files['catalog.sqlite3']['bytes']*3+64*1024*1024
    return dict(ids=ids,revision=digest_file(root/'manifest.json')['sha256'],
                approval=sum(row[1]=='approval' for row in rows),mail=sum(row[1]=='mail' for row in rows),
                titles=[row[2] for row in rows],temporary_bytes=temporary,raw_files=len(sources))


def journal(work, value):
    write_json(work/'journal.json',value)
    with (work/'journal.json').open('r+b') as stream:
        os.fsync(stream.fileno())


def recover(root):
    """Rollback an interrupted install; a committed operation only needs cleanup."""
    root=Path(root).resolve()
    work=root/WORK
    if (root/'.segio-browser-edit').exists():
        raise CollectionError('index.html에서 삭제 버튼을 눌러 백업 폴더를 선택하고 브라우저 삭제 복구를 완료하세요.')
    if not work.exists():
        return
    with edit_lock(root):
        _recover(root)


def _recover(root):
    work=root/WORK
    if work.is_symlink():
        raise CollectionError('삭제 작업 폴더를 확인하세요.')
    data=json.loads(local(work,'journal.json').read_text(encoding='utf-8'))
    if data.get('protocol')!=PROTOCOL or data.get('phase') not in ('preparing','installing','committed'):
        raise CollectionError('삭제 복구 기록을 확인하지 못했습니다.')
    if data['phase']=='installing':
        for record in reversed(data['paths']):
            name=record['name']
            if name not in ('manifest.json','catalog.json','catalog.sqlite3','index.html') and not name.startswith(('documents/','mail/','search/','viewer/')):
                raise CollectionError('삭제 복구 경로가 올바르지 않습니다.')
            target=local(root,name)
            backup=local(work/'backup',name)
            if backup.is_file():
                target.parent.mkdir(parents=True,exist_ok=True)
                os.replace(backup,target)
            elif not record['existed'] and target.is_file():
                target.unlink()
    shutil.rmtree(work)


def rewrite_mbox(root, stage, source, records, deleted, expected, checkpoint, report):
    path=local(root,source)
    output=local(stage,source)
    output.parent.mkdir(parents=True,exist_ok=True)
    old_hash,new_hash=hashlib.sha256(),hashlib.sha256()
    frame_start=0
    offsets={}
    iterator=iter(records)
    with path.open('rb') as stream,output.open('wb') as target:
        for begin,end in mbox_spans(path,checkpoint):
            checkpoint()
            row=next(iterator,None)
            if row is None or row[2]!=begin or not begin<=row[3]<=end:
                raise CollectionError('메일 위치 기록과 MBOX가 일치하지 않습니다.')
            if row[3]!=end:
                stream.seek(row[3])
                if end-row[3]>65536 or stream.read(end-row[3]).strip(b'\r\n'):
                    raise CollectionError('메일 끝 위치가 원본과 일치하지 않습니다.')
            keep=row[0] not in deleted
            destination=target.tell()
            stream.seek(frame_start)
            remaining=end-frame_start
            while remaining:
                checkpoint()
                block=stream.read(min(BLOCK,remaining))
                if not block:
                    raise CollectionError('메일 원본 읽기가 중단됐습니다.')
                old_hash.update(block)
                if keep:
                    target.write(block)
                    new_hash.update(block)
                remaining-=len(block)
                report('mail',stream.tell(),expected['bytes'])
            if keep:
                offsets[row[0]]=(destination+begin-frame_start,destination+row[3]-frame_start)
            frame_start=end
        target.flush()
        os.fsync(target.fileno())
        new_size=target.tell()
    if next(iterator,None) is not None or frame_start!=expected['bytes'] or old_hash.hexdigest()!=expected['sha256']:
        raise CollectionError('메일 원본 또는 위치 기록이 변경됐습니다. 삭제를 적용하지 않았습니다.')
    return offsets,dict(bytes=new_size,sha256=new_hash.hexdigest())


def delete_items(root, ids, revision, checkpoint=lambda:None, report=lambda stage,done,total:None, fault=lambda name:None):
    root=Path(root).resolve()
    with edit_lock(root):
        return _delete_items(root,ids,revision,checkpoint,report,fault)


def _delete_items(root, ids, revision, checkpoint, report, fault):
    root=Path(root).resolve()
    plan=preview(root,ids)
    if plan['revision']!=revision:
        raise CollectionError('백업본이 변경되었습니다. 삭제 범위를 다시 확인하세요.')
    if shutil.disk_usage(root).free<plan['temporary_bytes']:
        raise CollectionError('메일 재구성과 안전한 적용에 필요한 임시 공간이 부족합니다.')
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    files=manifest['files']
    original_files=dict(files)
    fingerprints={}
    for index,name in enumerate(files):
        checkpoint()
        path=local(root,name)
        if path.is_file():
            info=path.stat()
            fingerprints[name]=(info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        report('prepare',index+1,len(files))
    work=root/WORK
    work.mkdir(mode=0o700)
    stage=work/'new'
    data=dict(protocol=PROTOCOL,phase='preparing',paths=[])
    try:
        stage.mkdir()
        journal(work,data)
    except BaseException:
        shutil.rmtree(work)
        raise
    committed=False
    try:
        checkpoint()
        if (root/'.segio-browser-edit').exists():
            raise CollectionError('인덱스에서 브라우저 삭제 처리가 진행 중입니다.')
        for name in ('catalog.json','mail/sources.json'):
            if name in files:
                source=checked(root,name,files)
                target=local(stage,name)
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(source,target)
        with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as original,closing(sqlite3.connect(stage/'catalog.sqlite3')) as db:
            original.backup(db)
            rows=selected(db,ids)
            deleted=set(ids)
            catalog=json.loads((stage/'catalog.json').read_text(encoding='utf-8')) if (stage/'catalog.json').exists() else {'items':[]}
            remaining_catalog=[item for item in catalog['items'] if 'approval-'+item['id'] not in deleted]
            protected={name for item in remaining_catalog for name in (item['path'],item['json'])}
            protected.update(a['path'] for item in remaining_catalog for a in item.get('attachments',[]))
            remove=set()
            raw_sources=set()
            for identity,kind,_,html in rows:
                if kind=='approval':
                    item=next((item for item in catalog['items'] if 'approval-'+item['id']==identity and item['path']==html),None)
                    if item is None:
                        raise CollectionError('결재 원본 색인이 일치하지 않습니다.')
                    safe_member(html)
                    parts=Path(html).parts
                    if not (len(parts)==3 and parts[0]=='documents' or
                            len(parts)==4 and parts[0]=='documents' and re.fullmatch(r'b-[0-9a-f]{2}',parts[1])):
                        raise CollectionError('결재 경로가 올바르지 않습니다.')
                    prefix=Path(html).parent.as_posix()+'/'
                    for attachment in item.get('attachments',[]):
                        name=attachment['path']
                        if name in files and name not in protected:
                            parts=Path(name).parts
                            if not (len(parts)>=4 and parts[0]=='documents' and parts[2]=='attachments' or
                                    len(parts)>=5 and parts[0]=='documents' and re.fullmatch(r'b-[0-9a-f]{2}',parts[1]) and parts[3]=='attachments'):
                                raise CollectionError('첨부 파일 경로가 올바르지 않습니다.')
                            remove.add(name)
                else:
                    raw_sources.add(db.execute('SELECT source FROM mail_records WHERE id=?',(identity,)).fetchone()[0])
                    prefix=Path(page_path(identity)).parent.as_posix()+'/'
                remove.update(name for name in files if name.startswith(prefix) and name not in protected)
            for index,name in enumerate(sorted(remove)):
                checkpoint()
                checked(root,name,files)
                report('files',index+1,len(remove))
            sources=json.loads((stage/'mail/sources.json').read_text(encoding='utf-8')) if (stage/'mail/sources.json').exists() else {'sources':[]}
            updated_sources={}
            for source in sorted(raw_sources):
                checkpoint()
                entry=next((s for s in sources['sources'] if s['path']==source),None)
                if entry is None or any(entry[key]!=files[source][key] for key in ('bytes','sha256')):
                    raise CollectionError('메일 원본 목록이 일치하지 않습니다.')
                records=db.execute('SELECT id,source,begin,end FROM mail_records WHERE source=? ORDER BY begin',(source,)).fetchall()
                if entry['format']=='mbox':
                    offsets,details=rewrite_mbox(root,stage,source,records,deleted,files[source],checkpoint,report)
                    if offsets:
                        updated_sources[source]=details
                        for identity,(begin,end) in offsets.items():
                            db.execute('UPDATE mail_records SET begin=?,end=? WHERE id=?',(begin,end,identity))
                    else:
                        remove.add(source)
                        local(stage,source).unlink()
                elif entry['format']=='rfc822' and len(records)==1 and records[0][0] in deleted:
                    if records[0][2]!=0 or records[0][3]!=files[source]['bytes']:
                        raise CollectionError('개별 메일의 원본 범위가 일치하지 않습니다.')
                    checked(root,source,files)
                    remove.add(source)
                else:
                    raise CollectionError('이 메일 형식은 개별 삭제를 지원하지 않습니다.')
            db.execute('PRAGMA secure_delete=ON')
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for identity in ids:
                db.execute('DELETE FROM items WHERE id=?',(identity,))
                for table in ('mail_records','document_records'):
                    if table in tables:
                        db.execute('DELETE FROM '+table+' WHERE id=?',(identity,))
            db.commit()
            db.execute('VACUUM')
            db.commit()
            manifest['counts']=dict(db.execute('SELECT kind,count(*) FROM items GROUP BY kind'))
            manifest['counts'].setdefault('approval',0)
            manifest['counts'].setdefault('mail',0)
            for identity,source,begin,end in db.execute('SELECT id,source,begin,end FROM mail_records'):
                checkpoint()
                name=Path(page_path(identity)).parent.as_posix()+'/state.json'
                checked(root,name,files)
                state=json.loads(local(root,name).read_text(encoding='utf-8'))
                if source in updated_sources:
                    state['source']=[MAIL_VERSION,source,begin,end,updated_sources[source]['sha256']]
                write_json(local(stage,name),state)
                files[name]=digest_file(local(stage,name))
        for source,details in updated_sources.items():
            files[source]=details
        if (stage/'catalog.json').exists():
            write_json(stage/'catalog.json',{'items':remaining_catalog})
            if 'document_records' not in tables:
                for item in remaining_catalog:
                    source=checked(root,item['json'],original_files)
                    target=local(stage,item['json'])
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(source,target)
        if (stage/'mail/sources.json').exists():
            sources['sources']=[dict(entry,**updated_sources.get(entry['path'],{})) for entry in sources['sources'] if entry['path'] not in remove]
            write_json(stage/'mail/sources.json',sources)
        remove.update(name for name in files if name.startswith('search/'))
        for name in remove:
            files.pop(name,None)
        for name in ('catalog.sqlite3','catalog.json','mail/sources.json'):
            if (stage/name).is_file():
                files[name]=digest_file(stage/name)
        if manifest.get('portable_mail'):
            manifest['portable_mail']['count']=manifest['counts']['mail']
        edits=manifest.setdefault('local_edits',{'deleted_approval':0,'deleted_mail':0})
        edits['deleted_approval']+=plan['approval']
        edits['deleted_mail']+=plan['mail']
        edits['updated_at']=utc_now()
        manifest.setdefault('coverage',[]).append(dict(kind='local_deletion',approval=plan['approval'],mail=plan['mail'],time=edits['updated_at']))
        manifest['browser_index']=''
        from .search_page import install_search_page
        report('index',0,1)
        install_search_page(stage,manifest,checkpoint)
        operations=set(remove)
        for path in stage.rglob('*'):
            if path.is_file():
                name=path.relative_to(stage).as_posix()
                if name=='manifest.json' or manifest['files'].get(name)!=original_files.get(name):
                    operations.add(name)
        # The manifest is installed last. Until the journal is committed, any
        # interruption restores every original file from the same volume.
        ordered=sorted(operations-{'manifest.json'})+['manifest.json']
        if digest_file(root/'manifest.json')['sha256']!=revision:
            raise CollectionError('삭제 준비 중 백업본이 변경됐습니다.')
        for name in operations-{'manifest.json'}:
            path=local(root,name)
            if path.exists():
                before=fingerprints.get(name)
                now=path.stat()
                if before is None or before!=(now.st_ino,now.st_size,now.st_mtime_ns,now.st_ctime_ns):
                    raise CollectionError('삭제 준비 중 백업 파일이 변경됐습니다.')
        data['paths']=[dict(name=name,existed=local(root,name).is_file()) for name in ordered]
        for index,name in enumerate(ordered):
            checkpoint()
            replacement=local(stage,name)
            if replacement.is_file():
                with replacement.open('r+b') as stream:
                    os.fsync(stream.fileno())
            report('index',index+1,len(ordered))
        checkpoint()
        journal(work,dict(data,phase='installing'))
        report('commit',0,len(ordered))
        for index,name in enumerate(ordered):
            target=local(root,name)
            replacement=local(stage,name)
            if target.exists():
                backup=local(work/'backup',name)
                backup.parent.mkdir(parents=True,exist_ok=True)
                os.replace(target,backup)
            if replacement.is_file():
                target.parent.mkdir(parents=True,exist_ok=True)
                os.replace(replacement,target)
            fault(name)
            report('commit',index+1,len(ordered))
        journal(work,dict(data,phase='committed'))
        committed=True
        try:
            shutil.rmtree(work)
            pending=False
        except OSError:
            pending=True
        return dict(approval=plan['approval'],mail=plan['mail'],cleanup_pending=pending)
    except BaseException:
        if not committed:
            _recover(root)
        raise
