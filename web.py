"""天网 · Web 版：让没有构建链的电脑也能用。

## 为什么需要它

本机（笔记本）有完整构建链，能打出 exe 双击就用；
**另一台电脑没有构建链**，只能起浏览器版。所以推理与语音全在这台机器上，
那台电脑只当一个浏览器客户端——局域网内打开 http://<本机IP>:8765 即可。

## 分工

* **本机**：llama-server（脑） + 本文件（口：ASR/TTS 转接口）+ 角色卡系统
* **那台电脑**：浏览器（耳：浏览器 Web Speech API 或上传录音；眼：界面）

## 接口

| 路由 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 聊天界面（单文件 HTML，内嵌） |
| `/api/cards` | GET | 所有可用角色卡 |
| `/api/card` | POST | 切卡 `{"id": "whale_girl"}` |
| `/api/chat` | POST | 说话 `{"text": "...", "card": null}` -> `{"say","ms","degraded"}` |
| `/api/voice/say` | POST | TTS `{"text":"..."}` -> 音频（wav/mp3） |
| `/api/voice/recognize` | POST | ASR，上传 wav -> `{"text"}` |
| `/api/health` | GET | 各项能力自检 |

纯 stdlib，零框架依赖。
"""
import io
import json
import os
import subprocess
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dialogue            # noqa: E402
import persona_card        # noqa: E402

try:
    import voice            # noqa: E402
except Exception:
    voice = None

try:
    import journal          # noqa: E402
except Exception:
    journal = None

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except Exception:
    raise

HOST = os.environ.get("FLEET_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("FLEET_WEB_PORT", "8765"))

# 鲸鱼娘那套界面：深色、圆角、状态灯
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f1319">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>天网</title>
<style>
:root{--bg:#0f1319;--panel:#171d28;--line:#252d3c;--fg:#e8ecf1;--dim:#8b97a8;
--accent:#4a9eff;--ok:#1e8449;--warn:#b8860b;--err:#c0392b;--rec:#c0392b}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:"Microsoft YaHei UI",-apple-system,"PingFang SC",sans-serif;
height:100dvh;display:flex;flex-direction:column;overscroll-behavior:none}
header{display:flex;align-items:center;gap:10px;padding:calc(8px + env(safe-area-inset-top)) 14px 8px;
border-bottom:1px solid var(--line);background:var(--panel);flex:none}
header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:1px;flex:none}
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:8px 10px;font-size:14px;flex:1;min-width:0;
max-width:190px;font-family:inherit}
#dot{width:11px;height:11px;border-radius:50%;background:var(--dim);flex:none;
transition:background .2s}
#st{font-size:12px;color:var(--dim);min-width:44px;flex:none}
#hf{margin-left:auto;flex:none;min-height:38px;padding:0 12px;font-size:13px;
border-radius:19px}
#hf.on{background:var(--ok);border-color:var(--ok);color:#fff;font-weight:600}
#log{flex:1;overflow-y:auto;padding:16px 14px;display:flex;flex-direction:column;
gap:10px;-webkit-overflow-scrolling:touch}
.msg{max-width:84%;padding:10px 13px;border-radius:14px;line-height:1.65;
font-size:15px;white-space:pre-wrap;word-break:break-word}
.me{align-self:flex-end;background:var(--accent);color:#06101f}
.her{align-self:flex-start;background:var(--panel);border:1px solid var(--line)}
.sys{align-self:center;font-size:12.5px;color:var(--dim);text-align:center;
max-width:92%;line-height:1.5}
.err{align-self:center;font-size:12.5px;color:#ff8b7e;text-align:center;
max-width:92%;line-height:1.5;background:rgba(192,57,43,.12);
border:1px solid rgba(192,57,43,.35);border-radius:10px;padding:8px 10px}
footer{border-top:1px solid var(--line);background:var(--panel);flex:none;
padding:10px 12px calc(10px + env(safe-area-inset-bottom));
display:flex;gap:8px;align-items:flex-end}
textarea{flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:12px;padding:12px;font-size:16px;resize:none;height:48px;
max-height:120px;font-family:inherit;outline:none;line-height:1.4}
textarea:focus{border-color:var(--accent)}
button{background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:12px;padding:0 14px;font-size:15px;cursor:pointer;font-family:inherit;
min-height:48px;min-width:48px;transition:border-color .15s,background .15s}
button:active{transform:scale(.97)}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:var(--accent);color:#06101f;border-color:var(--accent);
font-weight:600}
button.rec{background:var(--rec);color:#fff;border-color:var(--rec);
animation:pulse 1.2s ease-in-out infinite}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(192,57,43,.55)}
50%{box-shadow:0 0 0 12px rgba(192,57,43,0)}}
#lvl{position:absolute;left:0;right:0;bottom:0;height:3px;background:transparent;
overflow:hidden;pointer-events:none}
#lvl i{display:block;height:100%;width:0;background:var(--rec);
transition:width .08s linear}
#bar{position:relative}
</style>
</head>
<body>
<header>
  <h1>天网</h1>
  <select id="cards"></select>
  <span id="dot"></span><span id="st">空闲</span>
  <button id="hf" title="打开后不用手，说完自动接下一轮">免手</button>
