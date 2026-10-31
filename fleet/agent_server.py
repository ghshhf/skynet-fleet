#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""天网 · 手机端云侧服务（P1 看得见 + P4 手机侧那一半）

手机不是「对话盒子」。它是这几家开源项目的功能合集控制台：
  - pocketpal 的本地模型管理
  - Operit 的「工具箱」（系统/文件/Shell/代码/截图/网络/GitHub/记忆/工作流）
  - freellmapi / zen-gate / dsh 的额度与路由
  - 天网自己的编队控制（手机既是节点也是控制台）

本服务把电脑侧已经做好的积木（providers / agentloop / catalog）以 HTTP 暴露给
手机。手机走 USB 反代（adb reverse）连 127.0.0.1，不裸奔到局域网。

四个端点（全部 Bearer 鉴权）：
  GET  /api/health     健康 + 概览（活着的网关数 / 能力总数）
  GET  /api/providers  网关快照（剔除 key —— 绝不把密钥发到手机）
  GET  /api/caps       能力按 domain 分组（工具箱的数据源）
  POST /api/agent      SSE：一轮 agent loop，逐事件推 step/answer/error
  POST /api/tool/exec  工具箱单点执行（人直接点一个能力，不绕 LLM）

判据（对应计划书 P1 / P4）：
  - 手机能看见 ≥2 个网关真实状态（/api/providers）
  - 手机工具箱能列出真实能力并可触发（/api/caps + /api/tool/exec）
  - 手机发「看下 2 号机的 model 目录」→ 卡片里出现真的 node.exec 与真输出
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 让脚本直接 `python fleet/agent_server.py` 也能 import 同包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import providers as P        # noqa: E402
from fleet import agentloop             # noqa: E402
from fleet import catalog               # noqa: E402
from fleet import toolschema as TS      # noqa: E402
from fleet import gate as G             # noqa: E402
from fleet.core import registry as regmod  # noqa: E402

TOKEN_HEADER = "X-Fleet-Token"

# 🔴 对话默认**只给秒回能力**。根因是 2026-10-25 真机实测：模型为答
# 「有哪些网关」去调 net.scan（局域网端口指纹扫描）——几十秒，直接把一轮
# 对话拖死，手机端 readTimeout 一到就报 timeout。工具表就是权限边界，
# 也是**时延边界**：慢扫描 / 长任务 / 会再调 LLM 的（递归）一律不进对话默认表。
# 这些能力仍在 /api/caps 全量可见、仍可在工具箱单点执行——只是不让 LLM 在
# 对话里随手挑中。要跑慢活，用户显式点工具箱，不走对话主链路。
SLOW_OR_UNSAFE_FOR_CHAT = {
    # 局域网/无线/设备扫描（慢，逐设备多问，实测 net.wallprobe 单发 17 秒）
    "net.scan", "net.nearby", "net.survey", "scene.scan", "adb.wifi.scan",
    "net.dissect", "net.wallprobe", "proximity.estimate", "proximity.track",
    "proximity.calibrate",
    # 联网探测（慢/外网）
    "web.discover", "web.diffuse", "web.probe", "web.corpus", "web.papers",
    "osint.crt", "osint.dns", "osint.rdap",
    "osint.shodan", "ssh.banner", "ssh.exec", "mqtt.probe", "mqtt.pub", "mqtt.sub",
    # 逆向/红外逐个试（交互式、慢）
    "re.ir", "re.ble", "re.binary", "re.ask", "re.proto", "re.bits", "re.bits_extract",
    "ir.trial", "ir.command", "ir.send",
    # 长渲染 / 生图生视频（分钟级）
    "render.reel", "render.html", "render.frame", "video.gen", "image.gen",
    # 会再调 LLM ⇒ 递归；重复探测网关（ai.list 已秒回）
    "ai.chat", "ai.complete", "ai.status",
    # 其它长任务/常驻
    "job.submit", "sentinel.watch", "life.sentinel", "life.log",
    "hotspot.roam", "phone.hotspot",
}


