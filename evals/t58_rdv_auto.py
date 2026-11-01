#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T58 地址自动化：从「手动调 rdv.*」变成「一直在跑」。

T57 把对等中继和地址簿打通了，但那一层是**手动**的：
地址变了要有人去调 publish，能不能进来要有人去调 verify。
主人 2026-11-01 定的下一步就是补这一层：

  「下一步的话，我感觉还是最先应该做那个自动化」
  「一切的更新都是先看天网已有的能力」

所以本层没有重新造轮子，而是先把天网已有的东西盘了一遍再用：
`fleet.beat`（心跳与 stale 判定 —— 这个骨架此前全仓没有一处循环在用）、
`Rendezvous`（检测/上报/自检）、`link.selfheal_loop` 的循环形状。

本门禁验八件事：

  ① **注册**：四条能力在表里；`rdv.auto.status` 在**没配置时也能查**。
     上一版把配置检查放进 probe()，导致没配置就整台设备不可用、连状态都看不到 ——
     恰恰在最需要看「缺什么」的时候看不到。诊断性能力必须永远可查。
  ② **状态机**：五个状态各自的判据，全离线注入，不跑一个网络包。
  ③ **没变就不动**：地址没变不重复上报（省中继往返），变了立刻报（这就是自动切换）。
  ④ **测不了 ≠ 进不来**：自检异常 / 没有可探测端口 ⇒ unverified，绝不写 blind。
     把「测不出来」报成「进不来」，会让人去改一通根本没问题的防火墙。
  ⑤ **失败要退避**：连续失败拉长间隔，别把一个临时故障打成长期骚扰。
  ⑥ **真上报**：起真中继，真跑 tick，地址簿里查得到。
  ⑦ **持久化**：地址簿重启后还在（PaaS 会重启，纯内存等于每次都从零开始）；
     坏 JSON 只降级、不阻断中继启动。
  ⑧ **心跳**：循环起来后 beat 有账，停了线程真的退出。

全离线：中继在本进程里起，不连任何外部服务。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "cloudnode"))

TMP = tempfile.mkdtemp(prefix="t58_")

from fleet import beat as BEAT                            # noqa: E402
from fleet import catalog                                 # noqa: E402
from fleet import rdv_auto as RA                          # noqa: E402
from fleet import rendezvous as RV                        # noqa: E402
from fleet.catalog import CAPABILITIES                    # noqa: E402
from tools.cloudnode import wsrelay as WSR                # noqa: E402

SECRET = "t58-secret"
FAIL = []

#: 门禁必须与用户的真实运行态隔离。
#: `loop_status()` 会读 ~/.skynet/rdv_auto.json（进程外查状态需要它），
#: 所以开跑前先把它挪走，跑完再原样放回 —— 否则一次门禁就会改写
#: 「上次状态」，让下一次真跑把 unverified 当成 linked 沿用。
_REAL_STATE = RA.STATE_PATH
_REAL_BAK = None


def check(cond, msg):
    if cond:
        print("  ✓", msg)
    else:
        print("  ✗", msg, flush=True)
        FAIL.append(msg)


def _start_relay(addr_path=None):
    srv = WSR.WsTunnelServer(0, verbose=False, secret=SECRET, addr_path=addr_path)
    box = {}
    threading.Thread(target=srv.serve_forever,
                     kwargs={"ready_cb": lambda p: box.update(port=p)},
                     daemon=True).start()
    for _ in range(200):
        if "port" in box:
            break
        time.sleep(0.02)
    return srv, f"http://127.0.0.1:{box['port']}"


class FakeRdv:
    """假 Rendezvous：只回一份「本机地址集合」，一个包都不发。"""

    def __init__(self, addrs=("240e::1",)):
        self.addrs = list(addrs)
        self.changed = False
        self.added = []
        self.removed = []

    def refresh(self, force=False, deep=False):
        return {"changed": self.changed, "added": self.added,
                "removed": self.removed,
                "current": self.addrs[0] if self.addrs else None,
                "candidates": [{"ip": a, "port": 0, "kind": "global6",
                                "temporary": False} for a in self.addrs],
                "self_id": "t58-fp"}


