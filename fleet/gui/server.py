"""Windows 图形版（W1）的本地后端：把注册表变成一个只给本机窗口用的 HTTP 服务。

为什么要有这一层（而不是让前端直接调设备）：
  浏览器里没有 Python，能力调用必须经过后端；但「能点鼠标键盘的 HTTP 接口」
  是个新引入的攻击面，所以这里钉死三条防线：

    1. **只绑 127.0.0.1 + 随机端口**（port=0 让系统分配）。
       不绑 0.0.0.0：局域网里的别的机器连进来就能驱动你的桌面。
       随机端口：固定端口会被同机别的程序撞上或被外部扫到。
    2. **一次性 token 鉴权**。启动时用 secrets 生成，只活在本进程内存里，
       由壳（app.py）通过 URL 片段注入页面，前端每次请求带 `X-Fleet-Token`。
       片段不进 HTTP 请求，所以 token 不会落到服务端日志、也不会进 Referer。
       token 绝不写进代码、绝不落盘（local.env 是 AI 后端的凭据，跟这个不是一回事）。
    3. **一切能力调用走 registry.call**。前端永远不直接 import python 模块调设备；
       闸门在服务端（前端绕不过去），这与命令行/T 版共用同一张注册表。

流式：/api/chat 用 SSE（text/event-stream）真流式——AI 设备的 `_on_line`
回调在生成过程中一行行回流，我们把它推进队列，由 HTTP 线程边收边写给浏览器。
不能等 reg.call 全部返回再一次性吐出去：那就不是流式了。

「不许写兜底/回退代码」：所有失败都带明确原因（code + err），
不静默降级、不假装成功。
"""
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ..catalog import build

# 仓库根（fleet/gui/server.py → 上三级）。角色卡、人格状态这些平铺模块都在根下。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIR = os.path.join(ROOT, "ui")
TOKEN_HEADER = "X-Fleet-Token"
PERSONA_PATH = os.environ.get(
    "FLEET_PERSONA", os.path.join(ROOT, "persona_state.json"))


# ------------------------------------------------------------------ 人格

class Persona:
    """当前人格：角色卡（她是谁）+ PersonaState（她现在怎么样）。

    两者分工沿用 persona_card.py 的约定，不另造一套：
      角色卡 = 静态设定；PersonaState = 情绪标量/口癖/执念。
    切卡改的是「她是谁」，记忆与刹车不切。
    """

    def __init__(self, card_dir=None, state_path=None):
        import persona as p
        import persona_card as pc
        self._pc = pc
        self.card_dir = card_dir
        self.state_path = state_path or PERSONA_PATH
        self._state = p.PersonaState(self.state_path)

    def card(self):
        return self._pc.active_card(self.card_dir)

    def state(self):
        return self._state

    def snapshot(self):
        """给 /api/persona 用的快照。"""
        card = self.card()
        return {
            "card": {"name": (card or {}).get("name") or "",
                     "spec": (card or {}).get("_spec") or "",
                     "creator": (card or {}).get("creator") or ""},
            "card_loaded": bool(card),
            "state": self._state.snapshot(),
        }

    def system_prompt(self, user_text=""):
        """拼 system prompt：卡给底色，状态给当天的体温，世界书按关键词注入。

        没有选中卡时返回空串——上层据此知道「现在是没有人格的裸助手」，
        而不是拿一个默认人设糊弄过去。
        """
        card = self.card()
        if not card:
            return ""
        extra = "\n".join(self._pc.match_world_book(card, user_text))
        return self._pc.build_system(card, state=self._state, extra=extra)


# ------------------------------------------------------------------ 闸门

def chat_gate(cap, dev, args, level):
    """对话专用的闸门：只放行 ai.chat，且只到 level 1。

    为什么对话要一个专门闸门：ai.chat 在 catalog 里是 side_effect=True
    （会真的连后端、真的烧 token），按注册表的 fail-closed 纪律，
    没有闸门时它一定被拒。而「用户在界面上敲了字点了发送」本身就是授权，
    所以这里给一个**窄**闸门：只认 ai.chat 一项，别的能力一律拒。
    真正的审批弹窗是 W2 的事，那时这里换成弹窗回调。
    """
    if cap.name == "ai.chat":
        return True, ""
    return False, f"对话通道只放行 ai.chat，不放行 {cap.name}"


# ------------------------------------------------------------------ 服务

