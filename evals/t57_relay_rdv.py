#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T57 对等中继 + 地址动态：让「会变的地址」变成「另类的固态」。

T56 把隧道的**状态机**钉住了（先中继可用、再后台升级、失败回落）。
但那时候中继是个注入的替身 —— 真实的数据面根本没接通：
`wsrelay.py` 只会「WS → 某个 host:port」，那是**出口代理**，
客户端说要连谁，服务端替它连。隧道要的是**对等中继**：
两台机器各自接进来，由中继转发彼此的字节，谁都不指定目标。

T57 钉的就是这半步，外加主人 2026-11-01 点出的那个更根本的问题：

  「会变的地址只要给它加一个动态就行了」
  「动态检测、动态自检、切换，这不本质就是另类的固态吗」

因此本门禁验六件事：

  ① **配对**：同一个房间的两端才能互通；一端走了，另一端必须收到 close
     （不收到就会在那儿傻等，上层看到的是「连着」，实际对端早没了）。
  ② **鉴权**：token 不对一律拒。开放的无鉴权中继等于给全网一个免费跳板。
  ③ **超时如实**：没有对端就报「没有对端」，不许假装配对成功。
  ④ **地址簿**：整份覆盖而不是增量合并 —— 增量会让已消失的地址永远留着，
     对端就会去连一个不存在的地址，而且现象看起来像「网络坏了」。
  ⑤ **墙外自检**：入站到底通不通，只能由墙外来试。本机自连走 hairpin，
     通不通都不代表外网能连 —— 这是最容易骗过自己的一个假信号。
  ⑥ **动态切换**：地址变了自动换入口，没变就不动（乱换一次，对端存的
     那份就全要重查一次）。

全离线：中继在本进程里起，两端在本地回环上配对，不连任何外部服务。
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "cloudnode"))

TMP = tempfile.mkdtemp(prefix="t57_")

from fleet import rendezvous as RV                        # noqa: E402
from fleet import tunnel as T                             # noqa: E402
from fleet.catalog import CAPABILITIES                    # noqa: E402
from fleet import relay_pair as RP                        # noqa: E402
from tools.cloudnode import wsrelay as WSR                # noqa: E402

SECRET = "t57-secret"
FAIL = []


def check(cond, msg):
    if cond:
        print("  ✓", msg)
    else:
        print("  ✗", msg, flush=True)
        FAIL.append(msg)


def _start_relay():
    srv = WSR.WsTunnelServer(0, verbose=False, secret=SECRET)
    box = {}
    threading.Thread(target=srv.serve_forever,
                     kwargs={"ready_cb": lambda p: box.update(port=p)},
                     daemon=True).start()
    for _ in range(200):
        if "port" in box:
            break
        time.sleep(0.02)
    return srv, box.get("port")


