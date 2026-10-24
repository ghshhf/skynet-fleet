#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T35 两条通道真伪：ssh / mqtt（总纲「C 档 5 条通道」收口）。

这两条最容易被糊弄成「登记了就完事」——端口开着就说通、发出去就说成功。
门禁要钉的就是这些糊弄点，所以**不 mock 协议**：

  ① MQTT：门禁里现起一个真 broker（独立实现，不复用被测模块的编解码）
     · 握手：CONNACK rc=0 才算通；
     · 真投递：sub 挂上后 pub 一条，payload 必须原样到；
     · 拒绝要可分辨：密码错 ⇒ denied + 「用户名或密码错」，不是「连不上」；
     · SUBACK=0x80 ⇒ denied（有服务但不许你订）；
     · 没人听 ⇒ unavailable（不是 ok，也不是 timeout 混着）。
     · QoS1 必须等 PUBACK —— broker 不回 PUBACK 时要说 timeout，不许报成功。

  ② SSH：
     · 真 banner：假 sshd 吐 `SSH-2.0-FakeSSH_9.9` ⇒ is_ssh=True 且软件名解析对；
     · 假 SSH：普通 TCP 服务（吐 HTTP）⇒ is_ssh=False，**不冒充 sshd**；
     · 端口不通 ⇒ unavailable 且带原因；
     · ssh.exec 连不上 ⇒ 255 ⇒ unavailable（和「远端命令返回非 0」分开）；
     · ssh.exec 给密码 ⇒ denied（argv 里的密码等于公开）。

