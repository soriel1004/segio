"""Employee SEGIO login and verified download of an administrator-prepared ZIP.

The server bridge chooses the archive from native SEGIO identity. This client
never submits a target ID, DB credentials or commands to that bridge.
"""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urljoin

from .browser import BrowserUnavailable, launch_browser
from .archive import safe_name
from .web import Collector, CollectionError, Config

BRIDGE = "/segio/archive/bridge.php"
PROTOCOL = "segio-login-archive/1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_opener(cookies):
    jar = http.cookiejar.CookieJar()
    for item in cookies:
        domain = item['domain']
        jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=item['name'], value=item['value'], port=None, port_specified=False,
            domain=domain, domain_specified=domain.startswith('.'),
            domain_initial_dot=domain.startswith('.'), path=item['path'], path_specified=True,
            secure=item['secure'], expires=int(item['expires']) if item['expires']>0 else None,
            discard=item['expires']<=0, comment=None, comment_url=None,
            rest={'HttpOnly': item.get('httpOnly', False)}, rfc2109=False))
    # Default HTTPS certificate validation is deliberately retained.
    return urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))


def safe_member(name):
    if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
        raise CollectionError("ZIP에 안전하지 않은 파일 경로가 있습니다.")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name:
        raise CollectionError("ZIP에 안전하지 않은 파일 경로가 있습니다.")
    for part in path.parts:
        stem = part.split('.')[0].upper()
        if (part in {'.', '..'} or part.endswith((' ', '.')) or
                re.search(r'[<>:"|?*\x00-\x1f]', part) or
                stem in {'CON', 'PRN', 'AUX', 'NUL'} or re.fullmatch(r'(COM|LPT)[1-9]', stem)):
            raise CollectionError("ZIP에 Windows에서 사용할 수 없는 파일 경로가 있습니다.")
    return path


