"""Administrator SSH transport: fixed export helper, SFTP resume, local indexing."""
from __future__ import annotations
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import stat
import sys
import threading
import time
import zipfile

import paramiko

from .archive import atomic_write
from .compact_mail import build_compact_catalog, digest_file
from .db_client import DBCollector, publish_local, safe_member
from .web import Cancelled, CollectionError

PROTOCOL = 'segio-ssh-archive/1'
TOOLS = ('remote_ssh_export.py','live_export_worker.py','export_approval_db.py',
         'export_schema_from_segio_config.py','approval_db_columns.json','db_archive_index.html')


@dataclass
class SSHConfig:
    host: str
    ssh_user: str
    username: str
    output: Path
    port: int = 22
    key_file: str = ''
    db_config: str = '/home/segio/cell000/etc/db.conf'
    cell: str = '/home/segio/cell000'
    mail_template: str = '/home/segio/cell000/home/{initial}/{user}/mbox'
    python: str = '/usr/bin/python'
    psql: str = '/usr/bin/psql'
    approvals: bool = True
    mail: bool = True
    make_zip: bool = False
    resume: bool = True
    known_hosts: Path = field(default_factory=lambda: Path.home()/'.segio-archive/known_hosts')
    cancel: threading.Event = field(default_factory=threading.Event,repr=False)
    tools_dir: Path | None = None

    def validate(self):
        if (not self.host or len(self.host)>253 or re.search(r'[\s/\\\x00-\x1f@]',self.host) or
                not 1 <= self.port <= 65535):
            raise CollectionError('SSH 서버 주소와 포트를 확인하세요.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',self.ssh_user):
            raise CollectionError('서버의 SSH 계정 이름을 입력하세요.')
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}',self.username) or self.username in ('.','..','guest'):
            raise CollectionError('백업할 직원의 정확한 SEGIO ID를 입력하세요.')
        if not self.approvals and not self.mail:
            raise CollectionError('결재 문서 또는 메일을 선택하세요.')
        for path in (self.db_config,self.cell,self.mail_template,self.python,self.psql):
            if not path.startswith('/') or re.search(r'[\x00-\x1f]',path):
                raise CollectionError('서버 경로는 절대 경로로 입력하세요.')
        if self.key_file and not Path(self.key_file).expanduser().is_file():
            raise CollectionError('SSH 개인키 파일을 찾지 못했습니다.')


class VerifiedHostPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, path, confirm):
        self.path, self.confirm = path, confirm

    def missing_host_key(self, client, hostname, key):
        fingerprint = 'SHA256:'+base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        if not self.confirm or not self.confirm(hostname,key.get_name(),fingerprint):
            raise CollectionError('SSH 서버 지문을 확인하지 않아 접속을 중단했습니다. [SSH_HOST_KEY_UNCONFIRMED]')
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        client.get_host_keys().add(hostname,key.get_name(),key)
        client.save_host_keys(str(self.path))
        os.chmod(self.path,0o600)


