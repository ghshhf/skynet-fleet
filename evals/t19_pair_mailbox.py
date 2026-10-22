"""门禁 t19（N+1b 第 1 步）：配对信箱内核——广播/验证码/会话/审计/链接。

锁的是真会发生的形状，一条条对着第 1 步的判据：

  ① **公告字段最小化**。AST 静态扫 fleet/pair.py：beacon_payload 的 dict
     字面量键 ⊆ BEACON_KEYS；模块里不许出现 6 位数字字面量（写死的码）；
     不许出现 "token"/"cap"/"secret" 当 payload 键。
  ② **验证码纪律**：6 位、一次性、过期拒、5 次试爆锁、锁后正确码也拒、
     刷新解锁并换码。
  ③ **fail-closed**：信箱默认关闭；没开放时 try_pair 直接拒。
  ④ **会话令牌**：长度 ≥32、随机（连发两枚不同）、session_for 对垃圾
     输入返回 None、revoke 后当场失效、revoke_all 全掐。
  ⑤ **审计**：配对成功留 open 带 topic；失败/锁定/刷新 done 不占待办；
     revoke 把 topic 闭环。**令牌不进 journal body**（扫最近记录）。
  ⑥ **链接往返**：make→parse 一致（带 fp/不带 fp）；坏 scheme、缺 #、
     短码、端口非数字一律 None，不补默认值。
  ⑦ **广播行为**：beat 真的往组播组发；setsockopt 钉了
     IP_MULTICAST_TTL=1（LAN-only 的网络层保证）；tap 只收合法公告，
     坏 JSON/错 app/多键/端口是字符串全丢。
  ⑧ **幂等熔断**：revoke 不存在的令牌不炸；没 start 过的 Beacon.stop 不炸。

全离线、可复现：socket 全假、journal 用临时 sqlite，不碰网络不碰真库。

跑法：python evals/t19_pair_mailbox.py
"""
import ast
import json
import os
import socket
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet import pair as P                    # noqa: E402
import journal as J                            # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ---------------------------------------------------------------- 假 socket

class FakeSock:
    """记录 sendto/setsockopt，不碰网络。"""

    def __init__(self):
        self.sent = []
        self.opts = []

    def setsockopt(self, level, opt, val):
        self.opts.append((level, opt, val))

    def sendto(self, data, addr):
        self.sent.append((data, addr))


# ---------------------------------------------------------------- ① 静态

