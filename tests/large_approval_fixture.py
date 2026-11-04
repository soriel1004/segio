# -*- coding: utf-8 -*-
"""Large related-record regression, runnable on actual Python 2.6."""
from __future__ import print_function
import hashlib
import json
import os
import re
import sys

sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..','tools')))
import export_approval_db as exporter
import remote_ssh_export as helper


def main(root):
    cell=os.path.join(root,'cell');os.makedirs(os.path.join(cell,'data'))
    job=os.path.join(root,'.segio-ssh-'+'f'*32);os.mkdir(job,0o700)
    with open(os.path.join(exporter.HERE,'approval_db_columns.json'),'rb') as stream:
        model=json.loads(stream.read().decode('utf-8'))
    counts=dict((table,0) for table in model)
    counts.update(approval_document=1,approval_document_backup=1,approval_member_info=1,
                  approval_line=400,approval_line_backup=400)
    signature=u'SYNTHETIC_SIGNATURE_'*(32768//20)
    context=u'<p>'+u'x'*16380+u'\U0001f600\ud55c\uae00</p>'
    def records(table):
        if table=='approval_document':yield {'no':'1','uid':'employee','title':u'\ud55c\uae00','context':context}
        if table=='approval_document_backup':yield {'no':'1','id':'2','uid':'employee','title':'History','context':'<p>old</p>'}
        if table=='approval_member_info':yield {'uid':'employee','rname':'Employee'}
        if table in ('approval_line','approval_line_backup'):
            for number in range(400,0,-1):
                yield {'no':str(number),'p_no':'1','order_no':str(number%7),'uid':'employee',
                       'rname':'PERSON_%04d'%number,'sign':signature}
    def execute(config,work,sql,*args):
        with open(os.path.join(work,'counts.csv'),'wb') as stream:
            stream.write(b'table_name,row_count\n')
            for table in sorted(counts):
                import binascii
                stream.write(binascii.hexlify(table.encode('ascii'))+b','+binascii.hexlify(str(counts[table]).encode('ascii'))+b'\n')
        for table,columns in model.items():
            with open(os.path.join(work,table+'.csv'),'wb') as stream:
                stream.write((','.join(columns)+'\n').encode('ascii'))
                for row in records(table):
                    values=[]
                    for key in columns:
                        value=row.get(key)
                        values.append(exporter.NULL.encode('ascii') if value is None else binascii.hexlify(exporter.text(value).encode('utf-8')))
                    stream.write(b','.join(values)+b'\n')
    exporter.psql_readonly=execute
    helper.memory_info=lambda:{}  # Do not depend on other CI jobs' disk caches.
    helper.write_json(os.path.join(job,'request.json'),{'protocol':helper.PROTOCOL,'user':'employee',
        'cell':cell,'db_config':'unused','psql':'unused','approvals':True,'mail':False,
        'mail_template':os.path.join(cell,'home/{initial}/{user}/mbox')})
    previous=None;limit=128*1024*1024
    if os.name=='posix':
        import resource
        previous=resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS,(limit,previous[1]))
    try:
        helper.prepare(job)
        status=helper.read_json(os.path.join(job,'status.json'))
        assert status['state']=='ready',status
    finally:
        if previous is not None:resource.setrlimit(resource.RLIMIT_AS,previous)
    # Inspection allocations are outside the export's explicit process limit.
    transfer=helper.read_json(os.path.join(job,'transfer.json'),None)
    raw_count=html_count=0
    for item in transfer['files']:
        if not item['path'].startswith('documents/'):continue
        import binascii
        with open(binascii.unhexlify(item['source_hex'].encode('ascii')),'rb') as stream:
            stream.seek(item['source_offset']);data=stream.read(item['bytes'])
        assert hashlib.sha256(data).hexdigest()==item['sha256']
        if item['path'].endswith('database.json'):
            raw=json.loads(data.decode('utf-8'))
            if raw['source']=='active':assert raw['document']['context']==context
            for table in ('approval_line','approval_line_backup'):
                assert len(raw['related'][table])==400
                assert all(row['sign']==signature for row in raw['related'][table])
            raw_count+=1
        else:
            names=re.findall(b'<td>(PERSON_[0-9]+)</td>',data)
            expected=[('PERSON_%04d'%n).encode('ascii') for n in sorted(range(1,401),key=lambda n:(n%7,n))]
            assert names==expected
            assert b'max-width:100%' in data and b'../../../../' not in data
            html_count+=1
    assert raw_count==html_count==2
    helper.verify_sources(job)
    print(json.dumps({'ok':True,'python':sys.version.split()[0],'limit':limit if previous else None,
                      'documents':raw_count,'related_rows_per_document':800}))


if __name__=='__main__':main(sys.argv[1])