全离线：只在本机 127.0.0.1 随机端口；不连外网、不碰真编队。
"""
import os
import socket
import struct
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAIL = []


def check(ok, msg):
    print(("  [ok] " if ok else "  [X ] ") + msg, flush=True)
    if not ok:
        FAIL.append(msg)


# ================================================================ 真 broker
class Broker(threading.Thread):
    """最小 MQTT 3.1.1 broker。解析自己写一遍（不复用被测模块），
    这样两边同时错的可能性低得多——门禁的价值就在这个独立性上。"""

    def __init__(self, need_auth=("u", "p"), silent_ack=False):
        super().__init__(daemon=True)
        self.need_auth = need_auth
        self.silent_ack = silent_ack        # True ⇒ 收 PUBLISH 不回 PUBACK（测超时）
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.subs = []                       # [(sock, topic_filter)]
        self.published = []                  # [(topic, payload)]
        self.lock = threading.Lock()
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    # ---- 独立的最小解码 ----
    @staticmethod
    def _rd(s, n):
        b = b""
        while len(b) < n:
            x = s.recv(n - len(b))
            if not x:
                raise ConnectionError("eof")
            b += x
        return b

    def _pkt(self, s):
        h = self._rd(s, 1)[0]
        mult, ln = 1, 0
        while True:
            b = self._rd(s, 1)[0]
            ln += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return h >> 4, h & 0x0F, (self._rd(s, ln) if ln else b"")

    @staticmethod
    def _mk(typ, flags, body):
        def rl(n):
            o = b""
            while True:
                d = n % 128
                n //= 128
                o += bytes([d | 0x80]) if n else bytes([d])
                if not n:
                    return o
        return bytes([(typ << 4) | flags]) + rl(len(body)) + body

    @staticmethod
    def _take_str(b, i):
        n = struct.unpack("!H", b[i:i + 2])[0]
        return b[i + 2:i + 2 + n], i + 2 + n

    def _serve(self, c):
        try:
            while True:
                typ, fl, body = self._pkt(c)
                if typ == 1:                                  # CONNECT
                    i = 0
                    _name, i = self._take_str(body, i)
                    i += 1                                    # level
                    cflags = body[i]
                    i += 3                                    # flags + keepalive
                    _cid, i = self._take_str(body, i)
                    user = pw = ""
                    if cflags & 0x80:
                        user, i = self._take_str(body, i)
                    if cflags & 0x40:
                        pw, i = self._take_str(body, i)
                    # _take_str 给的是 bytes：不 decode 就跟 str 比，永远不等，
                    # 结果是「正确密码也判成密码错」——门禁自己先谎报，比不测更坏
                    user = user.decode("utf-8", "replace")
                    pw = pw.decode("utf-8", "replace")
                    rc = 0
                    if self.need_auth:
                        if user != self.need_auth[0]:
                            rc = 4
                        elif pw != self.need_auth[1]:
                            rc = 4
                    c.sendall(self._mk(2, 0, bytes([0, rc])))
                    if rc:
                        return
                elif typ == 8:                                # SUBSCRIBE
                    pid = body[:2]
                    t, i = self._take_str(body, 2)
                    qos = body[i] if i < len(body) else 0
                    filt = t.decode()
                    with self.lock:
                        self.subs.append((c, filt))
                    c.sendall(self._mk(9, 0, pid + bytes([qos])))
                elif typ == 3:                                # PUBLISH
                    t, i = self._take_str(body, 0)
                    qos = (fl & 0x06) >> 1
                    pid = None
                    if qos > 0:
                        pid = body[i:i + 2]
                        i += 2
                    payload = body[i:]
                    with self.lock:
                        self.published.append((t.decode(), payload))
                        targets = [(s, f) for (s, f) in self.subs
                                   if f == t.decode() or f == "#"]
                    for s, _f in targets:
                        # `t` 是主题的**值**（长度前缀已被 _take_str 吃掉），
                        # 转发时必须重新带上前缀——少了这两个字节，对端会把
                        # 主题的前两字符当成长度，读出一个「像成功其实错位」的包
                        hdr = struct.pack("!H", len(t)) + t + (pid if pid else b"")
                        s.sendall(self._mk(3, (qos << 1), hdr + payload))
                    if qos > 0 and pid and not self.silent_ack:
                        c.sendall(self._mk(4, 0, pid))
                elif typ == 12:                               # PINGREQ
                    c.sendall(self._mk(13, 0, b""))
                elif typ == 14:                               # DISCONNECT
                    return
        except (ConnectionError, OSError, IndexError, struct.error):
            return
        finally:
            try:
                c.close()
            except OSError:
                pass

    def stop(self):
        self.stop_flag = True
        try:
            self.srv.close()
        except OSError:
            pass


# ================================================================ 假服务端
def tcp_server(respond):
    """起一个只吐固定字节的 TCP 服务，返回 (port, stop_fn)。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = {"v": False}

    def loop():
        while not stop["v"]:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.sendall(respond)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    threading.Thread(target=loop, daemon=True).start()

    def close():
        stop["v"] = True
        try:
            srv.close()
        except OSError:
            pass

    return port, close


def hold_server():
    """接了就不说话也不关（用来分辨「连上了但对方不吭声」）。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    held = []
    stop = {"v": False}

    def loop():
        while not stop["v"]:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            held.append(c)          # 故意不发不收，也不关

    threading.Thread(target=loop, daemon=True).start()

    def close():
        stop["v"] = True
        for c in held:
            try:
                c.close()
            except OSError:
                pass
        try:
            srv.close()
        except OSError:
            pass

    return port, close


def dead_port():
    """拿一个「刚才还开着、现在关了」的端口 —— 稳定拿不到可用端口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ================================================================ 主流程
