# -*- coding: utf-8 -*-
"""Write an existing large Unicode page with only 8 MiB additional headroom."""
from __future__ import print_function
import hashlib
import json
import os
import sys

sys.path.insert(0,os.path.abspath(os.path.join(os.path.dirname(__file__),'..','tools')))
import export_approval_db as exporter


def main(root):
    root=os.path.realpath(root)
    cell=os.path.join(root,'cell');os.makedirs(os.path.join(cell,'data'))
    output=os.path.join(root,'output');os.mkdir(output)
    pattern=u'\ud55c\uae00\U0001f600\r\n"\\<&>'
    repeat=1024*1024
    value=pattern*repeat
    prefix=b'<meta charset="utf-8"><pre>';suffix=b'</pre>'
    expected=hashlib.sha256(prefix)
    block=pattern.encode('utf-8')*1024
    for unused in range(1024):expected.update(block)
    expected.update(suffix)
    expected_size=len(prefix)+len(pattern.encode('utf-8'))*repeat+len(suffix)
    maximum=[0];chunks=[0]
    spool=os.path.join(root,'approval-pages.bin')
    def ranged_write(path,source):
        digest=hashlib.sha256();size=0
        with open(spool,'wb') as stream:
            for part in source:
                assert isinstance(part,bytes) and len(part)<=65536
                maximum[0]=max(maximum[0],len(part));chunks[0]+=1
                stream.write(part);digest.update(part);size+=len(part)
        return {'bytes':size,'sha256':digest.hexdigest()}
    bundle=exporter.Bundle(output,cell,'employee',write_file=ranged_write)
    previous=None;limit=None
    if os.name=='posix' and os.path.isfile('/proc/self/statm'):
        import resource
        with open('/proc/self/statm') as stream:virtual=int(stream.read().split()[0])*os.sysconf('SC_PAGE_SIZE')
        previous=resource.getrlimit(resource.RLIMIT_AS)
        limit=virtual+8*1024**2
        if previous[0]>0:limit=min(limit,previous[0])
        resource.setrlimit(resource.RLIMIT_AS,(limit,previous[1]))
    try:
        path='documents/1/document.html'
        bundle.write_chunks(path,exporter.format_chunks(u'<meta charset="utf-8"><pre>%s</pre>',(value,)))
        record=bundle.files[path]
        assert record['bytes']==expected_size
        assert record['sha256']==expected.hexdigest()
        actual=hashlib.sha256()
        with open(spool,'rb') as stream:
            while True:
                part=stream.read(65536)
                if not part:break
                actual.update(part)
        assert actual.hexdigest()==expected.hexdigest()
    finally:
        if previous is not None:resource.setrlimit(resource.RLIMIT_AS,previous)
    print(json.dumps({'ok':True,'python':sys.version.split()[0],'limit':limit,
                      'headroom':8*1024**2 if limit else None,'bytes':expected_size,
                      'chunks':chunks[0],'max_chunk':maximum[0]}))


if __name__=='__main__':main(sys.argv[1])