class SSHCollector:
    def __init__(self, config: SSHConfig, log=lambda _:None, confirm_host=None):
        self.config, self.log, self.confirm_host = config, log, confirm_host
        self.client = self.sftp = None
        self.phase, self.remote = 'SSH 연결', None

    def checkpoint(self):
        if self.config.cancel.is_set():
            raise Cancelled('SSH 수집을 중단했습니다. 다음 실행에서 같은 직원 자료를 이어받을 수 있습니다.')

    def connect(self, password):
        client = paramiko.SSHClient()
        if self.config.known_hosts.exists():
            client.load_host_keys(str(self.config.known_hosts))
        client.set_missing_host_key_policy(VerifiedHostPolicy(self.config.known_hosts,self.confirm_host))
        self.client = client
        client.connect(self.config.host,port=self.config.port,username=self.config.ssh_user,
                       password=password or None,passphrase=password or None,
                       key_filename=str(Path(self.config.key_file).expanduser()) if self.config.key_file else None,
                       look_for_keys=False,allow_agent=False,timeout=20,banner_timeout=30,auth_timeout=30,
                       disabled_algorithms={'pubkeys':['rsa-sha2-256','rsa-sha2-512']})
        client.get_transport().set_keepalive(20)
        self.sftp = client.open_sftp()
        self.sftp.get_channel().settimeout(60)

    def read_json(self,path,limit=64*1024*1024):
        with self.sftp.open(path,'rb') as stream:
            data = stream.read(limit+1)
        if len(data)>limit:
            raise CollectionError('SSH 추출 메타데이터가 너무 큽니다. [SSH_METADATA_LIMIT]')
        return json.loads(data)

    def execute(self,action,detach=False):
        command = [self.config.python,self.remote+'/remote_ssh_export.py','--root',self.remote,'--action',action]
        if detach:
            command.append('--detach')
        channel = self.client.get_transport().open_session(timeout=30)
        try:
            channel.exec_command(' '.join(shlex.quote(arg) for arg in command))
            output, deadline = bytearray(), time.monotonic()+600
            while not channel.exit_status_ready() or channel.recv_ready() or channel.recv_stderr_ready():
                self.checkpoint()
                if channel.recv_ready():
                    output.extend(channel.recv(8192))
                    if len(output)>65536:
                        raise CollectionError('SSH 작업기의 응답 크기가 올바르지 않습니다. [SSH_HELPER_RESPONSE]')
                if channel.recv_stderr_ready():
                    channel.recv_stderr(8192)  # Never log stderr: it may contain private data.
                if time.monotonic()>deadline:
                    raise CollectionError('SSH 작업기 응답 시간이 초과됐습니다. [SSH_HELPER_TIMEOUT]')
                self.config.cancel.wait(.05)
            code = channel.recv_exit_status()
            if detach and not output and code==0:
                return
            value = json.loads(output)
            if value.get('protocol') != PROTOCOL or code or value.get('ok') is not True:
                raise CollectionError(DBCollector.error_message(value.get('error')))
        finally:
            channel.close()

    def initialize(self,cache):
        state_path = cache/'session.json'
        if state_path.is_symlink():
            raise CollectionError('SSH 이어받기 정보에 심볼릭 링크를 사용할 수 없습니다.')
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            self.remote = state['remote']
            if not re.fullmatch(r'/tmp/\.segio-ssh-[0-9a-f]{32}',self.remote):
                raise CollectionError('SSH 이어받기 정보가 올바르지 않습니다.')
            try:
                info = self.sftp.lstat(self.remote)
            except OSError:
                raise CollectionError('서버 임시 작업이 없어 이어받을 수 없습니다. ‘이전 전송 이어받기’를 해제해 새로 시작하세요. [SSH_SNAPSHOT_MISSING]') from None
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise CollectionError('SSH 임시 작업 폴더가 안전하지 않습니다.')
            self.log('이전 SSH 작업에 연결해 전송을 이어갑니다.')
            return
        self.remote = '/tmp/.segio-ssh-'+secrets.token_hex(16)
        self.sftp.mkdir(self.remote,mode=0o700)
        self.sftp.chmod(self.remote,0o700)
        tools = self.config.tools_dir or (Path(sys.executable).parent/'tools' if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]/'tools')
        for name in TOOLS:
            self.checkpoint()
            self.sftp.put(str(tools/name),self.remote+'/'+name)
            self.sftp.chmod(self.remote+'/'+name,0o600)
        request = {'protocol':PROTOCOL,'user':self.config.username,'db_config':self.config.db_config,
                   'cell':self.config.cell,'mail_template':self.config.mail_template,'psql':self.config.psql,
                   'approvals':self.config.approvals,'mail':self.config.mail}
        for name, data in (('request.json',request),('status.json',{'protocol':PROTOCOL,'state':'queued','stage':'initializing'})):
            with self.sftp.open(self.remote+'/'+name,'wx') as stream:
                stream.write(json.dumps(data).encode())
            self.sftp.chmod(self.remote+'/'+name,0o600)
        atomic_write(state_path,json.dumps({'remote':self.remote,'protocol':PROTOCOL}).encode())
        os.chmod(state_path,0o600)
        self.execute('prepare',detach=True)

    def wait_ready(self):
        previous, deadline = None, time.monotonic()+72*3600
        while time.monotonic()<deadline:
            self.checkpoint()
            status = self.read_json(self.remote+'/status.json',65536)
            if status.get('protocol') != PROTOCOL:
                raise CollectionError('SSH 작업기 형식이 일치하지 않습니다.')
            stage = status.get('stage')
            if stage != previous:
                self.log({'initializing':'SSH 서버 작업 준비 중','database':'직원의 결재 DB를 읽기 전용으로 추출하는 중',
                          'mail':'MBOX 원본 목록과 SHA-256을 확인하는 중 · 서버에 메일 복사본을 만들지 않습니다'}.get(stage,'SSH 추출 준비 확인 중'))
                previous = stage
            if status.get('state') == 'failed':
                raise CollectionError(DBCollector.error_message(status.get('code')))
            if status.get('state') == 'ready':
                return
            self.config.cancel.wait(1)
        raise CollectionError('SSH 서버 추출 준비 대기 시간이 초과됐습니다.')

    def transfer(self,folder):
        metadata = self.read_json(self.remote+'/transfer.json')
        if (metadata.get('protocol') != PROTOCOL or metadata.get('user') != self.config.username or
                metadata.get('job') != self.remote.rsplit('/',1)[1] or not isinstance(metadata.get('files'),list)):
            raise CollectionError('SSH 추출 대상 또는 작업 번호가 일치하지 않습니다.')
        if len(metadata['files'])>200000:
            raise CollectionError('SSH 원본 파일 수가 제한을 초과합니다.')
        seen, records, total = set(), [], 0
        for item in metadata['files']:
            path = safe_member(item['path'])
            folded = str(path).casefold()
            if (folded in seen or type(item.get('bytes')) is not int or item['bytes']<0 or
                    not re.fullmatch('[a-f0-9]{64}',str(item.get('sha256',''))) or
                    not re.fullmatch('(?:[a-f0-9]{2}){1,8192}',str(item.get('source_hex','')))):
                raise CollectionError('SSH 파일 검증 목록이 올바르지 않습니다.')
            source = bytes.fromhex(item['source_hex'])
            if not source.startswith(b'/') or b'\0' in source:
                raise CollectionError('SSH 원본 경로가 올바르지 않습니다.')
            target = folder.joinpath(*path.parts)
            cursor = folder
            for part in path.parts:
                cursor = cursor/part
                if cursor.is_symlink():
                    raise CollectionError('SSH 저장 폴더 안의 심볼릭 링크는 사용할 수 없습니다.')
            seen.add(folded)
            records.append((item,source,target))
            total += item['bytes']
        existing = sum(min(target.stat().st_size,item['bytes']) for item,_,target in records if target.is_file())
        if total-existing+128*1024*1024 > shutil.disk_usage(folder).free:
            raise CollectionError('원본을 저장할 Windows 디스크 공간이 부족합니다.')
        done, logged = 0, 0.0
        for item,source,target in records:
            self.checkpoint()
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            size = target.stat().st_size if target.exists() else 0
            if target.is_file() and size==item['bytes'] and digest_file(target,self.checkpoint)=={'bytes':item['bytes'],'sha256':item['sha256']}:
                done += size
                continue
            if target.exists() and size>=item['bytes']:
                target.unlink()
                size=0
            attributes = self.sftp.lstat(source)
            if not stat.S_ISREG(attributes.st_mode) or attributes.st_size != item['bytes']:
                raise CollectionError('서버 원본이 변경됐습니다. 이어받기를 해제해 새로 추출하세요. [SSH_SOURCE_CHANGED]')
            with self.sftp.open(source,'rb') as incoming, target.open('ab') as outgoing:
                incoming.seek(size)
                done += size
                while size<item['bytes']:
                    self.checkpoint()
                    # Bound both the read queue and memory. Prefetching an
                    # entire 40GB file would build a huge request list.
                    end=min(size+1024*1024,item['bytes'])
                    chunks=[(offset,min(32768,end-offset)) for offset in range(size,end,32768)]
                    blocks=incoming.readv(chunks,max_concurrent_prefetch_requests=8)
                    for (_,expected_size),block in zip(chunks,blocks):
                        self.checkpoint()
                        if len(block)!=expected_size:
                            raise CollectionError('SSH 파일 전송이 중간에 끝났습니다. 다시 실행하면 이어받습니다.')
                        outgoing.write(block)
                        size += len(block)
                        done += len(block)
                        if time.monotonic()-logged>2:
                            self.log(f'SFTP 전송 {done/1024**3:.2f} / {total/1024**3:.2f} GiB · {100*done/max(total,1):.1f}%')
                            logged=time.monotonic()
            if digest_file(target,self.checkpoint) != {'bytes':item['bytes'],'sha256':item['sha256']}:
                raise CollectionError('내려받은 파일의 SHA-256이 일치하지 않습니다. 완료로 처리하지 않았습니다. [SSH_FILE_HASH_MISMATCH]')
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        snapshot=manifest.get('ssh_export',{})
        by_name={item['path']:item for item,_,_ in records}
        if (manifest.get('user')!=self.config.username or manifest.get('account_deletion_verified') is not False or
                snapshot.get('job')!=self.remote.rsplit('/',1)[1] or snapshot.get('approvals')!=self.config.approvals or
                snapshot.get('mail')!=self.config.mail or set(by_name)!=set(manifest['files'])|{'manifest.json'}):
            raise CollectionError('SSH 보관본과 전송 목록이 일치하지 않습니다.')
        for name, details in manifest['files'].items():
            if any(details[key]!=by_name[name][key] for key in ('bytes','sha256')):
                raise CollectionError('SSH 보관 파일 검증 목록이 일치하지 않습니다.')

    def run(self,password):
        self.config.validate()
        try:
            self.checkpoint()
            self.connect(password)
            password=''
            output=self.config.output.expanduser().resolve()
            output.mkdir(parents=True,exist_ok=True)
            identity={key:getattr(self.config,key) for key in ('host','port','ssh_user','username','db_config','cell','mail_template','approvals','mail')}
            token=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:24]
            cache=output/'.segio-ssh-cache'/token
            if cache.is_symlink() or cache.parent.is_symlink():
                raise CollectionError('SSH 이어받기 폴더에 심볼릭 링크를 사용할 수 없습니다.')
            if cache.exists() and not self.config.resume:
                shutil.rmtree(cache)
            cache.mkdir(parents=True,exist_ok=True,mode=0o700)
            contents=cache/'contents'
            if contents.is_symlink():
                raise CollectionError('SSH 저장 폴더에 심볼릭 링크를 사용할 수 없습니다.')
            contents.mkdir(exist_ok=True,mode=0o700)
            self.phase='SSH 서버 추출 준비'
            self.initialize(cache)
            self.wait_ready()
            self.phase='SFTP 원본 전송'
            self.transfer(contents)
            self.phase='서버 원본 변경 검사'
            self.execute('verify')
            self.phase='Windows 검색 색인 생성'
            self.log('메일 검색 DB와 바로보기 HTML·첨부를 만드는 중 · 추가 저장 공간이 필요합니다')
            build_compact_catalog(contents,self.config.username,self.checkpoint,self.log)
            archive=None
            if self.config.make_zip:
                self.phase='Windows ZIP 생성'
                archive=cache/'archive.zip'
                size=sum(p.stat().st_size for p in contents.rglob('*') if p.is_file())
                if size+128*1024*1024>shutil.disk_usage(cache).free:
                    raise CollectionError('추가 ZIP을 생성할 디스크 공간이 부족합니다. ZIP 선택을 해제하고 이어받으세요.')
                with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as zipped:
                    for path in contents.rglob('*'):
                        if path.is_file():
                            with path.open('rb') as source,zipped.open(path.relative_to(contents).as_posix(),'w',force_zip64=True) as target:
                                while block:=source.read(1024*1024):
                                    self.checkpoint()
                                    target.write(block)
            self.checkpoint()
            result=publish_local(contents,archive,output,self.config.username)
            try:
                self.execute('cleanup')
            except Exception:
                self.log('보관은 완료됐습니다. 서버의 SSH 임시 작업 정리를 확인하지 못했습니다.')
            shutil.rmtree(cache)
            self.log('SSH 백업과 SHA-256 검증 완료: '+str(result))
            return result
        except (CollectionError,Cancelled):
            raise
        except paramiko.BadHostKeyException:
            raise CollectionError('저장된 SSH 서버 지문과 다릅니다. 관리자에게 서버 지문을 확인하세요. [SSH_HOST_KEY_CHANGED]') from None
        except paramiko.AuthenticationException:
            raise CollectionError('서버 SSH 계정 인증에 실패했습니다. SEGIO 직원 암호가 아닌 SSH 암호·개인키를 확인하세요. [SSH_AUTH_FAILED]') from None
        except (paramiko.SSHException,OSError,socket.timeout):
            raise CollectionError(self.phase+' 중 연결 또는 파일 처리에 실패했습니다. 서버·저장 공간을 확인한 뒤 다시 실행하면 이어받습니다. [SSH_CONNECTION_OR_IO]') from None
        except Exception:
            raise CollectionError(self.phase+' 중 처리 오류가 발생했습니다. [SSH_PROCESSING_ERROR]') from None
        finally:
            password=''
            for connection in (self.sftp,self.client):
                if connection:
                    try:
                        connection.close()
                    except Exception:
                        pass