#: 告诉模型「动手类能力是过闸门的」，否则它一看到 denied 会以为工具坏了、
#: 换个参数反复重试——那是白白烧轮次，还把「等你点确认」讲成「我做不到」。
GATE_PROMPT = (
    "【动手类能力都过审批闸门】\n"
    "· 点按/输入/启动应用/改系统这类会改变设备状态的能力，执行前要过闸门。\n"
    "· 如果工具结果里 code=denied 且带「待确认 #xxxx」，说明**这条已经替你排上了**，"
    "正等人在手机上点一下允许。\n"
    "· 这时**不要换参数重试、不要换别的工具绕**，直接告诉人：哪一步等你确认、"
    "确认后我会继续。\n"
    "· 只有 code 是 unavailable / error 时，才值得换个办法再试一次。"
)


def _need(value, default):
    return (os.environ.get(value) or "").strip() or default


# 音色短名 → edge-tts 全名。手机端只传 `xiaoxiao` 这种短名，
# 因为全名 `zh-CN-XiaoxiaoNeural` 在 App 里写一次就得同步一次，容易漂。
_VOICE_SHORT = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "xiaoyi": "zh-CN-XiaoyiNeural",
    "xiaochen": "zh-CN-XiaochenNeural",
    "yunxi": "zh-CN-YunxiNeural",
    "yunjian": "zh-CN-YunjianNeural",
    "yunyang": "zh-CN-YunyangNeural",
    "yunxia": "zh-CN-YunxiaNeural",
}
_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def _voice_full(name):
    """短名补全；已经是全名（带 `-Neural`）就原样用；空则默认。

    不做白名单校验 —— 新音色上线时不该改这里。全名猜错由 edge-tts 自己报错。
    """
    n = (name or "").strip()
    if not n:
        return _DEFAULT_VOICE
    if n.endswith("Neural") or "-" in n:
        return n
    return _VOICE_SHORT.get(n.lower(), _DEFAULT_VOICE)


