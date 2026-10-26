"""Read-only approval status, personal relationships and draft dates."""
from datetime import datetime, timedelta, timezone
import json
from .dates import approval_timestamp

LABELS = {'complete':'완료문서', 'cancel':'반려문서', 'reference':'참조문서',
          'receive':'수신문서', 'progress':'진행문서', 'draft':'임시문서', 'unknown':'상태 미확인'}
STATES = {'complete':('complete','completed','완료','완료문서'),
          'cancel':('cancel','rejected','returned','반려','반려문서'),
          'progress':('progress','진행','진행문서'),
          'draft':('standby','draft','임시','임시문서')}


def members(value):
    return {part.split('@@',1)[0].strip() for part in str(value or '').split(',') if part.strip()}


def draft_date(value):
    if value is None or not str(value).strip() or str(value).strip()=='0':
        return ''
    try:
        stamp=int(value)
        return datetime.fromtimestamp(stamp,timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST') if stamp>0 else ''
    except (ValueError, OverflowError, OSError, TypeError):
        return str(value)


def describe(payload, user):
    document=payload.get('document') or {}
    related=payload.get('related') or {}
    state=str(document.get('progress') or '').strip().lower()
    primary=next((key for key,values in STATES.items() if state in values),'unknown')
    folders=[primary]
    lines=related.get('approval_line_backup' if payload.get('source')=='history' else 'approval_line',[])
    if user and (user in members(document.get('reference')) or
                 any(row.get('uid')==user and row.get('division')=='reference' for row in lines)):
        folders.append('reference')
    sends=related.get('approval_send_box',[])+related.get('approval_send_box_backup',[])
    if user and (user in members(document.get('doc_recv_uid')) or
                 any(user in members(row.get(key)) for row in sends for key in ('recv_user','recv_uid'))):
        folders.append('receive')
    return {'approval_folders':folders,'approval_label':LABELS[primary],
            'date_display':draft_date(document.get('wdate')),'date_sort':approval_timestamp(document.get('wdate'))}


class ApprovalFolders:
    def __init__(self, root, manifest, db, catalog):
        self.by_path={}
        user=manifest.get('user','')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_records'").fetchone():
            for path,payload in db.execute("SELECT i.html,d.payload FROM items i JOIN document_records d ON i.id=d.id WHERE i.kind='approval'"):
                self.by_path[path]=describe(json.loads(payload),user)
        else:
            # Older archives can retain the DB JSON beside the document only.
            from .db_client import safe_member
            for path,item in catalog.items():
                name=item.get('json')
                if name not in manifest['files']:
                    continue
                safe_member(name)
                local=(root/name).resolve()
                if local.is_relative_to(root):
                    self.by_path[path]=describe(json.loads(local.read_text(encoding='utf-8')),user)

    def info(self,path):
        return self.by_path.get(path,{'approval_folders':['unknown'],'approval_label':LABELS['unknown'],'date_display':'','date_sort':None})

    def contains(self,path,folder):
        return folder in self.info(path)['approval_folders']

    def boxes(self,db):
        counts=dict.fromkeys(LABELS,0)
        for path, in db.execute("SELECT html FROM items WHERE kind='approval'"):
            for folder in self.info(path)['approval_folders']:
                counts[folder]+=1
        return [dict(id=key,label=label,count=counts[key]) for key,label in LABELS.items()]
