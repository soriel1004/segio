"""Shrink redundant approval payloads without changing original document files."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3

from .approval_folders import compact_record
from .archive import atomic_write
from .compact_mail import digest_file
from .web import CollectionError
from .sqlite_access import connect_readonly

THRESHOLD=64*1024*1024


def compact_approval_index(root, manifest, checkpoint=lambda:None, progress=None, fault=lambda _:None):
    root=Path(root).resolve()
    if manifest.get('approval_index')=='summary/1' or (root/'catalog.sqlite3').stat().st_size<THRESHOLD:
        return manifest
    from .local_delete import WORK,PROTOCOL,edit_lock,journal,_recover,local
    from .progress import Reporter
    report=Reporter(progress)
    with edit_lock(root):
        if (root/WORK).exists() or (root/'.segio-browser-edit').exists():
            raise CollectionError('백업본의 편집 또는 복구를 먼저 완료하세요.')
        before=digest_file(root/'manifest.json')
        source=local(root,'catalog.sqlite3')
        if digest_file(source,checkpoint)!=manifest['files']['catalog.sqlite3']:
            raise CollectionError('검색 DB가 변경되어 정리하지 않았습니다.')
        work=root/WORK;work.mkdir(mode=0o700)
        data={'protocol':PROTOCOL,'phase':'preparing','paths':[]}
        journal(work,data)
        committed=False
        try:
            stage=work/'stage';stage.mkdir()
            with closing(connect_readonly(source)) as old, closing(sqlite3.connect(stage/'catalog.sqlite3')) as new:
                schema=old.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='table' DESC").fetchall()
                tables=[(name,sql) for kind,name,sql in schema if kind=='table']
                total=old.execute('SELECT count(*) FROM document_records').fetchone()[0] if any(name=='document_records' for name,_ in tables) else 0
                for name,sql in tables:
                    new.execute(sql)
                    quoted='"'+name.replace('"','""')+'"'
                    cursor=old.execute('SELECT * FROM '+quoted+' ORDER BY rowid')
                    insert='INSERT INTO '+quoted+' VALUES ('+','.join('?' for _ in cursor.description)+')'
                    for number,row in enumerate(cursor,1):
                        checkpoint()
                        if name=='document_records':
                            row=(row[0],json.dumps(compact_record(json.loads(row[1])),ensure_ascii=False))
                            report('index',number,total,'records','중복 결재 DB 색인 정리')
                        new.execute(insert,row)
                for kind,_,sql in schema:
                    if kind!='table':new.execute(sql)
                new.commit()
                if new.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise CollectionError('정리한 검색 DB를 확인하지 못했습니다.')
            updated=json.loads(json.dumps(manifest))
            updated['approval_index']='summary/1';updated['browser_index']=''
            updated['files']['catalog.sqlite3']=digest_file(stage/'catalog.sqlite3',checkpoint)
            atomic_write(stage/'manifest.json',json.dumps(updated,ensure_ascii=False,indent=2).encode())
            if digest_file(root/'manifest.json')!=before:raise CollectionError('검색 DB 정리 중 백업본이 변경됐습니다.')
            checkpoint()
            data['paths']=[{'name':name,'existed':True} for name in ('catalog.sqlite3','manifest.json')]
            journal(work,dict(data,phase='installing'))
            for name in ('catalog.sqlite3','manifest.json'):
                (work/'backup').mkdir(exist_ok=True)
                os.replace(root/name,work/'backup'/name)
                os.replace(stage/name,root/name);fault(name)
            journal(work,dict(data,phase='committed'));committed=True
            shutil.rmtree(work)
            return updated
        except BaseException:
            if not committed:_recover(root)
            raise