</header>
<div id="log"></div>
<div id="bar"><div id="lvl"><i></i></div></div>
<footer>
  <button id="mic" title="点一下开始说，再点一下结束">🎤</button>
  <textarea id="in" rows="1" placeholder="说点什么…"></textarea>
  <button id="send" class="primary">发送</button>
</footer>
<script>
const $=s=>document.querySelector(s);
const log=$('#log'), inp=$('#in'), mic=$('#mic'), hfBtn=$('#hf'), lvl=$('#lvl i');
let busy=false, handFree=false, recOn=false, wantStop=false;

function add(cls,text){
  const d=document.createElement('div');
  d.className='msg '+cls; d.textContent=text;
  log.appendChild(d); log.scrollTop=log.scrollHeight;
  return d;
}
function state(s,color){
  $('#st').textContent=s;
  $('#dot').style.background=color||'#8b97a8';
}
function reason(e){
  return (e&&e.message)?e.message:String(e);
}

/* ---------- 能力自检：先说清楚为什么不能用 ---------- */
function micBlockReason(){
  if(!window.isSecureContext){
    return '当前地址不是安全上下文（' + location.origin + '），浏览器一律拒绝麦克风。'
      + '请改用 http://localhost:8765（电脑上跑 adb reverse）或 https:// 访问。';
  }
  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){
    return '这个浏览器没有提供麦克风接口（getUserMedia）。请用 Chrome / Edge / 系统浏览器。';
  }
  return '';
}

/* ---------- 录音：PCM → 16k → WAV ---------- */
let ac=null, micStream=null, proc=null, zeroGain=null;
let pcmChunks=[], totalMs=0, silentMs=0, voiceSeen=false;

const SILENCE_RMS = 0.012;     /* 低于这个算安静 */
const SILENCE_MS  = 1200;      /* 连续安静这么久就自动收 */
const MIN_MS      = 700;       /* 至少录这么久才允许自动收 */
const NO_VOICE_MS = 7000;      /* 一直没人说话，录这么久就放弃 */
const MAX_MS      = 30000;     /* 单轮上限 */

async function openMic(){
  if(micStream) return;
  micStream = await navigator.mediaDevices.getUserMedia({
    audio:{channelCount:1, echoCancellation:true, noiseSuppression:true,
           autoGainControl:true}
  });
}

async function startRec(){
  if(recOn) return;
  const why = micBlockReason();
  if(why){ add('err', why); return false; }
  try{
    await openMic();
  }catch(e){
    add('err','拿不到麦克风：'+reason(e)+'（检查浏览器地址栏的权限图标）');
    return false;
  }
  try{
    ac = new (window.AudioContext||window.webkitAudioContext)();
    if(ac.state==='suspended') await ac.resume();
    const src = ac.createMediaStreamSource(micStream);
    proc = ac.createScriptProcessor(4096,1,1);
    pcmChunks=[]; totalMs=0; silentMs=0; voiceSeen=false; wantStop=false;
    proc.onaudioprocess = e=>{
      const d = e.inputBuffer.getChannelData(0);
      pcmChunks.push(new Float32Array(d));
      const frameMs = (d.length/ac.sampleRate)*1000;
      totalMs += frameMs;
      let sum=0;
      for(let i=0;i<d.length;i++) sum += d[i]*d[i];
      const rms = Math.sqrt(sum/d.length);
      lvl.style.width = Math.min(100, rms*420) + '%';
      if(rms > SILENCE_RMS){ silentMs = 0; voiceSeen = true; }
      else { silentMs += frameMs; }
      if(recOn && !wantStop && totalMs > MIN_MS){
        if(voiceSeen && silentMs > SILENCE_MS){ wantStop = true; stopRec(); }
        else if(!voiceSeen && totalMs > NO_VOICE_MS){ wantStop = true; stopRec(); }
        else if(totalMs > MAX_MS){ wantStop = true; stopRec(); }
      }
    };
    /* gain=0 再进 destination：不接会收不到 onaudioprocess，
       直连会把自己的声音放出来（自激） */
    zeroGain = ac.createGain();
    zeroGain.gain.value = 0;
    src.connect(proc); proc.connect(zeroGain); zeroGain.connect(ac.destination);
    recOn = true; mic.classList.add('rec');
    state('聆听', '#c0392b');
    return true;
  }catch(e){
    add('err','录音启动失败：'+reason(e));
    return false;
  }
}

