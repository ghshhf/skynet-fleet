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
import socket
import ssl
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .. import device_auth as DEVAUTH
from .. import pair as PAIR
from .. import pairtls as PTLS
from ..catalog import build
from .approval import ApprovalBroker

# 仓库根（fleet/gui/server.py → 上三级）。角色卡、人格状态这些平铺模块都在根下。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIR = os.path.join(ROOT, "ui")
TOKEN_HEADER = "X-Fleet-Token"
SESSION_HEADER = "X-Fleet-Session"
#: 长期设备凭据（配对换来的）。与上面两个不是一回事：
#:   X-Fleet-Token   —— 本窗口现生成，重启即失效（联调/本机网页用）
#:   X-Fleet-Session —— 被控侧信箱发的会话令牌（另一条 TLS 通道用）
#:   X-Fleet-Device  —— 配对一次长期有效（手机用这个，重启不用重新抄令牌）
DEVICE_HEADER = "X-Fleet-Device"
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

def _hdrsafe(v):
    """把任意文本变成合法的 HTTP 头值。

    🔴 血泪：头只能装 latin-1。角色卡 id 是中文时，`send_header` 会抛
    `UnicodeEncodeError: 'latin-1' codec can't encode ...` —— 而那一刻
    响应头已经写了一部分，客户端看到的是「unexpected end of stream」。
    现象是「语音有时候不出声」，日志里也只是一串断在 send_header 的栈，
    完全看不出根因是中文。这里按 RFC 3986 百分号编码，ASCII 原样保留。
    """
    s = str(v or "")
    out = []
    for ch in s:
        o = ord(ch)
        if 32 <= o < 127:
            out.append(ch)
        else:
            out.extend("%%%02X" % b for b in ch.encode("utf-8"))
    return "".join(out)


