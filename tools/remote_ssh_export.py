#!/usr/bin/env python
"""Private administrator SSH helper. Python 2.6+. Never modifies source records."""
from __future__ import print_function
import binascii
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from email.parser import Parser
from email.utils import parsedate_tz, mktime_tz
from optparse import OptionParser

from export_approval_db import run_export, approval_cutoff,MetadataStore
from export_schema_from_segio_config import SafeError
from live_export_worker import (read_json, write_json, check_user, mail_path, inventory,
                                fingerprint, file_identity, failure_details as worker_failure_details)

PROTOCOL = 'segio-ssh-archive/1'
REVISION = 'ssh-bounded-encoding-9'


def memory_info():
    result={}
    try:
        with open('/proc/meminfo') as stream:
            for line in stream:
                parts=line.split()
                if len(parts)>1:result[parts[0].rstrip(':')]=int(parts[1])*1024
    except (IOError,ValueError):pass
    return result


class WorkBudget(object):
    """Limits affect this helper/its psql client only; never the SEGIO daemon."""
    WRITE_RATE=8*1024**2
    PRESSURE_RATE=1024**2
    SYNC_BYTES=4*1024**2

    def __init__(self, root):
        self.root=root;self.last_document=0;self.last_memory=0;self.started=time.time();self.phase='query'
        self.lock=None
        self.io_enabled=True;self.io_throttled=False;self.io_syncing=False;self.io_written=0
        self.io_dirty=0;self.io_rate=self.WRITE_RATE;self.io_tick=lambda force=False:None
        self._io_next=self.started;self._io_clock=self.started;self._io_sample=-1;self._io_pending=0

    def enter(self, apply_limits=True):
        self.io_enabled=apply_limits
        if os.name!='posix':return
        import fcntl,resource
        path=os.path.dirname(self.root)
        self.lock=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0))
        info=os.fstat(self.lock)
        if not stat.S_ISDIR(info.st_mode):raise SafeError('unsafe_job')
        try:fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except IOError:raise SafeError('export_busy')
        if apply_limits and sys.platform.startswith('linux'):
            os.nice(10)
            for executable in ('/usr/bin/ionice','/bin/ionice'):
                if os.path.isfile(executable):
                    with open(os.devnull,'wb') as null:
                        subprocess.call([executable,'-c','3','-p',str(os.getpid())],stdout=null,stderr=null)
                    break
            memory=memory_info();total=memory.get('MemTotal',2*1024**3)
            cap=max(96*1024**2,min(512*1024**2,total//8))
            soft,hard=resource.getrlimit(resource.RLIMIT_AS)
            if soft>0:cap=min(cap,soft)
            if hard>0:cap=min(cap,hard)
            resource.setrlimit(resource.RLIMIT_AS,(cap,hard))

    def close(self):
        if self.lock is not None:os.close(self.lock);self.lock=None

    def check(self):
        now=time.time()
        if os.path.exists(os.path.join(self.root,'cancel')):raise SafeError('export_cancelled')
        lease=os.path.join(self.root,'lease')
        if os.path.isfile(lease) and now-os.stat(lease).st_mtime>120:raise SafeError('export_client_disconnected')
        if self.phase=='query' and now-self.started>900:raise SafeError('db_query_budget')
        if now-self.last_memory>=1:
            memory=memory_info();available=memory.get('MemAvailable',memory.get('MemFree',0)+memory.get('Buffers',0)+memory.get('Cached',0))
            if memory and available<max(128*1024**2,memory.get('MemTotal',0)//20):raise SafeError('server_memory_pressure')
            self.last_memory=now

    def document(self):
        self.phase='render';self.check()
        self.io_sample()
        remaining=.05-(time.time()-self.last_document)
        if remaining>0:time.sleep(remaining)
        self.last_document=time.time()

    def io_sample(self):
        now=time.time()
        if now<self._io_clock:self._io_next=now;self._io_sample=-1
        self._io_clock=now
        if now-self._io_sample>=.25 or self._io_sample<0:
            self.io_dirty=memory_info().get('Dirty',0)
            self.io_throttled=self.io_enabled and self.io_dirty>64*1024**2
            self.io_rate=self.PRESSURE_RATE if self.io_throttled else self.WRITE_RATE
            self._io_sample=now

    def write(self,stream,block):
        self.check();self.io_sample()
        if self.io_enabled:
            self._io_next=max(time.time(),self._io_next)+float(len(block))/self.io_rate
            while self._io_next>time.time():
                self.check();self.io_sample();self.io_tick()
                time.sleep(min(.25,max(0,self._io_next-time.time())))
        self.check()
        written=stream.write(block)
        if written is not None and written!=len(block):raise IOError(errno.EIO,'Incomplete generated-file write')
        self.io_written+=len(block);self._io_pending+=len(block)
        if self._io_pending>=self.SYNC_BYTES:self.finish(stream)

    def finish(self,stream):
        if not self._io_pending:return
        self.check();self.io_syncing=True;self.io_tick(True)
        try:
            stream.flush()
            getattr(os,'fdatasync',os.fsync)(stream.fileno())
            self._io_pending=0
        finally:
            self.io_syncing=False
        self.check();self.io_tick(True)


def open_source(path):
    return os.fdopen(os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)),'rb')


def message_spans(path,progress=lambda value:None):
    """MBOX offsets compatible with the Windows index; bounded Python 2.6 reads."""
    with open_source(path) as scan:
        with open_source(path) as envelopes:
            first=envelopes.readline(65537)
            if not first:return
            if not first.startswith(b'From ') or not first.endswith(b'\n'):
                raise SafeError('invalid_metadata')
            start,position,carry=len(first),0,b''
            while True:
                block=scan.read(1024*1024)
                if not block:break
                data=carry+block
                base,offset=position-len(carry),0
                while True:
                    found=data.find(b'\nFrom ',offset)
                    if found<0:break
                    boundary=base+found+1
                    envelopes.seek(boundary)
                    envelope=envelopes.readline(65537)
                    if len(envelope)>65536 or not envelope.endswith(b'\n'):raise SafeError('invalid_metadata')
                    yield start,boundary
                    start=boundary+len(envelope)
                    offset=found+6
                position+=len(block)
                progress(position)
                carry=data[-5:]
            if position>start:yield start,position


def recent_message(stream,start,end,since):
    stream.seek(start)
    lines=[]
    size=0
    while stream.tell()<end and size<1024*1024:
        line=stream.readline(min(65536,end-stream.tell()))
        if not line or line in (b'\n',b'\r\n'):break
        lines.append(line);size+=len(line)
    try:
        message=Parser().parsestr(b''.join(lines).decode('latin-1'),headersonly=True)
        values=[message.get('Date','')]
        values.extend(value.rsplit(';',1)[-1] for value in message.get_all('Received',[]))
        found=False
        for value in values:
            stamp=parsedate_tz(value)
            if stamp:
                found=True
                if mktime_tz(stamp)>=since:return True
        return not found
    except (ValueError,TypeError,OverflowError):
        pass
    return True  # Unknown dates must never silently lose a message.


def mail_delta(request,status,since):
    for attempt in range(1,4):
        def report(state,**fields):
            fields['attempt']=attempt;status(state,**fields)
        try:return mail_delta_attempt(request,report,since)
        except SafeError as exc:
            if str(exc)!='mail_changed_retry' or attempt==3:raise
        except (IOError,OSError) as exc:
            if exc.errno!=errno.ENOENT or attempt==3:raise


def mail_delta_attempt(request,status,since):
    """Transfer selected message ranges directly, without server MBOX copies."""
    before=inventory(mail_path(request['mail_template'],request['user']))
    sources,transfers,identities=[],[],[]
    total=sum(entry[2][2] for entry in before)
    completed,last_report=0,[0]
    def report(position=0):
        now=time.time()
        if now-last_report[0]>=1:
            status('running',files_done=len(identities),files_total=len(before),attempt=1,
                   completed_bytes=completed+position,total_bytes=total)
            last_report[0]=now
    for name,path,identity in before:
        with open_source(path) as stream:
            prefix=stream.read(8192)
            kind='mbox' if not prefix or prefix.startswith(b'From ') else 'rfc822' if re.match(b'[A-Za-z][A-Za-z0-9-]*:',prefix) else 'unrecognized'
            spans=message_spans(path,report) if kind=='mbox' else [(0,identity[2])]
            for start,end in spans:
                report(end)
                if kind!='unrecognized' and not recent_message(stream,start,end,since):continue
                stream.seek(start)
                digest=hashlib.sha256();position=start
                while position<end:
                    block=stream.read(min(1024*1024,end-position))
                    if not block:raise SafeError('mail_changed_retry')
                    digest.update(block);position+=len(block);report(position)
                raw=binascii.unhexlify(source_hex(name))
                try:display=raw.decode('utf-8')
                except UnicodeError:display=raw.decode('cp949','replace')
                relative='mail/raw/%06d.%s' % (len(sources),'bin' if kind=='unrecognized' else 'eml')
                source={'path':relative,'name':display,'name_bytes_hex':source_hex(name),
                        'format':'unrecognized' if kind=='unrecognized' else 'rfc822','folder_format':kind,
                        'bytes':end-start,'sha256':digest.hexdigest()}
                sources.append(source)
                transfers.append(dict(source,path=relative,source_hex=source_hex(path),source_offset=start,source_bytes=identity[2]))
        if fingerprint(path)!=identity:raise SafeError('mail_changed_retry')
        identities.append({'name_hex':source_hex(name),'identity':list(identity),'format':kind})
        completed+=identity[2]
    if inventory(mail_path(request['mail_template'],request['user']))!=before:raise SafeError('mail_changed_retry')
    status('running',files_done=len(before),files_total=len(before),attempt=1,completed_bytes=total,total_bytes=total)
    return sources,transfers,identities


def failure_details(exc, stage):
    code, diagnostic = worker_failure_details(exc, stage)
    kinds = ('SafeError', 'TypeError', 'ValueError', 'KeyError', 'AttributeError',
             'MemoryError', 'OverflowError', 'RuntimeError', 'IOError', 'OSError',
             'FileNotFoundError', 'PermissionError', 'UnicodeEncodeError',
             'UnicodeDecodeError', 'UnicodeError')
    kind = type(exc).__name__
    diagnostic['exception_type'] = kind if kind in kinds else 'unknown'
    modules = ('remote_ssh_export.py', 'live_export_worker.py', 'export_approval_db.py',
               'export_schema_from_segio_config.py')
    functions = ('prepare', 'verify_sources', 'read_json', 'write_json', 'run_export',
                 'reference_file', 'hash_file', 'fingerprint', 'inventory',
                 'render_bundle', 'attachment', 'local_file', 'image', 'freeze',
                 'query_database', 'psql_readonly', 'read_rows', 'iter_rows', 'iter_render_bundle', 'json_chunks', 'byte_chunks', 'write_chunks', 'format_chunks', 'private_root', 'text')
    trace = sys.exc_info()[2]
    while trace is not None:
        frame = trace.tb_frame
        module = os.path.basename(frame.f_code.co_filename)
        function = frame.f_code.co_name
        if module in modules and function in functions:
            diagnostic['site'] = {'module': module, 'function': function, 'line': trace.tb_lineno}
        trace = trace.tb_next
    return code, diagnostic


def save_failure(root, code, diagnostic):
    try:
        write_json(os.path.join(root, 'last-failure.json'),
                   {'protocol': PROTOCOL, 'revision': REVISION, 'code': code, 'diagnostic': diagnostic})
    except (IOError, OSError):
        pass  # A full/unwritable disk must not hide the original failure.


def source_hex(path):
    if not isinstance(path, bytes):
        path = path.encode(sys.getfilesystemencoding() or 'utf-8', 'surrogateescape' if sys.version_info[0]>=3 else 'strict')
    return binascii.hexlify(path).decode('ascii')


def hash_file(path, expected=None, progress=lambda count: None):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest, size = hashlib.sha256(), 0
    with os.fdopen(descriptor, 'rb') as stream:
        before = file_identity(os.fstat(stream.fileno()))
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or (expected and before != expected):
            raise SafeError('mail_changed_retry')
        while True:
            block = stream.read(min(1024 * 1024, before[2]-size+1))
            if not block:
                break
            digest.update(block)
            size += len(block)
            if size>before[2]:
                raise SafeError('mail_changed_retry')
            progress(size)
    if fingerprint(path) != before or size != before[2]:
        raise SafeError('mail_changed_retry')
    return {'bytes': size, 'sha256': digest.hexdigest()}


def mail_snapshot(request, status):
    """Retry changing inventories without copying raw MBOX or rehashing stable files."""
    cache={}
    for attempt in range(1,4):
        try:
            mailbox_root=mail_path(request['mail_template'],request['user'])
            before=inventory(mailbox_root)
            sources,transfers,identities=[],[],[]
            completed,total=0,sum(item[2][2] for item in before)
            last_report=[0]
            def report(count,force=False):
                now=time.time()
                if force or now-last_report[0]>=1:
                    status('running',attempt=attempt,files_done=len(sources),files_total=len(before),
                           completed_bytes=completed+count,total_bytes=total)
                    last_report[0]=now
            report(0,True)
            for number,(name,path,identity) in enumerate(before):
                cached=cache.get(name)
                if cached and cached[0]==identity:
                    details,kind=cached[1:]
                else:
                    descriptor=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
                    with os.fdopen(descriptor,'rb') as stream:
                        prefix=stream.read(8192)
                    is_mbox=not prefix or prefix.startswith(b'From ')
                    is_eml=(not is_mbox and re.match(b'[A-Za-z][A-Za-z0-9-]*:',prefix) and
                        re.search(b'(?im)^(subject|from|to|date|received|delivered-to|mime-version):',prefix))
                    kind='mbox' if is_mbox else 'rfc822' if is_eml else 'unrecognized'
                    details=hash_file(path,identity,report)
                    cache[name]=(identity,details,kind)
                relative='mail/raw/%06d.%s' % (number,'mbox' if kind=='mbox' else 'eml' if kind=='rfc822' else 'bin')
                raw_name=binascii.unhexlify(source_hex(name))
                try:
                    display=raw_name.decode('utf-8')
                except UnicodeError:
                    display=raw_name.decode('cp949','replace')
                source=dict(details,path=relative,name=display,name_bytes_hex=source_hex(name),format=kind)
                sources.append(source)
                transfers.append(dict(details,path=relative,source_hex=source_hex(path)))
                identities.append({'name_hex':source['name_bytes_hex'],'identity':list(identity),'format':kind})
                completed+=details['bytes']
                report(0,True)
            if inventory(mailbox_root)==before:
                return sources,transfers,identities
        except SafeError as exc:
            if str(exc)!='mail_changed_retry' and exc.code!='mail_changed_retry':
                raise
        except (IOError,OSError) as exc:
            if exc.errno!=errno.ENOENT:
                raise
    raise SafeError('mail_changed_retry')


def private_root(root):
    root = os.path.abspath(root)
    info = os.lstat(root)
    if (os.path.realpath(root) != root or not stat.S_ISDIR(info.st_mode) or
            not re.match(r'\A\.segio-ssh-[0-9a-f]{32}\Z', os.path.basename(root)) or
            (hasattr(os, 'geteuid') and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
        raise SafeError('unsafe_job')
    request = read_json(os.path.join(root, 'request.json'))
    if request.get('protocol') != PROTOCOL:
        raise SafeError('unsafe_job')
    check_user(request['user'])
    return root, request


def prepare(root, apply_limits=False):
    root, request = private_root(root)
    status_path = os.path.join(root, 'status.json')
    stage = 'initializing'
    references, reference_checks = {}, []
    metadata_store=None;exported=[None]
    budget=WorkBudget(root);last_report=[0];current={'phase':'query'}
    spool=[None];spooled=[0];spool_size=0;spool_path=os.path.join(root,'approval-pages.bin')
    def reference_file(path, relative):
        identity = fingerprint(path)
        details = hash_file(path, identity,lambda _:pulse())
        references[relative] = dict(details, path=relative, source_hex=source_hex(path))
        reference_checks.append({'source_hex': source_hex(path), 'identity': list(identity)})
        return details
    def status(state, **fields):
        if state=='running':
            if stage=='mail':budget.phase='mail'
            budget.check()
        value = {'protocol': PROTOCOL, 'revision': REVISION, 'state': state, 'stage': stage}
        value.update(fields)
        write_json(status_path, value)
    def pulse(force=False):
        budget.check()
        if force or time.time()-last_report[0]>=1:
            fields=dict(current)
            if current.get('phase')=='render' and (budget.io_throttled or budget.io_syncing):
                fields.update(phase='io_sync' if budget.io_syncing else 'io_throttle',
                    io_written_bytes=budget.io_written,io_limit_bytes=budget.io_rate,dirty_bytes=budget.io_dirty)
            status('running',**fields);last_report[0]=time.time()
    budget.io_tick=pulse
    def detail(phase,done,total):
        budget.phase=phase
        if phase=='render':budget.document()
        current.update(phase=phase,records_done=done,records_total=total);pulse()
    def write_file(relative,chunks):
        # One sequential file avoids tens of thousands of ext3 inode/journal
        # updates. Windows writes each verified range to its normal final path.
        if spool[0] is None:spool[0]=open(spool_path,'wb')
        offset=spool[0].tell()
        digest=hashlib.sha256();size=0
        for data in chunks:
            if not isinstance(data,bytes):data=data.encode('utf-8')
            for start in range(0,len(data),1024*1024):
                pulse();block=data[start:start+1024*1024];budget.write(spool[0],block);digest.update(block);size+=len(block)
        details={'bytes':size,'sha256':digest.hexdigest()}
        references[relative]=dict(details,path=relative,source_hex=source_hex(spool_path),source_offset=offset)
        spooled[0]+=1
        return details
    def accept_manifest(value):
        exported[0]=value
    try:
        budget.enter(apply_limits)
        metadata_store=MetadataStore(os.path.join(root,'metadata.sqlite3'))
        file_catalog=metadata_store.mapping('files')
        references=metadata_store.mapping('references',ordered=True)
        reference_checks=metadata_store.sequence('checks')
        transfers=metadata_store.sequence('transfers')
        issues=metadata_store.sequence('issues')
        user = request['user']
        since=approval_cutoff(request.get('since'))
        if request['approvals']:
            stage = 'database'
            status('running')
            options=dict(cell=request['cell'],binary=request['psql'],live_scope=True,pack=False,reference_file=reference_file,checkpoint=pulse,progress_detail=detail,write_file=write_file,file_catalog=file_catalog,issues=issues,manifest_sink=accept_manifest)
            if since is not None:options['approval_since']=since
            folder, archive = run_export(request['db_config'], user, root, **options)
            if spool[0] is not None:
                budget.finish(spool[0])
                spool[0].close();spool[0]=None
                identity=fingerprint(spool_path)
                spool_size=identity[2]
                reference_checks.append({'source_hex':source_hex(spool_path),'identity':list(identity)})
            if archive:
                os.unlink(archive)
            manifest = exported[0] if exported[0] is not None else read_json(os.path.join(folder, 'manifest.json'), None)
        else:
            folder = os.path.join(root, 'bundle')
            os.mkdir(folder, 0o700)
            with open(os.path.join(folder, 'catalog.json'), 'wb') as stream:
                stream.write(b'{"items":[]}')
            with open(os.path.join(folder, 'index.html'), 'wb') as stream:
                stream.write(b'<meta charset="utf-8"><p>Index pending</p>')
            manifest = {'format': 'segio-db-html/1', 'user': user, 'state': 'collected_with_warnings',
                'account_deletion_verified': False, 'files': {}, 'counts': {'approval': 0},
                'coverage': [], 'warnings': [], 'snapshot_rows': {}}
            for name in ('catalog.json', 'index.html'):
                manifest['files'][name] = hash_file(os.path.join(folder, name))
        if manifest['files'] is not file_catalog:
            file_catalog.update(manifest['files']);manifest['files']=file_catalog
        current.update(phase='metadata');budget.phase='metadata';pulse()
        write_json(os.path.join(root, 'approval-check.json'), reference_checks,checkpoint=pulse)
        for entry in references.values(ordered=True):
            pulse()
            if entry['source_hex']==source_hex(spool_path) and 'source_offset' in entry:entry['source_bytes']=spool_size
            transfers.append(entry)
        sources=[]
        if request['mail']:
            stage = 'mail'
            status('running', files_done=0)
            sources,mail_transfers,identities=mail_delta(request,status,since) if since is not None else mail_snapshot(request,status)
            transfers.extend(mail_transfers)
            for source in sources:
                manifest['files'][source['path']]=dict((key,source[key]) for key in ('bytes','sha256'))
            formats={}
            for entry in identities:formats[entry['name_hex']]=entry.pop('format')
            write_json(os.path.join(root,'mail-formats.json'),formats,checkpoint=pulse)
            write_json(os.path.join(root, 'mail-check.json'), identities,checkpoint=pulse)
            mail_dir = os.path.join(folder, 'mail')
            os.mkdir(mail_dir, 0o700)
            source_file = os.path.join(mail_dir, 'sources.json')
            write_json(source_file, {'user': user, 'sources': sources, 'raw_files_complete': True},checkpoint=pulse)
            manifest['files']['mail/sources.json'] = hash_file(source_file)
        stage='metadata';budget.phase='metadata';current.update(phase='metadata')
        status('running',phase='metadata')
        manifest['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        manifest['ssh_export'] = {'protocol': PROTOCOL, 'job': os.path.basename(root),
            'approvals': request['approvals'], 'mail': request['mail'], 'authentication': 'administrator_ssh',
            'source_transfer': 'direct-mail-and-attachments/1'}
        if spooled[0]:manifest['ssh_export']['derived_storage']='sequential-ranges/1'
        if since is not None:manifest['ssh_export']['since']=since
        manifest['warnings'].append('Export requested by SSH administrator; employee password was not verified.')
        manifest['counts']['mail_files'] = len(sources)
        manifest['coverage'].append({'kind': 'mail', 'status': 'direct_source_transfer', 'files': len(sources)})
        manifest_path = os.path.join(folder, 'manifest.json')
        write_json(manifest_path, manifest,checkpoint=pulse)
        for name in manifest['files']:
            pulse()
            if name.startswith('mail/raw/') or name in references:
                continue
            transfers.append(dict(manifest['files'][name], path=name,
                                  source_hex=source_hex(os.path.join(folder, *name.split('/')))))
        transfers.append(dict(hash_file(manifest_path), path='manifest.json', source_hex=source_hex(manifest_path)))
        write_json(os.path.join(root, 'transfer.json'), {'protocol': PROTOCOL, 'user': user,
                   'job': os.path.basename(root), 'files': transfers},checkpoint=pulse)
        status('ready', files=len(transfers), bytes=sum(item['bytes'] for item in transfers))
    except Exception as exc:
        code, diagnostic = failure_details(exc, stage)
        save_failure(root, code, diagnostic)
        status('failed', code=code, diagnostic=diagnostic)
    finally:
        if spool[0] is not None:
            try:spool[0].close()
            except (IOError,OSError):pass
        try:
            if metadata_store is not None:metadata_store.close()
        finally:budget.close()


def capture_directory(root):
    matches=[]
    for name in os.listdir(root):
        path=os.path.join(root,name)
        if os.path.isdir(path) and not os.path.islink(path) and os.path.isfile(os.path.join(path,'manifest.json')):
            if os.path.islink(os.path.join(path,'manifest.json')):raise SafeError('unsafe_job')
            matches.append(path)
    if len(matches)!=1:raise SafeError('invalid_metadata')
    return matches[0]


def verify_sources(root, mode='strict'):
    root, request = private_root(root)
    ready=read_json(os.path.join(root, 'status.json'))
    if ready.get('state') != 'ready':
        raise SafeError('export_not_ready')
    if mode not in ('strict','check','copy'):raise SafeError('invalid_metadata')
    if mode=='copy':
        receipt=read_json(os.path.join(root,'copy-receipt.json'))
        if (receipt.get('protocol')!='segio-ssh-copy/1' or receipt.get('job')!=os.path.basename(root) or
                receipt.get('files')!=ready.get('files') or receipt.get('bytes')!=ready.get('bytes') or
                receipt.get('manifest_sha256')!=hash_file(os.path.join(capture_directory(root),'manifest.json'))['sha256']):
            raise SafeError('invalid_metadata')
    result={'appendable':{},'growth_files':0,'growth_bytes':0}
    # These lists are generated inside this owned private job. They routinely
    # exceed the 64 KiB bound used for untrusted requests/control messages.
    for entry in read_json(os.path.join(root, 'approval-check.json'), None) if os.path.isfile(os.path.join(root, 'approval-check.json')) else []:
        path = binascii.unhexlify(entry['source_hex'].encode('ascii'))
        if list(fingerprint(path)) != entry['identity']:
            raise SafeError('mail_changed_retry')
    if request['mail']:
        before = read_json(os.path.join(root, 'mail-check.json'), None)
        boxes={}
        if mode!='strict':
            formats_path=os.path.join(root,'mail-formats.json')
            if os.path.isfile(formats_path):
                for name,kind in read_json(formats_path,None).items():
                    if kind=='mbox':boxes[name]=[]
            for source in read_json(os.path.join(capture_directory(root),'mail','sources.json'),None)['sources']:
                if source.get('format')=='mbox' or source.get('folder_format')=='mbox':
                    boxes.setdefault(source['name_bytes_hex'],[]).append(source['path'])
        actual = []
        for name, path, identity in inventory(mail_path(request['mail_template'], request['user'])):
            raw_name = name if isinstance(name, bytes) else name.encode(sys.getfilesystemencoding(), 'surrogateescape')
            actual.append({'name_hex': binascii.hexlify(raw_name).decode('ascii'), 'identity': list(identity)})
        if len(actual)!=len(before):raise SafeError('mail_changed_retry')
        for old,new in zip(before,actual):
            name=old['name_hex'];identity=old['identity'];current=new['identity']
            if name!=new['name_hex']:raise SafeError('mail_changed_retry')
            if identity!=current:
                # Size growth is provisional: check mode never marks a copy
                # verified. Copy mode is entered only after the client hashes
                # every captured byte/range and sends a job-bound receipt.
                if mode=='strict' or name not in boxes or identity[:2]!=current[:2] or current[2]<=identity[2]:
                    raise SafeError('mail_changed_retry')
                result['growth_files']+=1;result['growth_bytes']+=current[2]-identity[2]
            if mode=='check':
                for path in boxes.get(name,[]):result['appendable'][path]=identity[2]
    if mode=='check':
        write_json(os.path.join(root,'copy-plan.json'),dict(result,protocol='segio-ssh-copy-plan/1',job=os.path.basename(root)))
    if mode!='check':
        write_json(os.path.join(root, 'verified.json'), {'protocol': PROTOCOL, 'ok': True, 'revision': REVISION,
            'verification':'captured-sha256/1' if mode=='copy' else 'source-identity/1',
            'growth_files':result['growth_files'],'growth_bytes':result['growth_bytes']})
    return {'growth_files':result['growth_files'],'growth_bytes':result['growth_bytes']}


def main():
    parser = OptionParser()
    parser.add_option('--root')
    parser.add_option('--action')
    parser.add_option('--detach', action='store_true')
    options, args = parser.parse_args()
    if args or not options.root or options.action not in ('prepare', 'verify', 'check-copy', 'verify-copy', 'cleanup'):
        parser.error('Use fixed SSH helper actions only.')
    try:
        root, request = private_root(options.root)
        if options.action == 'prepare':
            if options.detach:
                if os.fork():
                    return 0
                os.setsid()
                if os.fork():
                    os._exit(0)
                descriptor = os.open(os.devnull, os.O_RDWR)
                for number in (0, 1, 2):
                    os.dup2(descriptor, number)
                if descriptor > 2:
                    os.close(descriptor)
            prepare(root,apply_limits=True)
        elif options.action == 'verify':
            verify_sources(root)
        elif options.action in ('check-copy','verify-copy'):
            result=verify_sources(root,'check' if options.action=='check-copy' else 'copy')
            print(json.dumps(dict(result,protocol=PROTOCOL,ok=True)))
            return 0
        else:
            if read_json(os.path.join(root, 'verified.json')).get('ok') is not True:
                raise SafeError('unsafe_job')
            shutil.rmtree(root)
            if os.path.lexists(root):
                raise SafeError('job_storage_failed')
        if not options.detach:
            print(json.dumps({'protocol': PROTOCOL, 'ok': True}))
        return 0
    except Exception as exc:
        stage = {'verify':'verification','check-copy':'verification','verify-copy':'verification','cleanup':'cleanup'}.get(options.action, 'initializing')
        code, diagnostic = failure_details(exc, stage)
        # Persist only after ownership and the native request have been checked.
        if 'root' in locals() and 'request' in locals():
            save_failure(root, code, diagnostic)
        print(json.dumps({'protocol': PROTOCOL, 'error': code, 'diagnostic': diagnostic}))
        return 1


if __name__ == '__main__':
    os.umask(0o077)
    sys.exit(main())
