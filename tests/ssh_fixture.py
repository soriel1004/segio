"""Real loopback Paramiko SSH/SFTP; all server files stay inside a temp root."""
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import threading
import time

import paramiko
from paramiko import SFTPAttributes,SFTPHandle,SFTPServerInterface,SFTP_OK


class FileHandle(SFTPHandle):
    def stat(self):
        return SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class Files(SFTPServerInterface):
    def __init__(self,server,owner):
        super().__init__(server)
        self.owner=owner

    def path(self,path):
        return self.owner.path(path)

    def stat(self,path):
        try:
            return SFTPAttributes.from_stat(os.stat(self.path(path)))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def lstat(self,path):
        try:
            attributes=SFTPAttributes.from_stat(os.lstat(self.path(path)))
            if os.fsdecode(path).startswith('/tmp/.segio-ssh-') and stat.S_ISDIR(attributes.st_mode):
                attributes.st_mode=stat.S_IFDIR|0o700
            return attributes
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self,path,flags,attr):
        try:
            actual=self.path(path)
            if actual.name=='transfer.json' and flags==os.O_RDONLY:
                data=json.loads(actual.read_text(encoding='utf-8'))
                for item in data['files']:
                    raw=bytes.fromhex(item['source_hex'])
                    if not raw.startswith(b'/files/'):
                        relative=os.path.relpath(raw,os.fsencode(self.owner.root)).replace(os.sep.encode(),b'/')
                        item['source_hex']=(b'/files/'+relative).hex()
                actual.write_text(json.dumps(data),encoding='utf-8')
            descriptor=os.open(actual,flags,getattr(attr,'st_mode',None) or 0o600)
            mode='r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
            stream=os.fdopen(descriptor,mode)
            handle=FileHandle(flags)
            handle.readfile=handle.writefile=stream
            original=handle.read
            def read(offset,length):
                if '/home/' in str(actual):
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
                    threading.Thread(target=helper.prepare,args=(str(root),),daemon=True).start()
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
        threading.Thread(target=execute,daemon=True).start()
        return True


class SSHServer:
    def __init__(self,root,key=None):
        self.root=Path(root).resolve()
        (self.root/'jobs').mkdir(exist_ok=True)
        self.key=key or paramiko.RSAKey.generate(2048)
        self.reads,self.actions,self.transports=[],[],[]
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
        if value.startswith('/tmp/.segio-ssh-'):
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
