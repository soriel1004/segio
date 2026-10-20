"""Portable archive list/search, including file:// without fetching SQLite."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from .archive import atomic_write

VERSION = 'offline-search/2'
CHUNK_SIZE = 500


def script_json(value):
    # Metadata is data, even when a title contains HTML or script delimiters.
    return json.dumps(value, ensure_ascii=True, separators=(',', ':')).replace('<', '\\u003c')


def install_search_page(root: Path, manifest=None, checkpoint=lambda: None):
    """Update only presentation/search metadata; never read or rewrite raw MBOX."""
    root = root.resolve()
    existing = manifest is None
    if existing:
        manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        if (manifest.get('browser_index') == VERSION and
                all((root/name).is_file() for name in manifest['files'] if name.startswith('search/'))):
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

    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro', uri=True)) as db:
        for kind, count in db.execute('SELECT kind,count(*) FROM items GROUP BY kind'):
            if kind in counts:
                counts[kind] = count
        cursor = db.execute('SELECT id,kind,title,folders,html,body FROM items ORDER BY rowid')
        while rows := cursor.fetchmany(CHUNK_SIZE):
            checkpoint()
            name = f'search/part-{len(chunks):06d}.js'
            if manifest.get('portable_mail'):
                from .portable_mail import page_path
                rows=[(*row[:4],page_path(row[0]),row[5]) if row[1]=='mail' else row for row in rows]
            write(name, 'window.archiveChunk='+script_json(rows)+';')
            chunks.append(name)
    config = {'counts': counts, 'chunks': chunks}
    write('index.html', INDEX.replace('__ARCHIVE_CONFIG__', script_json(config)))
    manifest['browser_index'] = VERSION
    atomic_write(root/'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
    return manifest


INDEX = r'''<!doctype html><html lang="ko"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' file: 'unsafe-inline'; connect-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>그룹웨어 문서 및 메일 백업 및 검색</title>
<style>
body{font:12px 'Malgun Gothic',sans-serif;line-height:1.6;background:#f3f5f8;color:#162234;max-width:1100px;margin:24px auto;padding:0 18px}
h1{font-size:20px}h2{font-size:14px;margin:0}input,button{font:inherit;box-sizing:border-box}button{cursor:pointer;padding:8px 18px;border:1px solid #a4b1c2;background:white;border-radius:5px}button:disabled{cursor:default;opacity:.45}button:focus-visible,input:focus-visible{outline:2px solid #075b9c;outline-offset:2px}
#search-form{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}#q{flex:1;min-width:160px;padding:10px;border:1px solid #a4b1c2;border-radius:5px}#search-button,[role=tab][aria-selected=true]{background:#075b9c;color:white;border-color:#075b9c}
[role=tablist]{display:flex;gap:8px;border-bottom:2px solid #075b9c;padding-bottom:8px}#summary{margin:12px 0}#results{background:white;border:1px solid #d8e0e9;border-radius:6px}article{display:grid;grid-template-columns:52px minmax(0,1fr) minmax(100px,25%);gap:10px;padding:12px;border-bottom:1px solid #e3e8ef;align-items:center}article:last-child{border-bottom:0}article h2{font-size:12px;font-weight:normal;overflow-wrap:anywhere}article p{margin:0;overflow-wrap:anywhere;color:#526176}a{color:#075b9c}#notice{color:#76551b}#pagination{display:flex;gap:14px;align-items:center;margin-top:16px}#empty{padding:20px}@media(max-width:600px){article{grid-template-columns:35px minmax(0,1fr)}article p{grid-column:2}}
</style>
<h1>그룹웨어 문서 및 메일 백업 및 검색</h1>
<p>메일 제목·보낸 사람·받는 사람과 결재 내용을 검색합니다. 한 페이지에 100건씩 표시합니다.</p>
<p id="notice" role="status"></p>
<form id="search-form" role="search"><input id="q" aria-label="검색어" placeholder="제목, 보낸 사람, 받는 사람 또는 결재 내용"><button id="search-button" type="submit">검색</button><button id="reset" type="button">전체 목록</button></form>
<div role="tablist" aria-label="백업 자료 종류"><button id="tab-approval" role="tab" aria-selected="true" aria-controls="results" type="button">결재</button><button id="tab-mail" role="tab" aria-selected="false" aria-controls="results" type="button">메일</button></div>
<p id="summary" role="status"></p><main id="results" role="tabpanel" aria-labelledby="tab-approval"></main>
<div id="pagination"><button id="prev" type="button">이전</button><span id="page"></span><button id="next" type="button">다음</button></div>
<script>
'use strict';
const config=__ARCHIVE_CONFIG__,offline=location.protocol==='file:',q=document.querySelector('#q');
let page=0,kind=location.hash==='#mail'?'mail':(config.counts.approval?'approval':'mail'),query='',sequence=0,pending=Promise.resolve();
const $=selector=>document.querySelector(selector);
function tabs(){for(const key of ['approval','mail']){$('#tab-'+key).textContent=(key==='approval'?'결재':'메일')+' ('+config.counts[key].toLocaleString()+'건)';$('#tab-'+key).setAttribute('aria-selected',String(key===kind))}$('#results').setAttribute('aria-labelledby','tab-'+kind)}
function readChunk(name){return new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=name;script.onload=()=>{const rows=window.archiveChunk;delete window.archiveChunk;script.remove();Array.isArray(rows)?resolve(rows):reject(Error('색인 형식'))};script.onerror=()=>{script.remove();reject(Error('색인 파일'))};document.head.append(script)})}
async function offlineSearch(seq){let total=0,items=[];const needle=query.toLowerCase();for(const name of config.chunks){if(seq!==sequence)return null;const rows=await readChunk(name);for(const row of rows){if(row[1]!==kind||![row[2],row[3],row[5]].some(value=>String(value||'').toLowerCase().includes(needle)))continue;if(total>=page*100&&items.length<100)items.push({id:row[0],kind:row[1],title:row[2],folder:row[3],html:row[4],detail:row[5]});total++}if(!needle&&items.length===100){total=config.counts[kind];break}}return {total,items}}
function render(data){$('#summary').textContent=(kind==='approval'?'결재':'메일')+' '+data.total.toLocaleString()+'건'+(query?' · 검색어: '+query:' · 전체 백업 목록');$('#page').textContent=(page+1)+' / '+Math.max(1,Math.ceil(data.total/100));$('#prev').disabled=page===0;$('#next').disabled=(page+1)*100>=data.total;const out=$('#results');out.replaceChildren();if(!data.items.length){const empty=document.createElement('p');empty.id='empty';empty.textContent=query?'검색 결과가 없습니다.':'이 종류의 백업 자료가 없습니다.';out.append(empty)}for(const [index,item] of data.items.entries()){const row=document.createElement('article'),number=document.createElement('span'),heading=document.createElement('h2'),link=document.createElement('a'),folder=document.createElement('p');number.textContent=String(page*100+index+1);link.textContent=item.title||'제목 없음';link.href=String(item.html||'').split('/').map(encodeURIComponent).join('/');heading.append(link);folder.textContent=item.folder;row.append(number,heading,folder);out.append(row)}}
function load(){const seq=++sequence;tabs();$('#summary').textContent='목록을 불러오는 중…';$('#prev').disabled=$('#next').disabled=true;pending=pending.catch(()=>{}).then(async()=>{if(seq!==sequence)return;try{let data;if(offline){data=await offlineSearch(seq)}else{const response=await fetch('api/search?'+new URLSearchParams({q:query,kind,page}));if(!response.ok)throw Error('검색 응답');data=await response.json()}if(seq!==sequence||!data)return;render(data)}catch(error){if(seq!==sequence)return;$('#summary').textContent='목록을 불러오지 못했습니다.';$('#results').replaceChildren();$('#notice').textContent='백업 폴더의 search 폴더를 함께 보관하세요. Windows 프로그램의 보관함 열기로 열면 검색 화면을 갱신할 수 있습니다.'}})}
$('#search-form').onsubmit=event=>{event.preventDefault();query=q.value.trim().slice(0,200);page=0;load()};$('#reset').onclick=()=>{q.value=query='';page=0;load()};for(const key of ['approval','mail']){$('#tab-'+key).onclick=()=>{kind=key;location.hash=key;page=0;load()}}$('#prev').onclick=()=>{if(page>0){page--;load()}};$('#next').onclick=()=>{page++;load()};$('#notice').textContent='백업 폴더에서 결재·메일 본문·첨부를 바로 열 수 있습니다. 폴더 전체를 함께 보관하세요.';load();
</script></html>'''