def t_static():
    print("\n[①] 静态：公告字段最小化 + 无写死验证码")
    src = open(os.path.join(ROOT, "fleet", "pair.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    # beacon_payload 里的 dict 字面量键 ⊆ BEACON_KEYS
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "beacon_payload":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    keys |= {k.value for k in sub.keys
                             if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    check(keys and keys <= set(P.BEACON_KEYS),
          "beacon_payload 键集合 %s ⊆ %s" % (sorted(keys), sorted(P.BEACON_KEYS)))

    # 全模块不许有 6 位数字字面量（写死的验证码）
    six = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value.strip()
            if len(v) == 6 and v.isdigit():
                six.add(v)
    check(not six, "无 6 位数字字面量（写死验证码）%s" % (sorted(six) or ""))

    # 公告 payload 函数内部不许出现 token/cap/secret/password 字面量键
    # （只扫 beacon_payload——会话表/返回里的 token 键是合法的）
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "beacon_payload":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Dict):
                    for k in sub.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            if k.value.lower() in ("token", "cap", "secret",
                                                   "password"):
                                bad.add(k.value)
    check(not bad, "公告 payload 无 token/cap/secret/password 键 %s" % (sorted(bad) or ""))


# ---------------------------------------------------------------- ②③④ 信箱

def _mb(journal=None, **kw):
    mb = P.PairingMailbox(name="PC-A", journal=journal, **kw)
    return mb


def t_code():
    print("\n[②] 验证码纪律")
    mb = _mb()
    code = mb.refresh_code()
    check(len(code) == 6 and code.isdigit(), "验证码 6 位数字：%s" % code)

    mb.enabled = True
    r = mb.try_pair(code, controller_name="PC-B")
    check(r.get("ok") and r.get("token"), "正确码配对成功并签发令牌")
    r2 = mb.try_pair(code, controller_name="PC-B")
    check(not r2.get("ok") and r2.get("err"), "一次性：同一码第二次被拒（%s）" % r2.get("err", "")[:20])

    # 过期
    mb2 = _mb(ttl=0.05)
    c2 = mb2.refresh_code()
    mb2.enabled = True
    time.sleep(0.08)
    r = mb2.try_pair(c2, controller_name="PC-B")
    check(not r.get("ok") and "过期" in r.get("err", ""),
          "过期码被拒且说清原因（%s）" % r.get("err", "")[:24])

    # 试爆锁定
    mb3 = _mb()
    mb3.enabled = True
    real = mb3.refresh_code()
    last = ""
    for i in range(P.CODE_MAX_FAILS):
        last = mb3.try_pair("000000", controller_name="evil")["err"]
    r = mb3.try_pair(real, controller_name="PC-B")
    check(not r.get("ok") and "锁定" in r.get("err", ""),
          "%d 次失败后锁定，正确码也拒（%s）" % (P.CODE_MAX_FAILS, r.get("err", "")[:20]))
    st = mb3.code_state()
    check(st["locked_for"] > 0 and st["fails"] >= P.CODE_MAX_FAILS,
          "code_state 反映锁定（剩 %.0fs，失败 %d 次）" % (st["locked_for"], st["fails"]))
    # 刷新解锁并换码：换一枚新的即可正常配对
    mb3.refresh_code()
    c3 = None
    st = mb3.code_state()
    check(st["locked_for"] == 0 and st["fails"] == 0, "刷新后解锁、试爆计数清零")
    c3 = mb3.refresh_code()
    r = mb3.try_pair(c3, controller_name="PC-B")
    check(r.get("ok"), "刷新换码后可正常配对")


def t_failclosed():
    print("\n[③] fail-closed")
    mb = _mb()
    r = mb.try_pair("123456", controller_name="PC-B")
    check(not r.get("ok") and "没有开放配对" in r.get("err", ""),
          "信箱默认关闭：未开放时配对直接拒（%s）" % r.get("err", "")[:22])
    check(mb.code_state()["enabled"] is False, "code_state.enabled 默认 False")


def t_sessions():
    print("\n[④] 会话令牌")
    mb = _mb()
    mb.enabled = True
    r = mb.try_pair(mb.refresh_code(), controller_name="PC-B")
    tok = r["token"]
    check(len(tok) >= 32, "令牌长度 ≥32（实际 %d）" % len(tok))
    r2 = mb.try_pair(mb.refresh_code(), controller_name="PC-C")
    check(r2["token"] != tok, "连发两枚令牌不同")

    check(mb.session_for("垃圾令牌") is None, "垃圾令牌 → None")
    check(mb.session_for("") is None, "空令牌 → None")
    s = mb.session_for(tok)
    check(s is not None and s["controller"] == "PC-B"
          and "token" not in s, "session_for 返回公开形态（无 token 字段）")

    mb.revoke(tok)
    check(mb.session_for(tok) is None, "revoke 后令牌当场失效")
    check(mb.revoke("不存在") is True, "revoke 不存在令牌幂等不炸")

    mb.revoke_all()
    check(mb.session_for(r2["token"]) is None, "revoke_all 全掐")
    check(mb.sessions() == [], "掐断后会话表空")


def t_audit():
    print("\n[⑤] 审计")
    tmp = tempfile.mkdtemp(prefix="t19_")
    jp = os.path.join(tmp, "j.db")
    j = J.Journal(jp)
    mb = _mb(journal=j)
    mb.enabled = True
    r = mb.try_pair(mb.refresh_code(), controller_name="PC-B", cert_fp="ab12cd34")
    tok = r["token"]
    mb.try_pair("000000", controller_name="evil")
    mb.refresh_code()
    mb.revoke(tok)

    rows = j.recent(hours=1, kinds=("see",), limit=50)
    ok_rows = [r for r in rows if r["topic"] == "pair:PC-B"]
    check(len(ok_rows) == 2, "pair:PC-B 有 open+done 两条（配对+闭环）")
    if ok_rows:
        check({x["status"] for x in ok_rows} == {"open", "done"},
              "配对 open、revoke 后闭环（状态: %s）"
              % "/".join(x["status"] for x in ok_rows))
    fails = [r for r in rows if "配对失败" in r["body"]]
    check(fails and all(r["status"] == "done" for r in fails),
          "配对失败记 done 不占待办")
    # 令牌绝不进 journal body
    blob = json.dumps([r["body"] for r in rows], ensure_ascii=False)
    check(tok not in blob and r["token"] not in blob, "令牌不进 journal body")
    # 注：Journal.close(topic) 是「闭环 topic」不是关库；临时 sqlite 随进程退出


# ---------------------------------------------------------------- ⑥ 链接

def t_link():
    print("\n[⑥] 链接往返")
    lk = P.make_link("192.168.1.5", 24560, "123456", fp="ab12cd34")
    check(lk == "fleetpair://192.168.1.5:24560#123456&fp=ab12cd34",
          "make_link 形态（%s）" % lk)
    d = P.parse_link(lk)
    check(d == {"host": "192.168.1.5", "port": 24560, "code": "123456",
                "fp": "ab12cd34"}, "parse 往返一致（带 fp）")
    d2 = P.parse_link(P.make_link("10.0.0.2", 8000, "654321"))
    check(d2 and d2["fp"] == "" and d2["port"] == 8000, "parse 往返一致（无 fp）")
    check(P.parse_link("http://1.2.3.4:80#123456") is None, "非 fleetpair scheme → None")
    check(P.parse_link("fleetpair://1.2.3.4") is None, "缺 # → None")
    check(P.parse_link("fleetpair://1.2.3.4:80#12345") is None, "短码 → None")
    check(P.parse_link("fleetpair://1.2.3.4:xx#123456") is None, "端口非数字 → None")
    check(P.parse_link("") is None, "空串 → None")


# ---------------------------------------------------------------- ⑦ 广播

def t_beacon():
    print("\n[⑦] 广播行为")
    fs = FakeSock()
    b = P.Beacon(name="PC-A", pair_port=24560, sock=fs)
    p = b.beat()
    check(set(p.keys()) == set(P.BEACON_KEYS), "payload 键恰为 BEACON_KEYS")
    check(fs.sent and fs.sent[0][1] == P.MCAST_GRP, "发往组播组 %s" % (P.MCAST_GRP,))
    ttl = [o for o in fs.opts if o[1] == socket.IP_MULTICAST_TTL]
    check(any(o[0] == socket.IPPROTO_IP and o[2] == 1 for o in ttl),
          "setsockopt 钉 IP_MULTICAST_TTL=1（LAN-only，假 sock 也钉）")
    b.stop()  # 没 start 过：不许炸

    # start/stop 真跑一轮（假 sock，不起真网络）
    b2 = P.Beacon(name="PC-B", pair_port=24560, sock=FakeSock(), interval=0.02)
    b2.start()
    time.sleep(0.1)
    n = b2.sent
    b2.stop()
    time.sleep(0.1)
    check(n >= 2, "后台公告器按间隔发送（%.1fs 内 %d 封）" % (0.1, n))
    check(b2.sent == n, "stop 后不再发")

    # tap 只收合法
    good = P.encode_beacon(P.beacon_payload("PC-A", 24560, "ab"))
    check(P.decode_beacon(good)["name"] == "PC-A", "decode 合法公告")
    check(P.decode_beacon("{坏JSON".encode("utf-8")) is None, "坏 JSON → None")
    check(P.decode_beacon(P.encode_beacon({"app": "other", "v": 1,
                                           "name": "x", "pair_port": 1,
                                           "nonce": "n"})) is None,
          "错 app → None")
    check(P.decode_beacon(P.encode_beacon({"app": "fleet", "v": 1,
                                           "name": "x", "pair_port": 1,
                                           "nonce": "n", "extra": 1})) is None,
          "多一个键 → None（字段最小化）")
    check(P.decode_beacon(P.encode_beacon({"app": "fleet", "v": 1,
                                           "name": "x", "pair_port": "80",
                                           "nonce": "n"})) is None,
          "pair_port 是字符串 → None")

    class RecvSock(FakeSock):
        def __init__(self, packets):
            super().__init__()
            self.packets = list(packets)

        def settimeout(self, t):
            pass

        def recvfrom(self, n):
            if not self.packets:
                raise socket.timeout()
            return self.packets.pop(0), ("1.2.3.4", 24560)

    rs = RecvSock([good, "{坏".encode("utf-8"), P.encode_beacon({"app": "other"})])
    got = P.tap_beacons(rs, timeout=0.3)
    check(len(got) == 1 and got[0]["name"] == "PC-A",
          "tap 滤掉坏包只留合法公告（收 %d 封）" % len(got))


# ---------------------------------------------------------------- main

def main():
    print("t19 配对信箱内核：", flush=True)
    t_static()
    t_code()
    t_failclosed()
    t_sessions()
    t_audit()
    t_link()
    t_beacon()
    print("\n[t19] 失败 %d 项" % len(FAIL), flush=True)
    if FAIL:
        for m in FAIL:
            print("  -", m)
        sys.exit(1)
    print("t19 配对信箱内核: PASS", flush=True)


if __name__ == "__main__":
    main()
