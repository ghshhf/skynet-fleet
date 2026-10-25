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
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# 让脚本直接 `python fleet/agent_server.py` 也能 import 同包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import providers as P        # noqa: E402
from fleet import agentloop             # noqa: E402
from fleet import catalog               # noqa: E402
from fleet.core import registry as regmod  # noqa: E402

TOKEN_HEADER = "X-Fleet-Token"


def _need(value, default):
    return (os.environ.get(value) or "").strip() or default


class FleetAgentServer:
    """构造即生成 token；start() 之后端口才定下来。"""

    def __init__(self, host="127.0.0.1", port=0, lan=False, token=None,
                 gate=None, build=True):
        self.host = "0.0.0.0" if lan else host
        self.want_port = port
        self.token = token or _need("FLEET_AGENT_TOKEN", secrets.token_urlsafe(32))
        self.gate = gate
        # 🔴 关键：self._reg 必须初始为 None，registry() 才会在首请求时真正
        # catalog.build()。否则这里预置了一个空 Registry，registry() 见它非 None
        # 就直接返回空的，导致 /api/caps 永远返回 0 项（手机工具箱全空）。
        self._reg = None
        self._lock = threading.Lock()
        self.httpd = None
        self.bind_port = port

    # --- 懒加载 registry：catalog.build 会注册能力表 + 探测设备，首请求再建 ----------
    def registry(self):
        if self._reg is None:
            with self._lock:
                if self._reg is None:
                    self._reg = catalog.build(gate=self.gate)
        return self._reg

    def executor(self, cap, args):
        """工具执行层：能力真在电脑侧跑。闸门（gate）交给 Registry 自己管。"""
        try:
            r = self.registry().call(cap, args=args or {})
        except Exception as e:           # 执行层抛了也得给手机一个可读的因
            return {"ok": False, "err": "%s: %s" % (type(e).__name__, e)}
        if hasattr(r, "as_dict"):
            return r.as_dict()
        if isinstance(r, dict):
            return r
        return {"ok": True, "data": r}

    # --- 鉴权：401（没带）/ 403（带了不对）必须分得开 --------------------------------
    def _authorized(self, headers):
        got = headers.get(TOKEN_HEADER) or headers.get("Authorization") or ""
        if got.startswith("Bearer "):
            got = got[7:].strip()
        if got:
            if secrets.compare_digest(got, self.token):
                return True, 200, ""
            return False, 403, "令牌不对：不是本次启动注入的那枚"
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

    def agent_sse(self, question, history=None):
        """生成器：逐事件 yield SSE 文本。executor 走本服务的真实执行层。"""
        yield "event: meta\ndata: %s\n\n" % json.dumps(
            {"question": question}, ensure_ascii=False)

        # on_event 是回调，不能 yield 给外层；用闭包把事件塞进 list，外层逐个发。
        events = []

        def fire(phase, payload=None):
            payload = payload or {}
            if phase in ("think",):
                return
            events.append((phase, payload))

        try:
            res = agentloop.run(question, self.executor, on_event=fire)
        except Exception as e:
            yield "event: error\ndata: %s\n\n" % json.dumps(
                {"err": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False)
            return

        # 先把过程中积累的事件发出去（step / exec / done / status）
        # 🔴 exec 必须进 step：手机端靠 exec 事件创建「运行中」卡片，
        # 否则只有 done 一个事件，卡片永远停在 RUNNING 或凭空变成 OK。
        for phase, payload in events:
            kind = "step" if phase in ("step", "exec", "done") else "status"
            yield "event: %s\ndata: %s\n\n" % (kind, json.dumps(
                {"phase": phase, **payload}, ensure_ascii=False))

        yield "event: answer\ndata: %s\n\n" % json.dumps({
            "ok": res.get("ok", False),
            "answer": res.get("answer", ""),
            "used": res.get("used", ""),
            "used_model": res.get("used_model", ""),
            "rounds": res.get("rounds", 0),
            "steps": res.get("steps", []),
        }, ensure_ascii=False)

    def tool_exec(self, name, args):
        r = self.executor(name, args)
        return r

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

            def _auth(self):
                ok, code, msg = server._authorized(self.headers)
                if not ok:
                    self._send(code, {"ok": False, "err": msg})
                return ok

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/api/health":
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
                else:
                    self._send(404, {"ok": False, "err": "no route: %s" % path})

            def do_POST(self):
                path = urlparse(self.path).path
                if path not in ("/api/agent", "/api/tool/exec"):
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
                    for chunk in server.agent_sse(q, body.get("history")):
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
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    srv = FleetAgentServer(host=args.host or "127.0.0.1", port=args.port,
                           lan=args.lan, token=args.token)
    print("[agent_server] token = %s" % srv.token)
    print("[agent_server] 绑定 %s:%d" % (srv.host, args.port or 0))
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