function closeRec(){
  recOn = false; mic.classList.remove('rec'); lvl.style.width='0%';
  try{ if(proc){ proc.disconnect(); proc.onaudioprocess=null; proc=null; } }catch(e){}
  try{ if(zeroGain){ zeroGain.disconnect(); zeroGain=null; } }catch(e){}
  try{ if(ac){ ac.close(); ac=null; } }catch(e){}
  try{ if(micStream){ micStream.getTracks().forEach(t=>t.stop()); micStream=null; } }catch(e){}
}

/* 采样率必须在 closeRec 之前取下来（ac 关掉后就读不到了） */
let lastSr = 48000;

function downsample(buf, from, to){
  if(from === to) return buf;
  const ratio = from/to;
  const n = Math.round(buf.length/ratio);
  const out = new Float32Array(n);
  for(let i=0;i<n;i++){
    const s0 = Math.floor(i*ratio);
    const s1 = Math.min(Math.floor((i+1)*ratio), buf.length);
    let sum=0;
    for(let j=s0;j<s1;j++) sum += buf[j];
    out[i] = sum/Math.max(1, s1-s0);
  }
  return out;
}

function encodeWav(samples, sampleRate){
  const buf = new ArrayBuffer(44 + samples.length*2);
  const v = new DataView(buf);
  const ws=(o,s)=>{ for(let i=0;i<s.length;i++) v.setUint8(o+i, s.charCodeAt(i)); };
  ws(0,'RIFF'); v.setUint32(4, 36+samples.length*2, true); ws(8,'WAVE');
  ws(12,'fmt '); v.setUint32(16,16,true); v.setUint16(20,1,true);
  v.setUint16(22,1,true); v.setUint32(24,sampleRate,true);
  v.setUint32(28,sampleRate*2,true); v.setUint16(32,2,true); v.setUint16(34,16,true);
  ws(36,'data'); v.setUint32(40, samples.length*2, true);
  let o=44;
  for(let i=0;i<samples.length;i++){
    let s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s<0 ? s*0x8000 : s*0x7FFF, true);
    o += 2;
  }
  return new Blob([buf], {type:'audio/wav'});
}

/* ---------- 上传识别 ---------- */
async function recognize(blob){
  const r = await fetch('/api/voice/recognize', {
    method:'POST', headers:{'Content-Type':'application/octet-stream'},
    body:blob
  });
  const j = await r.json().catch(()=>({error:'返回不是 JSON（HTTP '+r.status+'）'}));
  if(!r.ok || j.error) throw new Error(j.error || ('HTTP '+r.status));
  return (j.text||'').trim();
}

/* ---------- 说话（卡片音色优先，失败回落系统音色） ---------- */
let audioEl = null;
async function say(text){
  if(!text) return;
  state('说话', '#1e8449');
  try{
    const r = await fetch('/api/voice/say', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text, card: $('#cards').value || null})
    });
    if(!r.ok){
      let msg = 'HTTP '+r.status;
      try{ const j = await r.json(); if(j.error) msg = j.error; }catch(e){}
      throw new Error(msg);
    }
    const b = await r.blob();
    if(!b.size) throw new Error('返回空音频');
    const url = URL.createObjectURL(b);
    if(audioEl){ try{ audioEl.pause(); }catch(e){} }
    audioEl = new Audio(url);
    audioEl.playsInline = true;
    await new Promise((res, rej)=>{
      audioEl.onended = ()=>{ URL.revokeObjectURL(url); res(); };
      audioEl.onerror = ()=>{ URL.revokeObjectURL(url); rej(new Error('音频解码/播放失败')); };
      audioEl.play().catch(rej);
    });
  }catch(e){
    add('sys','桌面语音合成没成功（'+reason(e)+'），先用手机自带音色念。');
    await sysSpeak(text);
  }finally{
    state('空闲');
  }
}

