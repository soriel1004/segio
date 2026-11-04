"""Render local MBOX records to static HTML and attachments with bounded reads."""
from contextlib import closing
from email import policy
from email.parser import BytesHeaderParser
import base64
import binascii
import hashlib
import html
import json
import quopri
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

from bs4 import BeautifulSoup
from bs4.exceptions import ParserRejectedMarkup

from .archive import atomic_write, safe_name
from .compact_mail import digest_file
from .mail_import import decode_bytes, header_text
from .web import CollectionError
from email.errors import MessageError

VERSION = 'static-mail/1'
BLOCK = 65536
TEXT_LIMIT = 8 * 1024 * 1024


class MailFormatError(CollectionError):
    """Malformed message data; the verified raw source remains authoritative."""


def security_blocked(exc):
    # Windows reports these specifically for an antivirus rejection/quarantine.
    # Ordinary access denied, disk full and device errors must not be skipped.
    return isinstance(exc,OSError) and getattr(exc,'winerror',None) in (225,226)


def render_diagnostic(exc,record,position):
    kinds={'Error','ValueError','TypeError','AttributeError','KeyError','RecursionError',
           'MemoryError','OverflowError','UnicodeEncodeError','UnicodeDecodeError',
           'PermissionError','FileNotFoundError','OSError'}
    result={'protocol':'segio-mail-render/1','mail':record['id'],'position':position,
            'exception_type':type(exc).__name__ if type(exc).__name__ in kinds else 'unknown'}
    for field in ('errno','winerror'):
        value=getattr(exc,field,None)
        if type(value) is int and 0<=value<=65535: result[field]=value
    trace=sys.exc_info()[2]
    while trace:
        function=trace.tb_frame.f_code.co_name
        if function in {'render_record','mime_parts','headers','decoded','safe_html','write','local_path'}:
            result['site']={'function':function,'line':trace.tb_lineno}
        trace=trace.tb_next
    return result


def page_path(identity):
    if not re.fullmatch(r'mail-[0-9]{9}', identity):
        raise CollectionError('메일 식별자 형식이 올바르지 않습니다.')
    return f'mail/offline/{identity}/index.html'


def local_path(root, name):
    path = root/name
    if not path.resolve().is_relative_to(root):
        raise CollectionError('메일 열람 파일 경로가 보관 폴더를 벗어납니다.')
    return path


def chunks(stream, start, end, checkpoint):
    stream.seek(start)
    while stream.tell() < end:
        checkpoint()
        block = stream.read(min(BLOCK, end-stream.tell()))
        if not block:
            raise CollectionError('메일 원본이 잘렸습니다.')
        yield block


def headers(stream, start, end):
    stream.seek(start)
    data = bytearray()
    while stream.tell() < end:
        line = stream.readline(min(BLOCK, end-stream.tell()))
        if not line or line in (b'\n', b'\r\n'):
            break
        data.extend(line)
        if len(data) > 1024*1024:
            raise MailFormatError('메일 헤더가 너무 큽니다.')
    try:
        return BytesHeaderParser(policy=policy.compat32).parsebytes(bytes(data)), stream.tell()
    except (MessageError,ValueError,UnicodeError) as exc:
        raise MailFormatError('메일 헤더 형식이 손상되었습니다.') from exc


def mime_parts(stream, start, end, checkpoint, depth=0):
    """Find MIME leaves by offsets, never loading a full attachment/message."""
    if depth > 20:
        raise MailFormatError('메일 MIME 중첩 단계가 너무 많습니다.')
    message, body = headers(stream,start,end)
    if message.get_content_maintype() != 'multipart':
        return message, [(message,body,end)]
    boundary = message.get_boundary()
    if not boundary or len(boundary)>200:
        raise MailFormatError('메일 MIME 구분자가 올바르지 않습니다.')
    try:
        marker = b'--'+boundary.encode('ascii')
    except UnicodeError:
        raise MailFormatError('메일 MIME 구분자 인코딩이 올바르지 않습니다.') from None
    delimiters, line_start = [], True
    stream.seek(body)
    while stream.tell()<end:
        checkpoint()
        position=stream.tell()
        line=stream.readline(min(BLOCK,end-position))
        if not line:
            raise CollectionError('메일 원본이 잘렸습니다.')
        value=line.rstrip(b'\r\n \t') if line_start else b''
        if value in (marker,marker+b'--'):
            if len(delimiters)>10000:
                raise MailFormatError('메일의 MIME 부분이 너무 많습니다.')
            delimiters.append((position,stream.tell(),value==marker+b'--'))
            if value==marker+b'--':
                break
        line_start=line.endswith(b'\n')
    leaves=[]
    for number,(_,after,closed) in enumerate(delimiters):
        if closed:
            break
        stop=delimiters[number+1][0] if number+1<len(delimiters) else end
        # The newline immediately preceding a delimiter belongs to that delimiter.
        if stop>after:
            stream.seek(max(after,stop-2))
            tail=stream.read(stop-max(after,stop-2))
            stop-=2 if tail.endswith(b'\r\n') else int(tail.endswith(b'\n'))
        _,children=mime_parts(stream,after,stop,checkpoint,depth+1)
        leaves.extend(children)
        if len(leaves)>10000:
            raise MailFormatError('메일의 MIME 부분이 너무 많습니다.')
    if not delimiters:
        raise MailFormatError('메일 MIME 본문의 구분자를 찾지 못했습니다.')
    return message,leaves


