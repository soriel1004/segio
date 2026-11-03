"""Administrator SSH transport: fixed export helper, SFTP resume, local indexing."""
from __future__ import annotations
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
import posixpath
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

from . import __version__
from .archive import atomic_write,utc_now
from .compact_mail import build_compact_catalog, digest_file
from .db_client import DBCollector, publish_local, safe_member
from .web import Cancelled, CollectionError
from .progress import Reporter, LABELS

PROTOCOL = 'segio-ssh-archive/1'
CHECK_PROTOCOL = 'segio-ssh-check/1'
WORK_PROTOCOL = 'segio-ssh-workspace/1'
WORK_SCRIPT = '''import errno,json,os,sys
try:
 base=os.path.realpath(sys.argv[1])
 if not os.path.isdir(base): os.makedirs(base,448)
 info=os.statvfs(base)
 free=info.f_bavail*(info.f_frsize or info.f_bsize)
 if not os.access(base,os.W_OK|os.X_OK): raise OSError(errno.EACCES,'workspace')
 if free<8*1024*1024: raise OSError(errno.ENOSPC,'workspace')
 print(json.dumps({'protocol':'segio-ssh-workspace/1','ok':True,'base':base,'free':free}))
except (IOError,OSError) as error:
 code='server_disk_full' if error.errno==errno.ENOSPC else 'server_access_denied'
 print(json.dumps({'protocol':'segio-ssh-workspace/1','ok':False,'error':code}))
 sys.exit(1)
'''
CHECK_SCRIPT = '''import json,os,sys
config,cell,psql,mail,work=sys.argv[1:]
try:
 import hashlib,zipfile,zlib
 modules=True
except ImportError:
 modules=False
checks={'python_compatible':sys.version_info[:2]>=(2,6),
 'runtime_modules':modules,
 'database_config_readable':os.path.isfile(config) and os.access(config,os.R_OK),
 'cell_readable':os.path.isdir(cell) and os.access(cell,os.R_OK|os.X_OK),
 'psql_executable':os.path.isfile(psql) and os.access(psql,os.X_OK),
 'work_directory_writable':os.path.isdir(work) and os.access(work,os.W_OK|os.X_OK),
 'mail_readable':(os.path.exists(mail) and os.access(mail,os.R_OK|(os.X_OK if os.path.isdir(mail) else 0))) if mail else None}
ready=all(value is not False for value in checks.values())
print(json.dumps({'protocol':'segio-ssh-check/1','ok':True,'ready':ready,
 'python_version':list(sys.version_info[:3]),'checks':checks}))
'''
TOOLS = ('remote_ssh_export.py','live_export_worker.py','export_approval_db.py',
         'export_schema_from_segio_config.py','approval_db_columns.json','db_archive_index.html')


