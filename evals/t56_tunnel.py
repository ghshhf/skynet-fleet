#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T56 隧道编排：先能用，再变快。

这一层补的是 t53 之后剩下的那半步。t53 证明「隔着 NAT 能打通」，
但打通只是一个**动作**，不是一条**通路**：
打不通的时候怎么活，t53 没有回答，而真实网络里打不通是常态
（本机实测 APDF，双对称组合按机理成功率就低）。

所以 T56 钉的是四件事：

  ① IPv6 优先，且这一优先**不是「多一条备选」，是「让整套 NAT 分析失效」**。
     两端都有公网 v6 时没有 NAT 可打，就不该再跑 STUN、猜端口、绕中继。
     反过来做（先跑 40 秒探测再发现根本用不上）是纯浪费。
  ② 打洞时刻由会话 id 唯一决定，两端各算一次即对齐 —— 信令只花一轮。
     并且当对端给的时刻已经过去太多时，必须判定「时钟不同步」，
     而不是傻等负数超时。
  ③ 中继是保底不是降级：`open` 之后**立刻可用**，`upgrade` 失败
     必须退回 relayed 而不是 idle（退成 idle 等于业务被掐断）。
     以及直连中途失效时要**自动回落**中继，不把错误抛给业务。
  ④ 脏信令必须在打洞之前挡住。信令可能来自局域网里任何一个会说话的进程，
     也可能经中继转手；校验不过就拒，不能用「凑合」换成功率。

全离线：不连真实 STUN、不连真实中继。中继与打洞都用替身注入，
门禁验的是**协议形状与失败语义**，真实连通性由环境实测负责。
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="t56_")

from fleet import tunnel as T                            # noqa: E402
from fleet import nat as nat_mod                         # noqa: E402
from fleet.catalog import CAPABILITIES                   # noqa: E402
from fleet.kinds.tunnel_layer import TunnelLayer, _SESSIONS, _sweep  # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ✓ " + msg)
    else:
        print("  ✗ " + msg)
        FAIL.append(msg)


class AllowGate:
    """放行闸门。门禁验能力行为，不验审批流程（那由 t14/t29 单独钉）。"""

    def __call__(self, cap, dev, args, level):
        return True, ""


class FakeRelay:
    def __init__(self, alive=True):
        self.sent = []
        self.closed = False
        self.alive = alive

    def send(self, data):
        if not self.alive:
            raise OSError("中继断线")
        self.sent.append(data)
        return {"ok": True}

    def close(self):
        self.closed = True


class FakeSock:
    """直连 socket 替身：记下发了什么，也能模拟发送失败。"""

    def __init__(self, broken=False):
        self.sent = []
        self.closed = False
        self.broken = broken

    def sendto(self, data, addr):
        if self.broken:
            raise OSError("直连已失效")
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