def decoded(stream, start, end, encoding, checkpoint):
    encoding=encoding.lower().strip()
    carry=b''
    for block in chunks(stream,start,end,checkpoint):
        if encoding=='base64':
            block=carry+re.sub(rb'[ \t\r\n]',b'',block)
            if re.search(rb'[^A-Za-z0-9+/=]',block):
                raise MailFormatError('메일 Base64 데이터가 손상되었습니다.')
            padding=block.find(b'=')
            size=(padding if padding>=0 else len(block))//4*4
            carry=block[size:]
            if padding>=0 and len(carry)>4:
                raise MailFormatError('메일 Base64 끝부분이 손상되었습니다.')
            if size:
                yield base64.b64decode(block[:size],validate=True)
        elif encoding=='quoted-printable':
            block=carry+block
            split=block.rfind(b'=',max(0,len(block)-2))
            carry=block[split:] if split>=0 else b''
            yield quopri.decodestring(block[:split] if split>=0 else block)
        elif encoding in ('','7bit','8bit','binary'):
            yield block
        else:
            raise MailFormatError('지원하지 않는 메일 전송 인코딩입니다.')
    if carry:
        if encoding=='base64':
            try:
                yield base64.b64decode(carry+b'='*((-len(carry))%4),validate=True)
            except binascii.Error as exc:
                raise MailFormatError('메일 Base64 끝부분이 손상되었습니다.') from exc
        else:
            yield quopri.decodestring(carry)


def safe_html(text, images):
    soup=BeautifulSoup(text,'html.parser')
    for tag in soup.select('script,style,iframe,object,embed,svg,math,form,template,head'):
        if not tag.decomposed:
            tag.decompose()
    allowed=set('p br div span table thead tbody tfoot tr td th ul ol li strong em b i u s hr blockquote pre code h1 h2 h3 h4 h5 h6 img a'.split())
    for tag in list(soup.find_all(True)):
        # Broken HTML may give an image children. Removing that image also
        # detaches its descendants from this previously captured traversal.
        if tag.decomposed or tag.parent is None:
            continue
        if tag.name not in allowed:
            tag.unwrap()
            continue
        old=dict(tag.attrs)
        tag.attrs={}
        if tag.name=='img':
            source=old.get('src','')
            image=images.get(source[4:].strip('<>')) if isinstance(source,str) and source.lower().startswith('cid:') else None
            if not image:
                # Strip the image while retaining any misplaced text/children.
                # They are still sanitized by this traversal.
                tag.unwrap()
                continue
            tag['src']=image
            tag['alt']=old.get('alt','본문 이미지')
        if tag.name in ('td','th'):
            for name in ('colspan','rowspan'):
                value=str(old.get(name,''))
                if re.fullmatch(r'[0-9]+',value):
                    value=value.lstrip('0') or '0'
                    tag[name]=str(1000 if len(value)>4 else min(1000,int(value)))
    return str(soup)