def unpack_verified(source: Path, destination: Path, username: str, checkpoint=lambda: None):
    """Extract only manifest-listed regular files into a new private staging dir."""
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members)>200000:
            raise CollectionError("보관 ZIP의 파일 수가 제한을 초과합니다.")
        names = set()
        by_name = {}
        for member in members:
            path = safe_member(member.filename)
            folded = str(path).casefold()
            mode = member.external_attr >> 16
            if folded in names or member.is_dir() or (mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                raise CollectionError("ZIP에 중복 경로나 지원하지 않는 파일 형식이 있습니다.")
            names.add(folded)
            by_name[member.filename] = member
        if 'manifest.json' not in by_name or by_name['manifest.json'].file_size>16*1024*1024:
            raise CollectionError("정상적인 DB 보관함 manifest.json이 없습니다.")
        manifest_bytes = archive.read('manifest.json')
        manifest = json.loads(manifest_bytes)
        if (manifest.get('format')!='segio-db-html/1' or manifest.get('user')!=username or
                manifest.get('account_deletion_verified') is not False or
                manifest.get('state') not in {'collected', 'collected_with_warnings'}):
            raise CollectionError("보관본의 사용자 또는 보관 형식이 일치하지 않습니다.")
        files = manifest.get('files')
        if not isinstance(files, dict) or set(by_name)!=set(files)|{'manifest.json'} or 'index.html' not in files:
            raise CollectionError("ZIP 파일 목록과 검증 목록이 일치하지 않습니다.")
        total = sum(m.file_size for m in members)
        if total + 64*1024*1024 > shutil.disk_usage(destination).free:
            raise CollectionError("ZIP 압축을 풀 저장 공간이 부족합니다.")
        for name, expected in files.items():
            checkpoint()
            path = safe_member(name)
            if (not isinstance(expected, dict) or type(expected.get('bytes')) is not int or
                    by_name[name].file_size!=expected['bytes'] or expected['bytes']<0 or
                    not re.fullmatch(r'[a-f0-9]{64}', str(expected.get('sha256', '')))):
                raise CollectionError("파일 검증 정보가 올바르지 않습니다.")
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256()
            count = 0
            with archive.open(by_name[name]) as incoming, target.open('xb') as outgoing:
                while block := incoming.read(1024*1024):
                    checkpoint()
                    count += len(block)
                    outgoing.write(block)
                    digest.update(block)
            if count!=expected['bytes'] or digest.hexdigest()!=expected['sha256']:
                raise CollectionError("내려받은 보관 파일의 SHA-256 검증에 실패했습니다.")
        (destination / 'manifest.json').write_bytes(manifest_bytes)
    return manifest


def publish_local(staging, archive, output, username):
    """Reserve a short name without replacing either an existing ZIP or folder."""
    for number in range(1, 100000):
        name = 'segio-' + safe_name(username, 100) + (f'-{number}' if number>1 else '')
        folder, zipped = output/name, output/(name+'.zip')
        if folder.exists():
            continue
        try:
            with zipped.open('xb'):
                pass
        except FileExistsError:
            continue
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            zipped.unlink()
            continue
        try:
            # Windows cannot replace an existing directory using MoveFileEx.
            # Move verified children into our exclusively reserved directory.
            for child in staging.iterdir():
                os.replace(child, folder/child.name)
            staging.rmdir()
            os.replace(archive, zipped)
            return folder
        except BaseException:
            shutil.rmtree(folder)
            zipped.unlink(missing_ok=True)
            raise
    raise CollectionError("같은 이름의 보관함이 너무 많습니다. 다른 저장 폴더를 선택하세요.")


class DBCollector(Collector):
    def __init__(self, config: Config, log=lambda _: None):
        super().__init__(config, log)
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', config.username) or config.username.lower()=='guest':
            raise CollectionError("정확한 직원 SEGIO ID를 입력하세요.")

    def info(self):
        response = self.context.request.get(urljoin(self.base, BRIDGE+'?action=info'),
                                            timeout=self.config.timeout_ms, max_redirects=0)
        try:
            if response.status==404:
                raise CollectionError("서버 연결 기능이 설치되지 않았습니다. 관리자가 prepare_login_archive.py를 먼저 실행해야 합니다.")
            if response.status==401:
                raise CollectionError("서버에서 SEGIO 로그인 사용자를 확인하지 못했습니다. 관리자에게 native_login_required 확인을 요청하세요.")
            if response.status!=200 or 'application/json' not in response.headers.get('content-type', ''):
                raise CollectionError("서버의 로그인 보관함 연결 응답을 확인하지 못했습니다. 서버 설치와 로그인 상태를 확인하세요.")
            data = response.body()
            if len(data)>65536:
                raise CollectionError("서버의 연결 응답이 올바르지 않습니다.")
            result = json.loads(data)
        finally:
            response.dispose()
        if not isinstance(result, dict) or result.get('protocol')!=PROTOCOL:
            raise CollectionError("서버 연결 기능의 버전이 일치하지 않습니다.")
        if result.get('username')!=self.config.username:
            raise CollectionError("실제 로그인 계정이 입력한 ID와 다릅니다. 해당 직원 계정으로 다시 로그인하세요.")
        record = result.get('archive')
        if record is None:
            raise CollectionError("이 직원의 DB 보관본이 아직 준비되지 않았습니다. 관리자가 해당 ID로 prepare_login_archive.py를 실행해야 합니다.")
        if (not isinstance(record, dict) or type(record.get('bytes')) is not int or record['bytes']<=0 or
                not re.fullmatch(r'[a-f0-9]{32}', str(record.get('version', ''))) or
                not re.fullmatch(r'[a-f0-9]{64}', str(record.get('sha256', '')))):
            raise CollectionError("서버의 보관본 검증 정보가 올바르지 않습니다.")
        return record

    def receive(self, record):
        output = self.config.output.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        if record['bytes'] + 64*1024*1024 > shutil.disk_usage(output).free:
            raise CollectionError("ZIP을 저장할 공간이 부족합니다.")
        with tempfile.TemporaryDirectory(prefix='.segio-download-', dir=output) as temporary:
            root = Path(temporary)
            archive = root/'download.zip'
            destination = root/'contents'
            destination.mkdir(mode=0o700)
            endpoint = urljoin(self.base, BRIDGE+'?'+urlencode({'action':'download', 'version':record['version']}))
            opener = download_opener(self.context.cookies([endpoint]))
            user_agent = self.page.evaluate('navigator.userAgent')
            request = urllib.request.Request(endpoint, headers={'Accept':'application/zip', 'User-Agent':user_agent})
            digest = hashlib.sha256()
            count = 0
            self.log("DB 보관 ZIP 다운로드 중")
            try:
                with opener.open(request, timeout=60) as incoming, archive.open('xb') as outgoing:
                    if incoming.status!=200 or incoming.headers.get_content_type()!='application/zip':
                        raise CollectionError("ZIP 대신 다른 서버 응답을 받았습니다. 로그인 상태를 확인하세요.")
                    if incoming.headers.get('Content-Length')!=str(record['bytes']):
                        raise CollectionError("서버 보관본 크기가 변경되었습니다. 다시 받아주세요.")
                    while block := incoming.read(1024*1024):
                        self.checkpoint()
                        count += len(block)
                        if count>record['bytes']:
                            raise CollectionError("보관 ZIP 크기가 검증 정보와 다릅니다.")
                        outgoing.write(block)
                        digest.update(block)
            except (urllib.error.URLError, TimeoutError, OSError):
                raise CollectionError("다운로드가 끊겼거나 세션이 만료되었습니다. 다시 로그인해 받아주세요.") from None
            self.checkpoint()
            if count!=record['bytes'] or digest.hexdigest()!=record['sha256']:
                raise CollectionError("ZIP의 크기 또는 SHA-256 검증에 실패했습니다. 보관 완료로 처리하지 않았습니다.")
            self.log("ZIP과 내부 파일 검증 중")
            unpack_verified(archive, destination, self.config.username, self.checkpoint)
            self.checkpoint()
            return publish_local(destination, archive, output, self.config.username)

    def run(self, password: str) -> Path:
        from playwright.sync_api import sync_playwright
        try:
            self.checkpoint()
            with sync_playwright() as p:
                self.browser = launch_browser(p.chromium, headless=self.config.headless,
                                              explicit=self.config.browser_executable, log=self.log)
                try:
                    self.context = self.browser.new_context(accept_downloads=False, service_workers='block')
                    self.context.set_default_timeout(self.config.timeout_ms)
                    self.context.route('**/*', self.guard)
                    self.page = self.context.new_page()
                    self.log("직원 SEGIO ID·암호로 로그인 확인 중")
                    # The bridge's native identity response is the authentication
                    # proof for this mode; approval-menu HTML is not required.
                    self.login(password, probe=False)
                    password = ''
                    self.checkpoint()
                    record = self.info()
                    try:
                        prepared = datetime.fromisoformat(record['prepared_at']).astimezone(
                            timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')
                    except (ValueError, KeyError, TypeError):
                        prepared = '확인 필요'
                    self.log("본인 보관본 확인 · 서버 준비 시각: " + prepared)
                    result = self.receive(record)
                    self.log("다운로드와 파일 검증 완료: " + str(result))
                    return result
                finally:
                    self.browser.close()
                    self.context = self.page = self.browser = None
        except (CollectionError, BrowserUnavailable):
            raise
        except Exception:
            # Playwright/urllib errors can contain session URLs and cookies.
            raise CollectionError("로그인·다운로드 처리에 실패했습니다. 서버 연결과 설치 상태를 확인하세요.") from None
        finally:
            password = ''