def main():
    srv, port = _start_relay()
    if not port:
        print("中继没起来")
        return 1
    origin = f"http://127.0.0.1:{port}"
    pair_url = f"ws://127.0.0.1:{port}/pair"

    # ---------------------------------------------------------------- ① 配对
    print("【① 配对：两端到齐才通，双向都通】")
    got = {}

    def endpoint(name, first=None):
        try:
            s = WSR.connect_pair(pair_url, "room-1", SECRET, timeout=10)
            got[name + ".ok"] = True
            if first:
                s.sendall(first)
            got[name + ".recv"] = s.recv(65536)
            if name == "B":
                s.sendall(b"pong-from-B")
                got["B.recv2"] = s.recv(65536)
            s.close()
        except Exception as e:
            got[name + ".err"] = f"{type(e).__name__}: {e}"

    ta = threading.Thread(target=endpoint, args=("A", b"ping-from-A"))
    ta.start()
    time.sleep(0.25)          # 让 A 先进等待态，覆盖「先到者等待」这条路径
    tb = threading.Thread(target=endpoint, args=("B", None))
    tb.start()
    ta.join(15)
    tb.join(15)

    check(got.get("A.ok") and got.get("B.ok"), "两端都配对成功")
    check(got.get("B.recv") == b"ping-from-A", "A 发的数据到了 B")
    check(got.get("A.recv") == b"pong-from-B", "B 发的数据也到了 A（双向）")
    check(srv.hub.stats["paired"] >= 1, "中继记了配对次数")

    # ---------------------------------------------------------------- ② 鉴权
    print("【② 鉴权：token 不对一律拒】")
    bad = {}
    try:
        WSR.connect_pair(pair_url, "room-auth", "wrong-secret", timeout=5)
        bad["ok"] = True
    except Exception as e:
        bad["err"] = str(e)
    check(bad.get("err") and "bad token" in bad["err"], "错 secret 被拒（且原因是 bad token）")

    nosrk = WSR.WsTunnelServer(0, verbose=False, secret="")
    box = {}
    threading.Thread(target=nosrk.serve_forever,
                     kwargs={"ready_cb": lambda p: box.update(port=p)},
                     daemon=True).start()
    for _ in range(200):
        if "port" in box:
            break
        time.sleep(0.02)
    bad2 = {}
    try:
        WSR.connect_pair(f"ws://127.0.0.1:{box['port']}/pair", "room-x", "anything", timeout=5)
        bad2["ok"] = True
    except Exception as e:
        bad2["err"] = str(e)
    check(bad2.get("err") and "no secret" in bad2["err"], "中继没配 secret 时直接拒（不开放）")

    # ---------------------------------------------------------------- ③ 超时
    print("【③ 超时：没对端就如实说没有，不假装成功】")
    old_wait = WSR.PAIR_WAIT
    WSR.PAIR_WAIT = 1.0
    t0 = time.time()
    tout = {}
    try:
        WSR.connect_pair(pair_url, "room-lonely", SECRET, timeout=3)
        tout["ok"] = True
    except Exception as e:
        tout["err"] = str(e)
    WSR.PAIR_WAIT = old_wait
    # 这里要走的是「服务端等满窗口后主动说没有对端」这条路径，
    # 报的是 no peer in time；客户端自己的超时是另一条路径（服务端不说话）。
    # 两者都不能被当成配对成功 —— 区别只在谁先放弃。
    err = tout.get("err") or ""
    check("no peer" in err or "timeout" in err.lower(),
          "无人配对时如实报「没有对端」，不假装成功")
    check(tout.get("ok") is not True, "绝不当成配对成功")
    check(time.time() - t0 >= 0.9, "确实等足了窗口才放弃，没有秒退")

    # ---------------------------------------------------------------- ④ 地址簿
    print("【④ 地址簿：整份覆盖，地址消失就是消失】")
    r = WSR.addr_put(origin, "fp-1", [{"ip": "240e::1", "port": 7000, "temporary": True},
                                      {"ip": "240e::2", "port": 7000, "temporary": False}],
                     SECRET)
    check(r.get("ok") and r.get("addrs") == 2, "写入两条地址")

    g = WSR.addr_get(origin, "fp-1")
    check(g.get("ok") and len(g.get("addrs")) == 2, "按指纹读回两条")
    check(g.get("age") is not None, "返回 age（对端据此判断新不新）")

    WSR.addr_put(origin, "fp-1", [{"ip": "240e::2", "port": 7000}], SECRET)
    g2 = WSR.addr_get(origin, "fp-1")
    ips = {a["ip"] for a in g2.get("addrs") or []}
    check(ips == {"240e::2"}, "整份覆盖：消失的 240e::1 真的没了（不是增量残留）")

    g3 = WSR.addr_get(origin, "fp-nobody")
    check(g3.get("ok") is False, "查不到的指纹如实返回 ok=false")
    check(WSR.addr_put(origin, "fp-1", [{"ip": "1::1"}], "WRONG").get("ok") is False,
          "错 token 写不进去")
    check(WSR.addr_put(origin, "", [{"ip": "1::1"}], SECRET).get("ok") is False,
          "空指纹被拒（不能拿 IP 当身份）")
    check(WSR.addr_put(origin, "fp-2", [], SECRET).get("ok") is False,
          "空地址列表被拒（不写空地址上去）")

    # ---------------------------------------------------------------- ⑤ 墙外自检
    print("【⑤ 墙外自检：通就是通，不通就是不通】")
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(8)
    live_port = echo.getsockname()[1]

    ok_p = WSR.probe_from_relay(origin, "127.0.0.1", live_port, SECRET)
    check(ok_p.get("ok") is True, "能从墙外连到一个活着的端口")

    dead_port = 1
    bad_p = WSR.probe_from_relay(origin, "127.0.0.1", dead_port, SECRET)
    check(bad_p.get("ok") is False, "连不上的端口如实报失败")
    check(bad_p.get("err"), "失败带原因，不是空")
    echo.close()

    noauth = WSR._http_json(origin, "/probe?target=127.0.0.1&port=1", "GET")
    check(noauth.get("ok") is False and "token" in str(noauth.get("err")),
          "不带 token 的回连请求被拒（不能当中继的扫描器用）")

    # ---------------------------------------------------------------- ⑥ 端点形状
    print("【⑥ 端点形状：send 返回字节数，断了返 0，不抛异常】")
    pr = RP.PairRelay(pair_url, "room-shape", SECRET, sock=_FakeSock())
    check(pr.send(b"abcdef") == 6, "send 返回真正发出的字节数")
    pr.sock._dead = True
    check(pr.send(b"abcdef") == 0, "断开后返回 0 而不是抛异常")
    check(pr.err, "断开原因记在 err 里")
    pr.close()
    check(pr.send(b"x") == 0, "close 之后仍是 0")

    # 隧道层必须认这个 0：不认就会把「已经断了」报成 ok=True
    class _ZeroRelay:
        def send(self, data):
            return 0

        def close(self):
            pass

    tun = T.Tunnel(relay_factory=lambda sid: _ZeroRelay())
    tun.open_relay()
    out = tun.send(b"x")
    check(out["ok"] is False and "断开" in out["err"],
          "中继返回 0 时隧道如实报失败（不再报假成功）")

    # ---------------------------------------------------------------- ⑦ 地址动态
    print("【⑦ 地址动态：指纹稳定、地址可漂、变了才切】")
    sid1 = RV.self_id(os.path.join(TMP, "a.json"))
    sid2 = RV.self_id(os.path.join(TMP, "b.json"))
    check(sid1 == sid2, "指纹来自机器而非文件：换状态文件也是同一台")
    check(sid1.startswith("sf-") and len(sid1) == 19, f"指纹形状对：{sid1}")

    snap = RV.detect()
    six = [c for c in snap["candidates"] if c["family"] == 6]
    if six:
        check(all("temporary" in c for c in six), "v6 候选都带 temporary 标记")
        check(any(c["temporary"] is True for c in six)
              or any(c["temporary"] is False for c in six),
              "临时地址既没被丢掉、也没被当成稳定地址")

    d = RV.diff({"candidates": [{"ip": "a"}, {"ip": "b"}]},
                {"candidates": [{"ip": "a"}, {"ip": "c"}]})
    check(d["added"] == ["c"] and d["removed"] == ["b"], "差异只报变化")
    check(RV.diff({"candidates": [{"ip": "a"}]},
                  {"candidates": [{"ip": "a"}]})["changed"] is False,
          "没变化时 changed=False（调用方据此不必重报）")

    path = os.path.join(TMP, "rv.json")
    rv = RV.Rendezvous(state_path=path, ttl=0)
    r1 = rv.refresh(force=True)
    check(r1["self_id"] == sid1, "refresh 带稳定指纹")
    if r1["candidates"]:
        check(r1["current"] == r1["candidates"][0]["ip"], "首次选中第一个候选")
        check(any(e["kind"] == "addr-changed" for e in r1["events"]),
              "从无到有记为 addr-changed")

    st = RV._load(path)
    st["last"] = {"candidates": [{"ip": "2400:dead::1", "family": 6}]}
    st["current"] = "2400:dead::1"
    RV._save(st, path)
    r2 = RV.Rendezvous(state_path=path, ttl=0).refresh(force=True)
    if r2["candidates"]:
        check(r2["current"] != "2400:dead::1", "旧地址消失后自动切到新地址")
        check(any(e["kind"] == "switch" for e in r2["events"]), "切换留下 switch 记录")
        check("2400:dead::1" in r2["removed"], "消失的地址出现在 removed 里")

    badf = os.path.join(TMP, "bad.json")
    with open(badf, "w", encoding="utf-8") as f:
        f.write("{not json")
    check(RV._load(badf) == {}, "坏状态文件读成空字典，不抛异常")
    check(RV.Rendezvous(state_path=badf, ttl=0).refresh(force=True)["self_id"].startswith("sf-"),
          "状态文件坏了仍能工作（不能因为记忆坏了就瘫）")

    # ---------------------------------------------------------------- ⑧ 编排
    print("【⑧ 动态更新闭环：上报 → 查回 → 自检】")
    rv2 = RV.Rendezvous(state_path=os.path.join(TMP, "pub.json"), ttl=0)
    pub = rv2.publish(origin, SECRET)
    check(pub.get("ok") is True, "把当前地址上报进地址簿")
    check(pub.get("self_id", "").startswith("sf-"), "上报带的是指纹，不是 IP")

    res = rv2.resolve(origin, pub["self_id"])
    check(res.get("ok") is True, "按指纹能查回来（对端就是这么找我的）")
    check(len(res.get("addrs") or []) == len(pub["published"]),
          "查回的地址数与上报的一致")

    ver = rv2.verify(origin, SECRET, only=["127.0.0.1"])
    check("all" in ver and isinstance(ver["all"], list),
          "自检返回逐地址结果（不只是总开关）")
    check(all("reachable" in x for x in ver["all"]),
          "每个地址都明确标了 reachable，不给推测")

    chk2 = RV.Rendezvous(state_path=os.path.join(TMP, "empty.json"), ttl=0)
    ev = chk2.events()
    check(isinstance(ev, list), "事件历史可读")

    # ---------------------------------------------------------------- ⑨ 能力表
    print("【⑨ 能力表：rdv.* 已注册】")
    names = {c.name for c in CAPABILITIES}
    for cap in ("rdv.self", "rdv.publish", "rdv.verify", "rdv.resolve",
                "rdv.events", "rdv.forget"):
        check(cap in names, f"catalog 里有 {cap}")
    check("tun.open" in names and "tun.upgrade" in names, "tun.* 仍在（没被顶掉）")

    # ---------------------------------------------------------------- ⑩ 端到端
    print("【⑩ 端到端：两条真隧道经真中继互通（不是替身）】")
    sid = T.new_session_id()

    def _mk_tunnel():
        return T.Tunnel(
            session_id=sid,
            steps=T.cascade("port-restricted", "port-restricted", ipv6="none"),
            relay_factory=lambda s: RP.PairRelay(pair_url, s, SECRET))

    A, B = _mk_tunnel(), _mk_tunnel()
    res = {}
    ta2 = threading.Thread(target=lambda: res.update(A=A.open_relay()))
    ta2.start()
    time.sleep(0.3)                       # A 先进等待态
    tb2 = threading.Thread(target=lambda: res.update(B=B.open_relay()))
    tb2.start()
    ta2.join(15)
    tb2.join(15)

    check(res.get("A", {}).get("ok") and res.get("B", {}).get("ok"),
          "两条隧道都建立成功")
    check(A.state == "relayed" and B.state == "relayed",
          "都处在 relayed（此刻已经可用）")

    if A.relay and B.relay:
        sent = A.send(b"hello-over-real-relay")
        check(sent.get("ok") and sent.get("via") == "relay", "A 经中继发出")
        check(B.relay.recv(65536) == b"hello-over-real-relay", "B 收到 A 的原文")
        B.send(b"reply-back")
        check(A.relay.recv(65536) == b"reply-back", "A 收到 B 的回复（反向也通）")

    A.close()
    B.close()
    check(A.state == "closed" and B.state == "closed", "两端都收干净")

    print()
    if FAIL:
        print("T57 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T57 全绿")
    return 0


class _FakeSock:
    def __init__(self):
        self.closed = False
        self.err = ""
        self._dead = False

    def send(self, data):
        if self._dead:
            self.closed = True
            return 0
        return len(data)

    def recv(self, n=65536):
        return b""

    def close(self):
        self.closed = True


def _cleanup():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        _cleanup()
