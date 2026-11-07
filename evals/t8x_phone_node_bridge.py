#!/usr/bin/env python3
"""R1d eval：手机节点桥接（phone_node → fleet-node agent / 8770）。

不依赖真机：起一个 mock agent server 模拟 phone/agent.py 的 HTTP 协议，
验证 catalog 侧的 phone.* 执行能力能正确映射到 agent 本地协议名并转发。
门禁上限沿用（单测快），不跑半小时。

映射规则：
    phone.exec      → shell.exec
    phone.fs.read   → fs.read
    phone.fs.write  → fs.write
    phone.fs.list   → fs.list
    phone.llm.chat → llm.chat
    phone.termux.*  → termux.*（同名透传）
断言：
    · probe 认 agent（8770）即在线；
    · 每个执行能力转发后回显的本地 cap 名正确；
    · 未知 phone.* 能力返回 unsupported（不是 denied —— 不防御）。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PORT = 8770


class MockAgent(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode() or "{}")
        cap = body.get("cap")
        args = body.get("args") or {}
        out = {"ok": True, "cap": cap, "args": args}
        if cap == "shell.exec":
            out["rc"] = 0
            out["out"] = "ran:%s" % args.get("cmd")
        if cap == "sys.info":
            out["node_id"] = "phone"
        b = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def start_mock():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), MockAgent)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    srv = start_mock()
    time.sleep(0.3)
    fails = []
    try:
        os.environ["PHONE_AGENT_BASE"] = "http://127.0.0.1:%d" % PORT
        from fleet.kinds.phone_node import PhoneNode
        node = PhoneNode()

        ok, why = node.probe()
        if not ok:
            fails.append("probe 不认 agent(8770)：%s" % why)

        cases = [
            ("phone.exec", {"cmd": "echo hi"}, "shell.exec"),
            ("phone.fs.read", {"path": "x"}, "fs.read"),
            ("phone.fs.write", {"path": "x", "text": "y"}, "fs.write"),
            ("phone.fs.list", {}, "fs.list"),
            ("phone.llm.chat", {"prompt": "hi"}, "llm.chat"),
            ("phone.termux.camera", {}, "termux.camera"),
        ]
        for cap, args, local in cases:
            r = node.invoke(cap, args)
            if not r.ok:
                fails.append("%s 失败：%r" % (cap, r))
                continue
            data = r.data or {}
            if data.get("cap") != local:
                fails.append("%s 没映射到 %s（实得 %r）" % (cap, local, data.get("cap")))

        # 未知 phone.* 应 unsupported，绝不 denied（不防御）
        r = node.invoke("phone.nope", {})
        if r.ok or r.code != "unsupported":
            fails.append("phone.nope 应 unsupported，实得 %r" % (r,))

        # 走 registry 路径验证不被 fail-closed 默认拒绝
        try:
            from fleet.catalog import build
            reg = build(with_nodes=True)
            rr = reg.call("phone.exec", "phone:compute", {"cmd": "echo hi"})
            if not rr.ok:
                fails.append("registry.call(phone.exec) 被拒：%r" % (rr,))
            elif (rr.data or {}).get("cap") != "shell.exec":
                fails.append("registry 路径映射错：%r" % (rr,))
        except Exception as e:
            # 真机依赖（adbx/provision_phone 等）缺失时跳过，不误判桥接
            sys.stderr.write("warn: registry 路径未验（%s: %s）\n" % (type(e).__name__, e))
    finally:
        srv.shutdown()

    print("t8x_phone_node_bridge: %d 项失败" % len(fails))
    for f in fails:
        print("  x " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
