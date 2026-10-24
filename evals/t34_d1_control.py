#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T34 D1 控制面对外协议（总纲 P1-9 真代发）——「让另一台机器跑这条命令」。

要钉死的是**代发到底是真通了，还是又登记了一个空壳**。分三层验：

  ① 协议层（真端口、真认证、真转发）
     commander 起在随机端口；一个假节点连上来 AUTH + REGISTER；
     客户端（fleet.ctrl_client）走 AUTH + CLIENT，发 nodes/exec/push/pull，
     必须拿到节点真实回执。**节点不在编队里进（不注册）**，所以客户端通道
     不能顺手写注册表 —— 顺带断言注册表里没有客户端那一行。

  ② 失败要能分辨（静默失败是头号病）
     · 密码错 → 认证失败，不是「超时」；
     · 未知 op → 明确报「未知 op」并列出可用 op（不许当成功）；
     · 节点不在线 → offline=True（去叫它上线，而不是重试命令）。

  ③ 能力申报必须诚实（不许长出假按钮）
     控制面通 + FLEET_PASS 在环境里 ⇒ fleet_node 才申报 shell.exec/file.push/file.pull；
     缺任一条 ⇒ **整条不出现**（缺密码时申报了也调不通，那就是假按钮）。
     再验 invoke 真能经控制面拿到 out —— 不是只把参数拼对。

