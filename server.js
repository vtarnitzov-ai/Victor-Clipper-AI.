'use strict';
// Victor Clipper AI hosted service: paste a YouTube URL, get ready-to-use Shorts.
// Does not require any local installation for site visitors.
const http = require('node:http');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const readline = require('node:readline');
const PORT = Number(process.env.PORT || 3000);
const TMP = path.join(os.tmpdir(),'victor-clipper-ai');
const MAX_BYTES = 350*1024*1024;
const jobs = new Map();
const rate = new Map();
let active = null;
function sendJSON(res,code,value) {
 const b=Buffer.from(JSON.stringify(value));
 res.writeHead(code, {'Content-Type':'application/json; charset=utf-8','Content-Length':b.length,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(b);
}
function youtubeId(value) {
 if(typeof value!=='string'||value.length>1500)return null;
 try {
  const u=new URL(value);
  if(!['https:','http:'].includes(u.protocol)||u.username||u.password||u.port)return null;
  const h=u.hostname.toLowerCase();let id=null;
  if(['youtu.be','www.youtu.be'].includes(h))id=u.pathname.match(/^\/([\w-]{11})\/?$/)?.[1];
  if(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(h)){
   if(u.pathname==='/watch')id=u.searchParams.get('v');
   else id=u.pathname.match(/^\/(?:shorts|live|embed)\/([\w-]{11})\/?$/)?.[1];
  }
  return /^[A-Za-z0-9_-]{11}$/.test(id||'')?id:null;
 } catch {return null;}
}
function tooMany(ip) {
 const now=Date.now();const v=(rate.get(ip)||[]).filter(t=>now-t<3600000);
 if(v.length>=3)return true;
 v.push(now);rate.set(ip,v);return false;
}
async function requestJSON(req) {
 return new Promise((resolve,reject)=>{
  const body=[];let len=0;
  req.on('data',chunk=>{len+=chunk.length;if(len>8192){reject(new Error('Too large'));req.destroy();return;}body.push(chunk)});
  req.on('end',()=>{try{resolve(JSON.parse(Buffer.concat(body).toString('utf8')))}catch{reject(new Error('Invalid JSON'))}});
  req.on('error',reject);
 });
}
function run(bin,args,job,stage,base,top,timeout=600000) {
 return new Promise((resolve,reject)=>{
  const proc=spawn(bin,args,{stdio:['ignore','pipe','pipe'],env:{...process.env, PYTHONUNBUFFERED:'1'}});
  job.child=proc;let errorTail='';
  const timer=setTimeout(()=>proc.kill('SIGKILL'),timeout);
  const output=readline.createInterface({input:proc.stdout});
  output.on('line',line=>{
   if(line.startsWith('STATUS:')){
    try {const s=JSON.parse(line.slice(7));job.stage=s.stage;job.progress=s.progress;job.message=s.message;} catch{}
   } else if(line.startsWith('ERROR:')) errorTail=(errorTail+'\n'+line).slice(-2000);
  });
  proc.stderr.on('data',chunk=>{errorTail=(errorTail+'\n'+chunk).slice(-3000)});
  proc.on('error',err=>{clearTimeout(timer);reject(err)});
  proc.on('close',code=>{
   clearTimeout(timer);job.child=null;
   if(code===0)resolve();else reject(Error(/Sign in|not a bot|HTTP Error 403|HTTP Error 429|bot detection/i.test(errorTail) ?
    'YouTube ограничи достъпа до видеото. Опитай с видео, което имаш право да използваш.' :
    (stage==='download'?'YouTube видеото е недостъпно или ограничено.':'AI обработката не завърши. Пробвай по-кратко видео.')));
  });
 });
}
async function work(job) {
 active=job.id;
 try {
  await fsp.mkdir(job.directory,{recursive:true});
  job.stage='download';job.progress=5;job.message='Зареждам видеото от YouTube…';
  const args=['--no-playlist','--no-warnings','--no-progress','--no-part','--js-runtimes','node',
   '--format','bv*[ext=mp4][height<=720]+ba[ext=m4a]/b[ext=mp4][height<=720]/best[height<=720]',
   '--merge-output-format','mp4','--match-filter','duration <= 3600','--max-filesize','350M',
   '--socket-timeout','30','--retries','2','--fragment-retries','2',
   '--paths',job.directory,'--output','source.%(ext)s',
   'https://www.youtube.com/watch?v='+job.videoId];
  await run(process.env.YTDLP_BIN||'yt-dlp',args,job,'download',0,20,9*60*1000);
  const files=(await fsp.readdir(job.directory)).filter(n=>/^source\.(mp4|mkv|webm|mov)$/.test(n));
  if(!files.length)throw Error('Не е получен видеофайл от YouTube.');
  const sizes=await Promise.all(files.map(async n=>({n,stat:await fsp.stat(path.join(job.directory,n))})));
  sizes.sort((a,b)=>b.stat.size-a.stat.size);
  if(sizes[0].stat.size>MAX_BYTES)throw Error('Видеото е твърде голямо (максимум 350 MB).');
  const source=path.join(job.directory,sizes[0].n);
  job.stage='audio';job.progress=22;job.message='Разпознавам речта и намирам силните моменти…';
  await run(process.env.PYTHON_BIN||'python3',['clip_engine.py','--input',source,'--out',job.directory,'--count',String(job.count),'--length',String(job.length)],job,'process',20,100,50*60*1000);
  const result=JSON.parse(await fsp.readFile(path.join(job.directory,'result.json'),'utf8'));
  if(!result.clips?.length)throw Error('Не са намерени моменти за обработка.');
  job.result=result;job.stage='ready';job.progress=100;job.message='Готово! Свали Shorts с вградени субтитри.';job.finished=Date.now();
 } catch(e) {
  job.stage='error';job.message=e.message||'Неуспешна обработка.';job.finished=Date.now();
 } finally {active=null;job.child=null;}
}
async function serveOutput(req,res,job,name) {
 if(job.stage!=='ready')return sendJSON(res,409,{error:'Shorts все още не са готови.'});
 const allow = new Set(['Victor_Clipper_Shorts.zip',...job.result.clips.flatMap(c=>[c.video,c.subtitles])]);
 if(!allow.has(name))return sendJSON(res,404,{error:'Няма такъв файл.'});
 const filepath=path.join(job.directory,name);let stat;
 try {stat=await fsp.stat(filepath)} catch{return sendJSON(res,410,{error:'Файлът е изтекъл.'});}
 const isVideo=name.endsWith('.mp4'),isSrt=name.endsWith('.srt');
 const type=isVideo?'video/mp4':isSrt?'application/x-subrip; charset=utf-8':'application/zip';
 const headers={'Content-Type':type,'Accept-Ranges':'bytes','Cache-Control':'private, no-store','Content-Disposition':`${isVideo?'inline':'attachment'}; filename="${name}"`,'X-Content-Type-Options':'nosniff'};
 const range=req.headers.range;const match=range?.match(/^bytes=(\d+)-(\d*)$/);
 if(match){const start=Number(match[1]),end=match[2]?Number(match[2]):stat.size-1;
  if(start>=stat.size||end<start||end>=stat.size){res.writeHead(416,{'Content-Range':`bytes */${stat.size}`});return res.end()}
  res.writeHead(206,{...headers,'Content-Range':`bytes ${start}-${end}/${stat.size}`,'Content-Length':end-start+1});
  return fs.createReadStream(filepath,{start,end}).pipe(res);
 }
 res.writeHead(200,{...headers,'Content-Length':stat.size});if(req.method==='HEAD')return res.end();
 return fs.createReadStream(filepath).pipe(res);
}
async function handler(req,res) {
 let u;try{u=new URL(req.url,'http://localhost')}catch{return sendJSON(res,400,{error:'Bad URL'})}
 const name=u.pathname;
 if(req.method==='GET'&&name==='/health')return sendJSON(res,200,{ok:true,active:!!active,brand:'Victor Clipper AI'});
 if(req.method==='POST'&&name==='/api/jobs'){
  if(active)return sendJSON(res,429,{error:'Друг клип се обработва. Пробвай отново, след като приключи.'});
  const body=await requestJSON(req).catch(()=>null);const videoId=youtubeId(body?.url);
  if(!videoId)return sendJSON(res,400,{error:'Въведи валиден линк към YouTube видео.'});
  if(body?.rightsConfirmed!==true)return sendJSON(res,400,{error:'Трябва да имаш права за използване на видеото.'});
  const count=Number(body?.count),length=Number(body?.length);
  if(![1,2,3,5].includes(count)||![20,30,45,60].includes(length))return sendJSON(res,400,{error:'Невалидни настройки.'});
  if(tooMany(req.socket.remoteAddress||'anonymous'))return sendJSON(res,429,{error:'Лимит: 3 видеа на час.'});
  const id=crypto.randomUUID();const job={id,videoId,count,length,stage:'queued',progress:2,message:'Подготвям задачата…',directory:path.join(TMP,id),child:null,finished:null,result:null};
  jobs.set(id,job);work(job).catch(e=>{job.stage='error';job.message=e.message||'Грешка';job.finished=Date.now();active=null});
  return sendJSON(res,202,{jobId:id});
 }
 const m=name.match(/^\/api\/jobs\/([a-f0-9-]{36})(?:\/files\/([a-zA-Z0-9_.-]+))?$/);
 if(['GET','HEAD'].includes(req.method)&&m){const job=jobs.get(m[1]);if(!job)return sendJSON(res,404,{error:'Задачата не съществува или е изтекла.'});
  if(m[2])return serveOutput(req,res,job,m[2]);
  return sendJSON(res,200,{id:job.id,stage:job.stage,progress:job.progress,message:job.message,result:job.stage==='ready'?job.result:null});
 }
 if(name==='/api/jobs'||name.startsWith('/api/'))return sendJSON(res,404,{error:'Неизвестен API адрес'});
 if(['GET','HEAD'].includes(req.method)&&(name==='/'||name==='/index.html')){
  const f=path.join(__dirname,'public','index.html');const st=await fsp.stat(f);
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Content-Length':st.size,'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff'});
  if(req.method==='HEAD')return res.end();return fs.createReadStream(f).pipe(res);
 }
 sendJSON(res,404,{error:'Not found'});
}
setInterval(async()=>{const now=Date.now();for(const [id,job] of jobs){if(job.finished&&now-job.finished>40*60*1000){jobs.delete(id);await fsp.rm(job.directory,{recursive:true,force:true}).catch(()=>{})}}for(const [ip,times]of rate){if(times.every(t=>now-t>3600000))rate.delete(ip)}},60000).unref();
if(require.main===module)http.createServer((req,res)=>handler(req,res).catch(e=>sendJSON(res,500,{error:'Internal error'}))).listen(PORT,'0.0.0.0',()=>console.log(`Victor Clipper AI started on port ${PORT}`));
module.exports={handler,youtubeId};