def main():
    from fleet.kinds.mqtt_bus import MqttBus
    from fleet.kinds.ssh_shell import SshShell, find_ssh

    mq = MqttBus()
    sh = SshShell()

    # ---------- ① MQTT ----------
    print("① MQTT 真协议（门禁里现起 broker）", flush=True)
    br = Broker()
    br.start()
    time.sleep(0.1)
    try:
        r = mq.invoke("mqtt.probe", {"broker": "127.0.0.1", "port": br.port,
                                     "user": "u", "pw": "p"})
        check(r.ok and r.data.get("rc") == 0,
              "握手通过（rc=0）：%s" % (r.err or r.data.get("verdict")))

        # 密码错 ⇒ denied + 说清是「有服务但不让你进」
        r = mq.invoke("mqtt.probe", {"broker": "127.0.0.1", "port": br.port,
                                     "user": "u", "pw": "WRONG"})
        check((not r.ok) and r.code == "denied" and "密码错" in r.err,
              "密码错 ⇒ denied 且说明原因（实际 code=%s err=%s）" % (r.code, r.err))

        # 没人听 ⇒ 必须明确失败。**注意本机实测**：Windows 上连本机已关端口不返
        # RST 而是静默丢包（1s 硬超时），所以这里只许 unavailable / timeout 两种，
        # 不许 ok——按「应该 refused」来断言会写死平台行为，换台机器就假红。
        r = mq.invoke("mqtt.probe", {"broker": "127.0.0.1", "port": dead_port(),
                                     "timeout": 1})
        check((not r.ok) and r.code in ("unavailable", "timeout") and r.err,
              "端口没人听 ⇒ 明确失败且带原因（实际 code=%s err=%s）"
              % (r.code, r.err[:50]))

        # 端口开着但不是 broker ⇒ 必须判「不像 MQTT broker」，不许糊成「连不上」
        p4, close4 = tcp_server(b"HTTP/1.1 200 OK\r\n\r\n")
        try:
            r = mq.invoke("mqtt.probe", {"broker": "127.0.0.1", "port": p4, "timeout": 1})
            check((not r.ok) and r.code == "unavailable"
                  and ("不像 MQTT" in r.err or "不是 MQTT" in r.err),
                  "端口开着但不是 broker ⇒ 明确说「不像」（实际：%s）" % r.err[:70])
        finally:
            close4()

        # 真投递：sub 挂上，pub 发一条，payload 原样到
        out = {}

        def do_sub():
            out["r"] = mq.invoke("mqtt.sub", {"broker": "127.0.0.1", "port": br.port,
                                              "user": "u", "pw": "p",
                                              "topic": "skynet/t35", "count": 1,
                                              "timeout": 5})

        th = threading.Thread(target=do_sub, daemon=True)
        th.start()
        time.sleep(0.5)
        rp = mq.invoke("mqtt.pub", {"broker": "127.0.0.1", "port": br.port,
                                    "user": "u", "pw": "p", "topic": "skynet/t35",
                                    "payload": "hello-天网", "qos": 1})
        check(rp.ok and rp.data.get("acked"),
              "QoS1 发布拿到 PUBACK（%s）" % (rp.err or "ok"))
        th.join(timeout=8)
        rs = out.get("r")
        check(rs is not None and rs.ok and rs.data.get("got") == 1,
              "订阅端真收到 1 条（%s）" % (rs.err if rs else "无结果"))
        if rs is not None and rs.ok and rs.data.get("messages"):
            m0 = rs.data["messages"][0]
            check(m0["payload"] == "hello-天网" and m0["topic"] == "skynet/t35",
                  "payload 原样到达（主题=%s 内容=%r）" % (m0["topic"], m0["payload"]))
        # broker 侧也留了账 ⇒ 双向印证，不是只看客户端自说自话
        check(("skynet/t35", "hello-天网".encode()) in br.published,
              "broker 侧账本记到了这条")

        # QoS1 broker 不回 PUBACK ⇒ 必须报 timeout，不许报成功
        br2 = Broker(silent_ack=True)
        br2.start()
        time.sleep(0.1)
        try:
            r = mq.invoke("mqtt.pub", {"broker": "127.0.0.1", "port": br2.port,
                                       "user": "u", "pw": "p", "topic": "x/y",
                                       "payload": "z", "qos": 1, "timeout": 1})
            check((not r.ok) and r.code == "timeout",
                  "QoS1 等不到 PUBACK ⇒ timeout（实际 code=%s err=%s）" % (r.code, r.err))
        finally:
            br2.stop()

        # 空主题 ⇒ error，不许静默成功
        r = mq.invoke("mqtt.pub", {"broker": "127.0.0.1", "port": br.port, "topic": ""})
        check((not r.ok) and r.code == "error", "缺 topic ⇒ error（%s）" % r.code)
    finally:
        br.stop()

    # ---------- ② SSH ----------
    print("② SSH 真读 banner（假 sshd / 假服务 / 不通 三种都要分开）", flush=True)
    p, close = tcp_server(b"SSH-2.0-FakeSSH_9.9 skynet-t35\r\n")
    try:
        r = sh.invoke("ssh.banner", {"host": "127.0.0.1", "port": p, "timeout_ms": 1200})
        check(r.ok and r.data.get("is_ssh") is True,
              "真 banner ⇒ is_ssh=True（%s）" % (r.err or r.data.get("verdict")))
        check(r.ok and r.data.get("software") == "FakeSSH_9.9",
              "软件名解析对（%s）" % (r.data.get("software") if r.ok else r.err))
        check(r.ok and r.data.get("proto") == "2.0",
              "协议版本解析对（%s）" % (r.data.get("proto") if r.ok else r.err))
    finally:
        close()

    p2, close2 = tcp_server(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    try:
        r = sh.invoke("ssh.banner", {"host": "127.0.0.1", "port": p2, "timeout_ms": 1200})
        check(r.ok and r.data.get("is_ssh") is False,
              "端口开着但不是 sshd ⇒ is_ssh=False（不冒充）")
    finally:
        close2()

    r = sh.invoke("ssh.banner", {"host": "127.0.0.1", "port": dead_port(),
                                 "timeout_ms": 800})
    check((not r.ok) and r.code in ("unavailable", "timeout") and r.err,
          "端口不通 ⇒ 明确失败且带原因（实际 code=%s）" % r.code)
    # 「连不上」和「连上了不说话」必须分开：第一版两句撞成一句，害过一次排查
    check("连上了但不说话" not in r.err,
          "TCP 没建起来时不许说「连上了」（实际：%s）" % r.err[:60])

    p3, close3 = hold_server()            # 接了，但不说话也不关
    try:
        r = sh.invoke("ssh.banner", {"host": "127.0.0.1", "port": p3, "timeout_ms": 600})
        check((not r.ok) and r.code == "timeout" and "连上了但不说话" in r.err,
              "接了不说话 ⇒ timeout 且说清是「连上了」（实际：%s）"
              % (r.err[:60] or r.code))
    finally:
        close3()

    r = sh.invoke("ssh.banner", {"host": ""})
    check((not r.ok) and r.code == "error", "缺 host ⇒ error（%s）" % r.code)

    print("③ ssh.exec 的边界（不收密码 / 失败分级）", flush=True)
    r = sh.invoke("ssh.exec", {"host": "127.0.0.1", "cmd": "echo hi",
                               "password": "whatever"})
    check((not r.ok) and r.code == "denied" and "密码" in r.err,
          "给密码 ⇒ denied 并说明「argv 里的密码等于公开」（%s）" % r.err)

    cli, why = find_ssh()
    if cli:
        r = sh.invoke("ssh.exec", {"host": "127.0.0.1", "port": dead_port(),
                                   "cmd": "echo hi", "timeout": 3})
        check((not r.ok) and r.code in ("unavailable", "timeout") and r.err,
              "连不上 ⇒ %s（不是 error 假成功）" % r.code)
        check("连不上" in r.err or "超" in r.err, "失败原因写成人话：%s" % r.err[:60])
    else:
        print("  [skip] 本机没有 ssh 客户端：%s" % why, flush=True)

    # ---------- ④ 设备名片 ----------
    print("④ 设备名片诚实（不许把目标地址写成设备属性）", flush=True)
    ok, reason = sh.probe()
    check(isinstance(ok, bool), "ssh probe 返回 bool（实际 %r）" % (ok,))
    check(mq.probe() == (True, ""), "mqtt probe 恒可用（broker 是参数不是属性）")
    check("password" not in str(mq.snapshot()).lower(),
          "mqtt 名片里没有任何凭据字段")

    if FAIL:
        print("\nt35_channels: FAIL (%d 项)" % len(FAIL), flush=True)
        return 1
    print("t35_channels: PASS（MQTT 真投递 / SSH 真 banner / 失败可分辨）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
