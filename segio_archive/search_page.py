"""Portable archive list/search, including file:// without fetching SQLite."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .archive import atomic_write
from .mail_folders import MailFolders

VERSION = 'offline-search/5'
CHUNK_SIZE = 500


def script_json(value):
    # Metadata is data, even when a title contains HTML or script delimiters.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')


def install_search_page(root: Path, manifest=None, checkpoint=lambda: None, progress=None):
    """Update only presentation/search metadata; never read or rewrite raw MBOX."""
    root = root.resolve()
    from .progress import Reporter
    report=Reporter(progress)
    report('index',detail='목록·분류 메타데이터 확인',force=True)
    existing = manifest is None
    if existing:
        manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        if (manifest.get('browser_index') == VERSION and
                all((root/name).is_file() for name in manifest['files'] if name.startswith('search/'))):
            report('index',1,1,'steps','현재 인덱스 확인 완료',force=True)
            return manifest
        expected = manifest['files'].get('index.html')
        if expected and hashlib.sha256((root/'index.html').read_bytes()).hexdigest() != expected['sha256']:
            raise ValueError('index.html이 변경되어 화면을 자동 갱신하지 않았습니다. 파일 무결성을 확인하세요.')
    files, counts, chunks = manifest['files'], {'approval': 0, 'mail': 0}, []

    def write(name, value):
        if not (root/name).resolve().is_relative_to(root):
            raise ValueError('검색 화면 파일이 보관 폴더를 벗어납니다.')
        payload = value.encode('utf-8')
        atomic_write(root/name, payload)
        files[name] = {'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}

    folders=MailFolders(root)
    from .presentation import ArchivePresentation
    presentation=ArchivePresentation(root,manifest)
    versions={'active':0,'history':0}
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        boxes=folders.boxes(db)
        approval_boxes=presentation.load_approval_folders(db).boxes(db)
        for kind, count in db.execute('SELECT kind,count(*) FROM items GROUP BY kind'):
            if kind in counts:
                counts[kind] = count
        done,total=0,sum(counts.values())
        report('index',done,total,'records','검색 자료 생성',force=True)
        cursor = db.execute('SELECT id,kind,title,folders,html,body FROM items ORDER BY rowid')
        while rows := cursor.fetchmany(CHUNK_SIZE):
            checkpoint()
            name = f'search/part-{len(chunks):06d}.js'
            if manifest.get('portable_mail'):
                from .portable_mail import page_path
                rows=[(*row[:4],page_path(row[0]),row[5]) if row[1]=='mail' else row for row in rows]
            rows=[(*row[:3],folders.info(row[3])['label'],*row[4:],folders.info(row[3])['id'],row[3])
                  if row[1]=='mail' else (*row,'',row[3]) for row in rows]
            details=presentation.details(db,[dict(id=row[0],kind=row[1],html=row[4]) for row in rows])
            rows=[(*row,item['presentation']) for row,item in zip(rows,details)]
            rows=[(*row[:3],row[8]['approval_label'],*row[4:]) if row[1]=='approval' else row for row in rows]
            for row in rows:
                if row[1]=='approval' and row[8].get('source') in versions:
                    versions[row[8]['source']]+=1
            write(name, 'window.archiveChunk='+script_json(rows)+';')
            chunks.append(name)
            done+=len(rows)
            report('index',done,total,'records','검색 자료 생성')
    config = {'counts': counts, 'chunks': chunks, 'mailboxes': boxes, 'versions':versions,
              'approval_boxes':approval_boxes,
              'archive':{key:manifest.get(key) for key in
                         ('user','server','state','finished_at','coverage','warnings','snapshot_rows')},
              'reports':[name for name in ('issues.json','export-summary.txt') if presentation.path(name)]}
    write('index.html', INDEX.replace('__ARCHIVE_CONFIG__', script_json(config)))
    manifest['browser_index'] = VERSION
    atomic_write(root/'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
    report('index',done,total,'records','인덱스 생성 완료',force=True)
    return manifest


INDEX = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' file: 'unsafe-inline'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>그룹웨어 문서 및 메일 백업 및 검색</title>
<style>
*{box-sizing:border-box}body{font:12px 'Malgun Gothic',sans-serif;line-height:1.6;background:#f3f5f8;color:#162234;max-width:1136px;margin:18px auto;padding:0 18px}h1{font-size:20px;margin:0 0 14px}p{margin:12px 0}input,button,select{font:inherit}button,select{padding:8px 12px;border:1px solid #a4b1c2;background:white;border-radius:5px}button{cursor:pointer}button:disabled{cursor:default;opacity:.45}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid #075b9c;outline-offset:2px}a{color:#075b9c;overflow-wrap:anywhere}#notice{color:#76551b}#search-form{display:flex;gap:8px;margin:18px 0}#q{flex:1;min-width:0;padding:10px;border:1px solid #a4b1c2;border-radius:5px}#search-button{padding:8px 20px}#search-button,[role=tab][aria-selected=true],.box-list button[aria-pressed=true]{background:#075b9c;color:white;border-color:#075b9c}[role=tablist]{display:flex;gap:8px;border-bottom:2px solid #075b9c;padding-bottom:8px}[role=tab]{padding:8px 18px}.folder-panel{background:white;border:1px solid #d8e0e9;border-radius:6px;padding:12px;margin:12px 0}.box-list{display:flex;flex-wrap:wrap;gap:6px;max-height:180px;overflow:auto}.box-list button{padding:6px 12px}#custom-mailboxes h2{font-size:12px;margin:12px 0 6px}#approval-note{margin:10px 0 12px;color:#075b9c}#summary{margin:12px 0 18px}.table-scroll{overflow-x:auto}table{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;text-align:left}col.number{width:52px}col.folder{width:20.5%}col.date{width:23.5%}th{padding:0 12px 8px;font-weight:bold}td{background:white;padding:12px;border-bottom:1px solid #dce3eb;vertical-align:middle;overflow-wrap:anywhere}tbody tr:first-child td{border-top:1px solid #d8e0e9}tbody tr:first-child td:first-child{border-top-left-radius:6px}tbody tr:first-child td:last-child{border-top-right-radius:6px}tbody tr:last-child td:first-child{border-bottom-left-radius:6px}tbody tr:last-child td:last-child{border-bottom-right-radius:6px}td:first-child{border-left:1px solid #d8e0e9}td:last-child{border-right:1px solid #d8e0e9}td a{text-decoration:none}td a:hover{text-decoration:underline}#pagination{display:flex;gap:14px;align-items:center;margin:16px 0}#empty{padding:20px}details{margin-top:18px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.links{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.links a{padding:5px 10px;border:1px solid #c6d7e5;border-radius:5px;background:white;text-decoration:none}.record{padding:12px 0;border-bottom:1px solid #dce3eb}.meta{white-space:pre-line;color:#526176}#backup-status{white-space:pre-line}footer{margin:24px 0;color:#526176}@media(max-width:600px){body{padding:0 12px;margin:14px auto}table{min-width:650px}[role=tab]{padding:8px 12px}.box-list{max-height:160px}}
</style>
<h1>그룹웨어 문서 및 메일 백업 및 검색</h1>
<p>메일 제목·보낸 사람·받는 사람과 결재 내용을 검색합니다. 한 페이지에 100건씩 표시합니다.</p>
<p id="notice" role="status">백업 폴더에서 결재·메일 본문·첨부를 바로 열 수 있습니다. 폴더 전체를 함께 보관하세요.</p>
<form id="search-form" role="search"><select id="kind" aria-label="검색 범위"><option value="all">전체</option><option value="approval">결재</option><option value="mail">메일</option></select><input id="q" aria-label="검색어" placeholder="제목, 보낸 사람, 받는 사람 또는 결재 내용"><button id="search-button" type="submit">검색</button></form>
<div role="tablist" aria-label="백업 자료 종류"><button id="tab-all" role="tab" aria-selected="true" aria-controls="results" type="button">전체</button><button id="tab-approval" role="tab" aria-selected="false" aria-controls="results" type="button">결재</button><button id="tab-mail" role="tab" aria-selected="false" aria-controls="results" type="button">메일</button></div>
<section id="approval-folders" class="folder-panel" hidden aria-label="결재 문서 분류"><div id="approval-boxes" class="box-list" role="group" aria-label="결재 문서함"></div><p id="approval-note">상태를 확인할 수 없는 문서는 상태 미확인에 표시합니다. 참조·수신 문서는 다른 상태 메뉴에도 함께 표시될 수 있습니다.</p></section>
<section id="mail-folders" class="folder-panel" hidden aria-label="메일함 분류"><div id="base-mailboxes" class="box-list" role="group" aria-label="기본 메일함"></div><div id="custom-mailboxes"><h2>편지 보관함</h2><div class="box-list" role="group" aria-label="편지 보관함"></div></div></section>
<p id="summary" role="status"></p>
<main id="results" role="tabpanel" aria-labelledby="tab-all"><div class="table-scroll"><table aria-label="백업 목록"><colgroup><col class="number"><col><col class="folder"><col class="date"></colgroup><thead><tr><th scope="col">번호</th><th scope="col" id="title-label">제목</th><th scope="col" id="folder-label">자료 구분</th><th scope="col" id="date-label">기안일자 / 메일 헤더 날짜</th></tr></thead><tbody id="list"></tbody></table></div></main>
<div id="pagination"><button id="prev" type="button">이전</button><span id="page"></span><button id="next" type="button">다음</button><button id="reset" type="button">전체 목록</button></div>
<details id="record-tools"><summary>현재 목록의 원본·첨부 바로가기</summary><div id="record-links"></div></details>
<details id="archive-info"><summary>수집 범위·검증 내역 및 추가 옵션</summary><div id="owner"></div><div id="backup-time"></div><p id="backup-status"></p><label id="version-control">결재 자료 종류 <select id="version" aria-label="결재 자료 종류"><option value="">현재·과거 전체</option><option value="active">현재 문서</option><option value="history">과거 기록</option></select></label><pre id="coverage"></pre><div id="reports" class="links"></div></details>
<footer>첨부 원본은 해당 파일을 지원하는 Windows 프로그램으로 열립니다. 파일 무결성 검사는 수집 프로그램의 ‘파일 무결성 검증’ 기능을 이용하세요.</footer>
<script>
'use strict';
const config=__ARCHIVE_CONFIG__,offline=location.protocol==='file:',q=document.querySelector('#q'),$=selector=>document.querySelector(selector);
const initial=location.hash.slice(1).split('&'),params=new URLSearchParams(initial.slice(1).join('&'));
let page=0,kind=['approval','mail'].includes(initial[0])?initial[0]:'all',query='',version=params.get('version')||'',mailbox=params.get('box')||'',approvalFolder=params.get('approval_folder')||'',sequence=0,pending=Promise.resolve();
if(!config.mailboxes.some(box=>box.id===mailbox))mailbox='';
if(!config.approval_boxes.some(box=>box.id===approvalFolder))approvalFolder='';
if(!['active','history'].includes(version))version='';
const labels={all:'전체',approval:'결재',mail:'메일'},allCount=config.counts.approval+config.counts.mail;
function time(value){const date=new Date(value);return value&&!Number.isNaN(date.getTime())?date.toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' KST':String(value||'')}
function action(label,path){const link=document.createElement('a');link.textContent=label;link.href=String(path).split('/').map(encodeURIComponent).join('/');link.target='_blank';link.rel='noopener noreferrer';return link}
$('#owner').textContent='대상 ID: '+(config.archive.user||'미기록')+' · 서버: '+(config.archive.server||'미기록');
$('#backup-time').textContent=config.archive.finished_at?'백업 완료 시각: '+time(config.archive.finished_at):'';
const states={collected:'수집 종료 · 대조 확인 필요',collected_with_warnings:'수집 종료 · 확인할 항목 있음',partial:'일부 저장 후 오류 발생',failed:'수집 실패',cancelled:'수집 중단'};
$('#backup-status').textContent=(states[config.archive.state]||config.archive.state||'백업 자료')+'\n'+(config.archive.warnings||[]).slice(0,4).join('\n');
$('#coverage').textContent=JSON.stringify({수집범위:config.archive.coverage||[],확인사항:config.archive.warnings||[],DB스냅샷:config.archive.snapshot_rows||{}},null,2);
for(const name of config.reports)$('#reports').append(action(name==='issues.json'?'미확보·연결 확인 내역':'수집 건수 보고서',name));
function locationHash(){const fields=new URLSearchParams();if(kind==='mail'&&mailbox)fields.set('box',mailbox);if(kind==='approval'&&approvalFolder)fields.set('approval_folder',approvalFolder);if(kind==='approval'&&version)fields.set('version',version);location.hash=kind+(fields.size?'&'+fields:'')}
function boxButton(parent,id,label,count,approval=false){const button=document.createElement('button');button.type='button';button.dataset[approval?'approvalFolder':'mailbox']=id;button.textContent=label+' ('+count.toLocaleString()+'건)';button.onclick=()=>{if(approval)approvalFolder=id;else mailbox=id;page=0;locationHash();load()};parent.append(button)}
boxButton($('#approval-boxes'),'','전체 결재',config.counts.approval,true);
for(const box of config.approval_boxes)boxButton($('#approval-boxes'),box.id,box.label,box.count,true);
boxButton($('#base-mailboxes'),'','전체 메일',config.counts.mail);
for(const box of config.mailboxes)boxButton(box.category==='custom'?$('#custom-mailboxes .box-list'):$('#base-mailboxes'),box.id,box.label,box.count);
$('#custom-mailboxes').hidden=!config.mailboxes.some(box=>box.category==='custom');
function tabs(){
$('#kind').value=kind;$('#version').value=version;$('#mail-folders').hidden=kind!=='mail';$('#approval-folders').hidden=kind!=='approval';$('#version-control').hidden=kind!=='approval'||!(config.versions.active+config.versions.history);
$('#title-label').textContent=kind==='mail'?'메일 제목':kind==='approval'?'결재 제목':'제목';$('#folder-label').textContent=kind==='mail'?'메일함':'자료 구분';$('#date-label').textContent=kind==='mail'?'메일 헤더 날짜':kind==='approval'?'기안일자':'기안일자 / 메일 헤더 날짜';
for(const button of document.querySelectorAll('[data-mailbox]'))button.setAttribute('aria-pressed',String(button.dataset.mailbox===mailbox));
for(const button of document.querySelectorAll('[data-approval-folder]'))button.setAttribute('aria-pressed',String(button.dataset.approvalFolder===approvalFolder));
for(const key of ['all','approval','mail']){$('#tab-'+key).textContent=labels[key]+' ('+(key==='all'?allCount:config.counts[key]).toLocaleString()+'건)';$('#tab-'+key).setAttribute('aria-selected',String(key===kind))}$('#results').setAttribute('aria-labelledby','tab-'+kind)}
function readChunk(name){return new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=name;script.onload=()=>{const rows=window.archiveChunk;delete window.archiveChunk;script.remove();Array.isArray(rows)?resolve(rows):reject(Error('색인 형식'))};script.onerror=()=>{script.remove();reject(Error('색인 파일'))};document.head.append(script)})}
async function offlineSearch(seq){
let total=0,items=[];const needle=query.toLowerCase();
for(const name of config.chunks){if(seq!==sequence)return null;const rows=await readChunk(name);
for(const row of rows){const info=row[8];if((kind!=='all'&&row[1]!==kind)||(kind==='mail'&&mailbox&&row[6]!==mailbox)||(kind==='approval'&&approvalFolder&&!info.approval_folders.includes(approvalFolder))||(kind==='approval'&&version&&info.source!==version)||![row[2],row[3],row[5],row[7]].some(value=>String(value||'').toLowerCase().includes(needle)))continue;
if(total>=page*100&&items.length<100)items.push({id:row[0],kind:row[1],title:row[2],folder:row[3],html:row[4],detail:row[5],folder_id:row[6],source_folder:row[7],presentation:info});total++}
if(!needle&&items.length===100&&!(kind==='approval'&&version&&approvalFolder)){total=kind==='all'?allCount:kind==='mail'&&mailbox?config.mailboxes.find(box=>box.id===mailbox).count:kind==='approval'&&approvalFolder?config.approval_boxes.find(box=>box.id===approvalFolder).count:kind==='approval'&&version?config.versions[version]:config.counts[kind];break}}
return {total,items}}
function render(data){
const selected=kind==='mail'&&mailbox?config.mailboxes.find(box=>box.id===mailbox).label:kind==='approval'&&approvalFolder?config.approval_boxes.find(box=>box.id===approvalFolder).label:'';
$('#summary').textContent=labels[kind]+' '+data.total.toLocaleString()+'건'+(selected?' · '+selected:'')+(query?' · 검색어: '+query:' · 전체 백업 목록');$('#page').textContent=(page+1)+' / '+Math.max(1,Math.ceil(data.total/100));$('#prev').disabled=page===0;$('#next').disabled=(page+1)*100>=data.total;
const out=$('#list'),tools=$('#record-links');out.replaceChildren();tools.replaceChildren();
if(!data.items.length){const row=document.createElement('tr'),empty=document.createElement('td');empty.id='empty';empty.colSpan=4;empty.textContent=query?'검색 결과가 없습니다.':'이 종류의 백업 자료가 없습니다.';row.append(empty);out.append(row)}
for(const [index,item] of data.items.entries()){
const row=document.createElement('tr'),number=document.createElement('td'),heading=document.createElement('td'),link=document.createElement('a'),folder=document.createElement('td'),date=document.createElement('td'),info=item.presentation||{};
row.className='data-row';number.textContent=String(page*100+index+1);link.textContent=item.title||'제목 없음';link.href=String(item.html||'').split('/').map(encodeURIComponent).join('/');heading.className='item-title';heading.append(link);folder.textContent=item.folder;folder.className='mailbox-name';folder.title=item.source_folder||item.folder;date.className='item-date';date.textContent=info.date_display||'미기록';row.append(number,heading,folder,date);out.append(row);
const record=document.createElement('div'),title=document.createElement('strong'),meta=document.createElement('div'),links=document.createElement('div');record.className='record';meta.className='meta';links.className='links';title.textContent=item.title||'제목 없음';
if(item.kind==='approval'){meta.textContent=(info.source==='active'?'현재 문서':info.source==='history'?'과거 기록':'결재 문서')+(info.document_id?' · '+info.document_id:'');links.append(action('본문·결재선 보기',item.html));if(info.json)links.append(action('원본 DB 기록',info.json))}else{meta.textContent=[info.sender?'보낸 사람: '+info.sender:'',info.receiver?'받는 사람: '+info.receiver:'',info.date?'일시: '+info.date:''].filter(Boolean).join('\n');links.append(action('본문 보기',item.html));if(info.raw)links.append(action(info.raw_label,info.raw))}
if(info.captured_at)meta.textContent+=(meta.textContent?'\n':'')+'백업: '+time(info.captured_at);for(const attachment of info.attachments||[])links.append(action('첨부: '+attachment.name,attachment.path));record.append(title,meta,links);tools.append(record)}}
function load(){const seq=++sequence;tabs();$('#list').replaceChildren();$('#record-links').replaceChildren();$('#summary').textContent='목록을 불러오는 중…';$('#prev').disabled=$('#next').disabled=true;
pending=pending.catch(()=>{}).then(async()=>{if(seq!==sequence)return;try{let data;if(offline){data=await offlineSearch(seq)}else{const response=await fetch('api/search?'+new URLSearchParams({q:query,kind:kind==='all'?'':kind,page,mailbox:kind==='mail'?mailbox:'',version:kind==='approval'?version:'',approval_folder:kind==='approval'?approvalFolder:''}));if(!response.ok)throw Error('검색 응답');data=await response.json()}if(seq!==sequence||!data)return;render(data)}catch(error){if(seq!==sequence)return;$('#summary').textContent='목록을 불러오지 못했습니다.';$('#list').replaceChildren();$('#notice').textContent='백업 폴더의 search 폴더를 함께 보관하세요. Windows 프로그램의 기존 백업 갱신으로 검색 화면을 갱신할 수 있습니다.'}})}
function selectKind(value){kind=value;page=0;locationHash();load()}
$('#kind').onchange=()=>selectKind($('#kind').value);$('#version').onchange=()=>{version=$('#version').value;page=0;locationHash();load()};
$('#search-form').onsubmit=event=>{event.preventDefault();query=q.value.trim().slice(0,200);page=0;load()};
$('#reset').onclick=()=>{q.value=query='';mailbox=version=approvalFolder='';page=0;locationHash();load()};
for(const key of ['all','approval','mail'])$('#tab-'+key).onclick=()=>selectKind(key);
$('#prev').onclick=()=>{if(page>0){page--;load()}};$('#next').onclick=()=>{page++;load()};load();
</script></html>'''
