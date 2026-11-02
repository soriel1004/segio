"""Real loopback Paramiko SSH/SFTP; all server files stay inside a temp root."""
import json
import io
import os
from pathlib import Path
import shlex
import socket
import stat
import threading
import time
import types

import paramiko
from paramiko import SFTPAttributes,SFTPHandle,SFTPServerInterface,SFTP_OK


def metadata_reader(path):
    """Emulate POSIX open-inode reads across atomic status-file replacement."""
    if os.name!='nt':
        return path.open('rb')
    import ctypes,msvcrt
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                     wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    # CPython's usual Windows open omits FILE_SHARE_DELETE. A real CentOS
    # sshd keeps the old inode readable while the worker renames its update.
    handle=create(str(path),0x80000000,0x7,None,3,0x80,None)
    if handle==wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    try:return os.fdopen(descriptor,'rb')
    except BaseException:
        os.close(descriptor)
        raise


class FileHandle(SFTPHandle):
    def stat(self):
        if hasattr(self,'snapshot_stat'):
            return self.snapshot_stat
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class Files(SFTPServerInterface):
    def __init__(self,server,owner):
        super().__init__(server)
        self.owner=owner

    def path(self,path):
        return self.owner.path(path)

    def stat(self,path):
        try:
            with self.owner.metadata_lock:
                return SFTPAttributes.from_stat(os.stat(self.path(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self,path):
        try:
            value=os.fsdecode(path)
            actual=(self.path(value) if value.startswith(('/tmp/.segio-ssh-','/backup/.segio-ssh-')) and '/' not in value.rsplit('.segio-ssh-',1)[1] else self.owner.path(os.path.dirname(value))/os.path.basename(value))
            attributes=SFTPAttributes.from_stat(os.lstat(actual))
            if os.fsdecode(path).startswith(('/tmp/.segio-ssh-','/backup/.segio-ssh-')) and stat.S_ISDIR(attributes.st_mode):
                attributes.st_mode=stat.S_IFDIR|0o700
            return attributes
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self,path,flags,attr):
        try:
            actual=self.path(path)
            snapshot_stat=None
            if actual.name=='transfer.json' and flags==os.O_RDONLY:
                data=json.loads(actual.read_text(encoding='utf-8'))
                for item in data['files']:
                    raw=bytes.fromhex(item['source_hex'])
                    if not raw.startswith(b'/files/'):
                        relative=os.path.relpath(raw,os.fsencode(self.owner.root)).replace(os.sep.encode(),b'/')
                        item['source_hex']=(b'/files/'+relative).hex()
                actual.write_text(json.dumps(data),encoding='utf-8')
            if flags==os.O_RDONLY and actual.name in ('status.json','request.json','verified.json','mail-check.json','approval-check.json','transfer.json'):
                # Keep a snapshot of the opened inode. Windows stat/read opens
                # can briefly deny replacement even with FILE_SHARE_DELETE;
                # serialize those fixture-only opens with the worker rename.
                with self.owner.metadata_lock,metadata_reader(actual) as source:
                    snapshot_stat=SFTPAttributes.from_stat(os.fstat(source.fileno()))
                    stream=io.BytesIO(source.read())
            else:
                descriptor=os.open(actual,flags,getattr(attr,'st_mode',None) or 0o600)
                mode='r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
                stream=os.fdopen(descriptor,mode)
            handle=FileHandle(flags)
            if snapshot_stat is not None:
                handle.snapshot_stat=snapshot_stat
            handle.readfile=handle.writefile=stream
            original=handle.read
            def read(offset,length):
                if '/home/' in actual.as_posix():
                    self.owner.reads.append((str(actual),offset,length))
                return original(offset,length)
            handle.read=read
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def mkdir(self,path,attr):
        try:
            os.mkdir(self.path(path),getattr(attr,'st_mode',None) or 0o700)
            return SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def list_folder(self,path):
        try:
            entries=[]
            for child in self.path(path).iterdir():
                item=SFTPAttributes.from_stat(os.lstat(child));item.filename=child.name;entries.append(item)
            return entries
        except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self,path):
        try:
            value=os.fsdecode(path);os.unlink(self.owner.path(os.path.dirname(value))/os.path.basename(value));return SFTP_OK
        except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)

    def posix_rename(self,oldpath,newpath):
        try:os.replace(self.path(oldpath),self.path(newpath));return SFTP_OK
        except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)

    def rmdir(self,path):
        try:os.rmdir(self.path(path));return SFTP_OK
        except OSError as exc:return paramiko.SFTPServer.convert_errno(exc.errno)

    def chattr(self,path,attr):
        try:
            paramiko.SFTPServer.set_file_attr(str(self.path(path)),attr)
            return SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class BytePathsServer(paramiko.SFTPServer):
    def _process(self,kind,number,message):
        # OpenSSH accepts native file-name bytes. Preserve CP949 names on the
        # Python test server too, rather than forcing UTF-8 decoding.
        from paramiko.sftp import CMD_OPEN,CMD_STAT,CMD_LSTAT
        if kind in (CMD_OPEN,CMD_STAT,CMD_LSTAT):
            message.get_text=lambda:os.fsdecode(message.get_binary())
        return super()._process(kind,number,message)