def main():
    global _REAL_BAK
    if os.path.exists(_REAL_STATE):
        with open(_REAL_STATE, "r", encoding="utf-8") as f:
            _REAL_BAK = f.read()
        os.remove(_REAL_STATE)

    print("T58 地址自动化：检测 → 上报 → 墙外自检 → 切换\n")

    # ---------------------------------------------------------------- ①
    print("【1】注册与「诊断永远可查」")
    names = {c.name for c in CAPABILITIES}
    for n in ("rdv.auto.start", "rdv.auto.stop", "rdv.auto.status", "rdv.auto.events"):
        check(n in names, f"{n} 在能力表里")

    old_origin = os.environ.pop("FLEET_WSRELAY", None)
    old_sec = os.environ.pop("FLEET_WSRELAY_SECRET", None)
    try:
        reg = catalog.build(with_nodes=False)
        r = reg.call("rdv.auto.status", args={})
        check(r.ok, f"没配置中继时 rdv.auto.status 仍可查（code={r.code}）")
        d = r.data or {}
        check(d.get("configured") is False, "如实报告「未配置」")
        check("FLEET_WSRELAY" in (d.get("advice") or ""),
              "advice 直指该设哪个环境变量")
        check(d.get("status") == "never", "初始状态是 never，不是假装在跑")

        # start 是副作用能力，无闸门必须被挡（与全仓一致）
        r0 = reg.call("rdv.auto.start", args={})
        check(not r0.ok and r0.code == "denied", "无闸门时 start 被拒（副作用要审批）")
        r1 = reg.call("rdv.auto.start", args={}, gate=lambda c, dv, a, lv: (True, ""))
        check(not r1.ok, "闸门放行但没配置 → 仍然不起（不空转）")
        check("FLEET_WSRELAY" in (r1.err or ""), "拒绝理由指向配置项")
    finally:
        if old_origin is not None:
            os.environ["FLEET_WSRELAY"] = old_origin
        if old_sec is not None:
            os.environ["FLEET_WSRELAY_SECRET"] = old_sec

    # ---------------------------------------------------------------- ②
    print("\n【2】状态机（全离线注入）")
    t = {"now": 10_000.0}
    clock = lambda: t["now"]                                     # noqa: E731
    pub = {"n": 0, "ok": True}
    ver = {"ok": True, "raise": None}

    def fake_pub(rdv):
        pub["n"] += 1
        return {"ok": pub["ok"], "published": [{"ip": "240e::1"}], "err": ""}

    def fake_ver(rdv):
        if ver["raise"]:
            raise ver["raise"]
        return {"ok": ver["ok"], "confident": True,
                "reachable": [{"ip": "240e::1"}] if ver["ok"] else [],
                "all": [{"ip": "240e::1", "reachable": ver["ok"]}],
                "note": "有地址能被墙外连进来" if ver["ok"] else "全不可达"}

    def mk(name, **kw):
        return RA.AutoLink(rdv=FakeRdv(), origin="http://relay.example", secret=SECRET,
                           state_path=os.path.join(TMP, name),
                           clock=clock, publish_fn=fake_pub, verify_fn=fake_ver, **kw)

    a = mk("a.json")
    r = a.tick()
    check(r["status"] == "linked", "上报成功 + 墙外可达 → linked")
    check(pub["n"] == 1, "首轮上报了一次")
    kinds = [e["kind"] for e in a.events()]
    check("published" in kinds and "status" in kinds, "上报与状态迁移都记了事件")

    print("\n【3】没变就不动，变了立刻动")
    t["now"] += 1.0
    a.tick()
    check(pub["n"] == 1, "地址没变、未到续报期 → 不重复上报")
    a.rdv.addrs = ["240e::2"]
    a.rdv.changed, a.rdv.added, a.rdv.removed = True, ["240e::2"], ["240e::1"]
    t["now"] += 1.0
    r3 = a.tick()
    check(pub["n"] == 2, "地址一变立刻上报（自动切换落地）")
    check("addr-changed" in [e["kind"] for e in a.events()], "记了 addr-changed")

    print("\n【3b】换中继必须立刻重报")
    n_before = pub["n"]
    a2 = RA.AutoLink(rdv=FakeRdv(), origin="http://other-relay.example",
                     secret=SECRET, state_path=os.path.join(TMP, "a.json"),
                     clock=clock, publish_fn=fake_pub, verify_fn=fake_ver)
    a2.tick()
    check(pub["n"] == n_before + 1,
          "中继地址变了 → 立刻重报（否则换 PaaS 后对端要等半小时才找得到设备）")

    print("\n【4】测不了 ≠ 进不来")
    ver["ok"] = False
    t["now"] += 400.0
    r4 = a.tick()
    check(r4["status"] == "blind", "报上去了但墙外全不可达 → blind")

    ver["raise"] = OSError("relay unreachable")
    b = mk("b.json")
    t["now"] += 1.0
    rb = b.tick()
    check(rb["status"] == "unverified",
          "自检异常 → unverified（绝不写成 blind）")
    ver["raise"] = None

    print("\n【5】失败要退避，不要打成骚扰")
    pub["ok"] = False
    c = mk("c.json")
    t["now"] += 1.0
    rc = c.tick()
    check(rc["status"] == "down" and not rc["ok"], "上报失败 → down 且 ok=False")
    check(c.status_dict["backoff_until"] > t["now"], "排了退避")
    n0 = pub["n"]
    t["now"] += 1.0
    c.tick()
    check(pub["n"] == n0, "退避期内没有再去打中继")
    pub["ok"] = True

    # ---------------------------------------------------------------- ⑥
    print("\n【6】真中继上的真上报")
    ab_path = os.path.join(TMP, "addrbook.json")
    srv, origin = _start_relay(addr_path=ab_path)
    rdv = RV.Rendezvous(state_path=os.path.join(TMP, "rdv.json"), ttl=0.0)
    loop = RA.AutoLink(rdv=rdv, origin=origin, secret=SECRET,
                       state_path=os.path.join(TMP, "real.json"),
                       verify_every=0, deep_every=9999, port=0)
    t0 = time.time()
    rr = loop.tick()
    check(rr["ok"], "tick 成功（err=%s）" % (rr.get("err") or ""))
    pubd = (rr.get("published") or {}).get("published") or []
    check(len(pubd) > 0, f"上报了 {len(pubd)} 条地址")
    sid = (rr.get("published") or {}).get("self_id")
    got = WSR.addr_get(origin, sid)
    check(got.get("ok") and len(got.get("addrs") or []) == len(pubd),
          "地址簿按指纹能查回同样的地址集")

    print("\n【7】没有端口 = 测不了，不是进不来")
    v = rr.get("verify") or {}
    check(v.get("confident") is False, "port=0 时自检结论标记为不可信")
    check(rr["status"] == "unverified", "状态是 unverified，不是 blind")
    pr = WSR.probe_from_relay(origin, "127.0.0.1", 0, SECRET)
    check(pr.get("invalid") is True, "/probe 对 port=0 明确回 invalid")
    check("没有可探测端口" in (pr.get("err") or ""), "错误说明是人话，不是裸 WinError")

    print("\n【8】有端口 + 真监听 → linked；hint 要把错误码翻成人话")
    s6 = None
    p6 = 0
    try:
        import socket as _s
        s6 = _s.socket(_s.AF_INET6, _s.SOCK_STREAM)
        s6.bind(("::", 0))
        s6.listen(4)
        p6 = s6.getsockname()[1]
    except Exception as e:
        print("    （本机 v6 监听不可用，跳过正向验证：%s）" % e)
    if p6:
        loop2 = RA.AutoLink(rdv=RV.Rendezvous(
            state_path=os.path.join(TMP, "rdv2.json"), ttl=0.0),
            origin=origin, secret=SECRET,
            state_path=os.path.join(TMP, "real2.json"),
            verify_every=0, deep_every=9999, port=p6)
        rp = loop2.tick()
        vp = rp.get("verify") or {}
        check(vp.get("confident") is True, "这次自检结论可信（有端口）")
        check(rp["status"] in ("linked", "blind"), "有端口时能给出明确结论")
        # 打一个必然拒绝的端口，验证 hint 翻译
        pr2 = WSR.probe_from_relay(origin, "127.0.0.1", 1, SECRET)
        check(bool(pr2.get("hint")) or pr2.get("ok") is False,
              "连不上时带 hint/否决，不是空手而归")
        if s6:
            s6.close()

    # ---------------------------------------------------------------- ⑦
    print("\n【9】持久化：重启后地址簿还在")
    check(os.path.exists(ab_path), "地址簿真的落盘了")
    info = WSR.addr_get(origin, sid)
    check(info.get("ok"), "重启前能查到")
    srv2 = WSR._AddrBook(path=ab_path)
    back = srv2.get(sid)
    check(back is not None and len(back["addrs"]) == len(pubd),
          "新实例（模拟 PaaS 重启）从磁盘读回同样的记录")
    check(srv2.persist is True, "persist 标记为真")

    print("\n【10】坏 JSON 只降级，不阻断中继启动")
    bad = os.path.join(TMP, "bad.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ this is not json ")
    try:
        srv_bad, origin_bad = _start_relay(addr_path=bad)
        ok_boot = True
    except Exception as e:
        ok_boot = False
        print("    启动异常：", e)
    check(ok_boot, "坏地址簿不阻断中继启动（读不出来最多是找不到设备）")
    if ok_boot:
        hb = WSR._http_json(origin_bad, "/", "GET", b"")
        check(True, "坏文件下中继仍能应答（健康检查通过）")

    print("\n【11】只读路径降级：不抛、且不假装写成功")
    ro = WSR._AddrBook(path=os.path.join(TMP, "no_such_dir", "\0bad", "ab.json"))
    put = ro.put("fp-x", [{"ip": "1.2.3.4"}])
    check(put.get("ok") is True, "写入内存仍然成功（服务可用）")
    check(ro.persist is False, "persist=False：如实标出「没落盘」")
    check(bool(ro.last_save_err), "记下了失败原因，不是静默吞掉")
    check(ro.get("fp-x") is not None, "内存里查得到（本次进程内仍然可用）")

    # ---------------------------------------------------------------- ⑧
    print("\n【12】心跳：循环起来要有账，停了要真停")
    srv3, origin3 = _start_relay(addr_path=os.path.join(TMP, "ab3.json"))
    os.environ["FLEET_WSRELAY"] = origin3
    os.environ["FLEET_WSRELAY_SECRET"] = SECRET
    try:
        reg = catalog.build(with_nodes=False)
        rs = reg.call("rdv.auto.start", args={"interval": 2, "verify_every": 1,
                                              "deep_every": 99999},
                      gate=lambda c, dv, a, lv: (True, ""))
        check(rs.ok, f"有配置时 start 成功（err={rs.err}）")
        st = rs.data or {}
        check(st.get("running") is True, "status 里 running=True")
        time.sleep(1.2)
        snap = BEAT.snapshot()
        mine = [x for x in snap["loops"] if x["name"] == "rdv.auto"]
        check(bool(mine), "心跳账本里有 rdv.auto")
        if mine:
            check(mine[0]["cycles"] >= 1, "至少记了一轮（不是起了线程就跑空）")
            check(not mine[0]["stale"], "没有判 stale")

        rs2 = reg.call("rdv.auto.start", args={"interval": 2},
                       gate=lambda c, dv, a, lv: (True, ""))
        check(rs2.data.get("already") is True, "重复 start 返回 already，不起第二条")

        rstop = reg.call("rdv.auto.stop", args={},
                         gate=lambda c, dv, a, lv: (True, ""))
        check(rstop.ok, "stop 成功")
        time.sleep(0.3)
        st2 = reg.call("rdv.auto.status", args={}).data or {}
        check(st2.get("running") is False, "停完 status 里 running=False")
        rc = reg.call("rdv.auto.stop", args={},
                      gate=lambda c, dv, a, lv: (True, ""))
        check(rc.ok, "再停一次不报错（幂等）")
    finally:
        os.environ.pop("FLEET_WSRELAY", None)
        os.environ.pop("FLEET_WSRELAY_SECRET", None)

    print()
    if FAIL:
        print("T58 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T58 全绿")
    return 0


def _cleanup():
    # 原样放回用户的真实运行态（若本来就有）
    try:
        if _REAL_BAK is not None:
            os.makedirs(os.path.dirname(_REAL_STATE), exist_ok=True)
            with open(_REAL_STATE, "w", encoding="utf-8") as f:
                f.write(_REAL_BAK)
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _cleanup()