def render_record(root, manifest, stream, record, checkpoint, written=lambda count:None):
    directory=Path(page_path(record['id'])).parent.as_posix()
    local_path(root,directory).mkdir(parents=True,exist_ok=True)
    source_info=manifest['files'][record['source']]
    fingerprint=[VERSION,record['source'],record['begin'],record['end'],source_info['sha256']]
    state_path=local_path(root,directory+'/state.json')
    try:
        state=json.loads(state_path.read_text(encoding='utf-8'))
        if (state['source']==fingerprint and directory+'/index.html' in state['files']
                and all(name.startswith(directory+'/') for name in state['files'])
                and all(digest_file(local_path(root,name),checkpoint)==value for name,value in state['files'].items())):
            manifest['files'].update(state['files'])
            manifest['files'][directory+'/state.json']=digest_file(state_path,checkpoint)
            return state.get('issues',[])
    except (OSError,ValueError,KeyError):
        pass
    generated={}

    def write(name, parts):
        path=local_path(root,name)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.partial')
        digest,size=hashlib.sha256(),0
        try:
            with temporary.open('wb') as output:
                for part in parts:
                    checkpoint()
                    output.write(part)
                    digest.update(part)
                    size+=len(part)
                    written(len(part))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        generated[name]={'bytes':size,'sha256':digest.hexdigest()}

    plain,rich,attachments,images,notes=[],[],[],{},[]
    issues=[]
    try:
        message,parts=mime_parts(stream,record['begin'],record['end'],checkpoint)
    except (MailFormatError,MessageError,UnicodeError,ValueError,RecursionError):
        message,parts=None,[]
        issues.append({'mail':record['id'],'code':'damaged_mime'})
        plain.append('메일 구조가 손상되어 본문·첨부 바로보기를 생성하지 못했습니다. 원본 MBOX/EML은 그대로 보존되어 있습니다.')
    for number,(part,start,end) in enumerate(parts):
        checkpoint()
        attachment_path=None
        try:
            filename=part.get_filename()
            cid=str(part.get('Content-ID','')).strip('<> \t\r\n')
            encoding=str(part.get('Content-Transfer-Encoding',''))
            payload=decoded(stream,start,end,encoding,checkpoint)
            is_text=part.get_content_type() in ('text/plain','text/html')
            if filename or part.get_content_disposition()=='attachment' or not is_text:
                filename=header_text(filename) or ('message.eml' if part.get_content_type()=='message/rfc822' else 'attachment.bin')
                name=f'attachments/{number}-{safe_name(filename,80)}'
                attachment_path=directory+'/'+name
                write(attachment_path,payload)
                attachments.append((filename,name))
                if cid and part.get_content_type() in ('image/png','image/jpeg','image/gif','image/webp'):
                    images[cid]=quote(name,safe='/')
            else:
                data=bytearray()
                for block in payload:
                    remaining=TEXT_LIMIT-len(data)
                    data.extend(block[:remaining])
                    if len(block)>remaining:
                        notes.append('매우 큰 본문은 처음 8 MiB까지 표시합니다. 전체 내용은 원본 MBOX에 보존됩니다.')
                        break
                text=decode_bytes(bytes(data),part.get_content_charset())
                (rich if part.get_content_type()=='text/html' else plain).append(text)
        except (MailFormatError,MessageError,UnicodeError,binascii.Error,ValueError):
            code='damaged_part'
        except OSError as exc:
            if not security_blocked(exc):raise
            code='security_blocked'
        else:
            continue
        if attachment_path:
            # Never publish a partially decoded or rejected attachment.
            local_path(root,attachment_path).unlink(missing_ok=True)
            generated.pop(attachment_path,None)
        issues.append({'mail':record['id'],'part':number,'code':code})
        notes.append('보안 프로그램이 차단한 부분을 제외했습니다. 백신 설정은 변경하지 않았습니다.' if code=='security_blocked' else
                     '손상되었거나 해석할 수 없는 본문·첨부 부분을 제외했습니다. 원본 MBOX/EML은 보존되어 있습니다.')
    rendered=[]
    for text in rich:
        checkpoint()
        try:
            rendered.append(safe_html(text,images))
        except (ValueError,ParserRejectedMarkup,RecursionError):
            rendered.append('<pre>'+html.escape(text)+'</pre>')
            issues.append({'mail':record['id'],'code':'damaged_html'})
            notes.append('HTML 형식을 해석하지 못해 해당 본문을 텍스트 형식으로 표시합니다. 원본 MBOX/EML은 보존되어 있습니다.')
    empty_body='본문 바로보기를 생성하지 못했습니다. 제외 안내와 원본을 확인하세요.' if issues else '본문이 없는 메일입니다.'
    content='\n'.join(rendered) if rich else '<pre>'+html.escape('\n'.join(plain) or empty_body)+'</pre>'
    attachment_html=''.join('<li><a class="attachment" href="'+html.escape(quote(name,safe='/'),quote=True)+'">'+html.escape(filename)+'</a></li>' for filename,name in attachments)
    if not attachment_html:
        attachment_html='<li>제외된 부분이 있습니다. 위 안내와 원본을 확인하세요.</li>' if issues else '<li>첨부 파일 없음</li>'
    extra=''.join('<p>'+html.escape(note)+'</p>' for note in dict.fromkeys(notes))
    title=html.escape(record['title'])
    page=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{title}</title><style>body{{font:12px 'Malgun Gothic',sans-serif;line-height:1.6;margin:24px;overflow-wrap:anywhere}}h1{{font-size:18px}}pre{{white-space:pre-wrap;font:inherit}}table{{border-collapse:collapse;max-width:100%}}td,th{{border:1px solid #bbb;padding:6px}}img{{max-width:100%;height:auto}}a{{color:#075b9c}}</style>
<p><a href="../../../index.html#mail">메일 목록</a> · <a href="../../../{html.escape(quote(record['source'],safe='/'),quote=True)}">원본 메일 파일</a></p>
<h1>{title}</h1><p>보낸 사람: {html.escape(record['sender'])}<br>받는 사람: {html.escape(record['receiver'])}<br>참조: {html.escape(header_text(message.get('Cc')) if message is not None else '')}<br>일시: {html.escape(record['date'])}</p>
<main>{content}</main>{extra}<h2>첨부</h2><ul>{attachment_html}</ul></html>'''
    write(directory+'/index.html',[page.encode('utf-8')])
    atomic_write(state_path,json.dumps({'source':fingerprint,'files':generated,
        'issues':issues,
        'attachments':[{'name':filename,'path':directory+'/'+name} for filename,name in attachments]},
        ensure_ascii=False).encode('utf-8'))
    generated[directory+'/state.json']=digest_file(state_path,checkpoint)
    manifest['files'].update(generated)
    return issues


def build_portable_mail(root: Path, manifest=None, checkpoint=lambda:None, log=lambda _:None, progress=None):
    from .progress import Reporter
    report=Reporter(progress)
    report('mail',detail='메일 본문·첨부 바로보기 준비',force=True)
    root=root.resolve()
    if manifest is None:
        manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('viewer')!='sqlite-mail/1':
        return manifest
    if manifest.get('portable_mail',{}).get('version')==VERSION:
        count=manifest['portable_mail']['count']
        report('mail',count,count,'messages','이미 준비된 메일 바로보기 사용',force=True)
        return manifest
    from .db_client import safe_member
    count=0
    issues=[]
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        total=db.execute('SELECT count(*) FROM mail_records').fetchone()[0]
        report('mail',0,total,'messages','본문·첨부 처리',force=True)
        rows=db.execute('SELECT m.*,i.title FROM mail_records m JOIN items i ON m.id=i.id ORDER BY m.source,m.begin')
        with SourceReader() as opened:
            for row in rows:
                checkpoint()
                record=dict(row)
                name=record['source']
                safe_member(name)
                if not name.startswith('mail/raw/') or name not in manifest['files']:
                    raise CollectionError('검증되지 않은 메일 원본 경로입니다.')
                path=local_path(root,name)
                if path.stat().st_size!=manifest['files'][name]['bytes']:
                    raise CollectionError('메일 원본 파일의 크기가 변경되었습니다.')
                if not 0<=record['begin']<=record['end']<=path.stat().st_size:
                    raise CollectionError('메일 원본의 위치 또는 크기가 일치하지 않습니다.')
                stream=opened.open(path)
                generated_bytes=0
                def written(size):
                    nonlocal generated_bytes
                    generated_bytes+=size
                    report('mail',count,total,'messages',f'현재 메일 {count+1:,}/{total:,} · 본문·첨부 생성 {generated_bytes/1024**2:.1f} MiB')
                report('mail',count,total,'messages',f'현재 메일 {count+1:,}/{total:,} · 원본·완료 파일 확인 중')
                try:
                    record_issues=render_record(root,manifest,stream,record,checkpoint,written)
                except CollectionError:
                    raise
                except Exception as exc:
                    from .web import Cancelled
                    if isinstance(exc,Cancelled):raise
                    detail=render_diagnostic(exc,record,count+1)
                    try:atomic_write(root/'mail/render-failure.json',json.dumps(detail).encode('utf-8'))
                    except OSError:pass
                    code='LOCAL_DISK_FULL' if isinstance(exc,OSError) and exc.errno==28 else 'MAIL_RENDER_ERROR'
                    raise CollectionError(f'메일 {count+1:,}/{total:,} 바로보기 생성에 실패했습니다. '
                        f'완료한 자료는 이어받기로 재사용합니다. [{code}]\n진단: '+json.dumps(detail,ensure_ascii=False)) from None
                issues.extend(record_issues)
                for issue in record_issues:
                    log('메일 바로보기 제외: '+issue['mail']+' · '+issue['code'])
                count+=1
                report('mail',count,total,'messages','본문·첨부 바로보기 생성')
                if count%100==0 or count==total:
                    log(f'메일 바로보기 생성 {count:,} / {total:,}건')
        if count!=total:
            raise CollectionError('메일 원본 위치 기록과 목록의 건수가 일치하지 않습니다.')
    manifest['portable_mail']={'version':VERSION,'count':count,'text_limit':TEXT_LIMIT,
                               'excluded_parts':len(issues),'affected_messages':len({issue['mail'] for issue in issues})}
    issue_path=root/'mail/render-issues.json'
    atomic_write(issue_path,json.dumps({'protocol':'segio-mail-render-issues/1','issues':issues},indent=2).encode('utf-8'))
    manifest['files']['mail/render-issues.json']=digest_file(issue_path,checkpoint)
    (root/'mail/render-failure.json').unlink(missing_ok=True)
    if issues:
        manifest['state']='collected_with_warnings'
        warning=f'메일 바로보기에서 보안 차단·손상 부분 {len(issues):,}개를 제외했습니다. 내역: mail/render-issues.json'
        manifest['warnings']=[warning]+[value for value in manifest.get('warnings',[]) if value!=warning]
    old_warning='메일은 제목·보낸 사람·받는 사람을 검색합니다. 본문·첨부는 원본에서 열람할 때 읽습니다.'
    manifest['warnings']=[value for value in manifest.get('warnings',[]) if value!=old_warning]
    for entry in manifest.get('coverage',[]):
        if entry.get('kind')=='mail_index':
            entry['body']='static_html_and_attachments'
    atomic_write(root/'manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8'))
    report('mail',count,total,'messages','메일 바로보기 생성 완료',force=True)
    return manifest


class SourceReader:
    def __enter__(self):
        self.path,self.stream=None,None
        return self

    def open(self,path):
        if self.path!=path:
            if self.stream:
                self.stream.close()
            self.path,self.stream=path,path.open('rb')
        return self.stream

    def __exit__(self,*_):
        if self.stream:
            self.stream.close()


def prepare_archive(root, checkpoint=lambda:None, log=lambda _:None, progress=None):
    """One-time local conversion of an existing SSH backup, no server access."""
    root=Path(root).resolve()
    from .local_delete import recover
    recover(root)
    from .progress import Reporter
    report=Reporter(progress)
    report('validate',detail='기존 백업 파일 확인',force=True)
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('viewer')!='sqlite-mail/1':
        return manifest
    expected=manifest['files'].get('index.html')
    if expected and digest_file(root/'index.html',checkpoint,lambda done,total:report('validate',done,total,'bytes','인덱스 무결성 확인'))!=expected:
        raise CollectionError('index.html이 변경되었습니다. 파일 무결성을 확인하세요.')
    if digest_file(root/'catalog.sqlite3',checkpoint,lambda done,total:report('validate',done,total,'bytes','검색 DB 무결성 확인'))!=manifest['files'].get('catalog.sqlite3'):
        raise CollectionError('메일 검색 DB가 변경되었습니다. 파일 무결성을 확인하세요.')
    from .index_maintenance import compact_approval_index
    manifest=compact_approval_index(root,manifest,checkpoint,progress)
    if manifest.get('portable_mail',{}).get('version')==VERSION:
        count=manifest['portable_mail']['count']
        report('mail',count,count,'messages','이미 준비된 메일 바로보기 사용',force=True)
        from .search_page import install_search_page
        return install_search_page(root,checkpoint=checkpoint,progress=progress)
    manifest=build_portable_mail(root,manifest,checkpoint,log,progress=progress)
    from .search_page import install_search_page
    return install_search_page(root,manifest,checkpoint,progress=progress)