def ssh_failure_message(code, diagnostic=None, phase=''):
    codes={'export_failed','invalid_metadata','unsafe_job','invalid_identity','export_not_ready',
        'server_disk_full','server_access_denied','server_file_missing','server_file_too_large',
        'mail_changed_retry','mail_unsafe_file','mail_unsafe_symlink','mail_path_unconfigured',
        'mail_path_missing_or_symlink','job_storage_failed','db_config_invalid','db_config_unreadable',
        'psql_unavailable','db_authentication_failed','db_permission_denied','db_schema_mismatch',
        'db_query_timeout','db_database_missing','db_connection_failed','db_query_failed',
        'db_snapshot_mismatch','approval_user_not_found','cell_data_missing','db_encoding_failed'}
    code=code if isinstance(code,str) and code in codes else 'export_failed'
    message=DBCollector.error_message(code)
    if '[export_failed]' in message:
        message='SSH 서버 작업을 완료하지 못했습니다. 작업 폴더의 진단 기록을 확인하세요. ['+code+']'
    diagnostic=diagnostic if isinstance(diagnostic,dict) else {}
    stages={'initializing':'서버 작업 준비','database':'결재 DB 추출','mail':'메일 원본 검사',
            'verification':'서버 원본 변경 검사','cleanup':'서버 임시 작업 정리'}
    raw_stage=diagnostic.get('stage')
    stage=stages.get(raw_stage,phase) if isinstance(raw_stage,str) else phase
    if stage:message+='\n실패 단계: '+stage
    kinds={'SafeError','TypeError','ValueError','KeyError','AttributeError','MemoryError','OverflowError',
           'RuntimeError','IOError','OSError','FileNotFoundError','PermissionError',
           'UnicodeEncodeError','UnicodeDecodeError','UnicodeError'}
    details=[]
    if isinstance(diagnostic.get('exception_type'),str) and diagnostic['exception_type'] in kinds:details.append(diagnostic['exception_type'])
    number=diagnostic.get('errno')
    if type(number) is int and 0<=number<=4096:details.append('errno='+str(number))
    site=diagnostic.get('site')
    functions={'prepare','verify_sources','read_json','write_json','run_export','reference_file','hash_file',
               'fingerprint','inventory','render_bundle','attachment','local_file','image','freeze',
               'query_database','psql_readonly','read_rows','private_root','text'}
    if isinstance(site,dict) and isinstance(site.get('function'),str) and site['function'] in functions and type(site.get('line')) is int and 0<site['line']<1000000:
        details.append(site['function']+':'+str(site['line']))
    if details:message+='\n진단: '+' · '.join(details)
    return message


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
    work_base: str = '/backup'
    approvals: bool = True
    mail: bool = True
    make_zip: bool = False
    resume: bool = True
    known_hosts: Path = field(default_factory=lambda: Path.home()/'.segio-archive/known_hosts')
    cancel: threading.Event = field(default_factory=threading.Event,repr=False)
    tools_dir: Path | None = None

    def validate(self, for_backup=True):
        if (not self.host or len(self.host)>253 or re.search(r'[\s/\\\x00-\x1f@]',self.host) or
                not 1 <= self.port <= 65535):
            raise CollectionError('SSH 서버 주소와 포트를 확인하세요.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',self.ssh_user):
            raise CollectionError('서버의 SSH 계정 이름을 입력하세요.')
        if (for_backup or self.username) and (not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}',self.username) or self.username in ('.','..','guest')):
            raise CollectionError('백업할 직원의 정확한 SEGIO ID를 입력하세요.')
        if for_backup and not self.approvals and not self.mail:
            raise CollectionError('결재 문서 또는 메일을 선택하세요.')
        for path in (self.db_config,self.cell,self.mail_template,self.python,self.psql,self.work_base):
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
    def __init__(self, config: SSHConfig, log=lambda _:None, confirm_host=None, progress=None):
        self.config, self.log, self.confirm_host = config, log, confirm_host
        self.client = self.sftp = None
        self.phase, self.remote = 'SSH 연결', None
        self.retired=[]
        self.report=Reporter(progress)

    def begin_stage(self,stage,detail=''):
        self.phase=LABELS[stage]
        self.report(stage,detail=detail,force=True)

    def forward_progress(self,event):
        self.phase=LABELS[event['stage']]
        if self.report.callback:
            self.report.callback(event)

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

    def command(self,command,protocol=PROTOCOL,detach=False,timeout=600):
        channel = self.client.get_transport().open_session(timeout=30)
        try:
            channel.exec_command(' '.join(shlex.quote(arg) for arg in command))
            output, deadline = bytearray(), time.monotonic()+timeout
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
            try:
                value = json.loads(output)
                if not isinstance(value,dict):
                    raise ValueError()
            except (ValueError,UnicodeError):
                raise CollectionError('서버 작업기 응답을 확인하지 못했습니다. 서버 Python 경로와 실행 권한을 확인하세요. [SSH_HELPER_RESPONSE]') from None
            if value.get('protocol') != protocol or code or value.get('ok') is not True:
                raise CollectionError(ssh_failure_message(value.get('error'),value.get('diagnostic'),self.phase))
            return value
        finally:
            channel.close()

    def execute(self,action,detach=False):
        command = [self.config.python,self.remote+'/remote_ssh_export.py','--root',self.remote,'--action',action]
        if detach:
            command.append('--detach')
        return self.command(command,detach=detach)

    def close_connections(self):
        for connection in (self.sftp,self.client):
            if connection:
                try:
                    connection.close()
                except Exception:
                    pass
        self.sftp=self.client=None

    def check_connection(self,password):
        """Read-only authentication/readiness check; no upload or export job."""
        self.config.validate(for_backup=False)
        try:
            self.checkpoint()
            self.begin_stage('connect')
            self.connect(password)
            password=''
            self.checkpoint()
            mail=''
            if self.config.mail and self.config.username:
                try:
                    mail=self.config.mail_template.format(initial=self.config.username[0],user=self.config.username)
                except (ValueError,KeyError):
                    raise CollectionError('메일 경로 형식을 확인하세요.') from None
            self.begin_stage('environment')
            result=self.command([self.config.python,'-c',CHECK_SCRIPT,self.config.db_config,
                self.config.cell,self.config.psql,mail,self.config.work_base],protocol=CHECK_PROTOCOL,timeout=30)
            self.checkpoint()
            checks=result.get('checks')
            keys=('python_compatible','runtime_modules','database_config_readable','cell_readable',
                  'psql_executable','work_directory_writable','mail_readable')
            version=result.get('python_version')
            if (not isinstance(checks,dict) or any(type(checks.get(key)) is not bool for key in keys[:-1])
                    or checks.get('mail_readable') not in (True,False,None)
                    or type(result.get('ready')) is not bool
                    or not isinstance(version,list) or len(version)!=3 or any(type(n) is not int for n in version)):
                raise CollectionError('SSH 환경 확인 응답 형식이 올바르지 않습니다. [SSH_HELPER_RESPONSE]')
            return result
        except Cancelled:
            raise Cancelled('SSH 연결 확인을 중단했습니다.') from None
        except paramiko.BadHostKeyException:
            raise CollectionError('저장된 SSH 서버 지문과 다릅니다. 관리자에게 확인하세요. [SSH_HOST_KEY_CHANGED]') from None
        except paramiko.AuthenticationException:
            raise CollectionError('서버 SSH 계정 인증에 실패했습니다. SSH 암호·개인키를 확인하세요. [SSH_AUTH_FAILED]') from None
        except (paramiko.SSHException,OSError,socket.timeout):
            raise CollectionError('SSH 연결 확인에 실패했습니다. 서버 주소·포트·네트워크를 확인하세요. [SSH_CONNECTION_OR_IO]') from None
        finally:
            password=''
            self.close_connections()

    def initialize(self,cache):
        state_path = cache/'session.json'
        if state_path.is_symlink():
            raise CollectionError('SSH 이어받기 정보에 심볼릭 링크를 사용할 수 없습니다.')
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding='utf-8'))
            self.remote = state['remote']
            if not self.valid_remote(self.remote):
                raise CollectionError('SSH 이어받기 정보가 올바르지 않습니다.')
            try:
                info = self.sftp.lstat(self.remote)
            except OSError:
                raise CollectionError('서버 임시 작업이 없어 이어받을 수 없습니다. ‘이전 전송 이어받기’를 해제해 새로 시작하세요. [SSH_SNAPSHOT_MISSING]') from None
            if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
                raise CollectionError('SSH 임시 작업 폴더가 안전하지 않습니다.')
            self.retired=state.get('retired',[])
            if not isinstance(self.retired,list) or any(not self.valid_remote(path) for path in self.retired):
                raise CollectionError('SSH 이전 작업 경로를 확인하세요.')
            status=self.read_json(self.remote+'/status.json',65536)
            if status.get('protocol')!=PROTOCOL:
                raise CollectionError('SSH 이전 작업 상태를 확인하세요.')
            if status.get('state')!='failed':
                request=self.read_json(self.remote+'/request.json',65536)
                if request.get('since')!=getattr(self,'since',None):raise CollectionError('이어받기 날짜가 기존 작업과 다릅니다. [SSH_INCREMENTAL_SCOPE_MISMATCH]')
                if status.get('state')=='ready':
                    self.refresh_ready_helper()
                self.log('이전 SSH 작업에 연결해 전송을 이어갑니다. 작업 경로: '+self.remote)
                return
            self.retired.append(self.remote)
            self.log('이전 서버 추출이 실패해 새 작업 공간에서 다시 추출합니다.')
        workspace=self.command([self.config.python,'-c',WORK_SCRIPT,self.config.work_base],protocol=WORK_PROTOCOL,timeout=30)
        base=workspace.get('base')
        if not isinstance(base,str) or not base.startswith('/') or posixpath.normpath(base)!=base or '\0' in base:
            raise CollectionError('서버 작업 폴더 응답이 올바르지 않습니다.')
        self.remote = base.rstrip('/')+'/.segio-ssh-'+secrets.token_hex(16)
        self.log('서버 작업 경로: '+self.remote+' · 메일·첨부 원본은 직접 전송합니다.')
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
        if getattr(self,'since',None) is not None:request['since']=self.since
        for name, data in (('request.json',request),('status.json',{'protocol':PROTOCOL,'state':'queued','stage':'initializing'})):
            with self.sftp.open(self.remote+'/'+name,'wx') as stream:
                stream.write(json.dumps(data).encode())
            self.sftp.chmod(self.remote+'/'+name,0o600)
        atomic_write(state_path,json.dumps({'remote':self.remote,'protocol':PROTOCOL,'retired':self.retired}).encode())
        os.chmod(state_path,0o600)
        self.execute('prepare',detach=True)

    def refresh_ready_helper(self):
        """Repair verification in a finished server job without re-extracting it."""
        request=self.read_json(self.remote+'/request.json',65536)
        if request.get('protocol')!=PROTOCOL or request.get('user')!=self.config.username:
            raise CollectionError('이어받을 SSH 작업의 직원이 일치하지 않습니다.')
        tools=self.config.tools_dir or (Path(sys.executable).parent/'tools' if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]/'tools')
        temporary=self.remote+'/.verification-'+secrets.token_hex(8)+'.py'
        self.sftp.put(str(tools/'remote_ssh_export.py'),temporary)
        self.sftp.chmod(temporary,0o600)
        try:
            self.sftp.posix_rename(temporary,self.remote+'/remote_ssh_export.py')
        except OSError:
            # Keep the original helper and all downloaded data intact.
            try:self.sftp.remove(temporary)
            except OSError:pass
            raise CollectionError('서버 검증 도구를 안전하게 갱신하지 못했습니다. 내려받은 파일은 보존했습니다. [SSH_HELPER_UPDATE_FAILED]') from None
        self.log('완료된 서버 작업의 검증 도구를 갱신했습니다. 기존 전송 파일을 재사용합니다.')

    @staticmethod
    def valid_remote(path):
        return (isinstance(path,str) and path.startswith('/') and posixpath.normpath(path)==path and
                '\0' not in path and re.fullmatch(r'\.segio-ssh-[0-9a-f]{32}',posixpath.basename(path)) is not None)

    def remove_job(self,remote,failed=False,previously_validated=False):
        """Only a private job owned by this capture; never follow child symlinks."""
        if not self.valid_remote(remote):
            raise CollectionError('서버 정리 대상 경로를 확인하세요.')
        try:
            info=self.sftp.lstat(remote)
        except OSError as error:
            if error.errno==2:return
            raise
        if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:
            raise CollectionError('서버 임시 작업 폴더의 권한을 확인하세요.')
        if not previously_validated:
            request=self.read_json(remote+'/request.json',65536)
            if request.get('protocol')!=PROTOCOL or request.get('user')!=self.config.username:
                raise CollectionError('서버 정리 대상 직원이 일치하지 않습니다.')
            marker=self.read_json(remote+('/status.json' if failed else '/verified.json'),65536)
            if marker.get('protocol')!=PROTOCOL or (marker.get('state')!='failed' if failed else marker.get('ok') is not True):
                raise CollectionError('서버 작업 검증 상태를 확인하세요.')
        def delete_tree(path):
            self.checkpoint()
            for entry in self.sftp.listdir_attr(path):
                if entry.filename in ('.','..') or '/' in entry.filename or '\\' in entry.filename:
                    raise CollectionError('서버 임시 파일 경로를 확인하세요.')
                child=path+'/'+entry.filename
                mode=self.sftp.lstat(child).st_mode
                if stat.S_ISDIR(mode):delete_tree(child)
                else:self.sftp.remove(child)
            self.sftp.rmdir(path)
        delete_tree(remote)
        try:self.sftp.lstat(remote)
        except OSError as error:
            if error.errno==2:return
            raise
        raise CollectionError('서버 임시 작업 삭제를 확인하지 못했습니다.')

    def cleanup_identity(self):
        return dict(host=self.config.host,port=self.config.port,ssh_user=self.config.ssh_user,user=self.config.username)

    def finish_cleanup(self,record,path):
        # The receipt is written only after the Windows folder has been published.
        expected_user=record['identity']['user']
        if not isinstance(expected_user,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}',expected_user):
            raise CollectionError('서버 정리 기록의 직원을 확인하세요.')
        folder=Path(record['backup'])
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        current_job=manifest.get('ssh_export',{}).get('job')
        if (manifest.get('user')!=expected_user or record['job'] not in [current_job]+manifest.get('retained_ssh_jobs',[])):
            raise CollectionError('완료된 Windows 백업의 서버 정리 기록을 확인하세요.')
        for remote in record['paths']:
            if remote not in record['validated']:
                # Validate before deletion and persist that proof for partial retries.
                failed=posixpath.basename(remote)!=record['job']
                if not self.valid_remote(remote):raise CollectionError('서버 작업 경로를 확인하세요.')
                try:info=self.sftp.lstat(remote)
                except OSError as error:
                    if error.errno==2:
                        record['validated'].append(remote)
                        continue
                    raise
                if not stat.S_ISDIR(info.st_mode) or info.st_mode&0o077:raise CollectionError('서버 작업 권한을 확인하세요.')
                request=self.read_json(remote+'/request.json',65536)
                marker=self.read_json(remote+('/status.json' if failed else '/verified.json'),65536)
                if request.get('protocol')!=PROTOCOL or request.get('user')!=expected_user or marker.get('protocol')!=PROTOCOL or (marker.get('state')!='failed' if failed else marker.get('ok') is not True):
                    raise CollectionError('서버 정리 검증 상태가 일치하지 않습니다.')
                record['validated'].append(remote)
                atomic_write(path,json.dumps(record).encode('utf-8'));os.chmod(path,0o600)
            if remote==self.remote:
                try:self.execute('cleanup')
                except Exception:pass
            self.remove_job(remote,previously_validated=True)
        if record['job']==current_job:manifest['server_cleanup']={'state':'confirmed','job':record['job']}
        else:manifest.setdefault('server_cleanup_history',{})[record['job']]='confirmed'
        atomic_write(folder/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
        path.unlink()

    def retry_cleanup(self,output):
        pending=output/'.segio-ssh-cache'
        if pending.is_symlink():raise CollectionError('서버 정리 기록 폴더를 확인하세요.')
        for path in pending.glob('cleanup-*.json'):
            if path.is_symlink():continue
            try:
                record=json.loads(path.read_text(encoding='utf-8'))
                identity=self.cleanup_identity()
                if any(record.get('identity',{}).get(key)!=identity[key] for key in ('host','port','ssh_user')):continue
                self.finish_cleanup(record,path)
                self.log('이전 백업의 서버 임시 작업을 정리하고 삭제를 확인했습니다.')
            except Exception:
                self.log('이전 백업의 서버 정리가 대기 중입니다. 완료된 Windows 백업은 유지합니다.')

    def wait_ready(self):
        previous, deadline, reported = None, time.monotonic()+72*3600, 0.0
        while time.monotonic()<deadline:
            self.checkpoint()
            status = self.read_json(self.remote+'/status.json',65536)
            if status.get('protocol') != PROTOCOL:
                raise CollectionError('SSH 작업기 형식이 일치하지 않습니다.')
            stage = status.get('stage')
            detail={'initializing':'서버 작업 준비 중','database':'결재 DB 읽기 전용 추출 중',
                    'mail':'서버 메일 원본 검사 중'}.get(stage,'서버 추출 준비 확인 중')
            if stage=='mail' and status.get('total_bytes') is not None:
                self.report('server',status.get('completed_bytes',0),status['total_bytes'],'bytes',
                    detail+f' · {status.get("files_done",0):,}/{status.get("files_total",0):,}개 · 시도 {status.get("attempt",1)}/3')
            else:
                self.report('server',detail=detail)
            if stage != previous:
                self.log({'initializing':'SSH 서버 작업 준비 중','database':'직원의 결재 DB를 읽기 전용으로 추출하는 중',
                          'mail':'MBOX 원본 목록과 SHA-256을 확인하는 중 · 서버에 메일 복사본을 만들지 않습니다'}.get(stage,'SSH 추출 준비 확인 중'))
                previous = stage
            if stage=='mail' and status.get('total_bytes') is not None and time.monotonic()-reported>=2:
                done,total=status.get('completed_bytes',0),status['total_bytes']
                self.log(f'서버 메일 검사 {done/1024**3:.2f} / {total/1024**3:.2f} GiB · '
                         f'{status.get("files_done",0):,} / {status.get("files_total",0):,}개 · 시도 {status.get("attempt",1)} / 3')
                reported=time.monotonic()
            if status.get('state') == 'failed':
                raise CollectionError(ssh_failure_message(status.get('code'),status.get('diagnostic'),self.phase))
            if status.get('state') == 'ready':
                self.report('server',1,1,'steps','서버 추출 준비 완료',force=True)
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
            offset=item.get('source_offset',0)
            source_bytes=item.get('source_bytes',item['bytes'])
            if (type(offset) is not int or type(source_bytes) is not int or offset<0 or
                    source_bytes<0 or offset+item['bytes']>source_bytes):
                raise CollectionError('SSH 메일 원본 범위가 올바르지 않습니다.')
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
        self.report('transfer',0,total,'bytes',force=True)
        for item,source,target in records:
            self.checkpoint()
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            size = target.stat().st_size if target.exists() else 0
            if target.is_file() and size==item['bytes']:
                self.report('transfer',done,total,'bytes','이미 받은 파일 SHA-256 검사 중',force=size>=8*1024**2)
                if digest_file(target,self.checkpoint)=={'bytes':item['bytes'],'sha256':item['sha256']}:
                    done += size
                    self.report('transfer',done,total,'bytes','검증된 파일 이어받기',force=done==total)
                    continue
            if target.exists() and size>=item['bytes']:
                target.unlink()
                size=0
            attributes = self.sftp.lstat(source)
            offset=item.get('source_offset',0)
            if not stat.S_ISREG(attributes.st_mode) or attributes.st_size != item.get('source_bytes',item['bytes']):
                raise CollectionError('서버 원본이 변경됐습니다. 이어받기를 해제해 새로 추출하세요. [SSH_SOURCE_CHANGED]')
            with self.sftp.open(source,'rb') as incoming, target.open('ab') as outgoing:
                incoming.seek(offset+size)
                done += size
                while size<item['bytes']:
                    self.checkpoint()
                    # Bound both the read queue and memory. Prefetching an
                    # entire 40GB file would build a huge request list.
                    end=min(size+1024*1024,item['bytes'])
                    chunks=[(offset+position,min(32768,end-position)) for position in range(size,end,32768)]
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
                    self.report('transfer',done,total,'bytes','SFTP 전송 중')
            self.report('transfer',done,total,'bytes','수신 파일 SHA-256 검사 중',force=item['bytes']>=8*1024**2)
            if digest_file(target,self.checkpoint) != {'bytes':item['bytes'],'sha256':item['sha256']}:
                raise CollectionError('내려받은 파일의 SHA-256이 일치하지 않습니다. 완료로 처리하지 않았습니다. [SSH_FILE_HASH_MISMATCH]')
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        snapshot=manifest.get('ssh_export',{})
        if snapshot.get('since')!=getattr(self,'since',None):
            raise CollectionError('서버의 추가 백업 날짜가 요청과 다릅니다. 최신 tools를 사용하세요. [SSH_INCREMENTAL_SCOPE_MISMATCH]')
        by_name={item['path']:item for item,_,_ in records}
        if (manifest.get('user')!=self.config.username or manifest.get('account_deletion_verified') is not False or
                snapshot.get('job')!=self.remote.rsplit('/',1)[1] or snapshot.get('approvals')!=self.config.approvals or
                snapshot.get('mail')!=self.config.mail or set(by_name)!=set(manifest['files'])|{'manifest.json'}):
            raise CollectionError('SSH 보관본과 전송 목록이 일치하지 않습니다.')
        for name, details in manifest['files'].items():
            if any(details[key]!=by_name[name][key] for key in ('bytes','sha256')):
                raise CollectionError('SSH 보관 파일 검증 목록이 일치하지 않습니다.')
        self.report('transfer',done,total,'bytes','SFTP 전송 및 파일 검증 완료',force=True)

    def run(self,password):
        self.config.validate()
        try:
            self.checkpoint()
            self.begin_stage('connect')
            self.connect(password)
            password=''
            output=self.config.output.expanduser().resolve()
            from .backup_update import selection_output
            cleanup_output=selection_output(output) if (output/'manifest.json').is_file() else output
            self.retry_cleanup(cleanup_output)
            identity={key:getattr(self.config,key) for key in ('host','port','ssh_user','username','db_config','cell','mail_template','approvals','mail')}
            from .backup_update import workspace,load,save,merge,publish,named_folder
            output,cache,contents,base,plan=workspace(output,self.config.username,identity,self.config.resume)
            self.since=plan.get('since')
            origin={key:value for key,value in identity.items() if key not in ('approvals','mail')}
            if base and load(base/'manifest.json').get('backup_origin',origin)!=origin:
                raise CollectionError('기존 백업의 SSH 서버 정보가 다릅니다. [BACKUP_ORIGIN_MISMATCH]')
            self.log('Windows 백업 폴더: '+str(named_folder(output,self.config.username)))
            if plan.get('published'):
                state=load(cache/'session.json');self.remote=state['remote'];self.retired=state.get('retired',[])
                if not self.valid_remote(self.remote):raise CollectionError('완료 작업의 정리 경로를 확인하세요.')
                result=contents
            else:
                if self.since is not None:self.log('추가 백업: 마지막 완료일의 15일 전부터 결재·메일을 가져와 기존 자료와 병합합니다.')
                self.begin_stage('prepare')
                self.initialize(cache)
                self.begin_stage('server')
                self.wait_ready()
                self.begin_stage('transfer','전송 목록 확인 중')
                self.transfer(contents)
                self.begin_stage('verify')
                self.execute('verify')
                local_manifest=load(contents/'manifest.json')
                local_manifest['collector_version']=__version__
                local_manifest['backup_origin']=origin
                save(contents/'manifest.json',local_manifest)
                self.phase='Windows 검색 색인 생성'
                self.log('메일 검색 DB와 바로보기 HTML·첨부를 만드는 중 · 완료한 페이지는 재사용합니다')
                if base:
                    merge(base,contents,cache/'merged',self.checkpoint,self.log,self.forward_progress)
                    contents=cache/'merged'
                else:
                    build_compact_catalog(contents,self.config.username,self.checkpoint,self.log,progress=self.forward_progress)
                local_manifest=load(contents/'manifest.json');local_manifest['backup_origin']=origin
                local_manifest['last_backup_completed_at']=utc_now();save(contents/'manifest.json',local_manifest)
            archive=cache/'archive.zip' if plan.get('published') and self.config.make_zip and (cache/'archive.zip').is_file() else None
            if self.config.make_zip and not plan.get('published'):
                self.begin_stage('zip')
                archive=cache/'archive.zip'
                size=sum(p.stat().st_size for p in contents.rglob('*') if p.is_file())
                if size+128*1024*1024>shutil.disk_usage(cache).free:
                    raise CollectionError('추가 ZIP을 생성할 디스크 공간이 부족합니다. ZIP 선택을 해제하고 이어받으세요.')
                zipped_bytes=0
                self.report('zip',0,size,'bytes','원본 바이트 기준',force=True)
                with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,allowZip64=True) as zipped:
                    for path in contents.rglob('*'):
                        if path.is_file():
                            with path.open('rb') as source,zipped.open(path.relative_to(contents).as_posix(),'w',force_zip64=True) as target:
                                while block:=source.read(1024*1024):
                                    self.checkpoint()
                                    target.write(block)
                                    zipped_bytes+=len(block)
                                    self.report('zip',zipped_bytes,size,'bytes','원본 바이트 기준')
                self.report('zip',zipped_bytes,size,'bytes','ZIP 생성 완료',force=True)
            self.checkpoint()
            self.begin_stage('publish')
            if not plan.get('published'):
                result=publish(contents,named_folder(output,self.config.username),cache,base,plan.get('revision'))
            if archive is not None:os.replace(archive,output/(result.name+'.zip'))
            self.begin_stage('cleanup')
            record={'identity':self.cleanup_identity(),'backup':str(result),
                    'job':posixpath.basename(self.remote),'paths':list(dict.fromkeys([self.remote]+self.retired)),
                    'validated':[]}
            pending=cache.parent/('cleanup-'+posixpath.basename(self.remote)+'.json')
            atomic_write(pending,json.dumps(record).encode('utf-8'));os.chmod(pending,0o600)
            try:
                self.finish_cleanup(record,pending)
                self.log('서버 임시 작업 폴더 삭제 확인 완료 · 원본 DB·메일·첨부는 보존했습니다.')
            except Exception:
                manifest=json.loads((result/'manifest.json').read_text(encoding='utf-8'))
                manifest['server_cleanup']={'state':'pending','job':record['job']}
                atomic_write(result/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
                self.log('Windows 백업은 완료됐지만 서버 정리는 대기 중입니다. 다음 SSH 백업 접속 시 자동 재시도합니다.')
            final_manifest=load(result/'manifest.json');final_manifest['last_backup_completed_at']=utc_now()
            for kind in ('approvals','mail'):
                if getattr(self.config,kind):final_manifest.setdefault('backup_completed_by_kind',{})[kind]=final_manifest['last_backup_completed_at']
            save(result/'manifest.json',final_manifest)
            shutil.rmtree(cache)
            detail='서버 임시 파일 정리 대기 · Windows 백업 저장 완료' if pending.exists() else '서버 임시 파일 삭제 확인 · 백업 저장 완료'
            self.report('cleanup',1,1,'steps',detail,force=True)
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
            self.close_connections()
