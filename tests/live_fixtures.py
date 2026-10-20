"""Synthetic database adapter for real PHP + subprocess + mailbox integration tests."""
import hashlib
import json
from pathlib import Path
import zipfile


def synthetic_export(config, username, output, **kwargs):
    assert kwargs.get('live_scope') is True
    records = json.loads(Path(config).read_text(encoding='utf-8'))
    record = records[username]
    folder = Path(output)/'database'
    folder.mkdir()
    data = {'index.html':b'<p>DB import pending</p>',
            'documents/1/document.html':('<meta charset="utf-8"><p>'+record['title']+'</p>').encode(),
            'documents/1/database.json':json.dumps(record,ensure_ascii=False).encode(),
            'catalog.json':json.dumps({'items':[{'id':'1','title':record['title'],'source':'active',
                             'path':'documents/1/document.html','json':'documents/1/database.json',
                             'search':record['title'],'attachments':[]}]},ensure_ascii=False).encode()}
    manifest={'format':'segio-db-html/1','user':username,'server':'cell_000',
              'state':'collected_with_warnings','account_deletion_verified':False,
              'counts':{'approval':1},'coverage':[],'warnings':['삭제 영향 미검증'],
              'snapshot_rows':{'approval_document':1},
              'files':{name:{'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()} for name,body in data.items()}}
    for name,body in data.items():
        target=folder/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(body)
    (folder/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
    archive=Path(output)/'intermediate.zip'
    with zipfile.ZipFile(archive,'w'):
        pass
    return str(folder),str(archive)