def _fold_history(history, prompt, max_turns=6, max_chars=4000):
    """把多轮历史折成一段可读的前缀。

    取**最近** max_turns 轮（越近越相关，早期的最没价值），再按总字数截断——
    否则聊上二十轮，prompt 会膨胀到后端拒收。截断从最老的开始丢，
    丢完还要保证最后一条是「本轮问题」，不能让历史把问题挤掉。
    """
    if not isinstance(history, list) or not history:
        return prompt
    rows = []
    for h in history[-max_turns * 2:]:
        if not isinstance(h, dict):
            continue
        role = str(h.get("role") or "").strip()
        text = str(h.get("content") or h.get("text") or "").strip()
        if not text:
            continue
        if role == "assistant":
            rows.append(("它", text))
        elif role in ("user", "me"):
            rows.append(("我", text))
    if not rows:
        return prompt
    # 从老到新累加，超预算就把最老的丢掉
    while rows and sum(len(t) for _, t in rows) > max_chars:
        rows.pop(0)
    if not rows:
        return prompt
    lines = ["[之前的对话，按时间顺序]"]
    for who, t in rows:
        lines.append("%s：%s" % (who, t.replace("\n", " ")[:600]))
    lines.append("[本轮]")
    lines.append(prompt)
    return "\n".join(lines)


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
        # ---- 配对信箱（N+1b）：被控侧状态机 + 独立 TLS 监听器 ----
        # 信箱默认关闭；enable_pairing() 才开（fail-closed，同 --lan 的显式纪律）。
        # 监听器是**第二个** HTTP 服务（0.0.0.0 + TLS），只服务 /api/pair 与
        # /api/remote/*——GUI 主循环仍只绑 127.0.0.1，两边的攻击面不互通。
        self.mailbox = PAIR.PairingMailbox(name=socket.gethostname(),
                                           journal=evidence_journal)
        # ---- 设备凭据库：配对一次、长期免令牌 ----
        # 与上面的信箱分工不同，别混：
        #   信箱（mailbox）  = 「谁在控我」的会话，进程内，重启即清空；
        #   凭据库（devices）= 「这台设备被允许进门」的事实，落盘，重启仍在。
        # 手机走的是后者——主人要的是「配对完就不用再配」，不是「每次开机重配」。
        self.devices = DEVAUTH.DeviceVault(name=socket.gethostname())
        self.pair_httpd = None
        self.pair_tls = None          # {cert, key, fp, dir}
        self.pair_beacon = None
        self.pair_port = 0
        self.pair_host = ""

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
        self.disable_pairing()

    # ---------------- 配对信箱（N+1b 被控侧） ----------------

    def enable_pairing(self, pair_host="0.0.0.0", pair_port=None,
                       require_tls=True):
        """开「可被配对」：起广播 + 独立 TLS 监听器 + 备好验证码。

        显式动作（GUI 开关），不是默认态——同 `--lan` 的纪律：
        局域网里能被连进来驱动桌面，必须是主人亲手点的。
        require_tls=False 只允许 pair_host 是回环（测试用）；LAN 上
        禁明文——不给自己留「忘了开加密」的口子。
        """
        if self.mailbox.enabled:
            raise RuntimeError("配对已经开着了（先 disable_pairing）")
        pair_port = int(pair_port or PAIR.PAIR_TCP_PORT)
        if require_tls:
            san = [pair_host] if pair_host not in ("0.0.0.0", "::", "") \
                else ["127.0.0.1"]
            try:
                local_ips = PAIR.local_ips()
            except Exception:
                local_ips = []
            san += [ip for ip in local_ips if ip not in san]
            self.pair_tls = PTLS.gen_selfsigned(
                tempfile.mkdtemp(prefix="fleet_pair_tls_"), san_ips=san)
        elif pair_host not in ("127.0.0.1", "::1"):
            raise RuntimeError(
                "禁明文配对：只有回环地址允许 require_tls=False（测试用）")
        self.pair_host = pair_host
        self.pair_port = pair_port
        self.mailbox.enabled = True
        code = self.mailbox.refresh_code()
        httpd = ThreadingHTTPServer((pair_host, pair_port), _Handler)
        httpd.daemon_threads = True
        httpd.fleet = self
        if self.pair_tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.pair_tls["cert"], self.pair_tls["key"])
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self.pair_httpd = httpd
        self.pair_port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.2}, daemon=True).start()
        self.pair_beacon = PAIR.Beacon(name=self.mailbox.name,
                                       pair_port=self.pair_port)
        self.pair_beacon.start()
        return self.pair_info()

    def pair_info(self):
        """GUI 被控侧面板要的一切：码、链接、指纹、监听地址。"""
        code = self.mailbox.code_state()
        cur = self.mailbox.current_code() if self.mailbox.enabled else ""
        host = self.pair_host if self.pair_host not in ("0.0.0.0", "::", "") \
            else "127.0.0.1"
        return {
            "enabled": self.mailbox.enabled,
            "name": self.mailbox.name,
            "code": cur,
            "code_state": code,
            "link": PAIR.make_link(host, self.pair_port, cur,
                                   (self.pair_tls or {}).get("fp", "")[:8])
            if cur else "",
            "fp": (self.pair_tls or {}).get("fp", ""),
            "pair_host": self.pair_host,
            "pair_port": self.pair_port,
            "tls": bool(self.pair_tls),
        }

    def disable_pairing(self):
        """关配对：停广播、停监听、**所有会话当场作废**（熔断）。"""
        if self.pair_beacon is not None:
            self.pair_beacon.stop()
            self.pair_beacon = None
        if self.pair_httpd is not None:
            self.pair_httpd.shutdown()
            self.pair_httpd.server_close()
            self.pair_httpd = None
        n = self.mailbox.revoke_all()
        self.mailbox.enabled = False
        if self.pair_tls:
            for k in ("cert", "key"):
                try:
                    os.unlink(self.pair_tls[k])
                except OSError:
                    pass
            try:
                os.rmdir(self.pair_tls.get("dir") or "")
            except OSError:
                pass
            self.pair_tls = None
        return n

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

    def chat(self, prompt, backends="", history=None, web="auto"):
        """对话：走 registry.call('ai.chat')，带 _on_line 回调。

        backends：逗号分隔的后端白名单（空 = 全部后端，跟以前一样）。
        手机语音一次只念一句，多后端一起回会串台，所以手机端固定传一个。

        history：之前的来回 [{role, content}, ...]。**为什么折进 prompt 而不是
        传 messages 数组**：ai.chat 下面挂着十几个后端，绝大多数只吃单条 prompt，
        在这一层改成 messages 会让所有适配器一起崩。折成带标记的转写，
        语义等价、零回归，代价是历史也算进 prompt 的 token。

        web：「off」不联网 /「on」强制联网 /「auto」按问题判断（默认）。
        联网在这里做、**不在 ai.chat 里做**：ai.chat 挂着十几个后端，
        每个后端都去搜一遍是浪费；而且搜索是 10~20 秒的阻塞活，必须在
        这一层先把「正在查」推给前端，否则人看到的是一块不动的空白。

        reg.call 是阻塞的（生成完才返回），所以这里在**工作线程**里跑它，
        把 _on_line 收到的行推进队列；调用方（HTTP 线程）从队列取，边收边写 SSE。
        """
        # 联网只该看「这一轮问了什么」。必须先存下原始问题：下面 prompt 会被
        # 折成「用户:… 助手:… 用户:…」的转写，拿它去搜会变成一堆对话标记，
        # 搜出来全是垃圾；而且缓存 key 每轮都不同，缓存等于没有。
        raw_prompt = prompt
        prompt = _fold_history(history, prompt)
        system = self.persona.system_prompt(prompt)
        q = queue.Queue()
        web_mode = (web or "auto").strip().lower() if isinstance(web, str) else "auto"

        def _on_line(text):
            q.put({"type": "line", "text": text})

        def _work():
            try:
                sys2 = system
                # ── 工具面（2026-10-24）：决策 → 执行 → 资料回填 ──
                # 三档语义：
                #   web=off   → 不做任何工具（用户明确关了）
                #   web=on    → 强制 web_search（用户明确要联网）
                #   web=auto  → 问决策模型 7 选 1（6 工具 + none）；
                #               决策模型 = 手机 1.7B（schema 枚举实测 16/16）
                # 决策层连不上 / 超时 → 按无工具面继续答，不重试不拖垮对话。
                try:
                    if web_mode == "on":
                        import webtool as WEB
                        q.put({"type": "web", "data": {"phase": "searching",
                                                       "mode": "on"}})
                        sys2, rep = WEB.augment(system, raw_prompt, mode="on")
                        rep["phase"] = "done"
                        q.put({"type": "web", "data": rep})
                    elif web_mode == "auto":
                        import fleet.agent_tools as AT
                        d = AT.decide(raw_prompt)
                        if d and d.get("tool") != "none":
                            tool = d["tool"]
                            q.put({"type": "web", "data": {
                                "phase": "searching", "mode": "tool",
                                "tool": tool}})
                            if tool == "web_search":
                                import webtool as WEB
                                sys2, rep = WEB.augment(
                                    system, raw_prompt, mode="on")
                                rep["phase"] = "done"
                                q.put({"type": "web", "data": rep})
                            else:
                                label, text = AT.execute(
                                    tool, d.get("arg", ""), self.call)
                                block = AT.render_tool_block(label, text)
                                sys2 = system + block
                                q.put({"type": "web", "data": {
                                    "phase": "done", "used": True,
                                    "mode": "tool", "tool": tool,
                                    "label": label,
                                    "sources": [{"title": f"手机工具 {label}",
                                                 "url": ""}]}})
                except Exception as e:
                    # 工具面是增强不是前置条件：挂了照常答，但要说出来。
                    q.put({"type": "web", "data": {
                        "phase": "error", "used": False,
                        "reason": f"工具面：{type(e).__name__}: {e}"}})
                r = self.executor.call(
                    "ai.chat",
                    args={"prompt": prompt, "system": sys2,
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

    def _authorized(self, gui_only=False):
        """校验请求凭据。两种都认，但有先后：

          1. `X-Fleet-Token`  —— 本窗口注入的一次性令牌（本机网页、联调脚本）
          2. `X-Fleet-Device` —— 配对换来的长期设备凭据（手机走这条）

        为什么必须加第 2 条：令牌每次启动都重新随机，只活内存。电脑一重启
        手机就 403 —— 这不是主人用错了，是设计缺陷。配对一次就该一直能用，
        撤销由主人单方面决定，不该由「电脑重启了」决定。

        401 = 压根没带凭据；403 = 带了但对不上（旧令牌/已撤销设备/伪造）。
        分开是为了在日志里区分「调用方忘了加头」（bug）和「有人拿旧凭据来敲」
        （可疑行为）。比对一律 secrets.compare_digest，防时序侧信道。
        """
        self._device = None
        got = self.headers.get(TOKEN_HEADER) or ""
        if got:
            if secrets.compare_digest(got, self.fleet.token):
                return True, 200, ""
            return False, 403, "令牌不对：不是本次启动注入的那枚"

        raw = self.headers.get(DEVICE_HEADER) or ""
        if raw:
            if gui_only:
                # 管理类接口（读配对码/撤销设备）只认本机令牌：
                # 否则任意一台已配对设备就能替主人发新码、踢别人。
                return False, 403, f"{DEVICE_HEADER} 不行：这个接口只认本机令牌"
            pair = DEVAUTH.parse_header(raw)
            if not pair:
                return False, 403, (f"{DEVICE_HEADER} 格式不对"
                                    "（应为 dev-xxx:fw_yyy）")
            dev = self.fleet.devices.verify(*pair)
            if dev is None:
                return False, 403, "设备凭据无效（没配对、已被撤销，或是伪造的）"
            self._device = dev
            return True, 200, ""

        # 文案口径（2026-10-23 对齐全仓门禁）：先按机器可读的方式说清是哪一类
        # 凭据问题——「缺少 token」= 压根没带，和下面 403 的「带了但不对」必须分得开；
        # 再给人一句可执行的下一步（配对码怎么走）。
        return False, 401, (f"缺少 token（没带凭据）：这台设备还没配对。"
                            f"电脑上点「生成配对码」，"
                            f"手机里输入那 6 位即可（之后长期有效，不用再配）")

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
        # 配对/受控端点走自己的鉴权（会话令牌，不是 GUI 令牌）——
        # 必须在 _authorized 之前分流，否则主控端永远卡在 401。
        if path.startswith("/api/pair") or path.startswith("/api/remote"):
            return self._pair_api(path)
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
        if path == "/api/fleet":
            return self._json(self._fleet())
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
        if path == "/api/skills/evolved":
            return self._json(self._skills_evolved())
        if path == "/api/skill/stats":
            return self._json(self._skill_stats())
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
        # 同上：配对/受控端点用自己的鉴权，先分流。
        if path.startswith("/api/pair") or path.startswith("/api/remote"):
            return self._pair_api(path)
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
        if path == "/api/skill/use":
            return self._post_skill_use(body)
        if path == "/api/skill/propose":
            return self._post_skill_propose(body)
        if path == "/api/skill/promote":
            return self._post_skill_promote(body)
        if path == "/api/skill/rollback":
            return self._post_skill_rollback(body)
        self._err(404, f"没有这个接口: {path}")

    # ---------------- 配对 / 受控端点（N+1b） ----------------
    #
    # 这一组只活在配对监听器上（0.0.0.0 + TLS），鉴权用**会话令牌**
    # （X-Fleet-Session），不是 GUI 的 X-Fleet-Token。两类端点：
    #   /api/pair/*       配对握手（持码换令牌）
    #   /api/remote/*     受控能力（持令牌代理 pcctl.*）
    #
    # 🔴 受控能力走**白名单映射**，不是「传什么能力名就调什么」：
    # 远端能摸到的只有 pcctl.* 这一族；ai.chat / card.* / skill.* 这些
    # 想都别想（不在表里就是 404）。闸门仍在被控端本地：ask 档没批就是
    # denied，一次系统调用都不发生。

    REMOTE_CAPS = {
        # 只读一档（approval=auto）：看不是改，不弹窗
        "screenshot": "pcctl.screenshot",
        "windows": "pcctl.windows",
        "sysinfo": "pcctl.sysinfo",
        "clipboard": "pcctl.clipboard.read",
        "procs": "pcctl.proc.list",
        # 动手一档（approval=ask）：被控端审批弹窗不放行就 denied
        "mouse": "pcctl.mouse.move",
        "click": "pcctl.mouse.click",
        "key": "pcctl.key",
        "text": "pcctl.text",
        "clipboard_set": "pcctl.clipboard.write",
        "launch": "pcctl.app.launch",
        "kill": "pcctl.proc.kill",
    }

    def _session_authorized(self):
        """校验 X-Fleet-Session（配对握手发的那枚一次性令牌）。

        401 = 没带；403 = 带了但对不上（过期/已 revoke/根本不是本站发的）。
        比对走 mailbox.session_for：revoke 是即时的，不存在「缓存里
        还能用一会儿」。
        """
        got = self.headers.get(SESSION_HEADER) or ""
        if not got:
            return None, 401, f"缺少 {SESSION_HEADER} 头：先过 /api/pair 握手"
        s = self.fleet.mailbox.session_for(got)
        if s is None:
            return None, 403, "会话令牌无效（未配对、已过期或已被掐断）"
        return s, 200, ""

    def _pair_api(self, path):
        f = self.fleet
        # ---- 长期设备凭据（手机端配对走这组）----
        # 码本身**没有任何接口吐出来**：只能从电脑屏幕上肉眼抄。
        # 码一旦能通过网络读，配对就退化成「谁先连上谁赢」。
        if path == "/api/pair/claim":
            return self._post_pair_claim()
        if path == "/api/pair/me":
            return self._get_pair_me()
        if path == "/api/pair/devices":
            return self._get_pair_devices()
        if path == "/api/pair/code":
            return self._post_pair_new_code()
        if path == "/api/pair/devices/revoke":
            return self._post_pair_revoke_device()
        if path == "/api/pair/state":
            return self._json({"ok": True, "enabled": f.mailbox.enabled,
                               "name": f.mailbox.name})
        if path == "/api/pair":
            return self._post_pair()
        if path == "/api/remote/revoke":
            return self._post_remote_revoke()
        if path.startswith("/api/remote/shot"):
            return self._get_remote_shot()
        if path.startswith("/api/remote/"):
            return self._post_remote(path[len("/api/remote/"):])
        self._err(404, f"没有这个接口: {path}")

    # ---------------- 长期设备凭据 ----------------

    def _post_pair_claim(self):
        """设备持 6 位码换长期凭据。成功一次后这台设备不用再配。"""
        try:
            body = self._read_json()
        except ValueError as e:
            return self._err(400, str(e))
        r = self.fleet.devices.claim(
            body.get("code") or "",
            name=body.get("name") or body.get("device") or "",
            platform=body.get("platform") or "")
        if not r.get("ok"):
            # 401 而不是 400：语义是「凭据不足」，客户端据此提示重新配对
            return self._err(401, r.get("err") or "配对失败")
        return self._json({"ok": True, "device_id": r["device_id"],
                           "secret": r["secret"], "device": r.get("device"),
                           "host": socket.gethostname()})

    def _get_pair_me(self):
        """带设备凭据来问「我配对的那台电脑现在怎么样」。"""
        d = getattr(self, "_device", None)
        if not d:
            ok, code, why = self._authorized()
            if not ok:
                return self._err(code, why)
            d = self._device
        f = self.fleet
        return self._json({"ok": True, "device": d, "host": socket.gethostname(),
                           "caps": len(f.registry.capabilities()),
                           "devices": len(f.registry.devices())})

    def _get_pair_devices(self):
        """【本机】配对码状态 + 已配对设备清单。GUI 设置页读这个。"""
        ok, code, why = self._authorized(gui_only=True)
        if not ok:
            return self._err(code, why)
        f = self.fleet
        return self._json({"ok": True, "host": socket.gethostname(),
                           "code": f.devices.current_code(),
                           "code_state": f.devices.code_state(),
                           "devices": f.devices.list()})

    def _post_pair_new_code(self):
        ok, code, why = self._authorized(gui_only=True)
        if not ok:
            return self._err(code, why)
        c = self.fleet.devices.new_code()
        return self._json({"ok": True, "code": c,
                           "code_state": self.fleet.devices.code_state()})

    def _post_pair_revoke_device(self):
        ok, code, why = self._authorized(gui_only=True)
        if not ok:
            return self._err(code, why)
        try:
            body = self._read_json()
        except ValueError as e:
            return self._err(400, str(e))
        if body.get("all"):
            n = self.fleet.devices.revoke_all()
            return self._json({"ok": True, "revoked": n})
        did = body.get("device_id") or ""
        if not did:
            return self._err(400, "要指明 device_id（或传 all=true 全掐）")
        self.fleet.devices.revoke(did)
        return self._json({"ok": True, "revoked": 1})

    def _post_pair(self):
        try:
            body = self._read_json()
        except ValueError as e:
            return self._err(400, str(e))
        r = self.fleet.mailbox.try_pair(
            body.get("code") or "", controller_name=body.get("controller") or "",
            cert_fp=body.get("fp") or "")
        if not r.get("ok"):
            return self._err(401, r.get("err") or "配对失败")
        return self._json({"ok": True, "token": r["token"],
                           "fp": (self.fleet.pair_tls or {}).get("fp", ""),
                           "session": r.get("session")})

    def _post_remote_revoke(self):
        s, code, why = self._session_authorized()
        if s is None:
            return self._err(code, why)
        self.fleet.mailbox.revoke(self.headers.get(SESSION_HEADER) or "")
        return self._json({"ok": True, "revoked": True})

    def _get_remote_shot(self):
        """回取刚截的图（登记制，同 W3：远端不许指定任意文件路径）。"""
        s, code, why = self._session_authorized()
        if s is None:
            return self._err(code, why)
        q = parse_qs(urlparse(self.path).query)
        sid = (q.get("id") or [""])[0]
        p = self.fleet.shot_path(sid)
        if not p or not os.path.isfile(p):
            return self._err(404, f"没有这张截图（id={sid}）：先调 /api/remote/screenshot")
        try:
            data = open(p, "rb").read()
        except OSError as e:
            return self._err(500, f"截图文件读不出来：{e}")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _post_remote(self, name):
        s, code, why = self._session_authorized()
        if s is None:
            return self._err(code, why)
        cap = self.REMOTE_CAPS.get(name)
        if not cap:
            return self._err(404, f"受控端没有这个动作: {name}"
                                   "（可用：%s）" % "/".join(sorted(self.REMOTE_CAPS)))
        try:
            body = self._read_json()
        except ValueError as e:
            return self._err(400, str(e))
        args = body.get("args") if isinstance(body.get("args"), dict) else body
        r = self.fleet.call(cap, args=args, tag="remote:%s" % s["controller"])
        out = r if isinstance(r, dict) else {"ok": False, "err": "内部错误"}
        if name == "screenshot" and out.get("ok"):
            # 截图落的是被控端磁盘；登记后远端凭 id 回取，不给路径直读
            sid = self.fleet.register_shot((out.get("data") or {}).get("path") or "")
            out["shot_id"] = sid
        return self._json(out)

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

    def _fleet(self):
        """编队总览：一屏看完「谁在线 / 有没有活 / 活干成没干成 / 产物堆了多少」。

        为什么值得单独做一个聚合口
        --------------------------
        GUI 已经有 20 多个单项接口，但「现在整个编队什么状态」得自己拼好几个
        接口、还得自己算失败率 —— 一算就容易算错。口径必须在**这里**定一次。

        两条口径钉死（前端不许自己再算一遍）：
          1. `success_rate` 的分母是 done + failed，**cancelled 不算失败**。
             主动取消是主人的决定，不是系统出错；把它算进失败率会让
             「我一取消就变红」这种假警报天天出现。
          2. 一个作业都没跑过时 `success_rate` 是 **None（未知）而不是 0%**。
             0% 会读成「全失败了」，但真相是「还没数据」——这两件事不能混。

        凡是取不到的部分一律带 notes 说明原因，不给假 0、不装成功。
        """
        f = self.fleet
        reg = f.registry
        now = time.time()
        notes = []

        # ---- 1) 设备：谁在线、能干什么 ----
        try:
            rows = reg.availability()
        except Exception as e:
            rows = []
            notes.append("注册表读不到设备清单：%s" % (str(e)[:80]))
        dev_rows = []
        for r in rows:
            dev_rows.append({
                "name": r.get("name", ""),
                "kind": r.get("kind", ""),
                "via": r.get("via", ""),
                "ok": bool(r.get("ok")),
                "reason": r.get("reason") or "",
                "caps": len(r.get("caps") or []),
                "sample": (r.get("caps") or [])[:3],
            })
        online = [d for d in dev_rows if d["ok"]]
        # 离线设备按名字排前面：出问题的那台应该第一眼看到，不是沉在列表底下
        dev_rows.sort(key=lambda d: (d["ok"], d["name"]))

        # ---- 2) 作业：排队/在跑/成败/卡住 ----
        jobs_out = {"queued": 0, "running": 0, "done": 0, "failed": 0,
                    "cancelled": 0, "total": 0, "success_rate": None,
                    "stale": 0, "recent": []}
        try:
            from fleet import jobs as J
            js = J.get_store()
            st = js.stats()
            for k in ("queued", "running", "done", "failed", "cancelled", "total"):
                jobs_out[k] = int(st.get(k) or 0)
            tried = jobs_out["done"] + jobs_out["failed"]
            if tried:
                jobs_out["success_rate"] = round(jobs_out["done"] / tried, 3)
            for j in js.list(limit=8):
                # 租约过期还在 running = 卡住（节点掉线没回报），提示可以 reap
                stale = (j.get("state") == "running"
                         and float(j.get("lease_until") or 0) < now)
                if stale:
                    jobs_out["stale"] += 1
                jobs_out["recent"].append({
                    "id": j.get("id", ""), "cap": j.get("cap", ""),
                    "state": j.get("state", ""), "node": j.get("node") or "",
                    "attempts": j.get("attempts", 0),
                    "max_attempts": j.get("max_attempts", 0),
                    "created_at": j.get("created_at"),
                    "err": ((j.get("result") or {}).get("err") or "")[:80],
                    "artifacts": len(j.get("artifacts") or []),
                    "stale": stale,
                })
            if jobs_out["stale"]:
                notes.append("有 %d 个作业租约过期还在 running：节点掉线没回报，"
                             "跑一次 job reap 会退回队列换机重跑" % jobs_out["stale"])
        except Exception as e:
            notes.append("作业账本读不到：%s" % (str(e)[:80]))

        # ---- 3) 产物 ----
        art_out = {"count": 0, "bytes": 0, "kinds": {}}
        try:
            from fleet import artifacts as A
            art_out.update(A.get_store().stats())
        except Exception as e:
            notes.append("产物仓库读不到：%s" % (str(e)[:80]))

        # ---- 4) 手机快照（只读能力，失败就明说） ----
        phone = {"available": False, "brief": "", "anomalies": [], "device": ""}
        android_devs = [d for d in dev_rows if d["kind"] in ("android", "adb_wifi", "phone")]
        if not android_devs:
            phone["note"] = "编队里没有手机类设备"
        elif not any(d["ok"] for d in android_devs):
            phone["device"] = android_devs[0]["name"]
            phone["note"] = "手机不在线：%s" % (android_devs[0]["reason"] or "原因不明")
        else:
            r = reg.call("state.read", args={}, gate=f.gate)
            phone["device"] = next((d["name"] for d in android_devs if d["ok"]), "")
            if r.ok:
                d = r.data or {}
                phone.update({"available": True,
                              "brief": d.get("brief", ""),
                              "anomalies": d.get("anomalies") or []})
            else:
                phone["note"] = "读不到手机状态：%s" % (r.err or "原因不明")

        # ---- 5) 两台本机网关（有缓存文件才报，没有就 null，不假装） ----
        gws = self._gateway_digest()

        return {
            "ok": True,
            "ts": now,
            "devices": {"total": len(dev_rows), "online": len(online),
                        "offline": len(dev_rows) - len(online), "rows": dev_rows},
            "caps": {"online": sum(d["caps"] for d in online),
                     "total": sum(d["caps"] for d in dev_rows)},
            "jobs": jobs_out,
            "artifacts": art_out,
            "phone": phone,
            "gateways": gws,
            "notes": notes,
        }

    @staticmethod
    def _gateway_digest():
        """两台本机免费网关的当前账：活几条、几条在等额度刷新。

        只读缓存文件，**不触发探活**——总览页必须秒开，不能因为探活卡 30 秒。
        要刷新就点「重探」按钮（/api/llm/refresh），或者命令行跑 gateway_pool。
        缓存不存在时返回 None，前端显示「没探过」，而不是显示 0。
        """
        import json as _json
        p = os.path.expanduser(os.environ.get(
            "FLEET_GW_HEALTH", "~/.workbuddy/gateway_health.json"))
        try:
            with open(p, encoding="utf-8") as fh:
                d = _json.load(fh)
        except (OSError, ValueError):
            return None
        now = time.time()
        out = {}
        for name, gw in (d.get("gateways") or {}).items():
            ms = gw.get("models") or {}
            live = [m for m, r in ms.items() if r.get("ok")]
            waiting = [m for m, r in ms.items() if not r.get("ok")]
            quota = [m for m in waiting
                     if (ms[m].get("kind") in ("quota", "rate"))]
            # 最近一次「等 TTL 到期」的时间：过期后会自动重探，所以给前端看
            soonest = None
            for m in waiting:
                t = float(ms[m].get("ts") or 0) + float(ms[m].get("ttl") or 0)
                if soonest is None or t < soonest:
                    soonest = t
            out[name] = {"total": len(ms), "live": len(live),
                         "waiting": len(waiting), "quota_waiting": len(quota),
                         "probed_at": gw.get("probed_at"),
                         "next_reprobe_in": (max(0, int(soonest - now))
                                             if soonest else None),
                         "live_models": live[:6]}
        return out or None

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

    # ---------------- 自写技能（skills_evolved/）----------------
    #
    # 与 /api/skills 分开：那边是 vendored 第三方 + 用户级，只读清单；
    # 这边是「从跑通过的执行里长出来的」，带选用/记分/提交/回滚四个动作。
    # 分开两个端点，两套账，跟目录分开是同一个理由（P3 定的）。

    def _skills_evolved(self):
        """自写技能清单（只读）。正文不外发，同 skills.summary() 的纪律。"""
        from ..skills_evolve import evolved_summary
        try:
            return evolved_summary()
        except Exception as e:
            return {"ok": False, "count": 0, "skills": [],
                    "error": f"{type(e).__name__}: {e}"}

    def _skill_stats(self):
        """一张自写技能的记分。

        🔴 verdict 原样带回：「证据不足，分数不可信」不许前端自己解读成
        一个百分数——没有证据时明确说没有，是 P4 定的纪律。
        """
        from urllib.parse import parse_qs
        from ..skills_evolve import skill_stats, SkillError
        p = parse_qs(urlparse(self.path).query)
        name = (p.get("name") or [""])[0].strip()
        if not name:
            return {"ok": False, "err": "缺少 name（要查哪张技能的记分）"}
        try:
            st = skill_stats(name)
        except SkillError as e:
            return {"ok": False, "err": str(e)}
        except Exception as e:
            return {"ok": False, "err": f"{type(e).__name__}: {e}"}
        return {"ok": True, **st}

    def _post_skill_use(self, body):
        """记一次「这张技能被用了」（选用留痕）。

        这是**打点**不是审批：技能被读进提示词那一刻打一条，
        成功 done、失败 open。它只说明「有人调用了这张卡」，
        不说明「被照着执行了」——后者本仓观测不到，别假装能。
        """
        from ..skills_evolve import note_use, _safe_name, SkillError
        name = (body.get("name") or "").strip()
        if not name:
            return self._err(400, "缺少 name（要记哪张技能被用了）")
        try:
            _safe_name(name)
            i = note_use(name, ok=bool(body.get("ok", True)),
                         detail=str(body.get("detail") or "")[:120])
        except SkillError as e:
            return self._err(400, str(e))
        except Exception as e:
            return self._err(500, f"记不上：{type(e).__name__}: {e}")
        return self._json({"ok": True, "event_id": i, "name": name,
                           "used_ok": bool(body.get("ok", True))})

    def _post_skill_propose(self, body):
        """第一步：从一条执行长出候选，**不落盘**。

        为什么分两步：计划书§七「自动改技能并直接生效」明确不做——
        候选必须先给人看四闸结果，人点了确认才落盘。
        """
        from ..skills_evolve import propose, verify, SkillError
        trace = (body.get("trace") or "").strip()
        name = (body.get("name") or "").strip()
        desc = str(body.get("desc") or "")[:200]
        if not trace or not name:
            return self._err(400, "缺少 trace 或 name（从哪条执行链、长成什么名）")
        try:
            cand = propose(trace, name, desc)
        except SkillError as e:
            return self._json({"ok": False, "err": str(e), "stage": "propose"})
        except Exception as e:
            return self._err(500, f"提议失败：{type(e).__name__}: {e}")
        ok, gates = verify(cand)
        return self._json({"ok": ok, "stage": "verify", "name": name,
                           "trace": trace, "gates": gates,
                           "caps": cand.get("caps", []),
                           "evolved_from": cand.get("evolved_from", []),
                           "text": cand.get("text", "")})

    def _post_skill_promote(self, body):
        """第二步：落盘。服务端**重新 propose + 重新过闸**，不信任前端传来的文本。

        🔴 为什么不接受前端传候选文本：前端传什么就落什么 = 绕过四闸。
           两步都在服务端重算，前端带来的只有 (trace, name, desc) 三个参数。
        🔴 没有 force 开关：计划书§七「自动改技能并直接生效」不做。
           四闸不过就原样返回 gates，让前端显示哪闸红。
           `overwrite` 只放开「同名不覆盖」这一道守卫，四闸照样先跑——
           桌面端要能发布第二个版本，否则回滚永远碰不到（t18 立项时发现）。
        落盘后紧跟一次 scoped commit——**版本=git**，没有提交就没有快照，
        之后的回滚会没有目标。commit 不在 git 仓库里时不阻断落盘，
        但明确告诉前端「回滚不可用」。
        """
        from ..skills_evolve import (propose, promote, commit_evolved,
                                     SkillError)
        trace = (body.get("trace") or "").strip()
        name = (body.get("name") or "").strip()
        desc = str(body.get("desc") or "")[:200]
        if not trace or not name:
            return self._err(400, "缺少 trace 或 name（从哪条执行链、长成什么名）")
        try:
            cand = propose(trace, name, desc)
        except SkillError as e:
            return self._json({"ok": False, "err": str(e), "stage": "propose"})
        except Exception as e:
            return self._err(500, f"提议失败：{type(e).__name__}: {e}")
        ok, rep = promote(cand, allow_overwrite=bool(body.get("overwrite")))
        if not ok:
            r = dict(rep)
            # err / error 都认：promote 历史上用过 error，改成 err 之后
            # 这里两侧兼容，避免哪一边漏改就让失败原因整条消失。
            why = r.get("err") or r.get("error") or "落盘被拒"
            # 同名已存在时把「可以显式覆盖」说清楚，前端才知道下一步能干什么
            can_ow = bool(r.get("can_overwrite")) or "同名技能已存在" in str(why)
            return self._json({"ok": False, "stage": "promote", "err": why,
                               "gates": r.get("gates", []),
                               "can_overwrite": can_ow})
        sha = None
        commit_note = ""
        try:
            sha = commit_evolved(name)
        except SkillError as e:
            commit_note = str(e)
        if sha is None and not commit_note:
            commit_note = "没有可提交的改动（可能什么都没变）"
        return self._json({"ok": True, "stage": "promote", **rep,
                           "git": sha, "commit_note": commit_note,
                           "rollback_ready": bool(sha)})

    def _post_skill_rollback(self, body):
        """回滚一张自写技能到上一个已提交版本。

        自验三样（加载/审计/四闸）在 skills_evolve.rollback 里做，
        任一不过工作区会被改回原样——回滚不许把仓滚坏。
        """
        from ..skills_evolve import rollback, SkillError
        name = (body.get("name") or "").strip()
        if not name:
            return self._err(400, "缺少 name（要回滚哪张技能）")
        try:
            ok, rep = rollback(name, to_sha=(body.get("to") or "").strip() or None)
        except SkillError as e:
            return self._json({"ok": False, "err": str(e), "stage": "rollback"})
        except Exception as e:
            return self._err(500, f"回滚失败：{type(e).__name__}: {e}")
        return self._json({"ok": ok, "stage": "rollback", **rep})

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
        self.send_header("X-Voice-Backend", _hdrsafe(backend))
        self.send_header("X-Voice-Format", _hdrsafe(ctype.split("/")[-1]))
        self.send_header("X-Voice-Card", _hdrsafe(card_id))
        self.end_headers()
        self.wfile.write(data)

    def _post_chat(self, body):
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._err(400, "缺少 prompt（或 prompt 是空的）")
        # backends 不传 = 空 = 全部后端（零回归）；传了就只走那几个。
        backends = (body.get("backends") or "").strip()
        # 多轮：手机/网页可以带上前几轮，这里只做透传，折叠在 Fleet.chat 里做
        # （放那儿是因为「怎么折」是对话层的事，不是 HTTP 层的事）。
        hist = body.get("history")
        # 联网开关：off / on / auto（不传 = auto，保持老行为不变的前提下默认能联网）。
        # 之所以默认 auto 而不是 off：用户说「让模型能联网」，默认就该有这个能力；
        # auto 又只在判到时效问题时才真去搜，不会让每条闲聊都等十几秒。
        web = body.get("web")
        web = web if isinstance(web, str) and web.strip() else "auto"
        q = self.fleet.chat(prompt, backends, history=hist, web=web)
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
