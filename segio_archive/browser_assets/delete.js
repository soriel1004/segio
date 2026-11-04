/* Local archive editing through the File System Access API. No network service. */
(() => {
'use strict';
const WORK='.segio-browser-edit', PROTOCOL='segio-browser-delete/1', BLOCK=1024*1024;
const encoder=new TextEncoder(), decoder=new TextDecoder('utf-8',{fatal:true});
const json=value=>encoder.encode(JSON.stringify(value,null,2));
const fail=message=>{throw new Error(message)};
const safe=name=>{
    if(typeof name!=='string'||!name||name.includes('\\')||name.startsWith('/'))fail('백업 파일 경로가 올바르지 않습니다.');
    const parts=name.split('/');
    for(const part of parts)if(!part||part==='.'||part==='..'||/[<>:"|?*\x00-\x1f]/.test(part)||/[ .]$/.test(part)||/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(part))fail('안전하지 않은 백업 파일 경로입니다.');
    return parts;
};
const allowed=name=>name==='manifest.json'||name==='index.html'||name==='catalog.json'||name==='catalog.sqlite3'||/^(documents|mail|search)\//.test(name);
async function directory(root,name,create=false){let handle=root;for(const part of name?safe(name):[])handle=await handle.getDirectoryHandle(part,{create});return handle}
async function fileHandle(root,name,create=false){const parts=safe(name),last=parts.pop();return (await directory(root,parts.join('/'),create)).getFileHandle(last,{create})}
async function file(root,name){return (await fileHandle(root,name)).getFile()}
async function exists(root,name){try{const parts=safe(name),last=parts.pop();const parent=await directory(root,parts.join('/'));for await(const [entry] of parent.entries())if(entry===last)return true;return false}catch(error){if(error.name==='NotFoundError')return false;throw error}}
async function remove(root,name){const parts=safe(name),last=parts.pop();try{await (await directory(root,parts.join('/'))).removeEntry(last,{recursive:false})}catch(error){if(error.name!=='NotFoundError')throw error}}
async function write(root,name,payload){const writer=await (await fileHandle(root,name,true)).createWritable();try{await writer.write(payload);await writer.close()}catch(error){await writer.abort().catch(()=>{});throw error}}
async function readJson(root,name){return JSON.parse(decoder.decode(await (await file(root,name)).arrayBuffer()))}
function same(a,b){return a&&b&&a.bytes===b.bytes&&a.sha256===b.sha256}
async function digest(blob,checkpoint=()=>{},progress=()=>{}){const hash=sha256.create();for(let offset=0;offset<blob.size;offset+=BLOCK){checkpoint();hash.update(await blob.slice(offset,offset+BLOCK).arrayBuffer());progress(Math.min(offset+BLOCK,blob.size),blob.size)}return {bytes:blob.size,sha256:hash.hex()}}
async function checked(root,name,files,checkpoint=()=>{},progress=()=>{}){const blob=await file(root,name);if(!Object.hasOwn(files,name)||!same(await digest(blob,checkpoint,progress),files[name]))fail('백업 파일이 변경되었습니다. 삭제를 적용하지 않았습니다.');return blob}
async function copy(source,target,name,checkpoint=()=>{},progress=()=>{}){const writer=await (await fileHandle(target,name,true)).createWritable();try{for(let offset=0;offset<source.size;offset+=BLOCK){checkpoint();await writer.write(await source.slice(offset,offset+BLOCK).arrayBuffer());progress(Math.min(offset+BLOCK,source.size),source.size)}await writer.close()}catch(error){await writer.abort().catch(()=>{});throw error}}
function rows(db,sql,params=[]){const statement=db.prepare(sql);try{statement.bind(params);const result=[];while(statement.step())result.push(statement.getAsObject());return result}finally{statement.free()}}
function pagePath(id){if(!/^mail-\d{9}$/.test(id))fail('메일 ID 형식을 확인하세요.');return 'mail/offline/'+id+'/index.html'}
function indexConfig(text){const start='<script id="archive-config" type="application/json">',begin=text.indexOf(start),end=text.indexOf('</script>',begin+start.length);if(begin<0||end<0)fail('새 인덱스로 백업 화면을 갱신하세요.');return {start:begin+start.length,end,value:JSON.parse(text.slice(begin+start.length,end))}}
async function validateManifest(manifest,report=()=>{}){
    if(!manifest.files||typeof manifest.files!=='object'||Array.isArray(manifest.files)||manifest.viewer!=='sqlite-mail/1'||manifest.portable_mail?.version!=='static-mail/1')fail('새 프로그램으로 백업 인덱스를 한 번 갱신하세요.');
    const names=new Set(),entries=Object.entries(manifest.files);let count=0;for(const [name,info] of entries){safe(name);const key=name.toLowerCase();if(names.has(key)||!Number.isSafeInteger(info.bytes)||info.bytes<0||!/^[a-f0-9]{64}$/.test(info.sha256))fail('백업 파일 검증 정보가 올바르지 않습니다.');names.add(key);if(++count%500===0){report('백업 파일 목록 확인 '+count.toLocaleString()+' / '+entries.length.toLocaleString()+'개');await new Promise(resolve=>setTimeout(resolve,0))}}
}
async function guard(root,callback){
    // The exclusive writable is held open, then aborted: its original bytes are unchanged.
    const handle=await fileHandle(root,'.segio-browser.lock',true);
    let lock;
    try{lock=await handle.createWritable({mode:'exclusive'})}catch(error){fail('다른 창에서 백업본을 편집 중입니다. 작업이 끝난 뒤 다시 시도하세요.')}
    try{
        let probe;try{probe=await handle.createWritable({mode:'exclusive'})}catch(error){if(error.name!=='NoModificationAllowedError')throw error}
        if(probe){await probe.abort();fail('최신 Chrome 또는 Edge에서 백업 폴더를 열어주세요. 이 브라우저는 동시 편집 방지를 지원하지 않습니다.')}
        if(await exists(root,'.segio-edit'))fail('Windows 프로그램에서 삭제 처리 또는 복구가 진행 중입니다.');
        return await callback();
    }finally{await lock.abort().catch(()=>{})}
}
async function cleanup(root){await root.removeEntry(WORK,{recursive:true})}
async function recover(root){
    if(!await exists(root,WORK))return false;
    const work=await directory(root,WORK);
    // A crash before the first journal close cannot have touched originals.
    const entries=[];for await(const [name] of work.entries())entries.push(name);
    if(!entries.length||(entries.length===1&&entries[0]==='journal.json'&&(await file(work,'journal.json')).size===0)){await cleanup(root);return true}
    const journal=await readJson(work,'journal.json');
    if(journal.protocol!==PROTOCOL||!['preparing','installing','committed'].includes(journal.phase))fail('브라우저 삭제 복구 기록을 확인하지 못했습니다. 작업 폴더를 보존하세요.');
    if(journal.phase==='installing'){
        const before=await readJson(work,'before/manifest.json');await validateManifest(before);
        const seen=new Set();
        for(const operation of journal.paths){
            safe(operation.name);if(!allowed(operation.name)||seen.has(operation.name))fail('삭제 복구 경로가 올바르지 않습니다.');seen.add(operation.name);
            const expected=operation.name==='manifest.json'?journal.manifest:before.files[operation.name];
            if(operation.existed){if(!same(expected,operation.original))fail('삭제 복구 검증 정보가 올바르지 않습니다.');await checked(work,'before/'+operation.name,{['before/'+operation.name]:expected})}
            else if(expected||!operation.name.startsWith('search/'))fail('삭제 복구 범위가 올바르지 않습니다.');
        }
        // Backups are copied, not consumed, so recovery can itself be restarted.
        for(const operation of [...journal.paths].reverse())if(operation.existed)await copy(await file(work,'before/'+operation.name),root,operation.name);else await remove(root,operation.name);
    }
    await cleanup(root);return true;
}
async function remembered(id,value){
    try{return await new Promise((resolve,reject)=>{const request=indexedDB.open('segio-archive-folders',1);request.onupgradeneeded=()=>request.result.createObjectStore('folders');request.onerror=()=>reject(request.error);request.onblocked=()=>resolve(null);request.onsuccess=()=>{const db=request.result;try{const transaction=db.transaction('folders',value?'readwrite':'readonly'),store=transaction.objectStore('folders'),action=value?store.put(value,id):store.get(id);let result;action.onsuccess=()=>{result=action.result};transaction.oncomplete=()=>{db.close();resolve(value||result)};transaction.onerror=()=>{db.close();reject(transaction.error)}}catch(error){db.close();reject(error)}}})}catch(error){return null}
}
class BrowserEditor {
    static supported(){return isSecureContext&&typeof window.showDirectoryPicker==='function'}
    static folderKey(id){const url=new URL(location.href);url.hash='';url.search='';return id+'|'+url.href}
    static async remembered(id){const handle=await remembered(this.folderKey(id));try{return handle&&await handle.queryPermission({mode:'readwrite'})==='granted'?handle:null}catch(error){return null}}
    static async open(root,config,report=()=>{}){
        const editor=new BrowserEditor(root,config);
        const recovered=await guard(root,()=>recover(root));
        if(recovered)return {recovered:true};
        editor.report=report;await editor.load(false);void remembered(this.folderKey(config.archive.browser_edit_id),root);return editor;
    }
    constructor(root,config){this.root=root;this.config=config;this.plan=null;this.job=null;this.cancelled=false}
    checkpoint(){if(this.cancelled)fail('삭제를 중단하고 기존 자료를 복원했습니다.')}
    progress(stage,completed,total){Object.assign(this.job,{stage,completed,total})}
    async load(withDatabase=true){
        const root=this.root;
        if(location.protocol==='file:'){const folder=decodeURIComponent(new URL(location.href).pathname.split('/').at(-2)||'');if(root.name.toLowerCase()!==folder.toLowerCase())fail('현재 index.html이 들어 있는 백업 폴더를 선택하세요.')}
        if(await exists(root,WORK)||await exists(root,'.segio-edit'))fail('다른 삭제 작업 또는 복구가 진행 중입니다.');
        const report=this.report||(()=>{});report('백업 폴더 확인 중 · 검색 DB는 선택 후 읽습니다.');
        const manifestBlob=await file(root,'manifest.json');this.revision=await digest(manifestBlob);this.manifest=JSON.parse(await manifestBlob.text());await validateManifest(this.manifest,report);
        const index=await checked(root,'index.html',this.manifest.files);this.index=await index.text();
        if(JSON.stringify(indexConfig(this.index).value)!==JSON.stringify(this.config)||this.manifest.browser_edit_id!==this.config.archive.browser_edit_id)fail('현재 화면과 선택한 백업 폴더가 다릅니다. 해당 폴더의 index.html을 새로 열어주세요.');
        if(!withDatabase)return;
        // Only metadata is loaded into memory; raw MBOX bytes are streamed.
        const candidate=await file(root,'catalog.sqlite3');
        if(candidate.size>512*1024*1024)fail('기존 검색 DB가 너무 큽니다. 새 Windows 프로그램에서 보관함 열기로 인덱스를 한 번 갱신한 뒤 이 화면에서 삭제하세요. 본문·메일 원본은 유지됩니다.');
        const sqlite=await checked(root,'catalog.sqlite3',this.manifest.files,()=>{},(n,t)=>report('삭제용 검색 DB 확인 '+(n/1024**2).toFixed(1)+' / '+(t/1024**2).toFixed(1)+' MiB'));
        const SQL=await initSqlJs();this.db=new SQL.Database(new Uint8Array(await sqlite.arrayBuffer()));
    }
    async preview(ids){
        if(!this.db)await this.load();
        if(this.job?.state==='running')fail('삭제 작업이 진행 중입니다.');
        if(!Array.isArray(ids)||!ids.length||ids.length>1000||new Set(ids).size!==ids.length||ids.some(id=>typeof id!=='string'||id.length>150))fail('현재 페이지에서 삭제할 자료를 선택하세요.');
        if(!same(await digest(await file(this.root,'manifest.json')),this.revision))fail('백업본이 변경됐습니다. 새로고침 후 다시 선택하세요.');
        const selected=ids.map(id=>rows(this.db,'SELECT id,kind,title,html FROM items WHERE id=?',[id])[0]);
        if(selected.some(item=>!item||!['approval','mail'].includes(item.kind)))fail('삭제 대상 목록이 일치하지 않습니다.');
        const affected=new Set();for(const item of selected)if(item.kind==='mail'){const record=rows(this.db,'SELECT source FROM mail_records WHERE id=?',[item.id])[0];if(!record||!record.source.startsWith('mail/raw/')||!this.manifest.files[record.source])fail('메일 원본 연결 정보가 올바르지 않습니다.');affected.add(record.source)}
        // External folders do not expose free space. Include original backups,
        // staged replacements and each atomic writer's temporary copy.
        let space=128*1024*1024+this.manifest.files['catalog.sqlite3'].bytes*4;
        for(const name of affected)space+=this.manifest.files[name].bytes*3;
        for(const [name,info] of Object.entries(this.manifest.files))if(name.startsWith('documents/')||name.startsWith('mail/offline/')||name.startsWith('search/')||['catalog.json','index.html','mail/sources.json'].includes(name))space+=info.bytes*3;
        const plan={plan:crypto.randomUUID(),revision:this.revision,ids:[...ids],selected,approval:selected.filter(item=>item.kind==='approval').length,mail:selected.filter(item=>item.kind==='mail').length,titles:selected.map(item=>item.title),temporary_bytes:space,raw_files:affected.size,expires:Date.now()+300000};this.plan=plan;return plan;
    }
    async request(action,data){
        if(action==='preview')return this.preview(data.ids);
        if(action==='start'){
            const plan=this.plan;if(!plan||data.plan!==plan.plan||data.confirmation!=='delete-selected'||Date.now()>plan.expires||this.job?.state==='running')fail('삭제 확인이 만료됐습니다. 대상을 다시 선택하세요.');this.plan=null;this.cancelled=false;
            const identity=crypto.randomUUID();this.job={id:identity,state:'running',stage:'prepare',completed:0,total:0,can_cancel:true,message:'삭제 준비',started:Date.now(),elapsed:0};
            setTimeout(()=>this.run(plan),0);return {job:identity};
        }
        if(action==='cancel'&&this.job?.id===data.job&&this.job.can_cancel){this.cancelled=true;return {ok:true}}
        fail('삭제 요청을 확인하지 못했습니다.');
    }
    status(identity){if(identity!==this.job?.id)fail('삭제 작업을 확인하지 못했습니다.');return {...this.job,elapsed:Math.floor((Date.now()-this.job.started)/1000)}}
    async run(plan){
        try{const result=await guard(this.root,()=>this.apply(plan));Object.assign(this.job,{state:'done',can_cancel:false,message:result.cleanup_pending?'삭제를 완료했습니다. 임시 자료 정리가 남았으므로 인덱스에서 삭제 버튼을 한 번 더 눌러 정리를 완료하세요.':'선택한 자료를 삭제했습니다.',result})}
        catch(error){Object.assign(this.job,{state:'failed',can_cancel:false,message:this.cancelled?'삭제를 중단했습니다.':(error.name==='QuotaExceededError'?'저장 공간이 부족합니다. 여유 공간 확보 후 다시 시도하세요.':error.message||'파일 처리에 실패했습니다.'),recovery_required:await exists(this.root,WORK).catch(()=>true)})}
        finally{if(this.db){this.db.close();this.db=null}}
    }
    async rewrite(work,source,records,deleted,expected){
        const original=await file(this.root,source),hash=sha256.create(),newHash=sha256.create(),offsets=new Map(),frames=[];let boundary=0;
        for(let i=0;i<records.length;i++){
            const row=records[i];if(!Number.isSafeInteger(row.begin)||!Number.isSafeInteger(row.end)||row.begin<boundary||row.end<row.begin||row.end>original.size)fail('메일 원본의 위치 기록이 올바르지 않습니다.');
            if(row.begin-boundary>131072)fail('MBOX 구분 줄이 너무 깁니다.');
            const prefix=new Uint8Array(await original.slice(boundary,row.begin).arrayBuffer());let skip=0;while(skip<prefix.length&&(prefix[skip]===10||prefix[skip]===13))skip++;
            if((i===0&&skip)||prefix.length-skip>65536||decoder.decode(prefix.slice(skip,skip+5))!=='From '||prefix.at(-1)!==10)fail('MBOX 구분 줄과 위치 기록이 일치하지 않습니다.');
            const frame=boundary+skip;if(i)frames[i-1].end=frame;frames.push({row,begin:frame,end:original.size});boundary=row.end;
        }
        if(!frames.length||original.size-boundary>65536||new Uint8Array(await original.slice(boundary).arrayBuffer()).some(byte=>byte!==10&&byte!==13))fail('메일 원본의 마지막 위치가 일치하지 않습니다.');
        const hasKept=records.some(row=>!deleted.has(row.id));let writer=null,newSize=0;
        if(hasKept)writer=await (await fileHandle(work,'new/'+source,true)).createWritable();
        try{
            for(const frame of frames){const keep=!deleted.has(frame.row.id),destination=newSize;
                for(let position=frame.begin;position<frame.end;position+=BLOCK){this.checkpoint();const block=await original.slice(position,Math.min(position+BLOCK,frame.end)).arrayBuffer();hash.update(block);if(keep){await writer.write(block);newHash.update(block);newSize+=block.byteLength}this.progress('mail',Math.min(position+BLOCK,frame.end),original.size)}
                if(keep)offsets.set(frame.row.id,[destination+frame.row.begin-frame.begin,destination+frame.row.end-frame.begin]);
            }
            if(!same({bytes:original.size,sha256:hash.hex()},expected))fail('메일 원본이 변경됐습니다. 삭제를 적용하지 않았습니다.');
            if(writer)await writer.close();
        }catch(error){if(writer)await writer.abort().catch(()=>{});throw error}
        return {offsets,details:{bytes:newSize,sha256:newHash.hex()}};
    }
    async apply(plan){
        this.checkpoint();const root=this.root;
        if(await exists(root,WORK)||!same(await digest(await file(root,'manifest.json')),plan.revision))fail('백업본이 변경됐거나 다른 삭제 작업이 진행 중입니다.');
        const work=await directory(root,WORK,true),journal={protocol:PROTOCOL,phase:'preparing',paths:[],manifest:plan.revision};let installed=false,journalReady=false;
        try{
            await write(work,'journal.json',json(journal));journalReady=true;
            if(await exists(root,'.segio-edit'))fail('Windows 프로그램에서 삭제 처리가 진행 중입니다.');
            const manifest=structuredClone(this.manifest),files=manifest.files,originals=this.manifest.files,deleted=new Set(plan.ids),db=this.db,removeNames=new Set(),replacements=new Set();
            const catalog=originals['catalog.json']?JSON.parse(await (await checked(root,'catalog.json',originals,()=>this.checkpoint())).text()):{items:[]};
            const kept=catalog.items.filter(item=>!deleted.has('approval-'+item.id)),protectedNames=new Set(kept.flatMap(item=>[item.path,item.json,...(item.attachments||[]).map(a=>a.path)]));
            const sources=originals['mail/sources.json']?JSON.parse(await (await checked(root,'mail/sources.json',originals,()=>this.checkpoint())).text()):{sources:[]},raw=new Set(),updated=new Map();
            for(const item of plan.selected){let prefix;
                if(item.kind==='approval'){
                    const record=catalog.items.find(entry=>'approval-'+entry.id===item.id&&entry.path===item.html);if(!record||!/^documents\/(?:b-[0-9a-f]{2}\/)?[^/]+\/[^/]+$/.test(item.html))fail('결재 원본 색인이 올바르지 않습니다.');prefix=item.html.slice(0,item.html.lastIndexOf('/')+1);
                    for(const attachment of record.attachments||[])if(Object.hasOwn(files,attachment.path)&&!protectedNames.has(attachment.path)){if(!/^documents\/(?:b-[0-9a-f]{2}\/)?[^/]+\/attachments\//.test(attachment.path))fail('결재 첨부 경로가 올바르지 않습니다.');removeNames.add(attachment.path)}
                }else{raw.add(rows(db,'SELECT source FROM mail_records WHERE id=?',[item.id])[0].source);prefix=pagePath(item.id).replace(/index\.html$/,'')}
                for(const name of Object.keys(files))if(name.startsWith(prefix)&&!protectedNames.has(name))removeNames.add(name);
            }
            const stage=async(name,payload)=>{this.checkpoint();await write(work,'new/'+name,payload);files[name]=await digest(await file(work,'new/'+name));replacements.add(name)};
            for(const source of raw){this.checkpoint();const entry=sources.sources.find(item=>item.path===source);if(!entry||!same(entry,files[source]))fail('메일 원본 목록이 일치하지 않습니다.');
                const records=rows(db,'SELECT id,source,begin,end FROM mail_records WHERE source=? ORDER BY begin',[source]);
                if(entry.format==='mbox'){
                    const result=await this.rewrite(work,source,records,deleted,files[source]);
                    if(result.offsets.size){updated.set(source,result.details);files[source]=result.details;replacements.add(source);for(const [id,[begin,end]] of result.offsets)db.run('UPDATE mail_records SET begin=?,end=? WHERE id=?',[begin,end,id])}else removeNames.add(source);
                }else if(entry.format==='rfc822'&&records.length===1&&deleted.has(records[0].id)&&records[0].begin===0&&records[0].end===files[source].bytes){await checked(root,source,files,()=>this.checkpoint(),(n,t)=>this.progress('mail',n,t));removeNames.add(source)}else fail('이 메일 형식의 개별 삭제는 지원하지 않습니다.');
            }
            db.run('PRAGMA secure_delete=ON');const tables=new Set(rows(db,"SELECT name FROM sqlite_master WHERE type='table'").map(row=>row.name));
            for(const id of plan.ids){db.run('DELETE FROM items WHERE id=?',[id]);for(const table of ['mail_records','document_records'])if(tables.has(table))db.run('DELETE FROM '+table+' WHERE id=?',[id])}db.run('VACUUM');
            for(const row of rows(db,'SELECT id,source,begin,end FROM mail_records'))if(updated.has(row.source)){this.checkpoint();const name=pagePath(row.id).replace(/index\.html$/,'state.json'),state=JSON.parse(await (await checked(root,name,originals)).text());state.source=['static-mail/1',row.source,row.begin,row.end,updated.get(row.source).sha256];await stage(name,json(state))}
            if(originals['catalog.json'])await stage('catalog.json',json({items:kept}));
            sources.sources=sources.sources.filter(entry=>!removeNames.has(entry.path)).map(entry=>({...entry,...updated.get(entry.path)}));if(originals['mail/sources.json'])await stage('mail/sources.json',json(sources));
            await stage('catalog.sqlite3',db.export());manifest.counts={approval:0,mail:0};for(const row of rows(db,'SELECT kind,count(*) AS count FROM items GROUP BY kind'))manifest.counts[row.kind]=row.count;manifest.portable_mail.count=manifest.counts.mail;
            const edits=manifest.local_edits||{deleted_approval:0,deleted_mail:0};edits.deleted_approval+=plan.approval;edits.deleted_mail+=plan.mail;edits.updated_at=new Date().toISOString();manifest.local_edits=edits;manifest.coverage=manifest.coverage||[];manifest.coverage.push({kind:'local_deletion',approval:plan.approval,mail:plan.mail,time:edits.updated_at});
            const config=structuredClone(this.config);config.counts=manifest.counts;config.archive.local_edits=edits;config.archive.coverage=manifest.coverage;config.chunks=[];config.versions={active:0,history:0};for(const box of [...config.mailboxes,...config.approval_boxes])box.count=0;
            const found=new Set();let chunk=[],count=0;
            const flush=async()=>{if(!chunk.length)return;const name='search/part-'+String(config.chunks.length).padStart(6,'0')+'.js';await stage(name,encoder.encode('window.archiveChunk='+JSON.stringify(chunk).replace(/</g,'\\u003c')+';'));config.chunks.push(name);chunk=[]};
            for(const name of this.config.chunks){this.checkpoint();const text=await (await checked(root,name,originals)).text();if(!text.startsWith('window.archiveChunk=')||!text.endsWith(';'))fail('검색 자료 형식을 확인하지 못했습니다.');removeNames.add(name);
                for(const row of JSON.parse(text.slice(20,-1))){if(deleted.has(row[0]))continue;if(found.has(row[0]))fail('중복된 검색 기록입니다.');found.add(row[0]);chunk.push(row);count++;
                    if(row[1]==='mail'){const box=config.mailboxes.find(box=>box.id===row[6]);if(!box)fail('메일 분류가 일치하지 않습니다.');box.count++}else{if(Object.hasOwn(config.versions,row[8].source))config.versions[row[8].source]++;for(const id of row[8].approval_folders){const box=config.approval_boxes.find(box=>box.id===id);if(box)box.count++}}
                    if(chunk.length===500)await flush();this.progress('index',count,manifest.counts.approval+manifest.counts.mail);
                }
            }await flush();
            if(found.size!==manifest.counts.approval+manifest.counts.mail||rows(db,'SELECT id FROM items').some(row=>!found.has(row.id)))fail('검색 자료와 DB의 목록이 일치하지 않습니다.');
            for(const name of removeNames)if(!replacements.has(name))delete files[name];
            const parsed=indexConfig(this.index);await stage('index.html',encoder.encode(this.index.slice(0,parsed.start)+JSON.stringify(config).replace(/</g,'\\u003c')+this.index.slice(parsed.end)));
            await write(work,'new/manifest.json',json(manifest));replacements.add('manifest.json');
            const operations=[...new Set([...removeNames,...replacements])].filter(name=>name!=='manifest.json').sort();operations.push('manifest.json');
            // Copy all preimages before touching any original. Each copy and
            // replacement is verified, and the manifest is applied last.
            for(let i=0;i<operations.length;i++){
                this.checkpoint();const name=operations[i];safe(name);if(!allowed(name))fail('삭제 적용 범위가 올바르지 않습니다.');const existed=name==='manifest.json'||Object.hasOwn(originals,name);
                if(!existed&&await exists(root,name))fail('새 검색 파일 경로에 기존 파일이 있습니다.');
                let expected=null;if(existed){expected=name==='manifest.json'?plan.revision:originals[name];const source=await checked(root,name,{[name]:expected},()=>this.checkpoint(),(n,t)=>this.progress('files',n,t));await copy(source,work,'before/'+name,()=>this.checkpoint(),(n,t)=>this.progress('files',n,t));await checked(work,'before/'+name,{['before/'+name]:expected},()=>this.checkpoint())}
                journal.paths.push({name,existed,original:expected});
            }
            this.checkpoint();if(!same(await digest(await file(root,'manifest.json')),plan.revision))fail('삭제 준비 중 백업본이 변경됐습니다.');
            // Detect external changes once more after the potentially long copies.
            for(const operation of journal.paths)if(operation.existed)await checked(root,operation.name,{[operation.name]:operation.original},()=>this.checkpoint());
            journal.phase='installing';await write(work,'journal.json',json(journal));this.job.can_cancel=false;
            for(let i=0;i<operations.length;i++){
                const name=operations[i];this.progress('commit',i,operations.length);
                if(replacements.has(name)){const payload=await file(work,'new/'+name);await copy(payload,root,name);const expected=name==='manifest.json'?await digest(payload):files[name];await checked(root,name,{[name]:expected})}else await remove(root,name);
            }
            journal.phase='committed';await write(work,'journal.json',json(journal));installed=true;let pending=false;try{await cleanup(root)}catch(error){pending=true}return {approval:plan.approval,mail:plan.mail,cleanup_pending:pending};
        }catch(error){if(!installed){try{if(journalReady)await recover(root);else await cleanup(root)}catch(recoveryError){fail('삭제 중 오류가 발생했습니다. 폴더를 보존하고 인덱스에서 삭제 버튼을 눌러 자동 복구를 완료하세요.')}}throw error}
    }
}
window.SegioBrowserEditor=BrowserEditor;
})();
