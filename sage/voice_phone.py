"""The 'Talk to Sage' page for the iPhone (and any other phone browser).

Served by the voice gateway at /phone. It records your voice in the browser, sends it to /api/voice/phone/ask, and plays
Sage's spoken reply as it streams in. With "keep listening" on you can have a back-and-forth conversation.

Microphone access in Safari needs HTTPS, so reach it through Tailscale Serve (see VOICE-GUIDE.md). The page asks for the
phone access token (SAGE_PHONE_TOKEN) once and keeps it on the phone.

The helper functions between the <helpers> markers are pure so that tests/test_voice_phone.js can run them in Node.
"""

PAGE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Sage">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#0a101e">
<title>Sage</title>
<style>
:root{--bg:#0a101e;--card:#12203a;--line:#26385a;--text:#ecf2fc;--muted:#8c9dba;--accent:#5492ff;--teal:#34d0ba;--amber:#ffbc4c;--red:#f46868;--green:#4ace82}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
[hidden]{display:none!important}
html,body{margin:0;height:100%;background:radial-gradient(120% 70% at 30% 0,#14264a 0,var(--bg) 60%) fixed;color:var(--text);font:16px -apple-system,system-ui,sans-serif}
main{max-width:560px;margin:0 auto;height:100%;display:flex;flex-direction:column;padding:calc(env(safe-area-inset-top) + 14px) 16px calc(env(safe-area-inset-bottom) + 14px)}
header{display:flex;align-items:center;gap:10px}
header h1{font-size:22px;margin:0;font-weight:700}
#orb{width:22px;height:22px;border-radius:50%;background:radial-gradient(circle at 35% 35%,#bcd6ff,var(--accent) 55%,#1c3a78)}
#sleep{margin-left:auto;font-size:13px;color:var(--muted)}
#log{flex:1;overflow-y:auto;padding:14px 0;display:flex;flex-direction:column;gap:10px}
.b{max-width:86%;padding:10px 14px;border-radius:18px;line-height:1.35;white-space:pre-wrap;word-break:break-word}
.b.you{align-self:flex-end;background:linear-gradient(135deg,#3c74ee,#30a8c8)}
.b.sage{align-self:flex-start;background:var(--card);border:1px solid var(--line)}
#status{text-align:center;color:var(--muted);min-height:22px;margin:6px 0}
#approve{display:flex;gap:10px;margin:6px 0 10px}
#approve button{flex:1;padding:14px;border-radius:14px;border:0;font-size:17px;font-weight:600;color:#fff}
#yes{background:#1e7652}#no{background:#783038}
#mic{align-self:center;width:112px;height:112px;border-radius:50%;border:0;color:#fff;font-size:40px;background:linear-gradient(135deg,#3a70ec,#2ebcbe);box-shadow:0 8px 28px rgba(40,120,220,.45)}
body[data-mode=listen] #mic{animation:p 1.6s ease-in-out infinite}
body[data-mode=rec] #mic{background:linear-gradient(135deg,#e55a5a,#ff9f4c);animation:p .8s ease-in-out infinite}
body[data-mode=busy] #mic{background:linear-gradient(135deg,#8a6a1c,#ffbc4c)}
body[data-mode=speak] #mic{background:linear-gradient(135deg,#1e8a5a,#4ace82)}
@keyframes p{50%{transform:scale(1.08)}}
label{display:flex;gap:8px;align-items:center;justify-content:center;color:var(--muted);font-size:14px;margin-top:14px}
details{margin-top:10px;color:var(--muted);font-size:14px}
details input{width:100%;padding:11px;border-radius:12px;border:1px solid var(--line);background:#0d1830;color:var(--text);font-size:16px;margin:8px 0}
details button{padding:10px 16px;border-radius:12px;border:0;background:var(--accent);color:#fff;font-size:15px}
</style>
</head>
<body data-mode="idle">
<main>
  <header><div id="orb"></div><h1>Sage</h1><span id="sleep" hidden>sleep mode</span></header>
  <section id="log"></section>
  <div id="approve" hidden><button id="no">Cancel</button><button id="yes">Approve</button></div>
  <div id="status">Tap the microphone to talk</div>
  <button id="mic" aria-label="Talk to Sage">&#127908;</button>
  <label><input type="checkbox" id="keep" checked> Keep listening after each answer</label>
  <details id="setup"><summary>Access</summary>
    <input id="token" type="password" placeholder="Phone access token" autocomplete="off">
    <button id="save">Save</button>
  </details>
</main>
<script>
(() => {
'use strict';
const RATE = 16000;
// <helpers>
function concat(chunks){let n=0;for(const c of chunks)n+=c.length;const out=new Float32Array(n);let o=0;for(const c of chunks){out.set(c,o);o+=c.length;}return out;}
function rmsOf(f){let s=0;for(let i=0;i<f.length;i++)s+=f[i]*f[i];return Math.sqrt(s/Math.max(1,f.length));}
function makeResampler(inRate,outRate){
  const step=inRate/outRate;let pos=0,prev=0,has=false;
  return function(inp){
    if(!inp.length)return new Float32Array(0);
    const x=has?concat([Float32Array.of(prev),inp]):inp;
    const out=[];let p=pos;
    while(p<x.length-1){const j=Math.floor(p),f=p-j;out.push(x[j]+(x[j+1]-x[j])*f);p+=step;}
    pos=p-(x.length-1);prev=x[x.length-1];has=true;
    return Float32Array.from(out);
  };
}
function toPcm16(f){const out=new Int16Array(f.length);for(let i=0;i<f.length;i++){const s=Math.max(-1,Math.min(1,f[i]));out[i]=s<0?s*32768:s*32767;}return out;}
function pcmBytesToFloat(bytes,carry){
  let buf=bytes;
  if(carry&&carry.length){buf=new Uint8Array(carry.length+bytes.length);buf.set(carry,0);buf.set(bytes,carry.length);}
  const usable=buf.length-(buf.length%2);
  const view=new DataView(buf.buffer,buf.byteOffset,usable);
  const out=new Float32Array(usable/2);
  for(let i=0;i<out.length;i++)out[i]=view.getInt16(i*2,true)/32768;
  return {samples:out,carry:buf.slice(usable)};
}
// </helpers>

const $=id=>document.getElementById(id);
const els={log:$('log'),status:$('status'),mic:$('mic'),keep:$('keep'),approve:$('approve'),yes:$('yes'),no:$('no'),token:$('token'),save:$('save'),setup:$('setup'),sleep:$('sleep')};
let token='';try{token=localStorage.getItem('sagePhoneToken')||'';}catch(e){}
let ctx=null,stream=null,proc=null,wake=null,abort=null,mode='idle';
let pre=[],rec=[],speechMs=0,silenceMs=0,totalMs=0,noise=0.01,playing=[];

const dec=s=>{try{return decodeURIComponent(s||'');}catch(e){return s||'';}};
function setMode(m){mode=m;document.body.dataset.mode=m;}
function setStatus(t){els.status.textContent=t;}
function bubble(who,text){const d=document.createElement('div');d.className='b '+who;d.textContent=text;els.log.appendChild(d);els.log.scrollTop=els.log.scrollHeight;}
function authHeaders(extra){return Object.assign({'Authorization':'Bearer '+token},extra||{});}

function releaseMic(){
  if(proc){try{proc.disconnect();}catch(e){}proc=null;}
  if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}
  if(wake){wake.release().catch(()=>{});wake=null;}
}
function stopPlayback(){for(const s of playing){try{s.stop();}catch(e){}}playing=[];}
function stopAll(message){
  if(abort){abort.abort();abort=null;}
  stopPlayback();releaseMic();setMode('idle');els.approve.hidden=true;
  setStatus(message||'Tap the microphone to talk');
}
function listen(){pre=[];rec=[];speechMs=0;silenceMs=0;totalMs=0;setMode('listen');setStatus('Listening\u2026 speak now');}

async function start(){
  if(!token){els.setup.open=true;setStatus('Enter the access token first');return;}
  try{
    ctx=ctx||new (window.AudioContext||window.webkitAudioContext)();
    const resumed=ctx.resume();
    if(!stream){
      stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1}});
      const src=ctx.createMediaStreamSource(stream);
      proc=ctx.createScriptProcessor(4096,1,1);
      const mute=ctx.createGain();mute.gain.value=0;
      proc.onaudioprocess=e=>onAudio(e.inputBuffer.getChannelData(0));
      src.connect(proc);proc.connect(mute);mute.connect(ctx.destination);
    }
    await resumed;
  }catch(err){
    setStatus('Microphone not available ('+(err.message||err)+'). Safari needs HTTPS for this page.');
    return;
  }
  try{if('wakeLock' in navigator)wake=await navigator.wakeLock.request('screen');}catch(e){}
  listen();
}

function onAudio(input){
  if(mode!=='listen'&&mode!=='rec')return;
  const b=Float32Array.from(input),level=rmsOf(b),ms=b.length/ctx.sampleRate*1000;
  if(mode==='listen'){
    if(level<noise*3)noise=noise*0.95+level*0.05;
    pre.push(b);if(pre.length>4)pre.shift();
    if(level>Math.max(0.025,noise*3)){mode='rec';document.body.dataset.mode='rec';rec=pre.slice();pre=[];speechMs=ms;silenceMs=0;totalMs=ms;setStatus('Listening\u2026');}
  }else{
    rec.push(b);totalMs+=ms;
    if(level>Math.max(0.018,noise*2.2)){speechMs+=ms;silenceMs=0;}else silenceMs+=ms;
    if(silenceMs>1100||totalMs>28000)finish();
  }
}

function finish(){
  const audio=concat(rec);rec=[];
  if(speechMs<350){setStatus("I didn't catch that");return again(true);}
  setMode('busy');setStatus('Thinking\u2026');
  const pcm=toPcm16(makeResampler(ctx.sampleRate,RATE)(audio));
  send('/api/voice/phone/ask',pcm.buffer,'application/octet-stream');
}

async function playStream(res){
  const reader=res.body.getReader();
  const up=makeResampler(RATE,ctx.sampleRate);
  let carry=null,t=0,last=null;
  for(;;){
    const {done,value}=await reader.read();
    if(done)break;
    const r=pcmBytesToFloat(value,carry);carry=r.carry;
    const samples=up(r.samples);
    if(!samples.length)continue;
    const buf=ctx.createBuffer(1,samples.length,ctx.sampleRate);
    buf.copyToChannel(samples,0);
    const src=ctx.createBufferSource();src.buffer=buf;src.connect(ctx.destination);
    const startAt=Math.max(t,ctx.currentTime+0.03);src.start(startAt);t=startAt+buf.duration;
    playing.push(src);last=src;
  }
  await new Promise(done=>{if(!last)return done();last.onended=done;});
  playing=[];
}

function refused(res){
  if(res.status===401||res.status===403){token='';try{localStorage.removeItem('sagePhoneToken');}catch(e){}els.setup.open=true;stopAll('The access token was refused');return true;}
  if(res.status===503){stopAll('Phone access is off on the Sage PC (set SAGE_PHONE_TOKEN)');return true;}
  if(res.status===429){stopAll('Too many wrong tries. Wait a few minutes.');return true;}
  if(!res.ok&&res.status!==204){stopAll('Sage error '+res.status);return true;}
  return false;
}

async function send(path,body,type){
  abort=new AbortController();
  let res;
  try{res=await fetch(path,{method:'POST',headers:authHeaders({'Content-Type':type}),body,signal:abort.signal});}
  catch(err){if(err.name!=='AbortError')stopAll('Cannot reach Sage');return;}
  if(res.status===204){setStatus(path.endsWith('button')?'Nothing is waiting for approval':"I didn't catch that");els.approve.hidden=true;return again(false);}
  if(refused(res))return;
  const heard=dec(res.headers.get('X-Sage-Heard')),reply=dec(res.headers.get('X-Sage-Reply'));
  if(heard)bubble('you',heard);
  if(reply)bubble('sage',reply);
  els.sleep.hidden=res.headers.get('X-Sage-Asleep')!=='1';
  setMode('speak');setStatus('Speaking\u2026');
  try{await playStream(res);}catch(err){if(err.name==='AbortError')return;}
  const needsApproval=res.headers.get('X-Sage-Button')==='1';
  els.approve.hidden=!needsApproval;
  if(needsApproval){stopAll('Waiting for your approval');els.approve.hidden=false;return;}
  again(res.headers.get('X-Sage-Listen')==='1');
}

function again(follow){
  if(els.keep.checked||follow)return stream?listen():start();
  stopAll();
}

els.mic.addEventListener('click',()=>{if(mode==='idle')start();else stopAll();});
els.yes.addEventListener('click',async()=>{if(!ctx)return;try{await ctx.resume();}catch(e){}els.approve.hidden=true;setMode('busy');setStatus('Approving\u2026');send('/api/voice/phone/button',JSON.stringify({press:'short'}),'application/json');});
els.no.addEventListener('click',async()=>{if(!ctx)return;try{await ctx.resume();}catch(e){}els.approve.hidden=true;setMode('busy');setStatus('Cancelling\u2026');send('/api/voice/phone/button',JSON.stringify({press:'long'}),'application/json');});
els.save.addEventListener('click',()=>{token=els.token.value.trim();els.token.value='';try{localStorage.setItem('sagePhoneToken',token);}catch(e){}els.setup.open=false;setStatus(token?'Saved. Tap the microphone to talk':'Enter the access token first');});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&mode!=='idle')stopAll('Paused while the page was in the background');});
if(!token)els.setup.open=true;
})();
</script>
</body>
</html>
'''
