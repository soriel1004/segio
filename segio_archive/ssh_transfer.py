"""Bounded SSH transfer recovery and diagnostics without private record data."""
import errno
import json
from pathlib import Path
import socket

import paramiko

from . import __version__
from .archive import atomic_write
from .web import CollectionError


class TransferFault(Exception):
    def __init__(self,operation,error):
        self.operation,self.error=operation,error


def fault_details(fault):
    error=fault.error;remote=fault.operation.startswith('remote_')
    number=getattr(error,'errno',None);winerror=getattr(error,'winerror',None)
    number=number if type(number) is int and 0<=number<=65535 else None
    winerror=winerror if type(winerror) is int and 0<=winerror<=65535 else None
    retry=False
    if not remote:
        if number in (errno.ENOSPC,getattr(errno,'EDQUOT',122)) or winerror in (39,112):
            code,message='WINDOWS_DISK_FULL','Windows 저장 공간 또는 사용자 할당량이 부족합니다.'
        elif winerror in (225,226):
            code,message='WINDOWS_SECURITY_BLOCKED','Windows 보안 프로그램이 백업 파일을 차단했습니다. 차단 내역을 확인하세요.'
        elif winerror in (32,33):
            code,message='WINDOWS_FILE_LOCKED','다른 프로그램이 백업 파일을 사용 중입니다. 해당 파일을 닫고 다시 이어받으세요.'
        elif number==errno.EFBIG:
            code,message='WINDOWS_FILE_SIZE_LIMIT','Windows 저장 드라이브의 단일 파일 크기 제한에 도달했습니다.'
        elif number==errno.ENAMETOOLONG or winerror==206:
            code,message='WINDOWS_PATH_TOO_LONG','Windows 저장 경로가 너무 깁니다. 더 짧은 저장 상위 폴더를 사용하세요.'
        elif number in (errno.EACCES,errno.EPERM) or winerror==5:
            code,message='WINDOWS_ACCESS_DENIED','Windows 백업 파일의 저장 또는 읽기 권한이 거부됐습니다.'
        elif number==errno.ENOENT or winerror in (2,3):
            code,message='WINDOWS_FILE_MISSING','Windows 백업 파일 또는 폴더를 찾지 못했습니다. 삭제·격리 내역을 확인하세요.'
        else:
            code,message='WINDOWS_IO_ERROR','Windows 백업 파일 처리에 실패했습니다. 저장 드라이브와 파일 사용 상태를 확인하세요.'
    elif number==errno.ENOENT:
        code,message='SSH_SOURCE_MISSING','서버의 추출 파일 또는 원본을 찾지 못했습니다. 이미 받은 파일은 보존했습니다.'
    elif number in (errno.EACCES,errno.EPERM):
        code,message='SSH_SOURCE_ACCESS_DENIED','서버 파일 읽기 권한이 거부됐습니다.'
    else:
        code,message='SSH_TRANSFER_INTERRUPTED','서버 읽기 또는 전송 연결이 중단됐습니다.'
        retry=isinstance(error,(paramiko.SSHException,EOFError,socket.timeout)) or number in (
            None,errno.ECONNRESET,errno.ECONNABORTED,errno.ENOTCONN,errno.ETIMEDOUT,errno.EPIPE,errno.EBADF,10053,10054,10060)
    names={'OSError','IOError','PermissionError','FileNotFoundError','TimeoutError','SSHException','EOFError',
           'ConnectionResetError','ConnectionAbortedError','BrokenPipeError','NoValidConnectionsError','timeout'}
    return code,message,retry,{'operation':fault.operation,'exception_type':type(error).__name__ if type(error).__name__ in names else 'unknown',
                               'errno':number,'winerror':winerror}


