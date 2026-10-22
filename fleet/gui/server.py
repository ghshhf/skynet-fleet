"""Windows 图形版（W1）的本地后端：把注册表变成一个只给本机窗口用的 HTTP 服务。

为什么要有这一层（而不是让前端直接调设备）：
  浏览器里没有 Python，能力调用必须经过后端；但「能点鼠标键盘的 HTTP 接口」
  是个新引入的攻击面，所以这里钉死三条防线：

    1. **默认只绑 127.0.0.1 + 随机端口**（port=0 让系统分配）。
       不绑 0.0.0.0：局域网里的别的机器连进来就能驱动你的桌面。
       随机端口：固定端口会被同机别的程序撞上或被外部扫到。
       两种例外都是**显式**选的：`--link` 固定端口但仍只绑 127.0.0.1（手机走
       adb reverse 进来）；`--lan` 才绑 0.0.0.0，且 app.py 里守着一道硬闸——
       没固定 FLEET_GUI_TOKEN 就拒绝启动。
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
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from ..catalog import build
from .approval import ApprovalBroker

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
                 port=0, chat_gate_fn=chat_gate, approval=None,
                 evidence_journal=None):
        self.registry = registry if registry is not None else build()
        # 闸门在服务端：/api/call 走它。默认 None —— 于是注册表按 fail-closed
        # 拒绝一切有副作用的能力，答复里带明确原因（这是 W1 的真实状态，
        # 不是兜底：审批弹窗还没做，就先不许动手）。
        # W2 起：给了 approval（审批中枢）就改走它——注册表判定「要审批」时
        # 把这次调用挂起，等主人在弹窗里点确认。判定权始终在注册表层。
        self.gate = gate
        self.approval = approval
        self.chat_gate = chat_gate_fn
        self.persona = persona if persona is not None else Persona()
        # 默认每次启动现生成一枚，只活在内存里。手机端必须能改令牌就是为了这个：
        # 跨设备没法共享一个内存里的随机串。显式设 FLEET_GUI_TOKEN 才固定，
        # 不设就永远走随机——安全默认不变，只是多了一扇给联调用的门。
        self.token = (os.environ.get("FLEET_GUI_TOKEN") or "").strip() \
            or secrets.token_urlsafe(32)
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None
        self.started_at = 0.0
        self.calls = []                              # 调用流水（给 /api/health 看）
        # 执行证据（2026-10-22 新增）：能力调用前后写 journal，生成 exec: 级证据。
        # 之前每一层都只管调用、不管留痕 ⇒ journal 里 done = 0 条，
        # 技能进化没有原料、followups 全是挂着的账。接上以后就有了。
        # 不传 evidence_journal 就只调用不记录（测试与一次性脚本不受影响）。
        from fleet import execute as _X
        self.executor = _X.Executor(self.registry, journal=evidence_journal,
                                    actor="gui")
        self._shots = {}                            # 截图登记：id -> 路径（W3）
        self._shot_seq = 0
        self._shot_lk = threading.Lock()

    # ---------------- 截图登记（W3 的屏幕预览）----------------

    def register_shot(self, path):
        """登记一张截图，返回可回取的 id。

        🔴 为什么要登记而不是让前端直接传路径？
        因为那等于开了一个**任意文件读取**的洞——前端传 `C:\\...\\任何文件`
        服务端就给他读。登记制把「能看哪些图」限制在服务端自己截出来的那几张。
        """
        if not path:
            return ""
        with self._shot_lk:
            self._shot_seq += 1
            sid = "s%d" % self._shot_seq
            self._shots[sid] = path
            # 只留最近 16 张：截图是临时文件，存久了只是占地方
            if len(self._shots) > 16:
                for k in list(self._shots)[:-16]:
                    self._shots.pop(k, None)
        return sid

    def shot_path(self, sid):
        with self._shot_lk:
            return self._shots.get(sid)

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
    def dial_host(self):
        """对话框里能用的主机名。

        `--lan` 下 self.host 是 0.0.0.0——那是「监听哪个网卡」的意思，不是
        「连到哪」的地址。拿 0.0.0.0 去连（尤其喂给 webview）会连不上，
        所以对外一律换成 127.0.0.1。
        """
        return "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host

    @property
    def base_url(self):
        return f"http://{self.dial_host}:{self.port}"

    def page_url(self, theme="light"):
        """给壳用的页面地址。token 放**片段**里：片段不会随 HTTP 请求发出，
        所以它既不会进服务端日志，也不会进任何 Referer 头。
        """
        return f"{self.base_url}/index.html#t={self.token}&theme={theme}"

    # ---------------- 能力调用（唯一入口） ----------------

    def call(self, cap, args=None, device="", tag="", session=""):
        """走 registry.call。返回 DeviceResult 的字典形态。

        闸门怎么来的（两种，互斥）：
          · 给了 approval（审批中枢）→ 闸门就是它：注册表判定「这项要审批」时
            把这次调用挂起，等主人在弹窗里点确认。判定权始终在注册表层，
            UI 只能投「同意/不同意」这一票。
          · 没给 approval → 用构造时传入的 gate；再没有就是 None，于是注册表
            按 fail-closed 拒绝一切有副作用的能力，err 里写明原因。

        `tag` / `session` 是**回显与归属**用的，不参与判定：
          tag     —— UI 拿去把「这条待审批」认领回自己那次调用（客户端自己生成）。
          session —— 归属哪个客户端会话；别的会话拿着同一个 request_id 回应会被拒。
        """
        gate = self.gate
        if self.approval is not None:
            gate = self.approval.gate_for(tag=tag, session=session)
        r = self.executor.call(cap, device=device, args=args or {}, gate=gate)
        self.calls.append({"cap": cap, "code": r.code, "ms": round(r.ms, 1),
                           "at": time.time()})
        return r.as_dict()

    def chat(self, prompt, backends=""):
        """对话：走 registry.call('ai.chat')，带 _on_line 回调。

        backends：逗号分隔的后端白名单（空 = 全部后端，跟以前一样）。
        手机语音一次只念一句，多后端一起回会串台，所以手机端固定传一个。

        reg.call 是阻塞的（生成完才返回），所以这里在**工作线程**里跑它，
        把 _on_line 收到的行推进队列；调用方（HTTP 线程）从队列取，边收边写 SSE。
        """
        system = self.persona.system_prompt(prompt)
        q = queue.Queue()

        def _on_line(text):
            q.put({"type": "line", "text": text})

        def _work():
            try:
                r = self.executor.call(
                    "ai.chat",
                    args={"prompt": prompt, "system": system,
                          "backends": backends, "_on_line": _on_line},
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
        """校验 X-Fleet-Token。

        401 = 压根没带凭据；403 = 带了但不是本次启动发的那枚。
        分开是为了让「调用方忘了加头」和「有人拿着旧令牌来敲」在日志里
        长得不一样——这两种事的处置不一样（前者是 bug，后者是可疑行为）。
        比对一律走 secrets.compare_digest（常数时间，防时序侧信道）。
        """
        got = self.headers.get(TOKEN_HEADER) or ""
        if not got:
            return False, 401, (f"缺少 {TOKEN_HEADER} 头："
                                "这个服务只认本窗口启动时注入的令牌")
        if not secrets.compare_digest(got, self.fleet.token):
            return False, 403, "令牌不对：不是本次启动注入的那枚"
        return True, 200, ""

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
        ok, code, why = self._authorized()
        if not ok:
            return self._err(code, why)
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
        if path == "/api/cardstore":
            return self._json(self._cardstore())
        if path == "/api/cardstore/search":
            return self._json(self._cardstore_search())
        if path == "/api/cardstore/detail":
            return self._json(self._cardstore_detail())
        if path == "/api/card/lint":
            return self._json(self._card_lint())
        if path == "/api/audio/presets":
            return self._json(self._audio_presets())
        if path == "/api/skills":
            return self._json(self._skills())
        if path == "/api/connectors":
            return self._json(self._connectors())
        if path == "/api/memory":
            return self._json(self._memory())
        if path == "/api/roster":
            return self._json(self._roster())
        if path == "/api/approvals":
            return self._json(self._approvals())
        if path == "/api/host":
            return self._json(self._host_panel())
        if path == "/api/shot":
            return self._shot_png()
        self._err(404, f"没有这个接口: {path}")

    def _api_post(self, path):
        ok, code, why = self._authorized()
        if not ok:
            return self._err(code, why)
        # 语音识别收的是**裸二进制 wav/webm**，不是 JSON。必须赶在 _read_json 之前
        # 分流，否则会被判成「请求体不是合法 JSON」。
        if path == "/api/voice/recognize":
            return self._post_voice_recognize()
        try:
            body = self._read_json()
        except ValueError as e:
            return self._err(400, str(e))
        if path == "/api/voice/say":
            return self._post_voice_say(body)
        if path == "/api/call":
            return self._post_call(body)
        if path == "/api/chat":
            return self._post_chat(body)
        if path == "/api/card/use":
            return self._post_card_use(body)
        if path == "/api/card/fetch":
            return self._post_card_fetch(body)
        if path == "/api/card/ingest":
            return self._post_card_ingest(body)
        if path == "/api/audio/synth":
            return self._post_audio_synth(body)
        if path == "/api/approval/respond":
            return self._post_approval_respond(body)
        if path == "/api/memory/delete":
            return self._post_memory_delete(body)
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

    # ---------------- 角色卡商店（浏览 / 下载 / 体检） ----------------
    #
    # 🔴 读的动作（浏览/搜索/详情/体检）直接走只读能力；
    # 🔴 写的动作（下载/入库）走 registry.call —— 它们是副作用能力，
    #    闸门在服务端，前端绕不过去，主人会看到审批弹窗。
    #    **不许在这里直接调 card_store.fetch_card**：那会绕过闸门，
    #    等于「点一下就偷偷换掉她的人格」。

    def _cardstore(self):
        """卡源清单 + 本地卡库概况 + 投递口位置。"""
        r = self.fleet.call("card.sources", args={})
        if not r.get("ok"):
            return {"ok": False, "err": r.get("err", "读不到卡源"),
                    "sources": [], "cards": self._cards().get("cards", [])}
        d = r.get("data") or {}
        return {
            "ok": True,
            "sources": d.get("sources", []),
            "card_dir": d.get("card_dir", ""),
            "inbox": d.get("inbox", ""),
            "cards": self._cards().get("cards", []),
        }

    def _cardstore_search(self):
        """在卡源上搜卡。只读，不改任何东西。"""
        q = urlparse(self.path).query
        p = parse_qs(q)
        args = {
            "source": (p.get("source") or ["aicc"])[0],
            "query": (p.get("query") or p.get("q") or [""])[0],
            "skip": int((p.get("skip") or ["0"])[0] or 0),
            "limit": int((p.get("limit") or ["24"])[0] or 24),
        }
        r = self.fleet.call("card.search", args=args)
        if not r.get("ok"):
            return {"ok": False, "err": r.get("err", "搜不到"),
                    "items": [], "total": 0, "source": args["source"]}
        d = r.get("data") or {}
        return {"ok": True, "source": d.get("source", args["source"]),
                "query": d.get("query", ""), "total": d.get("total", 0),
                "items": d.get("items", []), "skip": d.get("skip", 0),
                "limit": d.get("limit", 24)}

    def _cardstore_detail(self):
        q = urlparse(self.path).query
        p = parse_qs(q)
        cid = (p.get("id") or [""])[0]
        if not cid:
            return self._err(400, "缺少 id（要查哪张卡）")
        r = self.fleet.call("card.detail", args={
            "source": (p.get("source") or ["aicc"])[0], "id": cid})
        if not r.get("ok"):
            return self._err(400, r.get("err", "查不到这张卡"))
        return self._json({"ok": True, **(r.get("data") or {})})

    def _card_lint(self):
        q = urlparse(self.path).query
        p = parse_qs(q)
        path = (p.get("path") or [""])[0]
        if not path:
            return self._err(400, "缺少 path（要体检哪张卡）")
        r = self.fleet.call("card.lint", args={"path": path})
        if not r.get("ok"):
            return self._err(400, r.get("err", "体检失败"))
        return self._json({"ok": True, **(r.get("data") or {})})

    def _post_card_fetch(self, body):
        """下载一张卡。**走 registry.call**（副作用 ⇒ 要审批）。"""
        cid = (body.get("id") or "").strip()
        if not cid:
            return self._err(400, "缺少 id（要下哪张卡）")
        args = {"source": (body.get("source") or "aicc").strip(), "id": cid}
        if body.get("force"):
            args["force"] = True
        r = self.fleet.call("card.fetch", args=args, tag=body.get("tag", ""),
                             session=body.get("session", ""))
        if not r.get("ok"):
            return self._json({"ok": False, "err": r.get("err", "下载失败")}, 200)
        return self._json({"ok": True, **(r.get("data") or {})})

    def _post_card_ingest(self, body):
        """把投递口里的卡入库。**走 registry.call**（副作用 ⇒ 要审批）。"""
        r = self.fleet.call("card.ingest",
                            args={"move": bool(body.get("move", True))},
                            tag=body.get("tag", ""),
                            session=body.get("session", ""))
        if not r.get("ok"):
            return self._json({"ok": False, "err": r.get("err", "入库失败")}, 200)
        return self._json({"ok": True, **(r.get("data") or {})})

    # ---------------- 代码生成音频 ----------------
    #
    # 🔴 合成是**副作用**（要写 WAV 文件）⇒ 走 registry.call ⇒ 会过审批闸门。
    #    不许在这里直接调 audio_synth.synth 绕过闸门。

    def _audio_presets(self):
        """音频预设清单（只读）。

        🔴 返回形状：与同类只读端点（/api/skills、/api/connectors、/api/memory）
        保持一致——失败时把原因放在 **`error` 键**里（前端 `renderAudioTab`
        读的就是 `d.error`，拼成 `err` 它认不到，失败就只剩一句没原因的空话）。
        成功时给 `ok/presets/note`，前端依赖 `d.presets`。
        """
        r = self.fleet.call("audio.presets", args={})
        if not r.get("ok"):
            return {"ok": False, "error": r.get("err", "读不到音频预设"),
                    "presets": []}
        d = r.get("data") or {}
        return {"ok": True, "presets": d.get("presets", []),
                "note": d.get("note", "")}

    def _post_audio_synth(self, body):
        """合成一段声音。**走 registry.call**（副作用 ⇒ 要审批）。"""
        spec = (body.get("spec") or "").strip()
        if not spec:
            return self._err(400, "缺少 spec（要合成哪种声音）")
        args = {"spec": spec, "seed": body.get("seed") or ""}
        if body.get("out"):
            args["out"] = str(body["out"]).strip()
        if body.get("sample_rate"):
            args["sample_rate"] = int(body["sample_rate"])
        r = self.fleet.call("audio.synth", args=args,
                             tag=body.get("tag", ""),
                             session=body.get("session", ""))
        if not r.get("ok"):
            # 失败时也要带上 code/err：前端 doSynth 靠 `res.code === "denied"`
            # 判断结果，靠 `res.err` 显示原因。拍平 data 会丢掉这些字段
            # ⇒ 直接返回完整结果信封（与 /api/call 同形状，showResult 也认）。
            return self._json(r)
        return self._json(r)

    def _skills(self):
        """技能清单（SKILL.md）。

        为什么技能单独一个接口而不塞进 /api/caps：
        **能力是「能做什么」**（要过闸门），**技能是「怎么做」**（方法论，
        自己不动手）。混在一起就会出现「技能要不要审批」这种没法答的问题。
        """
        from .. import skills as SK
        try:
            return SK.summary()
        except Exception as e:
            return {"ok": False, "count": 0, "skills": [],
                    "error": f"{type(e).__name__}: {e}"}

    def _connectors(self):
        """连接器清单：**内建设备 + 外部 MCP server，统一成一张表**。

        为什么统一：
        「设备也是 MCP」——设备本质上就是内置的 MCP server。
        于是「连接器」这个 Tab 不是「设备页的另一个马甲」，
        而是「我能通过 MCP 用到的所有 server」，来源不同（内建/外接）而已。

        每行的形状：
          {name, source(builtin|external), kind, ok, reason, tools, caps, via}
        tools 对内建行 = 这台设备支持的只读能力数；
        对外接行 = 外部 MCP server 真列出来的工具数（真起进程探的）。
        """
        from ..mcp.client import build_clients

        reg = self.fleet.registry
        rows = []

        # ---- 内建：每台设备就是一个 MCP server ----
        for dev in reg.availability():
            caps = list(dev.get("caps") or [])
            read_only = 0
            for cn in caps:
                c = reg.capability(cn)
                if c is not None and not (c.side_effect or c.destructive):
                    read_only += 1
            rows.append({
                "name": dev.get("name", ""),
                "source": "builtin",
                "kind": dev.get("kind", ""),
                "ok": bool(dev.get("ok")),
                "reason": dev.get("reason", ""),
                "tools": read_only,
                "caps": caps,
                "via": dev.get("via", ""),
            })

        # ---- 外接：用户挂的第三方 MCP server ----
        # 真探（起进程 + tools/list）——不探就不知道它死没死，
        # 而「摆着但不响应」是本仓明确禁止的界面骗人形态。
        try:
            clients = build_clients()
        except Exception as e:
            clients = []
            cfg_reason = f"{type(e).__name__}: {e}"
        else:
            cfg_reason = ""
        for cl in clients:
            st = cl.status()
            st.update({"source": "external", "kind": "mcp", "caps": [],
                       "via": "stdio", "reason": st.get("reason", "")})
            rows.append(st)

        rows.sort(key=lambda r: (r["source"] != "builtin", r["name"]))
        out = {"ok": True, "count": len(rows), "connectors": rows,
               "builtin": sum(1 for r in rows if r["source"] == "builtin"),
               "external": sum(1 for r in rows if r["source"] == "external")}
        if cfg_reason:
            out["error"] = f"外部 MCP 配置有问题：{cfg_reason}"
        if out["external"] == 0 and not cfg_reason:
            out["note"] = ("还没挂任何外部 MCP server。"
                           "要挂就写 ~/.fleet/mcp.json（格式见 fleet/mcp/client.py）")
        return out

    def _memory(self):
        """她的记忆条目（人格抽屉用）。

        🔴 E-7 落点：**个人数据绝不进被训练参数，只进可寻址可删的记忆层**。
        所以这里给的是「可寻址」的列表（ns + key + ts），
        前端据此可以**逐条删**（POST /api/memory/delete）。

        记忆库路径与 commander 一致（FLEET_CTX 环境变量，默认仓库根下
        fleet_context.db）——两处读同一份，才叫「同一份记忆」。
        """
        try:
            import memory as mem
        except Exception as e:
            return {"ok": False, "count": 0, "items": [],
                    "error": f"记忆模块用不了：{type(e).__name__}: {e}"}
        path = os.environ.get("FLEET_CTX", os.path.join(ROOT, "fleet_context.db"))
        try:
            ctx = mem.SharedContext(path)
            try:
                items = ctx.items(k=100)
            finally:
                ctx.close()
        except Exception as e:
            return {"ok": False, "count": 0, "items": [],
                    "error": f"记忆库读不出来：{type(e).__name__}: {e}"}
        return {"ok": True, "count": len(items), "items": items, "path": path}

    def _roster(self):
        """无限智能体名册（人格抽屉的「她是谁」那一栏旁边）。

        真读 `cards/` 下的角色卡，不硬编码名字。
        """
        try:
            from ..agents import roster
            profiles = roster.load_all()
            rows = roster.summary(profiles)
            try:
                active = roster.active_name()
            except Exception as e:
                active = ""
                active_reason = f"{type(e).__name__}: {e}"
            else:
                active_reason = ""
        except Exception as e:
            return {"ok": False, "count": 0, "roles": [],
                    "error": f"名册读不出来：{type(e).__name__}: {e}"}
        out = {"ok": True, "count": len(rows), "roles": rows, "active": active}
        if active_reason:
            out["reason"] = active_reason
        return out

    def _approvals(self):
        """当前在等主人确认的清单（给 UI 显示「哪些能力在等审批」）。

        没开审批中枢时也给答复（pending 为空），并说明为什么是空的——
        不静默返回一个空数组让人以为「没人等审批」。
        """
        f = self.fleet
        if f.approval is None:
            return {"ok": True, "enabled": False, "count": 0, "pending": [],
                    "reason": "这个服务没开审批中枢（没注入 approval），"
                              "有副作用的能力一律被注册表拒成 denied"}
        rows = f.approval.pending()
        return {"ok": True, "enabled": True, "count": len(rows), "pending": rows,
                "reason": ""}

    # ---------------- W3 设备页（主机可视化）----------------

    def _host_panel(self):
        """设备页的聚合数据：资源条 / 进程表 / 应用清单 / 屏幕预览。

        🔴 **一切能力调用都走 `registry.call`**，前端不许直接调设备——
        闸门在服务端，前端绕不过去（红灯 5）。
        🔴 **状态一律取自 `reg.availability()`**，界面不许自己算一套：
        「这台设备现在能不能用」只有注册表知道（它在探测、它在解释失败原因）。
        界面自己判断就会出现「界面说在线、实际调不动」的鬼故事。
        """
        f = self.fleet
        reg = f.registry
        rows = reg.availability()
        # 选哪台设备展示：优先本机（kind=host），否则第一台
        target = ""
        for r in rows:
            if r.get("kind") == "host":
                target = r["name"]
                break
        if not target and rows:
            target = rows[0]["name"]

        panel = {"ok": True, "device": target, "devices": rows,
                 "avail": None, "monitor": None, "apps": None, "screen": None,
                 "notes": []}

        if not target:
            panel["notes"].append("注册表里一台设备都没有：先让节点上线")
            return panel

        dev_row = next((r for r in rows if r["name"] == target), None)
        panel["avail"] = dev_row
        if dev_row is not None and not dev_row.get("ok"):
            panel["notes"].append(
                "这台设备现在不可用：%s" % (dev_row.get("reason") or "原因不明"))
            return panel

        # 资源快照（host.monitor）——失败就明说，不给假 0
        r = reg.call("host.monitor", args={}, gate=f.gate)
        if r.ok:
            panel["monitor"] = r.data
        else:
            panel["notes"].append("取不到资源快照：%s" % (r.err or "原因不明"))

        r = reg.call("host.apps", args={}, gate=f.gate)
        if r.ok:
            panel["apps"] = (r.data or {}).get("apps") or []
        else:
            panel["notes"].append("取不到进程清单：%s" % (r.err or "原因不明"))

        r = reg.call("host.screen", args={}, gate=f.gate)
        if r.ok:
            d = r.data or {}
            panel["screen"] = {"path": d.get("path"),
                               "shot_id": self.fleet.register_shot(d.get("path"))}
        else:
            panel["notes"].append("取不到屏幕：%s" % (r.err or "原因不明"))
        return panel

    def _shot_png(self):
        """把注册过的截图发回浏览器（W3 的屏幕预览要真图，不是盲文点阵）。

        只能发**注册表里登记过的**那张图（`fleet.register_shot` 登记的 id）——
        不接受任意路径参数，否则这个端点就成了任意文件读取的洞。
        """
        q = urlparse(self.path)
        sid = parse_qs(q.query).get("id", [""])[0]
        if not sid:
            return self._err(400, "缺少 id 参数（要哪张截图）")
        path = self.fleet.shot_path(sid)
        if not path:
            return self._err(404, f"没有这张截图（id={sid}）：可能已过期或从未登记")
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError as e:
            return self._err(500, f"读截图失败：{e}")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _post_approval_respond(self, body):
        """主人对一条待审批表态（同意 / 拒绝）。

        只负责**送达一次表态**；判定（要不要审批、几级、要不要双确认）全在
        注册表层与审批中枢里，这里不重做一遍。
        """
        f = self.fleet
        if f.approval is None:
            return self._err(409, "这个服务没开审批中枢，没有什么可回应的")
        rid = (body.get("request_id") or "").strip()
        if not rid:
            return self._err(400, "缺少 request_id 字段（回应哪一条审批）")
        if "approve" not in body:
            return self._err(400, "缺少 approve 字段（true=同意，false=拒绝）")
        approve = body.get("approve")
        if not isinstance(approve, bool):
            return self._err(400, "approve 必须是布尔值（true/false）")
        double = bool(body.get("double"))
        reason = (body.get("reason") or "").strip()
        session = body.get("session")
        if session is not None and not isinstance(session, str):
            return self._err(400, "session 必须是字符串")
        ok, why = f.approval.respond(rid, approve, reason=reason,
                                      session=session, double=double)
        if not ok:
            # 409 = 「这条已经结束了/不属于你」；不是 500，也不是假装成功。
            return self._err(409, why)
        return self._json({"ok": True, "request_id": rid,
                           "decision": "approved" if approve else "denied"})

    def _post_call(self, body):
        cap = (body.get("cap") or "").strip()
        if not cap:
            return self._err(400, "缺少 cap 字段（要调哪项能力）")
        if self.fleet.registry.capability(cap) is None:
            return self._err(404, f"注册表里没有这项能力: {cap}")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return self._err(400, "args 必须是 JSON 对象")
        tag = (body.get("tag") or "").strip()
        session = body.get("session")
        if session is not None and not isinstance(session, str):
            return self._err(400, "session 必须是字符串")
        res = self.fleet.call(cap, args=args, device=(body.get("device") or ""),
                              tag=tag, session=session or "")
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

    def _post_memory_delete(self, body):
        """逐条删一条记忆（E-7）。

        🔴 只认 (ns, key) 这一对**可寻址**坐标，不接受模糊删除。
        删不掉就说删不掉——不许返回「已删除」却什么都没删。
        """
        ns = (body.get("ns") or "").strip()
        key = (body.get("key") or "").strip()
        if not ns or not key:
            return self._err(400, "缺少 ns 或 key（记忆条目靠这两个坐标寻址）")
        try:
            import memory as mem
        except Exception as e:
            return self._err(500, f"记忆模块用不了：{type(e).__name__}: {e}")
        path = os.environ.get("FLEET_CTX", os.path.join(ROOT, "fleet_context.db"))
        try:
            ctx = mem.SharedContext(path)
            try:
                before = ctx.get(ns, key)
                if not before.get("ok"):
                    return self._err(404, f"没有这条记忆：{ns}/{key}")
                ctx.delete(ns, key)
                after = ctx.get(ns, key)
            finally:
                ctx.close()
        except Exception as e:
            return self._err(500, f"删记忆失败：{type(e).__name__}: {e}")
        if after.get("ok"):
            return self._err(500, f"说删了但它还在：{ns}/{key}")
        return self._json({"ok": True, "deleted": {"ns": ns, "key": key},
                            "remaining": self._memory().get("count", 0)})

    # ---------------- SSE 对话 ----------------

    # ---------------- 语音（对话面：光球视图用） ----------------
    #
    # 与 fleet/gui/voice.py 的「命令式语音」不是一回事，两者别混：
    #   那边 = 把「打开浏览器」听成能力调用、走 Polaris，服务于操控电脑；
    #   这边 = 陪伴对话，识别 → 人格 → 合成，服务于光球界面。
    # 所以这里要的是**仓库根目录的 voice.py**（kokoro/edge-tts + faster-whisper）。
    # 同名风险：本包内也有 voice.py，`import voice` 可能命中它 ⇒ 取到后校验
    # __file__ 必须落在 ROOT，拿错就带原因抛，不静默降级。

    def _dialog_voice(self):
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import voice as v
        got = os.path.dirname(os.path.abspath(getattr(v, "__file__", "") or ""))
        if got != os.path.abspath(ROOT):
            raise RuntimeError(
                "拿到的是 %s 的 voice（命令式那套），不是仓库根的对话语音模块" % got)
        return v

    def _read_raw(self, limit=32 * 1024 * 1024):
        """读二进制请求体。上限 32MB：一两分钟的 wav 远到不了。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length 不是整数")
        if n <= 0:
            return b""
        if n > limit:
            raise ValueError("音频太大：%d 字节（上限 %d）" % (n, limit))
        return self.rfile.read(n)

    def _post_voice_recognize(self):
        try:
            raw = self._read_raw()
        except ValueError as e:
            return self._err(400, str(e))
        if not raw:
            return self._err(400, "空音频：请求体是 0 字节")
        try:
            v = self._dialog_voice()
        except Exception as e:
            return self._err(503, "语音模块不可用：%s" % e)

        tmp_dir = v.TMP_DIR
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError as e:
            return self._err(500, "临时目录不可用：%s（%s）" % (tmp_dir, e))

        stamp = int(time.time() * 1000)
        src = os.path.join(tmp_dir, "api_%d.bin" % stamp)
        wav = src
        try:
            with open(src, "wb") as f:
                f.write(raw)
            # whisper 只吃 wav；手机 MediaRecorder 给的是 webm/opus，
            # edge_tts 给的是 mp3，别的客户端也可能直传任意容器。
            # 2026-10-22：改走 v.any_to_wav —— 内部优先 PyAV（faster-whisper
            # 自带，冻扁包里一定有），外部 ffmpeg 只做兜底。之前硬依赖
            # ffmpeg，冻扁包里没有 ⇒ 回喂 TTS 产物一律 503。
            if raw[:4] != b"RIFF":
                wav = os.path.join(tmp_dir, "api_%d.wav" % stamp)
                if not v.any_to_wav(src, wav):
                    return self._err(503, "收到非 wav 音频（头 %s），PyAV 与 ffmpeg 都不可用"
                                     % raw[:4].hex())
            text, meta = v.transcribe(wav, language="zh")
        except Exception as e:
            return self._err(500, "识别失败：%s: %s" % (type(e).__name__, e))
        finally:
            for p in {src, wav}:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
        body = (text or "").strip()
        # voice.transcribe 内部吞异常、把原因塞进 meta["error"]。空文本 + 有 error
        # 就是"识别器坏了"，不能报 ok:true —— 2026-10-22 实测：冻扁包里 ASR 崩了，
        # 接口却回 ok:true/text:""，前端只会显示"没听清，再说一次"，人永远查不出来。
        if not body and isinstance(meta, dict) and meta.get("error"):
            return self._err(500, "识别失败：%s" % meta["error"])
        return self._json({"ok": True, "text": body, "meta": meta})

    def _post_voice_say(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            return self._err(400, "缺少 text（或 text 是空的）")
        card_id = body.get("card_id")
        if not card_id:
            # 没指定就用当前生效的卡 ⇒ 「换卡换音色」自动成立，前端不必自己传。
            try:
                import persona_card as pc
                card_id = pc.current()
            except Exception:
                card_id = None
        try:
            v = self._dialog_voice()
        except Exception as e:
            return self._err(503, "语音模块不可用：%s" % e)
        try:
            backend, path = v.say_for_card(text, card_id=card_id, play=False)
        except Exception as e:
            return self._err(500, "合成失败：%s: %s" % (type(e).__name__, e))
        if not path or not os.path.isfile(path):
            # backend == "none" 的意思是 kokoro 和 edge_tts 都没成——这个字符串
            # 本身没法排查，把"为什么"直接带出去。冻扁包最常见的就是这一条。
            if backend == "none":
                return self._err(500,
                                 "没有可用的 TTS 后端：kokoro 需要 torch（冻扁包已排除），"
                                 "所以只能走 edge_tts（在线）。检查 ①edge-tts 是否装了 "
                                 "②这台机器能不能连微软的语音服务")
            return self._err(500, "合成失败：后端 %r 没有产出文件" % (backend,))
        ctype = v.audio_kind(path)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._err(500, "读不到合成产物 %s：%s" % (path, e))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        self.send_response(200)
        # 按真实字节给 MIME：冻扁包没有 ffmpeg ⇒ edge_tts 的 mp3 转不成 wav，
        # 之前一律声称 audio/wav 是撒谎。浏览器靠嗅探还能放，但 X-Voice-Format
        # 骗人的话，客户端做缓存/拼接就会出错。
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Voice-Backend", backend or "")
        self.send_header("X-Voice-Format", ctype.split("/")[-1])
        self.send_header("X-Voice-Card", card_id or "")
        self.end_headers()
        self.wfile.write(data)

    def _post_chat(self, body):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._err(400, "缺少 prompt（或 prompt 是空的）")
        # backends 不传 = 空 = 全部后端（零回归）；传了就只走那几个。
        backends = (body.get("backends") or "").strip()
        q = self.fleet.chat(prompt, backends)
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