function sysSpeak(text){
  return new Promise(res=>{
    try{
      if(!('speechSynthesis' in window)) return res();
      const u = new SpeechSynthesisUtterance(text);
      u.lang='zh-CN'; u.onend=res; u.onerror=res;
      speechSynthesis.speak(u);
    }catch(e){ res(); }
  });
}

/* ---------- 对话 ---------- */
async function chat(text){
  const card = $('#cards').value;
  const r = await fetch('/api/chat', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text, card})
  });
  const j = await r.json().catch(()=>({error:'返回不是 JSON（HTTP '+r.status+'）'}));
  if(!r.ok || j.error) throw new Error(j.error || ('HTTP '+r.status));
  return j;
}

async function round(text){
  add('me', text);
  busy = true; state('思考', '#b8860b');
  let j;
  try{
    j = await chat(text);
  }catch(e){
    add('err','她没接上：'+reason(e));
    busy = false; return;
  }
  busy = false;
  add('her', j.say || '（没听清）');
  if(j.degraded && j.degraded.length) add('sys','降级：'+j.degraded.join('、'));
  await say(j.say || '');
}

/* ---------- 免手循环：录 → 停 → 传 → 答 → 播完 → 再录 ---------- */
async function handFreeLoop(){
  while(handFree){
    const ok = await startRec();
    if(!ok){ handFree = false; syncHf(); break; }
    await new Promise(res=>{
      const t = setInterval(()=>{ if(!recOn){ clearInterval(t); res(); } }, 100);
    });
    const blob = stopRecNow();
    if(!blob){ /* 一点声音都没有，直接下一轮 */
      if(handFree) await new Promise(r=>setTimeout(r,300));
      continue;
    }
    state('识别', '#b8860b');
    let text = '';
    try{
      text = await recognize(blob);
    }catch(e){
      add('err','识别失败：'+reason(e));
      handFree = false; syncHf(); break;
    }
    if(!text){
      add('sys','这轮没听清，继续听。');
      continue;
    }
    if(/^(退出|结束|停止|别说了|不聊了)$/.test(text.replace(/[。！？，,\s]/g,''))){
      add('sys','好，免手模式关了。');
      handFree = false; syncHf(); break;
    }
    await round(text);
  }
}

/* stopRec 的包装：保证 closeRec 前记下采样率 */
function stopRecNow(){
  if(!recOn) return null;
  const sr = (ac && ac.sampleRate) || lastSr;
  lastSr = sr;
  recOn = false; mic.classList.remove('rec'); lvl.style.width='0%';
  let total=0; pcmChunks.forEach(c=>total+=c.length);
  const chunks = pcmChunks; pcmChunks = [];
  closeRec();
  if(!total) return null;
  const all = new Float32Array(total);
  let off=0; chunks.forEach(c=>{ all.set(c,off); off+=c.length; });
  return encodeWav(downsample(all, sr, 16000), 16000);
}

/* ---------- 卡片列表 ---------- */
async function loadCards(){
  try{
    const r = await fetch('/api/cards');
    const j = await r.json();
    const sel = $('#cards'); sel.innerHTML='';
    (j.cards||[]).forEach(c=>{
      const o = document.createElement('option');
      o.value = c.id;
      o.textContent = (c.name||c.id);
      if(c.id === j.current) o.selected = true;
      sel.appendChild(o);
    });
    if(!(j.cards||[]).length) add('sys','卡库里一张卡都没有。');
  }catch(e){
    add('err','卡片列表拉取失败：'+reason(e));
  }
}

/* ---------- 事件 ---------- */
$('#cards').addEventListener('change', async e=>{
  try{
    const r = await fetch('/api/card', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({id:e.target.value})});
    const j = await r.json();
    add('sys', j.ok ? ('已切卡：'+j.card+'（记忆与安全边界不变）')
                   : ('切卡失败：'+(j.error||'未知原因')));
  }catch(err){ add('err','切卡请求失败：'+reason(err)); }
});