class FleetServer:
    """本机 GUI 后端。构造即生成 token；start() 之后端口才定下来。"""

    def __init__(self, registry=None, gate=None, persona=None, host="127.0.0.1",
                 port=0, chat_gate_fn=chat_gate):
        self.registry = registry if registry is not None else build()
        # 闸门在服务端：/api/call 走它。默认 None —— 于是注册表按 fail-closed
        # 拒绝一切有副作用的能力，答复里带明确原因（这是 W1 的真实状态，
        # 不是兜底：审批弹窗还没做，就先不许动手）。
        self.gate = gate
        self.chat_gate = chat_gate_fn
        self.persona = persona if persona is not None else Persona()
        self.token = secrets.token_urlsafe(32)      # 一次性，只在内存里
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None
        self.started_at = 0.0
        self.calls = []                              # 调用流水（给 /api/health 看）

    # ---------------- 生命周期 ----------------

    def start(self):
        if self._httpd is not None:
            raise RuntimeError("服务已经起来了")
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True
        httpd.fleet = self
        self._httpd = httpd
        self.host, self.port = httpd.server_address[0], httpd.server_address[1]
        self.started_at = time.time()
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.2}, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def page_url(self, theme="light"):
        """给壳用的页面地址。token 放**片段**里：片段不会随 HTTP 请求发出，
        所以它既不会进服务端日志，也不会进任何 Referer 头。
        """
        return f"{self.base_url}/index.html#t={self.token}&theme={theme}"

    # ---------------- 能力调用（唯一入口） ----------------

    def call(self, cap, args=None, device=""):
        """走 registry.call。返回 DeviceResult 的字典形态。

        闸门没配时：有副作用的能力会被注册表拒成 denied，err 里写明原因。
        """
        r = self.registry.call(cap, device=device, args=args or {}, gate=self.gate)
        self.calls.append({"cap": cap, "code": r.code, "ms": round(r.ms, 1),
                           "at": time.time()})
        return r.as_dict()

    def chat(self, prompt):
        """对话：走 registry.call('ai.chat')，带 _on_line 回调。

        reg.call 是阻塞的（生成完才返回），所以这里在**工作线程**里跑它，
        把 _on_line 收到的行推进队列；调用方（HTTP 线程）从队列取，边收边写 SSE。
        """
        system = self.persona.system_prompt(prompt)
        q = queue.Queue()

        def _on_line(text):
            q.put({"type": "line", "text": text})

        def _work():
            try:
                r = self.registry.call(
                    "ai.chat",
                    args={"prompt": prompt, "system": system, "_on_line": _on_line},
                    gate=self.chat_gate)
                q.put({"type": "done", "result": r.as_dict()})
            except Exception as e:
                q.put({"type": "error", "err": f"{type(e).__name__}: {e}"})

        threading.Thread(target=_work, daemon=True).start()
        return q


# ------------------------------------------------------------------ HTTP