class Access(paramiko.ServerInterface):
    def __init__(self,owner):
        self.owner=owner

    def check_auth_password(self,username,password):
        return paramiko.AUTH_SUCCESSFUL if (username,password)==('root','SYNTHETIC_SSH_PASSWORD') else paramiko.AUTH_FAILED

    def get_allowed_auths(self,username):
        return 'password'

    def check_channel_request(self,kind,channel_id):
        return paramiko.OPEN_SUCCEEDED if kind=='session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self,channel,command):
        def execute():
            import remote_ssh_export as helper
            try:
                args=shlex.split(command.decode())
                from segio_archive.ssh_client import WORK_SCRIPT,WORK_PROTOCOL
                if len(args)==4 and args[1]=='-c' and args[2]==WORK_SCRIPT:
                    self.owner.workspace_requests.append(args[3])
                    channel.sendall(json.dumps({'protocol':WORK_PROTOCOL,'ok':True,'base':args[3],'free':100*1024**3}).encode())
                    channel.send_exit_status(0)
                    return
                if len(args)==8 and args[1]=='-c':
                    import subprocess,sys
                    from segio_archive.ssh_client import CHECK_SCRIPT
                    assert args[2]==CHECK_SCRIPT
                    paths=[str(self.owner.root/'jobs') if value=='/tmp' else
                           str(self.owner.path(value)) if value else '' for value in args[3:]]
                    payload=subprocess.check_output([sys.executable,'-c',CHECK_SCRIPT]+paths)
                    self.owner.probes+=1
                    channel.sendall(payload)
                    channel.send_exit_status(0)
                    return
                assert args[2]=='--root' and args[4]=='--action'
                root=self.owner.path(args[3])
                action=args[5]
                self.owner.actions.append(action)
                if action=='prepare':
                    request_path=root/'request.json'
                    request=json.loads(request_path.read_text(encoding='utf-8'))
                    for field in ('db_config','cell','mail_template'):
                        request[field]=self.owner.path(request[field]).as_posix()
                    request_path.write_text(json.dumps(request),encoding='utf-8')
                    prepare=helper.prepare
                    if os.name=='nt':
                        namespace=prepare.__globals__.copy()
                        write_json=namespace['write_json']
                        def serialized_write(path,value):
                            with self.owner.metadata_lock:
                                return write_json(path,value)
                        namespace['write_json']=serialized_write
                        prepare=types.FunctionType(prepare.__code__,namespace)
                    threading.Thread(target=prepare,args=(str(root),),daemon=True).start()
                    channel.send_exit_status(0)
                elif action=='verify':
                    helper.verify_sources(str(root))
                    channel.send(json.dumps({'protocol':helper.PROTOCOL,'ok':True}).encode())
                    channel.send_exit_status(0)
                elif action=='cleanup':
                    import shutil
                    helper.private_root(str(root))
                    assert (root/'verified.json').exists()
                    shutil.rmtree(root)
                    channel.send(json.dumps({'protocol':helper.PROTOCOL,'ok':True}).encode())
                    channel.send_exit_status(0)
                else:
                    raise AssertionError('unexpected helper action')
            except Exception as exc:
                code,_=helper.failure_details(exc,'initializing')
                channel.send(json.dumps({'protocol':'segio-ssh-archive/1','error':code}).encode())
                channel.send_exit_status(1)
            finally:
                channel.close()
        # Real sshd acknowledges exec before an immediate command can exit.
        # Let Paramiko send CHANNEL_SUCCESS before dispatching this fixture's
        # in-process workspace probe, otherwise the client sees Channel closed.
        timer=threading.Timer(.03,execute)
        timer.daemon=True
        timer.start()
        return True


class SSHServer:
    def __init__(self,root,key=None):
        self.root=Path(root).resolve()
        self.metadata_lock=threading.Lock()
        (self.root/'jobs').mkdir(exist_ok=True)
        self.key=key or paramiko.RSAKey.generate(2048)
        self.reads,self.actions,self.transports=[],[],[]
        self.probes=0
        self.workspace_requests=[]
        self.listener=socket.socket()
        self.listener.bind(('127.0.0.1',0))
        self.port=self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(.2)
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.accept,daemon=True)
        self.thread.start()

    def path(self,path):
        value=os.fsdecode(path)
        if value=='/backup' or value.startswith('/backup/.segio-ssh-'):
            value=str(self.root/'jobs'/value[len('/backup/'):]) if value!='/backup' else str(self.root/'jobs')
        elif value.startswith('/tmp/.segio-ssh-'):
            value=str(self.root/'jobs'/value[len('/tmp/'):])
        elif value=='/files' or value.startswith('/files/'):
            value=str(self.root/value[len('/files/'):]) if value!='/files' else str(self.root)
        actual=Path(value).resolve()
        if not actual.is_relative_to(self.root):
            raise PermissionError('outside synthetic SSH root')
        return actual

    def accept(self):
        while not self.stop.is_set():
            try:
                connection,_=self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            def serve(connection):
                transport=paramiko.Transport(connection)
                self.transports.append(transport)
                transport.add_server_key(self.key)
                # Exercise the algorithms available on CentOS 6/OpenSSH 5.3.
                security=transport.get_security_options()
                security.key_types=('ssh-rsa',)
                security.kex=('diffie-hellman-group14-sha1',)
                security.ciphers=('aes128-ctr',)
                security.digests=('hmac-sha1',)
                transport.set_subsystem_handler('sftp',BytePathsServer,Files,self)
                try:
                    transport.start_server(server=Access(self))
                    while transport.is_active() and not self.stop.wait(.1):
                        pass
                finally:
                    transport.close()
            threading.Thread(target=serve,args=(connection,),daemon=True).start()

    def close(self):
        self.stop.set()
        self.listener.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(5)
