"""Executable mail retry / compressed ZIP fixture for actual Python 2.6."""
from __future__ import print_function
import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
import struct
import time

sys.path.insert(0, sys.argv[1])
import live_export_worker as worker
import remote_ssh_export as ssh

root = tempfile.mkdtemp()
try:
    source = os.path.join(root, 'employee')
    os.mkdir(source)
    output = os.path.join(root, 'output')
    os.mkdir(output)
    path = os.path.join(source, 'inbox.mbox')
    body = b'From synthetic@example.test\nSubject: Original\n\nbody\n'
    with open(path, 'wb') as stream:
        stream.write(body)
    original = worker.copy_mail_file
    calls = [0]
    def copying(*args):
        info = original(*args)
        calls[0] += 1
        if calls[0] == 1:
            with open(path, 'ab') as stream:
                stream.write(b'new message\n')
        return info
    worker.copy_mail_file = copying
    manifest = {'files': {}, 'counts': {}, 'coverage': []}
    worker.copy_mail(os.path.join(root, '{user}'), 'employee', output, manifest, lambda: None)
    assert calls[0] == 2
    raw = os.path.join(output, 'mail', 'raw', '000000.bin')
    with open(raw, 'rb') as stream:
        data = stream.read()
    assert data == body + b'new message\n'
    assert manifest['files']['mail/raw/000000.bin']['sha256'] == hashlib.sha256(data).hexdigest()
    archive = os.path.join(root, 'archive.zip')
    events = []
    packed = zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
    packed.fp = worker.PackingProgress(packed.fp, lambda: None, lambda **event: events.append(event))
    worker.write_archive_file(packed, raw, 'mail/raw/000000.bin')
    packed.close()
    packed = zipfile.ZipFile(archive)
    assert packed.testzip() is None
    assert packed.read('mail/raw/000000.bin') == data
    packed.close()
    assert events and events[-1]['completed_bytes'] == os.path.getsize(archive)
    # Force a ZIP64 member at small size; actual 2.6 must reserve the local
    # extended header before compression and patch its complete 64-bit fields.
    large = os.path.join(root, 'large.mbox')
    with open(large, 'wb') as stream:
        stream.write(b'x' * 8192)
    original_limit = zipfile.ZIP64_LIMIT
    zipfile.ZIP64_LIMIT = 256
    packed = zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
    worker.write_archive_file(packed, large, 'large.mbox')
    packed.close()
    with open(archive, 'rb') as stream:
        header = stream.read(30)
        assert struct.unpack('<LL', header[18:26]) == (0xffffffff, 0xffffffff)
    zipfile.ZIP64_LIMIT = original_limit
    packed = zipfile.ZipFile(archive)
    assert packed.testzip() is None
    assert packed.read('large.mbox') == b'x' * 8192
    packed.close()
    progress=[]
    sources,transfers,identities=ssh.mail_snapshot(
        {'user':'employee','mail_template':os.path.join(root,'{user}')},
        lambda state,**fields:progress.append(fields))
    assert len(sources)==len(transfers)==len(identities)==1
    assert sources[0]['sha256']==hashlib.sha256(data).hexdigest()
    assert progress[-1]['completed_bytes']==len(data)
    assert progress[-1]['files_done']==1
    selected,ranges,checked=ssh.mail_delta(
        {'user':'employee','mail_template':os.path.join(root,'{user}')},lambda *a,**kw:None,int(time.time())-15*86400)
    assert len(selected)==len(ranges)==len(checked)==1
    assert ranges[0]['source_offset']>0
    with open(path,'rb') as stream:
        stream.seek(ranges[0]['source_offset'])
        assert hashlib.sha256(stream.read(ranges[0]['bytes'])).hexdigest()==ranges[0]['sha256']
    print('LEGACY_DELTA_RANGES_OK')
    job=os.path.join(root,'.segio-ssh-'+'a'*32)
    os.mkdir(job,0o700)
    ssh.write_json(os.path.join(job,'request.json'),
                   {'protocol':ssh.PROTOCOL,'user':'employee','mail':False,'approvals':True})
    ssh.write_json(os.path.join(job,'status.json'),{'state':'ready'})
    check=os.path.join(job,'approval-check.json')
    ssh.write_json(check,[{'source_hex':ssh.source_hex(path),'identity':list(ssh.fingerprint(path))}]*700)
    assert os.path.getsize(check)>65536
    ssh.verify_sources(job)
    assert ssh.read_json(os.path.join(job,'verified.json'))['ok']
    with open(path,'ab') as stream: stream.write(b'changed source')
    try: ssh.verify_sources(job)
    except worker.SafeError: pass
    else: raise AssertionError('Source change was not detected')
    print('LEGACY_LARGE_VERIFY_OK')
    print('LEGACY_SSH_OK')
    print('LEGACY_MAIL_OK')
finally:
    shutil.rmtree(root)