class TransferRecovery:
    def io(self,operation,callback,*args,**kwargs):
        self.transfer_context['operation']=operation
        try:return callback(*args,**kwargs)
        except (OSError,paramiko.SSHException,EOFError) as error:
            raise TransferFault(operation,error) from None

    def save_transfer_failure(self,code,details,attempt):
        value={'protocol':'segio-ssh-transfer-failure/1','collector_version':__version__,'code':code,
               'retry':attempt,'reader':'sequential' if self.sequential_reads else 'bounded-prefetch'}
        for key in ('file_number','files_total','completed_bytes','total_bytes','file_bytes','file_completed_bytes',
                    'resumed_bytes','hash_mismatches'):
            number=self.transfer_context.get(key)
            if type(number) is int and number>=0:value[key]=number
        value.update(details)
        path=getattr(self,'transfer_failure_path',None)
        if path:
            try:
                atomic_write(Path(path),json.dumps(value,ensure_ascii=False,indent=2).encode())
                self.log('전송 진단 저장: '+str(path))
            except OSError:pass  # A full local disk must not hide the original failure.

    def repair_hash_mismatch(self,item,target):
        """Reset only an unverified staging file; never accept a changed source."""
        count=self.hash_failures.get(item['path'],0)+1
        self.transfer_context['hash_mismatches']=count
        self.save_transfer_failure('SSH_FILE_HASH_MISMATCH',{
            'operation':'local_hash','exception_type':'SHA256Mismatch','errno':None,'winerror':None},self.connection_retries)
        number=self.transfer_context['file_number'];total=self.transfer_context['files_total']
        if count>2:
            self.hash_failures[item['path']]=count
            raise CollectionError('같은 파일을 다시 받았지만 SHA-256이 계속 일치하지 않습니다. 완료로 처리하지 않았습니다. '
                '[SSH_FILE_HASH_MISMATCH]\n진단: 파일 %s/%s · 검증 불일치 %s회\n'
                '받은 파일과 캐시는 보존했습니다. 로그의 전송 진단 저장 경로에서 transfer-failure.json을 확인하세요.' %
                (number,total,count))
        # This check precedes any deletion of the cached copy. Source changes
        # require a new snapshot, rather than repeatedly downloading new bytes.
        self.check_copy_sources()
        self.checkpoint()
        self.sequential_reads=True
        self.log('파일 %s/%s SHA-256 불일치 · 해당 파일만 처음부터 순차 재수신 %s/2' % (number,total,count))
        self.report('transfer',detail='SHA-256 불일치 파일 재수신 %s/2 · 다른 파일 보존' % count,force=True)
        self.io('local_remove',target.unlink)
        # A failed verification/reconnect did not start a clean download, so
        # it must not consume a file-repair attempt.
        self.hash_failures[item['path']]=count

    def transfer(self,folder):
        self.transfer_context={};self.sequential_reads=False;self.hash_failures={};self.connection_retries=0
        retries=0
        while True:
            try:
                if retries:
                    self.close_connections()
                    self.report('transfer',detail=f'전송 재연결 {retries}/3 · 받은 파일 보존 · 순차 읽기',force=True)
                    self.config.cancel.wait(min(2**retries,8));self.checkpoint()
                    self.io('remote_connect',self.reconnect_transfer)
                    status=self.io('remote_ready',self.read_json,self.remote+'/status.json',65536)
                    if status.get('protocol')!='segio-ssh-archive/1' or status.get('state')!='ready':
                        raise CollectionError('같은 서버 추출 작업이 준비 상태가 아닙니다. 받은 파일과 캐시를 보존하세요. [SSH_SNAPSHOT_NOT_READY]')
                    self.check_copy_sources()
                self.transfer_once(folder)
                path=getattr(self,'transfer_failure_path',None)
                if path:
                    try:Path(path).unlink(missing_ok=True)
                    except OSError:pass
                return
            except TransferFault as fault:
                if isinstance(fault.error,(paramiko.BadHostKeyException,paramiko.AuthenticationException)):
                    raise fault.error
                code,message,retry,details=fault_details(fault)
                self.save_transfer_failure(code,details,retries)
                if retry and retries<3 and self.reconnect_transfer is not None:
                    # Release the old transfer frame/metadata before loading it again.
                    fault.error.__traceback__=None
                    fault.__traceback__=None
                    retries+=1;self.sequential_reads=True
                    self.connection_retries=retries
                    self.log(f'전송 읽기·연결 오류 · 자동 재연결 {retries}/3 · 받은 위치부터 재개 · 순차 읽기')
                    continue
                labels={'remote_read':'서버 데이터 읽기','remote_stat':'서버 파일 확인','remote_open':'서버 파일 열기',
                        'remote_close':'서버 파일 닫기','remote_metadata':'서버 전송 목록 읽기','remote_connect':'SSH 재연결',
                        'remote_ready':'서버 작업 확인','remote_verify':'서버 원본 변경 확인','local_write':'Windows 파일 저장',
                        'local_open':'Windows 파일 열기','local_flush':'Windows 저장 마무리','local_close':'Windows 파일 닫기',
                        'local_hash':'Windows 파일 해시 검사','local_stat':'Windows 파일 확인','local_mkdir':'Windows 폴더 생성',
                        'local_remove':'잘못된 부분 파일 정리','local_space':'Windows 저장 공간 확인','local_manifest':'Windows 백업 목록 읽기'}
                detail=[labels.get(details['operation'],'전송 처리')]
                for key in ('errno','winerror'):
                    if details[key] is not None:detail.append(key+'='+str(details[key]))
                number=self.transfer_context.get('file_number');count=self.transfer_context.get('files_total')
                if number and count:detail.append(f'파일 {number:,}/{count:,}')
                raise CollectionError(message+' ['+code+']\n진단: '+' · '.join(detail)+
                    '\n받은 파일과 캐시는 보존했습니다. 같은 ID·저장 상위 폴더에서 ‘이전 전송 이어받기’를 체크해 다시 시작하세요.') from None
