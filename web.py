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
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dialogue            # noqa: E402
import tianwang_cards as persona_card   # noqa: E402  嵌合件：走 X6 统一卡层

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
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>天网</title>
<style>
:root{--bg:#0f1319;--panel:#171d28;--line:#252d3c;--fg:#e8ecf1;--dim:#8b97a8;
--accent:#4a9eff;--ok:#1e8449;--warn:#b8860b;--err:#c0392b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:"Microsoft YaHei UI",-apple-system,"PingFang SC",sans-serif;height:100vh;
display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:10px 16px;
border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:1px}
select{background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:6px 10px;font-size:13px}
#dot{width:11px;height:11px;border-radius:50%;background:var(--dim);flex:none}
#st{font-size:12px;color:var(--dim);min-width:52px}
#log{flex:1;overflow-y:auto;padding:18px 20px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:78%;padding:10px 14px;border-radius:14px;line-height:1.7;
font-size:14px;white-space:pre-wrap;word-break:break-word}
.me{align-self:flex-end;background:var(--accent);color:#06101f}
.her{align-self:flex-start;background:var(--panel);border:1px solid var(--line)}
.sys{align-self:center;font-size:12px;color:var(--dim);text-align:center}
.err{align-self:center;font-size:12px;color:var(--err);text-align:center}
footer{border-top:1px solid var(--line);background:var(--panel);
padding:10px 16px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:10px;padding:10px 12px;font-size:14px;resize:none;height:52px;
font-family:inherit;outline:none}
textarea:focus{border-color:var(--accent)}
button{background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:10px;padding:10px 14px;font-size:14px;cursor:pointer;font-family:inherit}
button:hover{border-color:var(--accent)}
button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:var(--accent);color:#06101f;border-color:var(--accent);
font-weight:600}
button.rec{background:var(--err);color:#fff;border-color:var(--err)}
</style>
</head>
<body>
<header>
  <h1>天网</h1>
  <select id="cards"></select>
  <span id="dot"></span><span id="st">空闲</span>
</header>
<div id="log"></div>
<footer>
  <button id="mic" title="按住说">🎤</button>
  <textarea id="in" placeholder="说点什么…（Enter 发送）"></textarea>
  <button id="send" class="primary">发送</button>
</footer>
<script>
const $=s=>document.querySelector(s), log=$('#log'), inp=$('#in');
let busy=false;
function add(cls,text){const d=document.createElement('div');
d.className='msg '+cls;d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
function state(s,color){$('#st').textContent=s;$('#dot').style.background=color||'#8b97a8';}
async function loadCards(){
  try{
    const r=await fetch('/api/cards'),j=await r.json();
    const sel=$('#cards');sel.innerHTML='';
    (j.cards||[]).forEach(c=>{
      const o=document.createElement('option');
      o.value=c.id;o.textContent=(c.name||c.id)+'（'+c.id+'）';
      if(c.id===j.current)o.selected=true;sel.appendChild(o);});
  }catch(e){add('err','卡片列表拉取失败：'+e);}
}
$('#cards').addEventListener('change',async e=>{
  const r=await fetch('/api/card',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id:e.target.value})});
  const j=await r.json();
  add('sys',j.ok?('已切卡：'+j.card+'（记忆与安全边界不变）'):('切卡失败：'+j.error));
});
async function send(){
  if(busy)return;const text=inp.value.trim();if(!text)return;
  inp.value='';add('me',text);busy=true;state('思考','#b8860b');
  const card=$('#cards').value;
  try{
    const r=await fetch('/api/chat',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,card})});
    const j=await r.json();
    add('her',j.say||'（没听清）');
    if(j.degraded&&j.degraded.length)add('sys','降级：'+j.degraded.join('、'));
    speak(j.say||'');
  }catch(e){add('err','出错：'+e);}
  busy=false;state('空闲');
}
function speak(t){
  if(!t)return;
  try{
    const u=new SpeechSynthesisUtterance(t);u.lang='zh-CN';
    const vs=speechSynthesis.getVoices();
    const v=vs.find(x=>/Chinese/i.test(x.lang));if(v)u.voice=v;
    speechSynthesis.speak(u);
  }catch(e){}
}
$('#send').onclick=send;
inp.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
// 浏览器自带听写（Chrome/Edge 有 Web Speech API）
let rec=null,recOn=false;
$('#mic').onclick=()=>{
  if(recOn){rec.stop();return;}
  const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
  if(!SR){add('err','这个浏览器不支持语音识别，请用 Chrome/Edge，或直接打字。');return;}
  rec=new SR();rec.lang='zh-CN';rec.interimResults=false;rec.continuous=false;
  rec.onstart=()=>{recOn=true;$('#mic').classList.add('rec');state('聆听','#c0392b');};
  rec.onresult=e=>{
    const t=e.results[0][0].transcript;inp.value+=t;};
  rec.onerror=e=>{add('err','识别出错：'+e.error);};
  rec.onend=()=>{recOn=false;$('#mic').classList.remove('rec');
    state('空闲');if(inp.value.trim())send();};
  rec.start();
};
loadCards();
add('sys','天网已连接。回车发送，点 🎤 说话。');
</script>
</body>
</html>
"""


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
        j = self._body_json()
        text = (j.get("text") or "").strip()
        if not text or voice is None:
            self._json({"error": "TTS 不可用"}, 503)
            return
        try:
            path = voice.say(text, play=False)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        if not path or not os.path.exists(path):
            self._json({"error": "合成失败"}, 500)
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
        if voice is None:
            self._json({"error": "ASR 不可用"}, 503)
            return
        raw = self._body()
        if not raw:
            self._json({"error": "空音频"}, 400)
            return
        tmp = os.path.join("G:/_tmp/fleet_voice", "web_%d.wav" % int(time.time() * 1000))
        try:
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(raw)
            text = voice.transcribe(tmp, language="zh")
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
        self._json({"text": text or ""})


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
