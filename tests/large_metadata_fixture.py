"""Many-file SSH finalization regression; compatible with actual Python 2.6."""
from __future__ import print_function
import binascii
import hashlib
import json
import os
import sys

sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..','tools')))
import export_approval_db as exporter
import remote_ssh_export as helper


def main(root):
    root=os.path.realpath(root)
    cell=os.path.join(root,'cell');os.makedirs(os.path.join(cell,'data'))
    job=os.path.join(root,'.segio-ssh-'+'a'*32);os.mkdir(job,0o700)
    total=31000
    with open(os.path.join(exporter.HERE,'approval_db_columns.json'),'rb') as stream:model=json.loads(stream.read().decode('utf-8'))
    counts=dict((name,0) for name in model);counts['approval_document']=total
    exporter.query_database=lambda *args,**kwargs:(dict((name,[]) for name in model),counts)
    def render(bundle,rows,expected,allow_empty=False,checkpoint=None,progress_detail=None):
        for number in range(1,total+1):
            if checkpoint and number%100==0:checkpoint()
            identity=str(number);directory=exporter.document_directory(identity)
            raw=b'{"synthetic":true}';body=b'<meta charset="utf-8"><p>SYNTHETIC</p>'
            data=bundle.write(directory+'/database.json',raw)
            page=bundle.write(directory+'/document.html',body)
            if number%500==0:bundle.issue('synthetic_issue',identity)
            yield {'id':identity,'title':'Synthetic','path':page,'json':data,'source':'active','search':'Synthetic','attachments':[]}
    exporter.iter_render_bundle=render
    helper.memory_info=lambda:{}
    helper.write_json(os.path.join(job,'request.json'),{'protocol':helper.PROTOCOL,'user':'employee',
        'cell':cell,'db_config':'unused','psql':'unused','approvals':True,'mail':False,
        'mail_template':os.path.join(cell,'home/{initial}/{user}/mbox')})
    previous=None;limit=128*1024*1024
    if os.name=='posix':
        import resource
        previous=resource.getrlimit(resource.RLIMIT_AS);resource.setrlimit(resource.RLIMIT_AS,(limit,previous[1]))
    try:
        helper.prepare(job)
        status=helper.read_json(os.path.join(job,'status.json'))
        assert status['state']=='ready',status
    finally:
        if previous is not None:resource.setrlimit(resource.RLIMIT_AS,previous)
    # Full inspection is intentionally after the bounded export completed.
    entries=helper.read_json(os.path.join(job,'transfer.json'),None)['files']
    manifest_entry=next(row for row in entries if row['path']=='manifest.json')
    manifest=helper.read_json(binascii.unhexlify(manifest_entry['source_hex'].encode('ascii')),None)
    assert manifest['counts']['approval']==total
    assert set(row['path'] for row in entries)==set(manifest['files'])|set(['manifest.json'])
    spool_path=os.path.join(job,'approval-pages.bin')
    seen=0;boundary=0
    with open(spool_path,'rb') as stream:
        for item in entries:
            if not item['path'].startswith('documents/'):continue
            assert item['source_offset']==boundary
            stream.seek(item['source_offset']);data=stream.read(item['bytes'])
            assert hashlib.sha256(data).hexdigest()==item['sha256']
            assert manifest['files'][item['path']]==dict((key,item[key]) for key in ('bytes','sha256'))
            boundary+=item['bytes'];seen+=1
    assert seen==total*2 and boundary==os.path.getsize(spool_path)
    index_entry=next(row for row in entries if row['path']=='index.html')
    assert index_entry['bytes']<4096
    assert not os.path.exists(os.path.join(job,'metadata.sqlite3'))
    helper.verify_sources(job)
    print(json.dumps({'ok':True,'documents':total,'derived_files':seen,'limit':limit if previous else None}))


if __name__=='__main__':main(sys.argv[1])