全离线：只在本机 127.0.0.1 随机端口跑；不碰真编队、不连外网。
"""
import base64
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import common                 # noqa: E402
import commander as CM        # noqa: E402

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


# ---------------------------------------------------------------- 假节点
class FakeNode(threading.Thread):
    """最小的节点：能认证、能注册、能答 exec/push/pull。"""

    def __init__(self, host, port, password, name="N1"):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.password, self.name = password, name
        self.ready = threading.Event()
        self.got = []
        self._stop = threading.Event()

    def run(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=8)
        except OSError as e:
            print("    [假节点] 连不上控制面：%s" % e, flush=True)
            return
        s.settimeout(1.0)
        common.send_msg(s, {"t": common.AUTH, "name": self.name,
                            "password": self.password})
        r = common.recv_msg(s)
        if not r or r.get("t") != common.AUTH_OK:
            print("    [假节点] 认证失败：%r" % (r,), flush=True)
            return
        common.send_msg(s, {"t": common.REGISTER, "name": self.name,
                            "platform": "windows", "info": {"session": False},
                            "capabilities": ["exec", "push", "pull"]})
        self.ready.set()
        while not self._stop.is_set():
            try:
                m = common.recv_msg(s)
            except OSError:
                continue
            if m is None:
                break
            t = m.get("t")
            self.got.append(t)
            if t == common.EXEC:
                common.send_msg(s, {"t": common.EXEC_RESULT, "id": m.get("id"),
                                    "rc": 0, "out": "ran:" + (m.get("cmd") or ""),
                                    "err": ""})
            elif t == common.PUSH:
                payload = base64.b64decode(m.get("data") or "")
                common.send_msg(s, {"t": common.PUSH_RESULT, "id": m.get("id"),
                                    "ok": True, "bytes": len(payload)})
            elif t == common.PULL:
                body = base64.b64encode(b"hello-from-node").decode("ascii")
                common.send_msg(s, {"t": common.PULL_RESULT, "id": m.get("id"),
                                    "ok": True, "data": body, "bytes": 15})
        try:
            s.close()
        except OSError:
            pass

    def stop(self):
        self._stop.set()


def main():
    # 隔离注册表：绝不碰真的 nodes.json（真编队里可能有正在跑的节点）
    tdir = tempfile.mkdtemp(prefix="fleet_t34_")
    import registry as REG
    old_reg, old_nodes_path = CM.REGISTRY, getattr(CM, "NODES_PATH", None)
    CM.REGISTRY = REG.NodeTable(os.path.join(tdir, "nodes.json"), interval=0)

    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv_sock.bind(("127.0.0.1", 0))
    srv_sock.listen(16)
    port = srv_sock.getsockname()[1]
    PW = "t34-pass"
    threading.Thread(target=CM.serve, args=("127.0.0.1", port, PW, srv_sock),
                     daemon=True).start()
    time.sleep(0.3)

    from fleet import ctrl_client as CC

    node = FakeNode("127.0.0.1", port, PW, name="N1")
    node.start()
    check(node.ready.wait(6), "假节点已认证并注册（控制面协议可用）")
    time.sleep(0.4)

    old_env = {k: os.environ.get(k) for k in ("FLEET_HOST", "FLEET_PORT", "FLEET_PASS")}
    os.environ["FLEET_HOST"] = "127.0.0.1"
    os.environ["FLEET_PORT"] = str(port)
    os.environ["FLEET_PASS"] = PW
    try:
        cli = CC.ControlClient(host="127.0.0.1", port=port, password=PW)

        # -------- ① 协议层：真转发
        nodes = cli.nodes()
        check("N1" in nodes, "客户端能看到在线节点（%s）" % (nodes,))
        check("fleet-client" not in nodes,
              "客户端**没有**被写进编队（通道不等于节点身份）")

        d = cli.exec("N1", "whoami")
        check(d.get("out") == "ran:whoami", "exec 拿到节点真实回执（%r）" % (d.get("out"),))
        check(d.get("rc") == 0, "exec 带回 rc")

        b64 = base64.b64encode(b"abcdef").decode("ascii")
        p = cli.push("N1", "/tmp/x.bin", b64)
        check(p.get("bytes") == 6, "push 节点收到 6 字节（实得 %r）" % (p.get("bytes"),))

        f = cli.pull("N1", "/tmp/x.bin")
        check(base64.b64decode(f.get("data") or "") == b"hello-from-node",
              "pull 拿回节点真实内容")

        # -------- ② 失败要能分辨
        bad = CC.ControlClient(host="127.0.0.1", port=port, password="wrong")
        try:
            bad.nodes()
            check(False, "密码错应该报错")
        except CC.ControlError as e:
            check("认证失败" in str(e), "密码错 → 明确说认证失败（%s）" % str(e)[:40])

        try:
            cli.request("no_such_op", node="N1")
            check(False, "未知 op 应该报错")
        except CC.ControlError as e:
            s = str(e)
            check("未知 op" in s and "exec" in s,
                  "未知 op → 报错并列出可用 op（%s）" % s[:56])

        try:
            cli.exec("NOT-ONLINE", "whoami")
            check(False, "离线节点应该报错")
        except CC.ControlError as e:
            check(bool(getattr(e, "offline", False)),
                  "节点不在线 → offline 标记为真（调用方能分辨该叫它上线）")
            check("不在线" in str(e), "离线原因写清是「不在线」（%s）" % str(e)[:40])

        # -------- ③ 能力申报诚实 + invoke 真通
        from fleet.kinds.fleet_node import FleetNode
        dev = FleetNode("N1", rec={"capabilities": ["exec"], "last_seen": time.time()})
        caps = dev.caps()
        for c in ("shell.exec", "file.push", "file.pull"):
            check(c in caps, "控制面通 + 有密码 ⇒ 申报 %s" % c)

        r = dev.invoke("shell.exec", {"cmd": "hostname"})
        check(bool(r.ok), "invoke(shell.exec) 成功（%s）" % (r.err or ""))
        data = r.data if hasattr(r, "data") else (r or {}).get("data") or {}
        check(data.get("out") == "ran:hostname",
              "invoke 真的把命令送到节点并拿回输出（%r）" % (data.get("out"),))

        r2 = dev.invoke("file.pull", {"path": "/tmp/x.bin"})
        d2 = r2.data if hasattr(r2, "data") else (r2 or {}).get("data") or {}
        check(bool(r2.ok) and d2.get("bytes") == 15,
              "invoke(file.pull) 拿回节点文件（bytes=%r）" % (d2.get("bytes"),))

        # 缺密码 ⇒ 一条都不许申报（否则就是假按钮）
        os.environ.pop("FLEET_PASS", None)
        caps2 = dev.caps()
        check(not any(c in caps2 for c in ("shell.exec", "file.push", "file.pull")),
              "缺 FLEET_PASS ⇒ 三条能力整条不申报（不长假按钮）；实际 %s" % (caps2,))
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        node.stop()
        CM.REGISTRY = old_reg
        if old_nodes_path is not None:
            CM.NODES_PATH = old_nodes_path
        try:
            srv_sock.close()
        except OSError:
            pass

    if FAIL:
        print("\nt34_d1_control: FAIL (%d 项)" % len(FAIL), flush=True)
        return 1
    print("t34_d1_control: PASS（协议真转发 / 失败可分辨 / 申报诚实）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
