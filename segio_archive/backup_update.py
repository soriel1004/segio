"""Named SSH workspaces and transactional, overlapping personal archive updates."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .archive import atomic_write,safe_name,utc_now
from .compact_mail import digest_file,build_compact_catalog,read_headers
from .dates import KST,text_timestamp
from .web import CollectionError


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path,value):
    atomic_write(Path(path),json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))


def complete(path):
    try:
        data=load(Path(path)/'manifest.json')
        return (data.get('viewer')=='sqlite-mail/1' and bool(data.get('portable_mail'))
                and bool(data.get('browser_index')) and (Path(path)/'catalog.sqlite3').is_file())
    except (OSError,ValueError):return False


def cutoff(path,kinds=None):
    data=load(Path(path)/'manifest.json')
    stamp=text_timestamp(data.get('last_backup_completed_at') or data.get('finished_at'))
    if kinds:
        values=[]
        for kind in kinds:
            previous=data.get('backup_completed_by_kind',{}).get(kind)
            if previous is None and data.get('ssh_export',{}).get(kind):
                previous=data.get('last_backup_completed_at') or data.get('finished_at')
            value=text_timestamp(previous)
            if value is None:return None
            values.append(value)
        stamp=min(values)
    if stamp is None:raise CollectionError('이전 백업 완료 날짜를 확인할 수 없습니다. [BACKUP_DATE_MISSING]')
    day=datetime.fromtimestamp(stamp,KST).replace(hour=0,minute=0,second=0,microsecond=0)
    return int((day-timedelta(days=15)).timestamp())


def named_folder(output,user):
    return Path(output)/('SSH-'+safe_name(user,100))


def hide_control(path):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetFileAttributesW.argtypes=[wintypes.LPCWSTR];kernel.GetFileAttributesW.restype=wintypes.DWORD
        kernel.SetFileAttributesW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD];kernel.SetFileAttributesW.restype=wintypes.BOOL
        attributes=kernel.GetFileAttributesW(str(path))
        if attributes!=0xffffffff:kernel.SetFileAttributesW(str(path),attributes|2)


def workspace(output,user,identity,resume=True):
    """Migrate old numeric caches without rereading or copying their raw files."""
    output=Path(output).resolve()
    if (output/'manifest.json').is_file():
        if load(output/'manifest.json').get('user')!=user:raise CollectionError('선택한 백업의 직원 ID가 다릅니다.')
        output=selection_output(output)
    output.mkdir(parents=True,exist_ok=True)
    control=output/'.segio-ssh-cache'
    if control.is_symlink():raise CollectionError('백업 작업 폴더의 링크를 사용할 수 없습니다.')
    control.mkdir(mode=0o700,exist_ok=True);hide_control(control)
    token=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:24]
    cache=control/safe_name(user,100)
    legacy=control/token
    if not cache.exists() and legacy.exists():legacy.rename(cache)
    if cache.is_symlink():raise CollectionError('백업 작업 폴더의 링크를 사용할 수 없습니다.')
    cache.mkdir(mode=0o700,exist_ok=True)
    target=named_folder(output,user)
    journal=cache/'publish.json'
    if journal.exists():
        operation=load(journal);retired=cache/'previous'
        if operation.get('target')!=str(target):raise CollectionError('백업 교체 기록을 확인하세요.')
        if target.exists() and not Path(operation['stage']).exists():
            plan=load(cache/'plan.json');plan['published']=True;save(cache/'plan.json',plan)
            if retired.exists():shutil.rmtree(retired)
        elif retired.exists():
            base=Path(operation['base'])
            if base.parent!=output or base.exists():raise CollectionError('기존 백업 복구 경로를 확인하세요.')
            retired.rename(base)
        journal.unlink()
    binding=cache/'identity.json'
    if binding.exists() and load(binding)!=identity:
        raise CollectionError('같은 ID의 다른 서버 작업이 있습니다. 다른 저장 상위 폴더를 지정하세요. [BACKUP_ORIGIN_MISMATCH]')
    save(binding,identity)
    if target.is_symlink():raise CollectionError('백업 대상 폴더의 링크를 사용할 수 없습니다.')
    old_contents=cache/'contents'
    if old_contents.is_dir() and not (cache/'plan.json').exists() and (not target.exists() or not any(target.iterdir())):
        if target.exists():target.rmdir()
        old_contents.rename(target)
    plan_path=cache/'plan.json'
    if plan_path.exists():
        plan=load(plan_path)
        base=Path(plan['base']) if plan.get('base') else None
        if base is not None and base.parent!=output:raise CollectionError('추가 백업의 기준 경로를 확인하세요.')
    else:
        candidates=[path for path in [target]+list(output.glob('segio-ssh-'+safe_name(user,100)+'*'))
                    if complete(path) and load(path/'manifest.json').get('user')==user]
        base=target if target in candidates else max(candidates,key=lambda path:text_timestamp(load(path/'manifest.json').get('last_backup_completed_at') or load(path/'manifest.json').get('finished_at')) or 0) if candidates else None
        if target.exists() and base!=target and not (target/'manifest.json').is_file() and any(target.iterdir()):
            raise CollectionError('대상 폴더에 다른 파일이 있습니다. 다른 저장 상위 폴더를 지정하세요.')
        # A migrated pending capture retains its original full/dated request.
        pending=(cache/'session.json').exists()
        plan={'base':str(base) if base else None,'since':None if pending or base is None else cutoff(base,[key for key in ('approvals','mail') if identity[key]]),
              'revision':digest_file(base/'manifest.json') if base else None}
        save(plan_path,plan)
    if plan.get('published'):
        return output,cache,target,None,plan
    if not resume:
        (cache/'session.json').unlink(missing_ok=True)
        if (cache/'incoming').exists():shutil.rmtree(cache/'incoming')
    data=cache/'incoming' if base else target
    if base and target!=base and target.exists() and not complete(target) and not data.exists():target.rename(data)
    if data.is_symlink():raise CollectionError('백업 데이터 폴더의 링크를 사용할 수 없습니다.')
    data.mkdir(mode=0o700,parents=True,exist_ok=True)
    return output,cache,data,base,plan


def verify_sources(root,manifest,checkpoint,progress=None,stage='validate'):
    """Check source data; indexes/pages can be regenerated after interruption."""
    from .db_client import safe_member
    from .progress import Reporter
    report=Reporter(progress);done=0
    entries=[(name,value) for name,value in manifest['files'].items() if name not in ('index.html','catalog.sqlite3') and not name.startswith(('search/','viewer/','mail/offline/'))]
    total=sum(expected['bytes'] for _,expected in entries)
    for name,expected in entries:
        checkpoint();safe_member(name)
        path=root/name
        if not path.resolve().is_relative_to(root):raise CollectionError('원본 경로가 보관 폴더를 벗어납니다.')
        if digest_file(path,checkpoint,lambda count,size:report(stage,done+count,total,'bytes','기존 원본 SHA-256 확인'))!=expected:raise CollectionError('기존 백업의 원본 검증에 실패했습니다. [BACKUP_SOURCE_CHANGED]')
        done+=expected['bytes']


def mail_fingerprint(path,begin,end,checkpoint,with_raw=False):
    digest=hashlib.sha256();canonical=digest.copy()
    with path.open('rb') as stream:
        stream.seek(begin)
        while begin<end:
            checkpoint();block=stream.read(min(1024*1024,end-begin))
            if not block:raise CollectionError('기존 메일 원본이 잘렸습니다.')
            # MBOX writers may append separator newlines to a previous final
            # message. Ignore only terminal CR/LF for comparison, preserving
            # every original byte and a separate exact hash for integrity.
            trimmed=block.rstrip(b'\r\n')
            if trimmed:
                digest.update(trimmed);canonical=digest.copy();digest.update(block[len(trimmed):])
            else:digest.update(block)
            begin+=len(block)
    return (canonical.hexdigest(),digest.hexdigest()) if with_raw else canonical.hexdigest()


def link_copy(source,target):
    try:os.link(source,target)
    except OSError:shutil.copy2(source,target)


def merge(base,incoming,stage,checkpoint=lambda:None,log=lambda _:None,progress=None):
    """Copy-on-write staging: older mail IDs/pages and large raw files stay stable."""
    from .mail_folders import MailFolders,classify
    from .portable_mail import build_portable_mail
    from .search_page import install_search_page
    from .db_client import safe_member
    old,new=load(base/'manifest.json'),load(incoming/'manifest.json')
    if old['user']!=new['user']:raise CollectionError('추가 백업의 직원 ID가 다릅니다.')
    if any((base/name).exists() for name in ('.segio-edit','.segio-browser-edit')):
        raise CollectionError('기존 백업의 삭제 작업을 완료하거나 복구한 뒤 다시 갱신하세요.')
    verify_sources(base,old,checkpoint,progress,'catalog')
    if digest_file(base/'catalog.sqlite3',checkpoint)!=old['files']['catalog.sqlite3']:
        raise CollectionError('기존 검색 DB가 변경되었습니다. [BACKUP_SOURCE_CHANGED]')
    if stage.exists():shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    # Only declared files are carried forward; atomic writers never modify a
    # hard-linked original. This does not duplicate 50GB MBOX/attachment bytes.
    for name in list(old['files'])+['manifest.json']:
        checkpoint();safe_member(name)
        source=base/name;target=stage/name
        if not source.resolve().is_relative_to(base) or source.is_symlink():raise CollectionError('기존 백업 원본 링크를 확인하세요.')
        target.parent.mkdir(parents=True,exist_ok=True);link_copy(source,target)
    result=json.loads(json.dumps(old));files=result['files']
    catalog=load(stage/'catalog.json')
    by_id={item['id']:item for item in catalog['items']}
    fresh=load(incoming/'catalog.json')
    replacing={item['id'] for item in fresh['items']}
    keep=[item for item in catalog['items'] if item['id'] not in replacing]+fresh['items']
    protected={name for item in keep for name in [item['path'],item['json']]+[a['path'] for a in item.get('attachments',[])]}
    # Moving an existing ID to a bucket must not leave an orphaned old page
    # behind after later deletion. Attachments referenced by other versions stay.
    obsolete_directories=set()
    for item in fresh['items']:
        previous=by_id.get(item['id'])
        if previous and previous['path']!=item['path']:
            safe_member(previous['path'])
            directory=Path(previous['path']).parent.as_posix()
            if not directory.startswith('documents/'):raise CollectionError('기존 결재 경로를 확인하세요.')
            obsolete_directories.add(directory)
    for name in list(files):
        if name not in protected and any(parent.as_posix() in obsolete_directories for parent in Path(name).parents):
            (stage/name).unlink();files.pop(name)
    for item in fresh['items']:
        by_id[item['id']]=item
        for name in [item['path'],item['json']]+[a['path'] for a in item.get('attachments',[])]:
            checkpoint();safe_member(name)
            target=stage/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.unlink(missing_ok=True);shutil.copy2(incoming/name,target);files[name]=new['files'][name]
    save(stage/'catalog.json',{'items':list(by_id.values())});files['catalog.json']=digest_file(stage/'catalog.json')
    # SQLite is a private copy, never a writable hard link to the old catalog.
    (stage/'catalog.sqlite3').unlink();shutil.copy2(base/'catalog.sqlite3',stage/'catalog.sqlite3')
    old_sources=load(stage/'mail/sources.json') if (stage/'mail/sources.json').exists() else {'user':old['user'],'sources':[]}
    new_sources=load(incoming/'mail/sources.json') if (incoming/'mail/sources.json').exists() else {'sources':[]}
    folder=MailFolders(base)
    added=0
    with closing(sqlite3.connect(stage/'catalog.sqlite3')) as db:
        db.execute('CREATE TABLE IF NOT EXISTS mail_fingerprints (id TEXT PRIMARY KEY,folder TEXT,digest TEXT)')
        known={identity for identity, in db.execute('SELECT id FROM mail_fingerprints')}
        records=db.execute('SELECT m.id,m.source,m.begin,m.end,i.folders FROM mail_records m JOIN items i ON m.id=i.id').fetchall()
        for identity,source,begin,end,name in records:
            checkpoint()
            if identity not in known:
                db.execute('INSERT INTO mail_fingerprints VALUES (?,?,?)',(identity,folder.info(name)['id'],mail_fingerprint(base/source,begin,end,checkpoint)))
        signatures={(name,digest):count for name,digest,count in db.execute('SELECT folder,digest,count(*) FROM mail_fingerprints GROUP BY folder,digest')}
        seen={}
        next_id=1+max([int(identity.rsplit('-',1)[-1]) for identity in known]+[int(identity.rsplit('-',1)[-1]) for identity,_,_,_,_ in records]+[-1])
        source_id=1+max([int(Path(s['path']).stem) for s in old_sources['sources']]+[-1])
        from .mail_import import header_text
        for source in new_sources['sources']:
            checkpoint()
            path=incoming/source['path']
            from .compact_mail import mbox_spans
            spans=mbox_spans(path,checkpoint) if source['format']=='mbox' else [(0,source['bytes'])]
            if source['format'] not in ('mbox','rfc822'):
                if any(s['name']==source['name'] and s['sha256']==source['sha256'] for s in old_sources['sources']):continue
                name=f'mail/raw/{source_id:06d}.bin';source_id+=1
                target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);link_copy(path,target)
                files[name]=new['files'][source['path']];old_sources['sources'].append(dict(source,path=name))
                continue
            box=classify(source['name'],source.get('folder_format',source['format']))['id']
            for begin,end in spans:
                digest,raw_digest=mail_fingerprint(path,begin,end,checkpoint,with_raw=True)
                key=(box,digest);seen[key]=seen.get(key,0)+1
                if seen[key]<=signatures.get(key,0):continue
                name=f'mail/raw/{source_id:06d}.eml';source_id+=1
                target=stage/name;target.parent.mkdir(parents=True,exist_ok=True)
                with path.open('rb') as stream,target.open('xb') as output:
                    stream.seek(begin);position=begin
                    while position<end:
                        checkpoint();block=stream.read(min(1024*1024,end-position))
                        if not block:raise CollectionError('추가 메일 원본이 잘렸습니다.')
                        output.write(block);position+=len(block)
                details={'bytes':end-begin,'sha256':raw_digest};files[name]=details
                old_sources['sources'].append(dict(source,**details,path=name,format='rfc822',folder_format=source.get('folder_format',source['format'])))
                with target.open('rb') as stream:message=read_headers(stream,0,end-begin)
                identity=f'mail-{next_id:09d}';next_id+=1
                title=header_text(message.get('Subject')) or '제목 없음'
                sender,receiver,date=(header_text(message.get(k)) for k in ('From','To','Date'))
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',(identity,'mail',title,source['name'],'\n'.join((sender,receiver,date)),'mail/'+identity))
                db.execute('INSERT INTO mail_records VALUES (?,?,?,?,?,?,?)',(identity,name,0,end-begin,sender,receiver,date))
                db.execute('INSERT INTO mail_fingerprints VALUES (?,?,?)',(identity,box,digest));added+=1
        for item in fresh['items']:
            identity='approval-'+item['id']
            db.execute('INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?)',(identity,'approval',item['title'],'결재 문서',item['search'],item['path']))
            from .approval_folders import compact_record
            db.execute('INSERT OR REPLACE INTO document_records VALUES (?,?)',(identity,json.dumps(compact_record(load(incoming/item['json'])),ensure_ascii=False)))
        db.commit()
        result['counts']=dict(db.execute('SELECT kind,count(*) FROM items GROUP BY kind'))
        result['counts'].setdefault('mail',0);result['counts'].setdefault('approval',0)
    save(stage/'mail/sources.json',old_sources);files['mail/sources.json']=digest_file(stage/'mail/sources.json')
    files['catalog.sqlite3']=digest_file(stage/'catalog.sqlite3')
    result['counts']['mail_files']=len(old_sources['sources'])
    result.update(ssh_export=new['ssh_export'],collector_version=new['collector_version'],finished_at=new.get('finished_at',old.get('finished_at')))
    if new.get('document_layout'):result['document_layout']='catalog-paths/1'
    result['retained_ssh_jobs']=list(dict.fromkeys(old.get('retained_ssh_jobs',[])+[old['ssh_export']['job']]))
    result['warnings']=list(dict.fromkeys(old.get('warnings',[])+new.get('warnings',[])))
    result.pop('portable_mail',None);result.pop('browser_index',None)
    result['incremental']={'since':new['ssh_export'].get('since'),'overlap_days':15,'added_mail':added,
                           'updated_approval':len(fresh['items']),'previous_completed_at':old.get('last_backup_completed_at',old.get('finished_at'))}
    # Keep both old and new missing-file diagnostics; an incremental export's
    # report must not disappear when its temporary transfer folder is removed.
    diagnostics=[];seen_issues=set()
    for folder,metadata in ((base,old),(incoming,new)):
        if 'issues.json' not in metadata['files']:continue
        values=load(folder/'issues.json')
        if not isinstance(values,list):raise CollectionError('결재 확인 보고서의 형식을 확인하세요.')
        for value in values:
            key=json.dumps(value,sort_keys=True,ensure_ascii=True)
            if key not in seen_issues:diagnostics.append(value);seen_issues.add(key)
    if 'issues.json' in old['files'] or 'issues.json' in new['files']:
        save(stage/'issues.json',diagnostics);files['issues.json']=digest_file(stage/'issues.json')
    summary=('SEGIO_SSH_MERGED_V1\nApproval documents: %d\nMail messages: %d\n'
             'Updated approval documents: %d\nAdded mail messages: %d\n'
             'Approval diagnostic entries (including earlier captures): %d\n'
             'Account deletion verified: false\nEND_SEGIO_SSH_MERGED_V1\n' %
             (result['counts']['approval'],result['counts']['mail'],len(fresh['items']),added,len(diagnostics)))
    atomic_write(stage/'export-summary.txt',summary.encode('ascii'));files['export-summary.txt']=digest_file(stage/'export-summary.txt')
    result['incremental']['snapshot_rows']=new.get('snapshot_rows',{})
    for entry in result.get('coverage',[]):
        if entry.get('kind')=='approval':entry['saved']=result['counts']['approval']
        elif entry.get('kind')=='mail_index':entry['saved']=result['counts']['mail']
        elif entry.get('kind')=='mail':entry['files']=len(old_sources['sources'])
    result.setdefault('coverage',[]).append(dict(kind='incremental',since=new['ssh_export'].get('since'),
                                               updated_approval=len(fresh['items']),added_mail=added))
    save(stage/'manifest.json',result)
    log(f'추가 백업 병합: 새 메일 {added:,}건 · 결재 {len(fresh["items"]):,}건 · 기존 메일 ID/페이지 유지')
    result=build_portable_mail(stage,result,checkpoint,log,progress)
    return install_search_page(stage,result,checkpoint,progress)


def publish(stage,target,cache,base=None,revision=None):
    """Journal both renames; recover the complete old archive after interruption."""
    if base and digest_file(base/'manifest.json')!=revision:raise CollectionError('기존 백업이 변경되었습니다. 다시 갱신하세요. [BACKUP_BASE_CHANGED]')
    if stage==target:
        plan=load(cache/'plan.json');plan['published']=True;save(cache/'plan.json',plan)
        return target
    retired=cache/'previous'
    if retired.exists():shutil.rmtree(retired)
    save(cache/'publish.json',{'target':str(target),'base':str(base) if base else None,'stage':str(stage)})
    try:
        if base:base.rename(retired)
        stage.rename(target)
    except BaseException:
        if retired.exists() and not target.exists():retired.rename(base)
        raise
    plan=load(cache/'plan.json');plan['published']=True;save(cache/'plan.json',plan)
    (cache/'publish.json').unlink()
    if retired.exists():shutil.rmtree(retired)
    if base and base!=target:
        for receipt in cache.parent.glob('cleanup-*.json'):
            data=load(receipt)
            if data.get('backup')==str(base):data['backup']=str(target);save(receipt,data)
    return target


def resolve_selection(path,user=''):
    path=Path(path).resolve()
    if (path/'manifest.json').is_file():return path
    choices=list(path.glob('*/manifest.json'))+list(path.glob('.segio-ssh-cache/*/contents/manifest.json'))
    if user:choices=[item for item in choices if load(item).get('user')==user]
    if len(choices)!=1:raise CollectionError('직원 ID 폴더 또는 중단된 contents 폴더를 직접 선택하세요.')
    return choices[0].parent


def selection_output(path):
    path=Path(path).resolve()
    if path.name in ('contents','incoming','merged') and path.parent.parent.name=='.segio-ssh-cache':return path.parent.parent.parent
    return path.parent


def repair_local(path,checkpoint=lambda:None,log=lambda _:None,progress=None,origin=None):
    """Finish downloaded raw checkpoints as well as old complete local archives."""
    from .portable_mail import prepare_archive
    original_progress=progress
    def mapped(event):
        if original_progress:original_progress(dict(event,stage='validate') if event['stage']=='catalog' else event)
    progress=mapped
    root=Path(path).resolve();manifest=load(root/'manifest.json');was_complete=complete(root)
    cache=root.parent if root.name in ('contents','incoming','merged') and root.parent.parent.name=='.segio-ssh-cache' else root.parent/'.segio-ssh-cache'/safe_name(manifest['user'],100)
    if cache.parent.exists():hide_control(cache.parent)
    plan=load(cache/'plan.json') if (cache/'plan.json').exists() else {}
    output=cache.parent.parent if cache.parent.name=='.segio-ssh-cache' else root.parent
    target=named_folder(output,manifest['user'])
    if plan.get('base'):
        was_complete=False
        base=Path(plan['base'])
        if base.parent!=output or digest_file(base/'manifest.json')!=plan['revision']:
            raise CollectionError('추가 백업 기준이 변경되었습니다. 수집 시작에서 다시 진행하세요.')
        incoming=cache/'incoming'
        verify_sources(incoming,load(incoming/'manifest.json'),checkpoint,progress)
        merge(base,incoming,cache/'merged',checkpoint,log,progress)
        root=publish(cache/'merged',target,cache,base,plan['revision'])
    elif manifest.get('viewer')=='sqlite-mail/1':prepare_archive(root,checkpoint,log,progress)
    elif manifest.get('ssh_export',{}).get('authentication')=='administrator_ssh':
        verify_sources(root,manifest,checkpoint,progress)
        build_compact_catalog(root,manifest['user'],checkpoint,log,progress)
    else:raise CollectionError('SSH 백업 정보가 있는 폴더를 선택하세요.')
    manifest=load(root/'manifest.json')
    if root.name in ('contents','incoming') and root.parent.parent.name=='.segio-ssh-cache':
        if target.exists():raise CollectionError('직원 ID 폴더가 이미 있습니다. 수집 시작으로 이어받기/병합하세요.')
        root.rename(target);root=target
    elif root.parent==output and root!=target and complete(root):
        if target.exists():raise CollectionError('직원 ID 폴더가 이미 있습니다. 기존 백업을 보존했습니다.')
        root.rename(target);root=target
    manifest['last_backup_completed_at']=(manifest.get('last_backup_completed_at') or manifest.get('finished_at')) if was_complete else utc_now()
    for kind in ('approvals','mail'):
        if manifest.get('ssh_export',{}).get(kind) and (not was_complete or kind not in manifest.get('backup_completed_by_kind',{})):
            manifest.setdefault('backup_completed_by_kind',{})[kind]=manifest['last_backup_completed_at']
    save(root/'manifest.json',manifest)
    session=load(cache/'session.json') if (cache/'session.json').exists() else None
    binding=load(cache/'identity.json') if (cache/'identity.json').exists() else origin
    if session and binding:
        import re,posixpath
        remote=session.get('remote','')
        if re.fullmatch(r'/[^\x00]*?/\.segio-ssh-[0-9a-f]{32}',remote) and posixpath.basename(remote)==manifest['ssh_export']['job']:
            record={'identity':{'host':binding['host'],'port':binding['port'],'ssh_user':binding['ssh_user'],'user':manifest['user']},
                    'backup':str(root),'job':posixpath.basename(remote),
                    'paths':list(dict.fromkeys([remote]+session.get('retired',[]))),'validated':[]}
            save(cache.parent/('cleanup-'+record['job']+'.json'),record)
            if not root.is_relative_to(cache):shutil.rmtree(cache)
    return root