class _Handler(BaseHTTPRequestHandler):
    server_version = "FleetGUI/0.1"

    def log_message(self, fmt, *args):
        """静默默认的 stderr 日志（会打每个请求）——我们自己记流水。"""
        pass

    # ---------------- 工具 ----------------

    @property
    def fleet(self):
        return self.server.fleet

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, err, extra=None):
        obj = {"ok": False, "err": err}
        if extra:
            obj.update(extra)
        self._json(obj, code)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError(f"请求体不是合法 JSON: {e}")
        if not isinstance(obj, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return obj

    def _authorized(self):
        """校验 X-Fleet-Token。失败一律 401 + 明确原因。"""
        got = self.headers.get(TOKEN_HEADER) or ""
        if not got:
            return False, f"缺少 {TOKEN_HEADER} 头：这个服务只认本窗口启动时注入的令牌"
        if not secrets.compare_digest(got, self.fleet.token):
            return False, "令牌不对：不是本次启动注入的那枚"
        return True, ""

    # ---------------- 路由 ----------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path == "/app.js":
            return self._static("app.js")
        if path == "/app.css":
            return self._static("app.css")
        if path.startswith("/api/"):
            return self._api_get(path)
        self._err(404, f"没有这个路径: {path}")

    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            return self._api_post(path)
        self._err(404, f"没有这个路径: {path}")

    def _api_get(self, path):
        ok, why = self._authorized()
        if not ok:
            return self._err(401, why)
        f = self.fleet
        if path == "/api/health":
            return self._json(self._health())
        if path == "/api/caps":
            return self._json(self._caps())
        if path == "/api/devices":
            return self._json(self._devices())
        if path == "/api/persona":
            return self._json({"ok": True, **f.persona.snapshot()})
        if path == "/api/cards":
            return self._json(self._cards())
        self._err(404, f"没有这个接口: {path}")

    def _api_post(self, path):
        ok, why = self._authorized()
        if not ok:
            return self._err(401, why)
        try:
            body = self._read_json()
        except ValueError as e:
            return self._err(400, str(e))
        if path == "/api/call":
            return self._post_call(body)
        if path == "/api/chat":
            return self._post_chat(body)
        if path == "/api/card/use":
            return self._post_card_use(body)
        self._err(404, f"没有这个接口: {path}")

    # ---------------- 各接口的实现 ----------------

    def _health(self):
        f = self.fleet
        reg = f.registry
        backends = []
        be_reason = ""
        r = reg.call("ai.list", args={}, gate=f.gate)
        if r.ok:
            backends = [{"id": b.get("id"), "title": b.get("title"),
                         "protocol": b.get("protocol"), "model": b.get("model")}
                        for b in (r.data.get("backends") or [])]
        else:
            be_reason = r.err
        return {
            "ok": True,
            "devices": len(reg.devices()),
            "caps_count": len(reg.capabilities()),
            "backend": backends[0] if backends else None,
            "backends": backends,
            "backend_reason": be_reason,
            "persona": f.persona.snapshot(),
            "uptime_s": round(time.time() - f.started_at, 1),
            "calls": len(f.calls),
        }

    def _caps(self):
        rows = []
        domains = {}
        for c in self.fleet.registry.capabilities():
            d = c.as_dict()
            d["devices"] = [dev.name for dev in self.fleet.registry.owners(c.name)]
            rows.append(d)
            domains.setdefault(d["domain"], []).append(d["name"])
        return {"ok": True, "count": len(rows), "caps": rows,
                "domains": {k: sorted(v) for k, v in sorted(domains.items())}}

    def _devices(self):
        rows = self.fleet.registry.availability()
        return {"ok": True, "count": len(rows), "devices": rows}

    def _cards(self):
        import persona_card as pc
        cards = []
        for c in pc.list_cards(self.fleet.persona.card_dir):
            s = c.get("summary") or {}
            cards.append({"file": c["file"], "ok": c["ok"], "name": s.get("name", ""),
                          "spec": s.get("spec", ""), "error": c.get("error", "")})
        return {"ok": True, "cards": cards}

    def _post_call(self, body):
        cap = (body.get("cap") or "").strip()
        if not cap:
            return self._err(400, "缺少 cap 字段（要调哪项能力）")
        if self.fleet.registry.capability(cap) is None:
            return self._err(404, f"注册表里没有这项能力: {cap}")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return self._err(400, "args 必须是 JSON 对象")
        res = self.fleet.call(cap, args=args, device=(body.get("device") or ""))
        return self._json(res)

    def _post_card_use(self, body):
        import persona_card as pc
        fn = (body.get("file") or "").strip()
        if not fn:
            return self._err(400, "缺少 file 字段（要切到哪张卡）")
        try:
            data = pc.set_active(fn, self.fleet.persona.card_dir)
        except Exception as e:
            return self._err(400, f"切卡失败: {e}")
        return self._json({"ok": True, "card": {"name": data.get("name"),
                                                "spec": data.get("_spec")},
                           **self.fleet.persona.snapshot()})

    # ---------------- SSE 对话 ----------------

    def _post_chat(self, body):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._err(400, "缺少 prompt（或 prompt 是空的）")
        q = self.fleet.chat(prompt)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                try:
                    ev = q.get(timeout=300)
                except queue.Empty:
                    self._sse({"type": "error", "err": "等后端回复超时（300 秒）"})
                    break
                self._sse(ev)
                if ev.get("type") in ("done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器关掉了页面：这不是错误，后台线程自会收尾。
            pass
        self.close_connection = True

    def _sse(self, obj):
        data = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # ---------------- 静态资源 ----------------

    def _static(self, name):
        """只放行三个已知文件名（不做路径拼接，防目录穿越）。"""
        if name not in ("index.html", "app.js", "app.css"):
            return self._err(404, f"没有这个静态文件: {name}")
        path = os.path.join(UI_DIR, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._err(500, f"读不到前端资源 {name}: {e}")
        ctype = {"index.html": "text/html; charset=utf-8",
                 "app.js": "application/javascript; charset=utf-8",
                 "app.css": "text/css; charset=utf-8"}[name]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