mic.addEventListener('click', async ()=>{
  if(handFree) return;
  if(recOn){ stopRecNow(); return; }
  const ok = await startRec();
  if(!ok) return;
  await new Promise(res=>{
    const t=setInterval(()=>{ if(!recOn){ clearInterval(t); res(); } }, 100);
  });
  const blob = stopRecNow();
  if(!blob){ add('sys','这一下没录到声音。'); return; }
  state('识别','#b8860b');
  let text='';
  try{ text = await recognize(blob); }
  catch(e){ add('err','识别失败：'+reason(e)); state('空闲'); return; }
  if(!text){ add('sys','没听清，再说一次？'); state('空闲'); return; }
  inp.value = '';
  await round(text);
});

function syncHf(){
  hfBtn.classList.toggle('on', handFree);
  hfBtn.textContent = handFree ? '免手中' : '免手';
}
hfBtn.addEventListener('click', ()=>{
  handFree = !handFree;
  syncHf();
  if(handFree){
    const why = micBlockReason();
    if(why){ add('err', why); handFree=false; syncHf(); return; }
    add('sys','免手已开：说完自动发，她念完自动接着听。说「退出」可以关。');
    handFreeLoop();
  }else{
    add('sys','免手已关。');
    if(recOn) stopRecNow();
  }
});

$('#send').onclick = async ()=>{
  if(busy) return;
  const text = inp.value.trim();
  if(!text) return;
  inp.value='';
  await round(text);
};
inp.addEventListener('keydown', e=>{
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); $('#send').click(); }
});
inp.addEventListener('input', ()=>{
  inp.style.height='48px';
  inp.style.height=Math.min(120, inp.scrollHeight)+'px';
});