class FleetAgentServer:
    """构造即生成 token；start() 之后端口才定下来。"""

    def __init__(self, host="127.0.0.1", port=0, lan=False, token=None,
                 gate=None, build=True):
        self.host = "0.0.0.0" if lan else host
        self.want_port = port
        self.token = token or _need("FLEET_AGENT_TOKEN", secrets.token_urlsafe(32))
        # 🔴 默认**装**一道闸门，不是留 None（2026-10-25）。
        # `Registry.call` 对副作用能力是 fail-closed 的：没 gate 就一律拒。
        # 这个服务原先 gate=None，结果手机端 `input.tap` / `pc.shell` 全撞在这
        # 一行上——看着连上了，一动手就「需要审批闸门」。留 None 等于**拆门**，
        # 所以默认给一个真闸门（默认策略 = 逐次确认，仍然 fail-closed）。
        self.gate = gate if gate is not None else G.ApprovalGate()
        # 🔴 关键：self._reg 必须初始为 None，registry() 才会在首请求时真正
        # catalog.build()。否则这里预置了一个空 Registry，registry() 见它非 None
        # 就直接返回空的，导致 /api/caps 永远返回 0 项（手机工具箱全空）。
        self._reg = None
        self._lock = threading.Lock()
        self.httpd = None
        self.bind_port = port
        self._journal = None            # 懒加载，见 journal()

    # --- 懒加载 registry：catalog.build 会注册能力表 + 探测设备，首请求再建 ----------
    def registry(self):
        if self._reg is None:
            with self._lock:
                if self._reg is None:
                    self._reg = catalog.build(gate=self.gate)
        return self._reg

    def journal(self):
        """跨请求复用的对话流水（**每次 new 一个 = 每次失忆**）。

        默认和桌面共用 `fleet_journal.db`：手机聊的、桌面聊的在同一张表里，
        换设备也接着上文——这正是「她记得」要的效果。
        建失败（路径/权限）就记个 False 别再重试，agentloop 会自动退化成
        无记忆的一轮：记忆是增强不是主链路，不许因为它把服务起不来。
        """
        if self._journal is None:
            with self._lock:
                if self._journal is None:
                    try:
                        import journal as journal_mod
                        self._journal = journal_mod.Journal("fleet_journal.db")
                    except Exception:
                        self._journal = False    # 试过了，别再试
        return self._journal or None

    def executor(self, cap, args):
        """工具执行层：能力真在电脑侧跑。闸门（gate）交给 Registry 自己管。"""
        try:
            r = self.registry().call(cap, args=args or {})
        except Exception as e:           # 执行层抛了也得给手机一个可读的因
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
        if hasattr(r, "as_dict"):
            out = r.as_dict()
        elif isinstance(r, dict):
            out = r
        else:
            out = {"ok": True, "data": r}
        # ---- 被闸门拦下的，把「票号」一起带回去 ----
        # 光回一句「需要确认」手机端没法接：它不知道该确认哪一条。
        # 票号是这次调用的唯一锚点，批准后再用**同一组参数**重发一次即可。
        if not out.get("ok") and str(out.get("code") or "") == "denied":
            try:
                t = self.gate.find_pending(cap, out.get("device") or "", args)
            except Exception:
                t = None
            if t:
                data = dict(out.get("data") or {})
                data["approval"] = {"id": t["id"], "level": t["level"],
                                    "cap": t["cap"], "device": t["dev"],
                                    "why": t["why"],
                                    "how": "POST /api/approve {id, allow} 后重发本次调用"}
                out["data"] = data
        return out

    # --- 鉴权：401（没带）/ 403（带了不对）必须分得开 --------------------------------
    def _authorized(self, headers):
        got = headers.get(TOKEN_HEADER) or headers.get("Authorization") or ""
        if got.startswith("Bearer "):
            got = got[7:].strip()
        if got:
            if secrets.compare_digest(got, self.token):
                return True, 200, ""
            # 🔴 2026-10-28：这里原来回 403。语义是错的——403 的意思是
            # 「身份我认了，但你没这个权限」，而这个服务只有一个令牌、没有权限分级，
            # 令牌不对就是**没通过认证**，该回 401。客户端对这两个码的处理通常不同：
            # 见到 401 会提示重新配对/清掉旧令牌，见到 403 会以为「这个人没权限」直接放弃
            # ——结果是主人在手机上改完端口却一直连不上，还看不出该重新配对。
            return False, 401, "令牌不对：不是本次启动注入的那枚（重新配对或换回原令牌）"
        return False, 401, "缺少 token（没带凭据）"

    # --- 各端点 ---------------------------------------------------------------------
    def health(self):
        provs = P.probe_all()
        alive = [p for p in provs if p.alive]
        caps = self.registry().capabilities()
        return {"ok": True, "ts": time.time(),
                "providers_alive": len(alive), "providers_total": len(provs),
                "caps_total": len(caps)}

    def providers(self):
        _, data = P.snapshot()           # 写盘但返回 data；snapshot 已剔除 key
        return data

    def caps(self):
        caps = self.registry().capabilities()
        groups = {}
        flat = []
        for c in caps:
            d = c.as_dict()
            flat.append(d)
            groups.setdefault(d["domain"], []).append(d["name"])
        return {"caps": flat, "groups": groups}

    def _persona_system(self):
        """人格层：从卡库取当前卡渲染成 system。取不到返回空串。

        🔴 2026-10-26 核出来的断头：老的桌面链（dialogue.py）有完整人格锚
        （`_card_anchor` + `_worldview` + 记忆块），但**新的 agent_server 完全
        绕开了这一套**——system 只有执行层口径（"不许编造、查不到就说查不到"）。
        结果：手机聊的是个冷冰冰的执行层，不是「她」。同一台机器，换条链路就换了个人。
        人格不生效，等于主人调的那套卡只在桌面有用。

        卡库坏了（缺文件/编译失败）不许把服务搞挂——人格是增强，不是启动依赖。
        """
        try:
            import tianwang_cards
            cid = tianwang_cards.current()
            if not cid:
                return ""
            return (tianwang_cards.build_system(cid) or "").strip()
        except Exception:
            return ""

    def _env_facts(self):
        """把实时探测到的网关/后端状态压成一句事实，喂给模型当「当前环境」。

        🔴 2026-10-25 真机实测的教训：模型为答「有哪些网关」会去挑 net.scan /
        net.wallprobe 这类慢扫描工具（单个 17 秒），把一轮对话拖到 timeout。
        根治不是继续加黑名单（打地鼠），而是**把答案直接递到模型眼前**——
        它读到「zen-gate 活(125 模型)」就会直接引用，不再为了环境问题去跑重活。
        探测失败就返回空串，不影响主流程。
        """
        try:
            provs = P.probe_all()
        except Exception:
            return ""
        alive = [p for p in provs if p.alive]
        dead = [p for p in provs if not p.alive]
        parts = ["%s 活(%d 模型/%dms)" % (p.id, len(p.models or []), p.latency_ms or 0)
                 for p in alive]
        parts += ["%s 死" % p.id for p in dead]
        return ("【当前环境实况·已实时探测，直接引用，别再调扫描工具】\n"
                "网关/后端共 %d 个：%s"
                % (len(provs), "；".join(parts) if parts else "无"))

    def agent_sse(self, question, history=None, lang=""):
        """生成器：**真流式**逐事件 yield SSE 文本。executor 走本服务的真实执行层。

        [lang] 是手机端设置里的「回复语言」（默认 zh-CN）。由**手机端**决定、
        这里只负责落到 system 提示里 —— 语言偏好属于设备主人的习惯，
        不该在服务端写死，同一台服务器可能连着好几个人的设备。

        🔴 2026-10-25 修的假流式坑：旧版用闭包 events=[] 先 collect 完再 yield，
        agentloop.run() 整个跑完（几十秒）手机端一个字节都收不到，最后 readTimeout
        一到期就报 timeout。改成 queue + worker 线程：事件一产生就立刻推给手机，
        thinking/exec/done 边跑边显示，不再是 60 秒黑屏后一窝蜂涌出。
        """
        yield "event: meta\ndata: %s\n\n" % json.dumps(
            {"question": question}, ensure_ascii=False)

        # 对话默认工具表：只留秒回能力，剔掉慢扫描/长任务/递归 LLM（见 SLOW_OR_UNSAFE_FOR_CHAT）
        caps = self.registry().capabilities()
        fast_caps = [c for c in caps if c.name not in SLOW_OR_UNSAFE_FOR_CHAT]
        # 🔴 `include_unsafe=True`（2026-10-25 修）
        # 原先给的是 False，而 `_visible()` 见到 side_effect 就整条剔掉 ——
        # 于是 `input.tap` / `input.type` / `app.launch` 这些「让手机真动起来」
        # 的能力**根本没进过工具表**，模型压根不知道它们存在。
        # 表现就是：手机端问「点一下工具箱」，模型读完屏幕、读完 UI 树，
        # 然后客客气气回一段话，一步都没动 —— 看起来像模型不肯干，
        # 其实是**表里没这支笔**。
        #
        # 为什么不在这儿再设一道白名单：工具表只回答「模型能不能提议」，
        # 「能不能真做」是闸门的事。两处都判 = 两套真相，迟早打架。
        # 危险能力进表不会自己执行，它只会被闸门拦成「待确认 #id」，
        # 反而让模型能把「这一步要你点一下」如实讲给主人听。
        tools = TS.tools_for(TS.FLAVOR_OPENAI, fast_caps, include_unsafe=True)
        # 🔴 把实时探测到的网关/后端状态作为「环境实况」直接喂给模型：
        # 于是「有哪些网关」从「跑一堆慢扫描工具」变成「读这句直接答」，
        # 一轮出结果、不调工具，时延从几十秒压到几秒。
        # 🔴 人格在前、纪律在后，中间加一句仲裁。
        # 不写仲裁那半句，模型会把「不许推测」读成「不要有人格」，
        # 回一堆没温度的条目——这正是「她」在云端消失的原因。
        # 口径与 MEMORY 里的位阶同一套：身份层管「我是谁」，
        # 纪律只约束「信息从哪来」，不覆盖自称与语气。
        _segs = []
        _persona = self._persona_system()
        if _persona:
            _segs.append(_persona)
            _segs.append("【上面是你的身份与说话方式——自称、称呼、语气照它来。"
                         "下面几条只约束「信息从哪来」，不改变你是谁。】")
        _segs.append(agentloop.SYSTEM)
        _segs.append(self._env_facts())
        _segs.append(GATE_PROMPT)
        system = "\n\n".join(s for s in _segs if s)
        # 语言约束：手机端设的「回复语言」优先。空/未传 = 不加任何约束（跟随提问）。
        _LANG = {
            "zh-CN": "始终使用简体中文回答。即使用户用其他语言提问，也用简体中文回复。",
            "zh-TW": "始終使用繁體中文回答，用語以台灣習慣為準。",
            "en": "Always answer in English, even if the question is in another language.",
        }
        if lang in _LANG:
            system += "\n\n" + _LANG[lang]

        q = queue.Queue()

        def fire(phase, payload=None):
            payload = payload or {}
            if phase in ("think",):
                return
            q.put((phase, payload))

        def worker():
            try:
                res = agentloop.run(question, self.executor, caps=fast_caps,
                                    tools=tools, system=system, on_event=fire,
                                    journal=self.journal())
                q.put(("__answer__", res))
            except Exception as e:
                q.put(("__error__", "%s: %s" % (type(e).__name__, e)))

        threading.Thread(target=worker, daemon=True).start()

        while True:
            phase, payload = q.get()
            if phase == "__answer__":
                res = payload
                yield "event: answer\ndata: %s\n\n" % json.dumps({
                    "ok": res.get("ok", False),
                    "answer": res.get("answer", ""),
                    "used": res.get("used", ""),
                    "used_model": res.get("used_model", ""),
                    "rounds": res.get("rounds", 0),
                    "steps": res.get("steps", []),
                }, ensure_ascii=False)
                return
            if phase == "__error__":
                yield "event: error\ndata: %s\n\n" % json.dumps(
                    {"err": payload}, ensure_ascii=False)
                return
            # 🔴 exec 必须进 step：手机端靠 exec 事件创建「运行中」卡片，
            # 否则只有 done 一个事件，卡片永远停在 RUNNING 或凭空变成 OK。
            kind = "step" if phase in ("step", "exec", "done") else "status"
            yield "event: %s\ndata: %s\n\n" % (kind, json.dumps(
                {"phase": phase, **payload}, ensure_ascii=False))

    def tool_exec(self, name, args):
        r = self.executor(name, args)
        return r

    # --- 审批：把「闸门」从一句拒绝变成一个可点的确认 ---------------------------
    def approvals(self):
        """当前策略 + 挂着等确认的票。手机设置页靠它显示「有几件事等你点」。"""
        try:
            snap = self.gate.snapshot()
        except AttributeError:
            return {"ok": True, "mode": "custom",
                    "note": "闸门是外部传入的自定义实现，没有快照接口", "waiting": []}
        snap["ok"] = True
        return snap

    def approve(self, tid, allow=True):
        if not tid:
            return {"ok": False, "err": "缺 id"}
        fn = getattr(self.gate, "approve", None)
        if fn is None:
            return {"ok": False, "err": "当前闸门不支持逐条批准"}
        return fn(tid, allow)

    def nodes(self):
        """编队节点快照：本机 + 控制面注册表里的在线节点。

        🔴 为什么要有这个端点（2026-10-25）：手机端「编队」页原先用的是写死的
        四条假节点（1号机/2号机/旧笔记本…）。换一台电脑、换一个局域网，那四条
        立刻全是错的，而且会把「其实一个节点都没连上」显示成「2/4 在线」。
        节点的真实来源只有控制面注册表 —— 拿得到就给真的，拿不到就明说拿不到。
        """
        import platform as _pf
        import socket as _sk

        try:
            host = _sk.gethostname()
        except Exception:
            host = ""
        try:
            ips = sorted({a for a in _sk.gethostbyname_ex(host)[2] if not a.startswith("127.")})
        except Exception:
            ips = []

        caps_n = 0
        try:
            caps_n = len(self.registry().capabilities())
        except Exception:
            pass

        cp_ok, cp_err, members = False, "", []
        try:
            from fleet import controlplane as CP
            cp_ok = bool(CP.available())
            if cp_ok:
                members = list(CP.registry_nodes() or [])
            else:
                cp_err = CP.import_error() or "控制面未就绪"
        except Exception as e:
            cp_err = "%s: %s" % (type(e).__name__, e)

        nodes = []
        for name in members:
            nodes.append({"name": str(name), "online": True, "kind": "member",
                          "note": "控制面注册表"})
        return {
            "self": {
                "name": host,
                "ips": ips,
                "platform": _pf.system() + " " + _pf.release(),
                "caps": caps_n,
                "role": "控制面",
            },
            "controlplane": {"available": cp_ok, "err": cp_err},
            "nodes": nodes,
            "at": time.time(),
        }

    def tasks(self):
        """项目进度：阶段（计划书 §六 的 P0-P4 ✅/❌）+ 近期提交 + 分支。
        手机端「任务」页的数据源——开发时能在手机上看整体进度。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        phases = []
        plan = os.path.join(root, "docs",
                           "计划书_手机端与网络层融合_2026-10-25.md")
        try:
            txt = open(plan, encoding="utf-8").read()
            for m in re.finditer(r"\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*(✅|❌)", txt):
                phases.append({"id": "P" + m.group(1), "name": m.group(2).strip(),
                               "done": m.group(3) == "✅"})
        except Exception:
            pass

        def _git(*a):
            try:
                return subprocess.check_output(
                    ["git", "-C", root, *a], text=True, timeout=8).strip()
            except Exception:
                return ""

        recent = []
        for line in _git("log", "--oneline", "-8").splitlines():
            if line.strip():
                recent.append(line.strip())
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        return {"phases": phases, "recent_commits": recent,
                "branch": branch, "at": time.time()}

    def start(self):
        server = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *a):           # 静默：服务日志不刷屏
                pass

            def _send(self, code, obj, ctype="application/json; charset=utf-8"):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def _send_bytes(self, code, body, ctype):
                """二进制响应（音频）。和 _send 分开是因为它多一步 json 序列化。

                ctype 必须是纯 ASCII —— `send_header` 里塞中文会半路抛异常，
                客户端只看到 `unexpected end of stream`（这条踩过）。
                """
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                ok, code, msg = server._authorized(self.headers)
                if not ok:
                    self._send(code, {"ok": False, "err": msg})
                return ok

            def _tts(self):
                """GET /api/tts?text=...&voice=xiaoxiao&engine=cloud → audio/wav

                为什么网关要出这个接口：**手机自带的系统 TTS 在真机上是废的**。
                2026-11-01 实测，HyperOS 上 `TextToSpeech` 初始化直接回 -1，
                连系统设置页自带的『播放』按钮都不产生任何音频轨道
                （`dumpsys media.audio_flinger` 的 active tracks 恒为 0）。
                所以手机出声不能靠系统引擎，得由 PC 合成好、手机只负责播。

                默认 engine=cloud（edge-tts）：手机就在手上，走本地要
                PC→adb→手机合成→传回 PC→再传回手机，绕两圈没意义。
                """
                q = parse_qs(urlparse(self.path).query)
                text = (q.get("text") or [""])[0].strip()
                if not text:
                    self._send(400, {"ok": False, "err": "缺 text"})
                    return
                voice = (q.get("voice") or [""])[0].strip()
                engine = (q.get("engine") or ["cloud"])[0].strip().lower()
                tmp = None
                try:
                    import tempfile
                    from fleet import storyboard as SB
                    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="fleet-tts-")
                    os.close(fd)
                    if engine == "local":
                        SB.tts_local(text, tmp)
                    else:
                        SB._edge_tts(text, tmp, voice=_voice_full(voice))
                    with open(tmp, "rb") as fh:
                        data = fh.read()
                    if not data:
                        self._send(500, {"ok": False, "err": "合成结果为空"})
                        return
                    self._send_bytes(200, data, "audio/wav")
                except Exception as e:
                    self._send(500, {"ok": False,
                                     "err": "%s: %s" % (type(e).__name__, e)})
                finally:
                    if tmp:
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/api/tts":
                    if not self._auth():
                        return
                    self._tts()
                elif path == "/api/health":
                    if not self._auth():
                        return
                    self._send(200, server.health())
                elif path == "/api/providers":
                    if not self._auth():
                        return
                    self._send(200, server.providers())
                elif path == "/api/caps":
                    if not self._auth():
                        return
                    self._send(200, server.caps())
                elif path == "/api/tasks":
                    if not self._auth():
                        return
                    self._send(200, server.tasks())
                elif path == "/api/nodes":
                    if not self._auth():
                        return
                    self._send(200, server.nodes())
                elif path == "/api/approvals":
                    if not self._auth():
                        return
                    self._send(200, server.approvals())
                else:
                    self._send(404, {"ok": False, "err": "no route: %s" % path})

            def do_POST(self):
                path = urlparse(self.path).path
                if path not in ("/api/agent", "/api/tool/exec", "/api/approve"):
                    self._send(404, {"ok": False, "err": "no route: %s" % path})
                    return
                if not self._auth():
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    body = json.loads(raw or b"{}")
                except Exception as e:
                    self._send(400, {"ok": False, "err": "body 不是合法 JSON: %s" % e})
                    return

                if path == "/api/tool/exec":
                    name = body.get("name") or ""
                    args = body.get("args") or {}
                    if not name:
                        self._send(400, {"ok": False, "err": "缺 name"})
                        return
                    self._send(200, server.tool_exec(name, args))
                    return

                if path == "/api/approve":
                    self._send(200, server.approve(body.get("id") or "",
                                                   body.get("allow", True) is not False))
                    return

                # /api/agent —— SSE
                q = (body.get("question") or "").strip()
                if not q:
                    self._send(400, {"ok": False, "err": "缺 question"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    for chunk in server.agent_sse(q, body.get("history"),
                                                  body.get("lang") or ""):
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return

        self.httpd = ThreadingHTTPServer((self.host, self.want_port), _H)
        self.bind_port = self.httpd.server_address[1]
        self.httpd.serve_forever()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()

    def shell_url(self, theme="light"):
        return "http://%s:%d/api/health#t=%s&theme=%s" % (
            self.host, self.bind_port, self.token, theme)


def main(argv=None):
    ap = argparse.ArgumentParser(description="天网手机端云侧服务")
    ap.add_argument("--host", default=None, help="覆盖绑定地址")
    ap.add_argument("--port", type=int, default=0, help="端口（0=随机）")
    ap.add_argument("--lan", action="store_true", help="暴露到局域网（默认只绑 127.0.0.1）")
    ap.add_argument("--token", default=None, help="Bearer token（默认启动随机生成）")
    ap.add_argument("--selftest", action="store_true", help="跑自测后退出")
    # ---- 审批闸门（有副作用的能力要不要放、放哪些）----
    ap.add_argument("--gate-file", default="",
                    help="闸门策略文件（默认 fleet_gate.json；文件不存在 = 动手类能力逐条问，"
                         "只读能力不受影响）")
    ap.add_argument("--gate-mode", default=None, choices=("ask", "allow", "deny"),
                    help="没写进 allow/deny 的能力怎么办（默认 ask = 逐次确认）")
    ap.add_argument("--allow", action="append", default=[], metavar="PATTERN",
                    help="免问的能力（可多次给；支持 input.* 这种通配）。"
                         "破坏性动作盖不住——它们永远要一次一票")
    ap.add_argument("--deny", action="append", default=[], metavar="PATTERN",
                    help="明文否决的能力（可多次给；连确认票都不给）")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    gate = G.ApprovalGate(path=args.gate_file or G.DEFAULT_PATH,
                          mode=args.gate_mode,
                          allow=args.allow or None,
                          deny=args.deny or None)
    srv = FleetAgentServer(host=args.host or "127.0.0.1", port=args.port,
                           lan=args.lan, token=args.token, gate=gate)
    snap = gate.snapshot()
    print("[agent_server] token = %s" % srv.token)
    print("[agent_server] 绑定 %s:%d" % (srv.host, args.port or 0))
    print("[agent_server] 闸门 mode=%s allow=%s deny=%s%s" % (
        snap["mode"], snap["allow"] or "-", snap["deny"] or "-",
        # 🔴 文案改准：闸门只对「有副作用/破坏性」的能力生效，只读能力**根本不走闸门**。
        # 早先这里写「无策略文件 ⇒ 什么都问」，会让人以为连查个 CPU 都要点允许——
        # 真跑一次就知道 host.monitor 直接执行了。会让人误判的文案比没文案更糟。
        "（无策略文件 ⇒ 动手类能力逐条问，只读不受影响）"
        if not snap["file_present"] else ""))
    print("[agent_server] 手机地址（配合 adb reverse tcp:%d tcp:%d）：" %
          (args.port or 0, srv.bind_port))
    try:
        srv.start()
    except KeyboardInterrupt:
        srv.stop()
    return 0


def _selftest():
    import threading
    import urllib.request

    srv = FleetAgentServer(host="127.0.0.1", port=0, token="selftest-token", build=False)
    srv._reg = regmod.Registry()        # 能力表在，但不探测设备（自测快、不依赖本机）
    srv._reg.register_all(catalog.CAPABILITIES)
    t = threading.Thread(target=srv.start, daemon=True)
    t.start()
    time.sleep(0.4)
    port = srv.bind_port
    base = "http://127.0.0.1:%d" % port
    hdr = {"X-Fleet-Token": srv.token}

    fails = []

    def get(path):
        req = urllib.request.Request(base + path, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    # 1) 无 token → 401
    req = urllib.request.Request(base + "/api/health")
    try:
        urllib.request.urlopen(req, timeout=5)
        fails.append("缺 token 应当 401，却放行了")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            fails.append("缺 token 返回 %d 不是 401" % e.code)

    # 2) 带 token → health 形状
    c, h = get("/api/health")
    if c != 200 or "caps_total" not in h:
        fails.append("health 形状不对: %r" % h)

    # 3) providers 快照不含 key
    c, pv = get("/api/providers")
    if c != 200 or "providers" not in pv:
        fails.append("providers 形状不对")
    else:
        for p in pv["providers"]:
            if "key" in p:
                fails.append("providers 把 key 发给手机了！")

    # 4) caps 分组
    c, caps = get("/api/caps")
    if c != 200 or "groups" not in caps or not caps["groups"]:
        fails.append("caps 分组不对: %r" % caps)

    # 5) /api/agent 真跑一轮（patch 掉真实模型与执行层，钉死 SSE 形状）
    real_chat = P.chat
    P.chat = lambda *a, **k: {"ok": True, "content": "（自测收尾）",
                              "used_model": "fake", "used": "fake"}
    real_exec = srv.executor
    srv.executor = lambda cap, args: {"ok": True, "data": "EXEC-%s" % cap}

    req = urllib.request.Request(base + "/api/agent",
                                data=json.dumps({"question": "测试"}).encode(),
                                headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = ""
        fails.append("agent 端点 %d: %s" % (e.code, e.read().decode()))
    P.chat = real_chat
    srv.executor = real_exec

    events = [ln for ln in raw.split("\n") if ln.startswith("event:")]
    if "event: answer" not in raw:
        fails.append("agent SSE 没有 answer 事件")
    # 只要有 answer 且 health/providers/caps 都过，SSE 链路就算通
    if "event: answer" in raw and "（自测收尾）" not in raw:
        fails.append("agent answer 内容未透传")

    # 6) /api/tool/exec 形状
    req = urllib.request.Request(base + "/api/tool/exec",
                                data=json.dumps({"name": "probe.echo",
                                                "args": {"x": 1}}).encode(),
                                headers=hdr)
    with urllib.request.urlopen(req, timeout=5) as r:
        te = json.loads(r.read().decode("utf-8"))
    if "ok" not in te:
        fails.append("tool/exec 形状不对: %r" % te)

    srv.stop()
    if fails:
        print("agent_server selftest: FAIL")
        for f in fails:
            print("  - " + f)
        return 1
    print("agent_server selftest: OK（health/providers/caps/agent-SSE/tool-exec 全过）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
