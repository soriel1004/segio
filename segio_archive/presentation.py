"""Read existing archive metadata for consistent file and HTTP views."""
import json
from pathlib import Path
import re


class ArchivePresentation:
    def __init__(self, root, manifest=None):
        self.root = Path(root).resolve()
        self.manifest = manifest if manifest is not None else json.loads(
            (self.root/'manifest.json').read_text(encoding='utf-8'))
        self.files = self.manifest['files']
        self.approvals = {}
        self.approval_folders = None
        if 'catalog.json' in self.files:
            for item in json.loads((self.root/'catalog.json').read_text(encoding='utf-8'))['items']:
                if item.get('kind', 'approval') == 'approval':
                    self.approvals[item['path'] if 'path' in item else item['html']] = item

    def load_approval_folders(self, db):
        from .approval_folders import ApprovalFolders
        if self.approval_folders is None:
            self.approval_folders=ApprovalFolders(self.root,self.manifest,db,self.approvals)
        return self.approval_folders

    def path(self, name):
        from .db_client import safe_member
        from .web import CollectionError
        if not isinstance(name, str) or name not in self.files:
            return ''
        try:
            safe_member(name)
        except (ValueError, CollectionError):
            return ''
        if not (self.root/name).resolve().is_relative_to(self.root):
            return ''
        return name

    def approval_source(self, path):
        return self.approvals.get(path, {}).get('source', '')

    def details(self, db, items):
        """Enrich one bounded page/chunk without reading MBOX or attachments."""
        approvals=self.load_approval_folders(db)
        mail_ids = [item['id'] for item in items if item['kind'] == 'mail']
        mail = {}
        if mail_ids and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mail_records'").fetchone():
            for start in range(0,len(mail_ids),500):
                batch=mail_ids[start:start+500]
                for row in db.execute('SELECT id,source,sender,receiver,date FROM mail_records WHERE id IN ('+
                                      ','.join('?' for _ in batch)+')', batch):
                    mail[row[0]] = dict(zip(('raw', 'sender', 'receiver', 'date'), row[1:]))
        for item in items:
            info = {'captured_at': self.manifest.get('finished_at', ''), 'attachments': []}
            if item['kind'] == 'approval':
                info.update(approvals.info(item['html']))
                record = self.approvals.get(item['html'], {})
                info.update(source=record.get('source', ''), document_id=record.get('id', ''),
                            json=self.path(record.get('json')))
                info['attachments'] = [dict(name=a.get('name', '첨부'), path=self.path(a.get('path')))
                                       for a in record.get('attachments', []) if self.path(a.get('path'))]
            else:
                info.update(mail.get(item['id'], {}))
                from email.utils import parsedate_to_datetime
                value=info.get('date','')
                try:
                    parsed=parsedate_to_datetime(value)
                    info['date_display']=parsed.strftime('%Y-%m-%d %H:%M %z') if parsed.tzinfo else str(value)
                except (ValueError, TypeError, IndexError, OverflowError):
                    info['date_display']=str(value)
                info['raw'] = self.path(info.get('raw'))
                info['raw_label'] = '원본 MBOX' if info['raw'].endswith('.mbox') else '원본 메일 파일'
                from .portable_mail import page_path
                directory = str(Path(page_path(item['id'])).parent.as_posix())
                state_path = self.path(directory+'/state.json')
                if state_path:
                    state = json.loads((self.root/state_path).read_text(encoding='utf-8'))
                    attachments = state.get('attachments')
                    if attachments is None:
                        # Older static-mail/1 backups retain paths in state.json.
                        # Do not reparse raw mail just to restore the index buttons.
                        attachments = [dict(name=re.sub(r'^\d+-', '', Path(name).name), path=name)
                                       for name in state['files'] if name.startswith(directory+'/attachments/')]
                    info['attachments'] = [dict(name=a['name'], path=self.path(a['path']))
                                           for a in attachments if a['path'].startswith(directory+'/attachments/')
                                           and self.path(a['path'])]
            item['presentation'] = info
        return items