/* ---------- 启动 ---------- */
loadCards();
const why = micBlockReason();
if(why){
  add('err', why);
  mic.disabled = true;
  hfBtn.disabled = true;
}else{
  add('sys','天网已连接。点 🎤 说一句，或打开「免手」连续聊。');
}
fetch('/api/health').then(r=>r.json()).then(j=>{
  if(j && j.voice){
    const v = j.voice;
    add('sys','语音能力：识别=' + (v.asr?'就绪':'不可用')
      + '｜合成=' + (v.tts||'不可用'));
  }
}).catch(()=>{});
</script>
</body>
</html>
"""


def to_wav(src, dst):
    """任意音频 -> 16k 单声道 wav（whisper.transcribe 只认 RIFF）。

    前端已经优先传 16k wav，这里是兜底：Safari / 旧内核、以及任何
    直接拿 MediaRecorder 的 webm/opus 上来时都能救回来。
    返回 (ok, 原因)；失败必须带原因，不许静默变空串。
    """
    exe = None
    try:
        exe = voice._ffmpeg() if voice is not None else None
    except Exception:
        exe = None
    if not exe:
        return False, "找不到 ffmpeg（设 FLEET_FFMPEG 或装 ffmpeg 到 E:/dev/ffmpeg/bin）"
    cmd = [exe, "-y", "-loglevel", "error", "-i", src,
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dst]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=90)
    except Exception as e:
        return False, "调用 ffmpeg 失败：%s" % e
    if r.returncode != 0:
        detail = (r.stderr or b"").decode("utf-8", "replace").strip()
        return False, (detail[:200] or "ffmpeg 退出码 %d" % r.returncode)
    if not os.path.isfile(dst) or os.path.getsize(dst) <= 44:
        return False, "ffmpeg 没产出有效 wav"
    return True, ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 基础工具 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _body_json(self):
        try:
            return json.loads(self._body().decode("utf-8")) or {}
        except Exception:
            return {}

    def log_message(self, *_a):
        pass          # 别刷屏

    # ---- 路由 ----
    def do_OPTIONS(self):
        self._send(200, b"", "text/plain")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path == "/api/cards":
            self._json({"cards": persona_card.list_cards(),
                        "current": persona_card.current()})
        elif path == "/api/health":
            self._json({"ok": True, "voice": (voice.caps() if voice else None),
                        "current": persona_card.current(),
                        "llm": dialogue.llm is not None if hasattr(dialogue, "llm") else None})
        else:
            self._json({"error": "no such route"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/card":
            j = self._body_json()
            self._json(persona_card.switch(j.get("id")))
        elif path == "/api/chat":
            self._chat()
        elif path == "/api/voice/say":
            self._say()
        elif path == "/api/voice/recognize":
            self._recognize()
        else:
            self._json({"error": "no such route"}, 404)

    # ---- 具体实现 ----
    def _chat(self):
        j = self._body_json()
        text = (j.get("text") or "").strip()
        if not text:
            self._json({"say": "", "degraded": ["空输入"]})
            return
        t0 = time.time()
        try:
            res = dialogue.reply(text, card_id=j.get("card") or None, journal=journal)
        except Exception as e:
            self._json({"say": "", "error": str(e), "degraded": ["异常"]}, 500)
            return
        res["ms"] = int((time.time() - t0) * 1000)
        self._json(res)

    def _say(self):
        """文字 -> 音频。

        🔴 老代码 `path = voice.say(...)` 直接把元组 (backend, path) 当路径用，
        `os.path.exists(元组)` 抛 TypeError ⇒ 这个接口从来没能用过。
        现在按真实约定解包，并且**音色跟当前卡走**（say_for_card），
        这样「切卡换音色」才成立。
        """
        j = self._body_json()
        text = (j.get("text") or "").strip()
        if not text:
            self._json({"error": "空文本"}, 400)
            return
        if voice is None:
            self._json({"error": "TTS 不可用（voice 模块没加载）"}, 503)
            return
        card = j.get("card") or persona_card.current()
        try:
            backend, path = voice.say_for_card(text, card_id=card, play=False)
        except Exception as e:
            self._json({"error": "合成异常：%s" % e}, 500)
            return
        if not path or not os.path.exists(path):
            self._json({"error": "合成失败：后端=%s，没有产出音频（离线且 edge 不可用时会发生）"
                                  % (backend or "none")}, 500)
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        ctype = "audio/wav" if path.lower().endswith(".wav") else "audio/mpeg"
        self._send(200, data, ctype)

    def _recognize(self):
        """上传音频 -> 文字。

        🔴 两条历史 bug 在这里修掉：
        1. `voice.transcribe()` 返回的是 (text, meta) 元组，老代码把整个元组
           塞进 {"text": ...}，前端 j.text 拿到的是数组而不是字符串；
        2. 老代码假定上传的永远是 wav。手机侧 MediaRecorder 出的是 webm/opus，
           whisper 按 RIFF 解析会判成 0 秒静音。现在非 RIFF 先过 ffmpeg。

        返回约定（前端据此区分「没听清」和「真出错」）：
          * 真识别出字  -> 200 {"text": "...", "ms": n}
          * 静音/空音   -> 200 {"text": "", "reason": "静音音频"}   ← 不是错误
          * 真出错      -> 4xx/5xx {"error": "带原因的话"}
        """
        if voice is None:
            self._json({"error": "ASR 不可用（voice 模块没加载）"}, 503)
            return
        raw = self._body()
        if not raw:
            self._json({"error": "空音频：请求体是 0 字节"}, 400)
            return
        stamp = int(time.time() * 1000)
        tmp = os.path.join(voice.TMP_DIR, "web_%d.wav" % stamp)
        conv = os.path.join(voice.TMP_DIR, "web_%d_16k.wav" % stamp)
        try:
            os.makedirs(voice.TMP_DIR, exist_ok=True)
        except Exception as e:
            self._json({"error": "语音临时目录不可用：%s（设 FLEET_VOICE_TMP）" % e}, 500)
            return
        t0 = time.time()
        try:
            with open(tmp, "wb") as f:
                f.write(raw)
            if raw[:4] != b"RIFF":
                ok, why = to_wav(tmp, conv)
                if not ok:
                    self._json({"error": "音频格式不是 wav，且转换失败：%s" % why}, 400)
                    return
                tmp = conv
            text, meta = voice.transcribe(tmp, language="zh")
        except Exception as e:
            self._json({"error": "识别异常：%s" % e}, 500)
            return
        finally:
            for p in (tmp, conv):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        if text:
            self._json({"text": text, "ms": int((time.time() - t0) * 1000),
                        "backend": (meta or {}).get("backend")})
            return
        self._json({"text": "",
                    "reason": (meta or {}).get("error") or "识别结果为空",
                    "ms": int((time.time() - t0) * 1000)})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("天网 Web 版已启动： http://127.0.0.1:%d" % PORT)
    print("局域网内其他电脑访问本机 IP 的 %d 端口即可。" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