def main():
    print("T56 隧道编排\n")

    # ---------------------------------------------------------------- ① IPv6
    print("[1] IPv6：能让整套 NAT 分析失效的那一步")

    check(T.ipv6_state(["240e::1"], ["2400::1"]) == "both", "两端都有公网 v6 → both")
    check(T.ipv6_state(["240e::1"], []) == "one", "只有一端 → one")
    check(T.ipv6_state([], []) == "none", "都没有 → none")

    addrs = T.ipv6_addresses()
    import ipaddress as _ipa
    check(isinstance(addrs, list), "ipv6_addresses 恒返回列表（无 v6 不抛异常）")
    check(all(_ipa.ip_address(a).is_global for a in addrs),
          f"只返回全球可达地址，fe80/ULA 已滤掉（本机 {len(addrs)} 个）")
    check(all(not str(a).startswith("fe80") for a in addrs), "链路本地地址不入选")

    # 有 v6 时不该再去打 NAT —— 这是本层最重要的判断
    steps6 = T.cascade("symmetric", "symmetric", ipv6="both")
    check(steps6[0]["name"] == "ipv6", "双对称 + 双 v6 → 首选 IPv6")
    check(steps6[0]["step"] == 0, "IPv6 记为第 0 步（不需要打洞，不是另一条打洞路线）")
    check(T.first_feasible(steps6)["name"] == "ipv6", "首个可行步就是 ipv6")
    check(not T.cascade("symmetric", "symmetric", ipv6="one")[0]["feasible"],
          "只有一端有 v6 → 如实标不可行")

    # ---------------------------------------------------------------- ② 级联
    print("\n[2] 策略级联：裁剪得当，且顺序是先便宜后贵")

    s = T.cascade("port-restricted", "port-restricted")
    names = [x["name"] for x in s]
    check(names == list(T.CASCADE_ORDER), "顺序严格等于 CASCADE_ORDER")
    check(names.index("relay") == len(names) - 1, "中继永远排最后（不抢直连的路）")
    check(T.first_feasible(s)["name"] == "udp-punch", "双锥形首选 UDP 打洞")
    check(s[0]["chance"] >= 0.0 and "why" in s[0], "每一步都带人话理由")

    ss = T.cascade("symmetric", "symmetric")
    check(not ss[2]["feasible"], "双对称判 UDP 打洞不可行（不假装能打）")
    check(ss[3]["chance"] <= 0.15, "双对称的生日攻击把握如实压低（不虚报三成）")
    check(ss[-1]["chance"] == 1.0, "中继标 100%（它确实总能成）")
    check(not T.cascade("symmetric", "symmetric", has_relay=False)[-1]["feasible"],
          "没有中继时 relay 标不可行，而不是假装有")

    check(not T.direct_worth_trying(ss), "双对称不值得在后台折腾（省电省流量）")
    check(T.direct_worth_trying(s), "双锥形值得升级")
    check(T.direct_worth_trying(steps6), "有 IPv6 时值得升级")

    p = T.plan("open", "port-restricted")
    check(p["first"]["name"] == "direct" and p["worth_upgrade"] is True,
          "plan 一步给出第一步 + 要不要升级")
    check("把握" in p["verdict"], "verdict 是人话，不是枚举名")

    # ---------------------------------------------------------------- ③ 时钟
    print("\n[3] 打洞时刻：一轮对齐，不许等负数")

    t1 = T.punch_at("sess-a", now=1000.0)
    t2 = T.punch_at("sess-a", now=1000.0)
    t3 = T.punch_at("sess-b", now=1000.0)
    check(t1 == t2, "同会话 id 两端算出同一时刻（这正是只发一轮信令的前提）")
    check(t1 != t3, "不同会话 id 时刻不同（抖动让旁观者无法预测）")
    check(1001.2 <= t1 <= 1002.0, "时刻落在 lead+spread 窗口内")

    w, ok = T.punch_window(1005.0, now=1000.0)
    check(w == 5.0 and ok, "未来时刻 → 正等待、时钟正常")
    w2, ok2 = T.punch_window(900.0, now=1000.0)
    check(w2 < 0 and not ok2, "对端时刻已过去太多 → 判定时钟不同步")

    # ---------------------------------------------------------------- ④ 信令
    print("\n[4] 信令校验：脏数据在打洞之前就被挡住")

    off = T.make_offer("port-restricted",
                       [{"ip": "1.2.3.4", "port": 5000, "kind": "srflx"},
                        {"ip": "192.168.1.3", "port": 5000}],
                       "s1", nonce="0123456789abcdef", now=1000.0)
    ok, clean, why = T.validate_offer(off)
    check(ok and len(clean["candidates"]) == 2, "正常 offer 通过并清洗")
    check(set(clean.keys()) <= set(T.OFFER_FIELDS), "清洗后只剩白名单字段")

    check(not T.validate_offer(None)[0], "非对象被拒")
    check(not T.validate_offer({"sid": "x"})[0], "缺字段被拒")
    b1 = dict(off); b1["candidates"] = []
    check(not T.validate_offer(b1)[0], "空候选被拒（不拿脏数据去打洞）")
    b2 = dict(off); b2["candidates"] = [{"ip": "1.1.1.1", "port": i} for i in range(50)]
    check(not T.validate_offer(b2)[0], f"候选超 {T.MAX_CANDIDATES} 条被拒")
    b3 = dict(off); b3["punch_at"] = "不是数字"
    check(not T.validate_offer(b3)[0], "punch_at 非数字被拒")
    b4 = dict(off); b4["candidates"] = [{"ip": "1.1.1.1", "port": 70000}]
    check(not T.validate_offer(b4)[0], "端口越界被拒")
    b5 = dict(off); b5["nonce"] = "short"
    check(not T.validate_offer(b5)[0], "nonce 过短被拒")
    b6 = dict(off); b6["punch_at"] = 99999999999
    check(not T.validate_offer(b6)[0], "punch_at 明显离谱被拒")

    ans = T.make_answer(off, "symmetric", [{"ip": "5.6.7.8", "port": 6000}], now=1000.0)
    check(ans["sid"] == off["sid"], "应答沿用同一个 sid")
    check(ans["nonce"] == off["nonce"], "应答照抄 nonce（两端不一致就永远验不过对端的包）")

    # ---------------------------------------------------------------- ⑤ 状态机
    print("\n[5] 状态机：中继先可用，升级失败不许掐断业务")

    relay = FakeRelay()
    sock = FakeSock()
    tu = T.Tunnel("s", steps=s, relay_factory=lambda sid: relay,
                  punch_factory=lambda sid, c, sec: {"ok": True, "sock": sock,
                                                     "peer": ("1.2.3.4", 5000)})
    check(tu.state == T.S_IDLE, "初始为 idle")
    check(tu.open_relay()["ok"] and tu.state == T.S_RELAYED, "open_relay 后 relayed（此刻已可用）")
    check(tu.send(b"a")["via"] == "relay", "升级前走中继")
    tu.upgrade()
    st = tu.wait_upgrade(5)
    check(st["state"] == T.S_DIRECT and st["path"] == "direct", "打洞成功 → direct")
    check(st["has_relay"] is True, "切直连后中继仍保留（兜底不拆）")
    check(tu.send(b"b")["via"] == "direct", "升级后走直连")
    sock.broken = True
    r = tu.send(b"c")
    check(r["ok"] and r["via"] == "relay", "直连中途失效 → 自动回落中继（不抛错给业务）")
    check(tu.state == T.S_RELAYED, "回落之后状态如实变成 relayed")
    tu.close()
    check(tu.state == T.S_CLOSED and tu.relay is None, "close 收干净")

    # 失败路径：打洞失败必须仍然是「可用」
    tu2 = T.Tunnel("s2", steps=s, relay_factory=lambda sid: FakeRelay(),
                   punch_factory=lambda sid, c, sec: {"ok": False, "err": "超时"})
    tu2.open_relay()
    tu2.upgrade()
    st2 = tu2.wait_upgrade(5)
    check(st2["state"] == T.S_RELAYED, "打洞失败退回 relayed（不是 idle —— 业务没断）")
    check(tu2.send(b"x")["ok"], "打洞失败后仍能发数据")
    tu2.close()

    # 打洞工厂抛异常也不许把状态机带崩
    def boom(sid, c, sec):
        raise RuntimeError("假装炸了")
    tu3 = T.Tunnel("s3", steps=s, relay_factory=lambda sid: FakeRelay(),
                   punch_factory=boom)
    tu3.open_relay()
    tu3.upgrade()
    st3 = tu3.wait_upgrade(5)
    check(st3["state"] == T.S_RELAYED, "打洞工厂抛异常 → 仍留在 relayed")
    tu3.close()

    # 双对称不折腾
    tu4 = T.Tunnel("s4", steps=T.cascade("symmetric", "symmetric"),
                   relay_factory=lambda sid: FakeRelay(),
                   punch_factory=lambda *a: {"ok": True})
    tu4.open_relay()
    r4 = tu4.upgrade()
    check(not r4["ok"] and "不折腾" in r4["err"], "双对称跳过后台升级")
    tu4.close()

    # ---------------------------------------------------------------- ⑥ 能力层
    print("\n[6] 能力层：挂得上，且没有中继时如实说不")

    tun_caps = [c.name for c in CAPABILITIES
                if getattr(c, "name", "").startswith("tun.")]
    check(len(tun_caps) == 12,
          f"catalog 里登记了 12 条 tun.* 能力（实际 {len(tun_caps)}）："
          "9 条隧道 + 3 条 pinhole（tun.pinhole / .open / .close）")
    check("tun.plan" in tun_caps and "tun.open" in tun_caps,
          "关键的 plan / open 都在能力表里")
    dev = TunnelLayer()
    caps = dev.caps()
    for c in ("tun.ipv6", "tun.plan", "tun.offer", "tun.accept",
              "tun.open", "tun.upgrade", "tun.status",
              "tun.pinhole", "tun.pinhole.open", "tun.pinhole.close",
              "tun.sessions", "tun.close"):
        check(c in caps, f"{c} 已声明")

    okp, _msg = dev.probe()
    check(okp, "probe 本地判断可用（不联网）")

    r6 = dev.invoke("tun.ipv6")
    check(r6.ok, "tun.ipv6 返回成功")
    d6 = r6.data if hasattr(r6, "data") else r6.get("data")
    check(isinstance(d6, dict) and "addresses" in d6, "tun.ipv6 带 addresses")

    r7 = dev.invoke("tun.plan", {"mine": "port-restricted", "theirs": "symmetric",
                                 "ipv6": "none"})
    check(r7.ok, "tun.plan 成功")
    d7 = r7.data if hasattr(r7, "data") else r7.get("data")
    check(d7.get("mine") == "port-restricted", "plan 回显本端画像")
    check(isinstance(d7.get("steps"), list) and len(d7["steps"]) == len(T.CASCADE_ORDER),
          "plan 返回完整阶梯")

    # 没有中继必须老实说不可用 —— 不许造个假中继让流程「走通」
    r8 = TunnelLayer().invoke("tun.open", {"mine": "open", "theirs": "open"})
    check(not r8.ok, "没有中继时 tun.open 如实失败")

    # 注入中继后能跑通完整能力链
    dev2 = TunnelLayer()
    fake = FakeRelay()
    r9 = dev2.invoke("tun.open", {"sid": "cap1", "mine": "port-restricted",
                                  "theirs": "port-restricted",
                                  "relay_factory": lambda sid: fake})
    check(r9.ok, "注入中继后 tun.open 成功")
    d9 = r9.data if hasattr(r9, "data") else r9.get("data")
    check(d9.get("sid") == "cap1" and d9.get("state") == T.S_RELAYED,
          "open 之后立刻是 relayed（可用）")
    check("cap1" in _SESSIONS, "会话进了登记表")

    r10 = dev2.invoke("tun.status", {"sid": "cap1"})
    check(r10.ok, "tun.status 能查到会话")
    r11 = dev2.invoke("tun.sessions")
    check(r11.ok, "tun.sessions 能列出会话")
    r12 = dev2.invoke("tun.close", {"sid": "cap1"})
    check(r12.ok and "cap1" not in _SESSIONS, "tun.close 真删掉")

    print()
    if FAIL:
        print("T56 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T56 全绿")
    return 0


def _cleanup():
    for sid in list(_SESSIONS.keys()):
        try:
            _SESSIONS[sid]["tunnel"].close()
        except Exception:
            pass
        _SESSIONS.pop(sid, None)
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _cleanup()
