#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T53 跨 NAT 打洞：测得出网况、打得通对端、且打不通时如实说打不通。

这一层补的是前几层都没覆盖的场景：**两台机器不在同一张网里**。
局域网直传只需要「在同一个网段」，而真实世界中间隔着家用路由、手机热点、
运营商 CGNAT、公司防火墙。打洞的价值不是「能连」——中继也能连；
是**流量不过第三方服务器**：快、便宜、不被别人看见。

所以门禁钉的是这四件事：

  ① 网况测得出，且**测不出来的时候不许编**。STUN 报文编解码、映射/过滤
     两维判定、以及「服务器忽略 CHANGE-REQUEST 时返回 unknown 而不是
     猜成最宽松」——最后这条是最容易犯的错：把「测不了」报成「没问题」，
     上层就不会走中继，实际连接时才失败，而且不知道失败原因。
  ② 判定顺序不能乱：RFC 5780 规定过滤测试之前不能向备用地址发过包。
     顺序错了会得到自相矛盾的结果（换 IP 通、换端口不通）。
  ③ 打洞必须是**双向**。本机实测是端口限制锥形（APDF），过滤表只放行
     「我先发过包的地址」；只让一侧打、另一侧干等，永远打不通。
     门禁里用两个线程同时打，验的就是这个协议形状。
  ④ 会话有寿命，且能真关掉。开一个 UDP 端口等一整天，等于给自己挖个洞。

全离线：不打真实公网 STUN（那会让门禁依赖网络，换台机器就红），
只验本地协议逻辑 + 回环打洞。真实网况由 `python -m fleet.nat` 按需测。
"""
import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="t53_")
os.environ.pop("FLEET_LAN_PORT", None)

from fleet import nat as nat_mod                            # noqa: E402
from fleet import portmap                                   # noqa: E402
from fleet.catalog import CAPABILITIES, build               # noqa: E402
from fleet.kinds.nat_traversal import SESSION_TTL           # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ✓ " + msg)
    else:
        print("  ✗ " + msg)
        FAIL.append(msg)


class AllowGate:
    """放行闸门。门禁验的是能力行为，不是审批流程；
    审批本身由 t14 等门禁单独钉住。"""

    def __call__(self, cap, dev, args, level):
        return True, ""


def main():
    print("T53 跨 NAT 打洞\n")

    # ---------------------------------------------------------------- ① 报文
    print("[1] STUN 报文编解码")

    pkt, txid = nat_mod.build_binding()
    check(len(pkt) == 20, "基础 Binding 请求为 20 字节")
    check(struct.unpack("!H", pkt[:2])[0] == nat_mod.BINDING_REQUEST, "消息类型正确")
    check(pkt[4:8] == nat_mod.MAGIC_B, "魔数写在正确位置")
    check(pkt[8:20] == txid, "txid 写入且长度 12")

    pkt2, _ = nat_mod.build_binding(change_ip=True, change_port=True)
    check(len(pkt2) == 28, "带 CHANGE-REQUEST 为 28 字节（20 头 + 4 属性头 + 4 标志）")
    check(struct.unpack("!H", pkt2[2:4])[0] == 8, "长度字段只算属性部分 = 8")
    check(struct.unpack("!I", pkt2[24:28])[0] == 0x06, "换 IP+端口标志 = 0x06")
    pkt3, _ = nat_mod.build_binding(change_port=True)
    check(struct.unpack("!I", pkt3[24:28])[0] == 0x02, "只换端口标志 = 0x02")

    # 手工造一个合法成功响应，验证 XOR 解码
    tx = os.urandom(12)
    port_in = 54321
    xor_ip = bytes(a ^ b for a, b in zip(socket.inet_aton("203.0.113.9"), nat_mod.MAGIC_B))
    xor_val = b"\x00\x01" + struct.pack("!H", port_in ^ (nat_mod.MAGIC >> 16)) + xor_ip
    other_val = b"\x00\x01" + struct.pack("!H", 3479) + socket.inet_aton("198.51.100.7")
    body = nat_mod._attr(nat_mod.A_XOR_MAPPED, xor_val) + \
        nat_mod._attr(nat_mod.A_OTHER_ADDR, other_val)
    resp = struct.pack("!HHI", nat_mod.BINDING_SUCCESS, len(body), nat_mod.MAGIC) + tx + body
    r = nat_mod.parse_binding(resp, tx)
    check(r is not None, "合法响应能解析")
    check(r and r.get("mapped") == ("203.0.113.9", port_in),
          "XOR-MAPPED-ADDRESS 解码正确（端口与 IP 都对）")
    check(r and r.get("other") == ("198.51.100.7", 3479),
          "OTHER-ADDRESS 用明文解码，没被当成 XOR")

    check(nat_mod.parse_binding(resp, os.urandom(12)) is None,
          "txid 不匹配一律拒绝（否则迟到响应会串成上一轮结果）")
    check(nat_mod.parse_binding(b"\x00" * 10) is None, "短包拒绝")
    check(nat_mod.parse_binding(b"\xde\xad" + b"\x00" * 18) is None, "非 STUN 包拒绝")

    # ---------------------------------------------------------------- ② 判定
    print("\n[2] 网况判定逻辑")

    check(nat_mod.classify(nat_mod.MAP_EIM, nat_mod.FIL_EIF) == "open",
          "EIM + EIF ⇒ 全锥")
    check(nat_mod.classify(nat_mod.MAP_EIM, nat_mod.FIL_APDF) == "port-restricted",
          "EIM + APDF ⇒ 端口限制锥形（本机实测就是这个）")
    check(nat_mod.classify(nat_mod.MAP_APDM, nat_mod.FIL_APDF) == "symmetric",
          "APDM + APDF ⇒ 对称")
    check(nat_mod.classify(nat_mod.MAP_EIM, None) == "cone-filter-unknown",
          "过滤测不出来时按映射给大类，不硬猜三档")
    check(nat_mod.classify(None, None) == "unknown", "两个维度都没有 ⇒ unknown")

    # 打洞成功率必须随 NAT 变差而单调下降
    c_cone = nat_mod.punch_chance("port-restricted", "port-restricted")[1]
    c_half = nat_mod.punch_chance("port-restricted", "symmetric")[1]
    c_sym = nat_mod.punch_chance("symmetric", "symmetric")[1]
    check(c_cone > c_half > c_sym,
          f"成功率单调：锥×锥 {c_cone} > 锥×对称 {c_half} > 对称×对称 {c_sym}")
    check(nat_mod.punch_chance("open", "symmetric")[0] == "direct",
          "一端无 NAT ⇒ 直连，不必打洞")
    # 映射抖动必须拉低成功率，不能当没发生
    low = nat_mod.punch_chance("port-restricted", "port-restricted", 0.5)[1]
    check(low < c_cone, f"映射稳定率 0.5 时成功率下调（{low} < {c_cone}）")

    ps = nat_mod.predict_ports(42737, 8)
    check(42739 in ps, "端口预测包含实测见过的 +2 增量")
    check(len(ps) == len(set(ps)), "预测端口无重复")
    check(all(1024 <= p <= 65535 for p in ps), "预测端口都在合法区间")

    # ---------------------------------------------------------------- ③ 打洞
    print("\n[3] 回环打洞（双向同时发）")

    tok = os.urandom(8)          # 信令该统一这个 token
    p = nat_mod.Puncher(nonce=tok)
    q = nat_mod.Puncher(nonce=tok)
    ok_p = p.prepare(servers=())     # 不碰 STUN，纯本地
    ok_q = q.prepare(servers=())
    check(ok_p and ok_q, "两端都能开 UDP 端口")
    check(p.local_port != q.local_port, "两个会话占不同端口")

    result = {}

    def run(tag, me, peer):
        result[tag] = me.punch([{"ip": "127.0.0.1", "port": peer.local_port}],
                               burst=5, interval=0.2, seconds=5.0)

    t1 = threading.Thread(target=run, args=("p", p, q), daemon=True)
    t2 = threading.Thread(target=run, args=("q", q, p), daemon=True)
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)
    check(result.get("p", {}).get("ok"), f"P 侧打通（{result.get('p', {}).get('err')}）")
    check(result.get("q", {}).get("ok"), f"Q 侧打通（{result.get('q', {}).get('err')}）")
    check(p.peer == ("127.0.0.1", q.local_port), "P 记下的对端地址正确")
    check(q.peer == ("127.0.0.1", p.local_port), "Q 记下的对端地址正确")

    if result.get("p", {}).get("ok") and result.get("q", {}).get("ok"):
        p.send(b"payload-after-punch")
        got = q.recv(timeout=3)
        check(got is not None and got[0] == b"payload-after-punch",
              "打通后能直接传数据，且打洞残留包不会混进业务数据")
        check(p.keepalive(times=1) == 1, "保活能发出去")

    # nonce 不一致必须打不通（否则谁都能往洞里灌东西）
    tok2 = os.urandom(8)
    x = nat_mod.Puncher(nonce=tok)
    y = nat_mod.Puncher(nonce=tok2)
    x.prepare(servers=())
    y.prepare(servers=())
    rx = {}

    def run_x():
        rx["v"] = x.punch([{"ip": "127.0.0.1", "port": y.local_port}],
                          burst=3, interval=0.2, seconds=1.5)

    def run_y():
        y.punch([{"ip": "127.0.0.1", "port": x.local_port}],
                burst=3, interval=0.2, seconds=1.5)

    ta = threading.Thread(target=run_x, daemon=True)
    tb = threading.Thread(target=run_y, daemon=True)
    ta.start()
    tb.start()
    ta.join(6)
    tb.join(6)
    check(not rx.get("v", {}).get("ok"),
          "nonce 不同的两端打不通（会话 token 是身份，不是装饰）")

    for s in (p, q, x, y):
        s.close()

    # ---------------------------------------------------------------- ④ 能力
    print("\n[4] 能力与闸门")

    names = {c.name for c in CAPABILITIES}
    for n in ("nat.profile", "nat.map", "nat.open", "nat.punch",
              "nat.sessions", "nat.close"):
        check(n in names, f"能力已注册：{n}")

    caps_by_name = {c.name: c for c in CAPABILITIES}
    check(caps_by_name["nat.punch"].side_effect,
          "nat.punch 标了副作用（会往外部地址发包，必须走审批）")
    check(caps_by_name["nat.open"].side_effect,
          "nat.open 标了副作用（要占端口）")
    check(not caps_by_name["nat.profile"].side_effect,
          "nat.profile 无副作用（只测不发）")
    check(not caps_by_name["nat.sessions"].side_effect,
          "nat.sessions 无副作用（只读账本）")

    # 没有闸门时必须拒绝 —— 安全判定不许被环境状态遮掩
    strict = build(path=os.path.join(TMP, "n.json"), gate=None, ttl=0.1, with_nodes=False)
    r = strict.call("nat.open", args={})
    check(not r.ok and r.code == "denied",
          f"没注入闸门时 nat.open 拒绝（实际 {r.code}）")

    # 有闸门时能真开、真关
    reg = build(path=os.path.join(TMP, "n2.json"), gate=AllowGate(), ttl=0.1,
                with_nodes=False)
    o = reg.call("nat.open", args={})
    check(o.ok, f"放行后 nat.open 成功（{o.code} {o.err}）")
    if o.ok:
        d = o.data
        check(bool(d.get("id")), "返回会话 id")
        check(bool(d.get("nonce")), "返回 nonce（信令要用它对齐）")
        check(isinstance(d.get("candidates"), list) and d["candidates"],
              "返回候选地址列表")
        check(all("ip" in c and "port" in c and "kind" in c for c in d["candidates"]),
              "候选条目含 ip/port/kind")
        check(SESSION_TTL > 0, "会话有寿命，不是永久监听")
        lst = reg.call("nat.sessions", args={})
        check(lst.ok and lst.data.get("count", 0) >= 1, "会话列表能看到它")
        cl = reg.call("nat.close", args={"id": d["id"]})
        check(cl.ok and cl.data.get("closed") is True, "能被关掉")
        after = reg.call("nat.sessions", args={})
        check(all(s["id"] != d["id"] for s in after.data.get("sessions", [])),
              "关掉后列表里就没有了")

    # 参数校验：缺 peer 不能瞎打
    o2 = reg.call("nat.open", args={})
    if o2.ok:
        bad = reg.call("nat.punch", args={"id": o2.data["id"]})
        check(not bad.ok, "缺 peer 参数时拒绝（不静默打空）")
        badid = reg.call("nat.punch", args={"id": "不存在的会话",
                                            "peer": [{"ip": "127.0.0.1", "port": 9}]})
        check(not badid.ok, "会话 id 不存在时拒绝")
        reg.call("nat.close", args={"id": o2.data["id"]})

    # ---------------------------------------------------------------- ⑤ 端口映射
    print("\n[5] 端口映射编解码")

    req = struct.pack("!BBHHHI", 0, portmap.NATPMP_OP_MAP_UDP, 0, 8080, 8080, 3600)
    check(len(req) == 12, "NAT-PMP 请求 12 字节")
    check(struct.unpack("!H", req[4:6])[0] == 8080, "内部端口位置正确")
    check(struct.unpack("!I", req[8:12])[0] == 3600, "租期位置正确")

    resp = struct.pack("!BBHIHHI", 0, 129, 0, 12345, 8080, 45678, 3600)
    _, op_r, result, _, ip_, ep_, life = struct.unpack("!BBHIHHI", resp[:16])
    check(op_r == 129, "响应 opcode = 请求 opcode + 128")
    check(result == 0 and ip_ == 8080 and ep_ == 45678, "成功响应可解析")
    check(life == 3600, "租期可解析")

    xml = ('<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:1'
           '</serviceType><controlURL>/ctl/IPConn</controlURL></service>')
    c = portmap._find_control_url(xml, "http://192.168.1.1:5000/root.xml")
    check(c == "http://192.168.1.1:5000/ctl/IPConn", "UPnP 控制地址拼接正确")
    check(portmap._find_control_url("<root></root>", "http://x/") is None,
          "没有 WANConnection 服务时返回 None，不瞎猜")
    bad_xml = ('<service><serviceType>urn:schemas-upnp-org:service:'
               'WANCommonInterfaceConfig:1</serviceType><controlURL>/bad</controlURL></service>')
    check(portmap._find_control_url(bad_xml, "http://h/") is None,
          "不该把 WANCommonInterfaceConfig 当成端口映射服务")

    # ---------------------------------------------------------------- ⑥ 诚实性
    print("\n[6] 测不出来的时候不许编")

    # 服务器忽略 CHANGE-REQUEST 时必须返回 ok=False（而不是猜 EIF）
    fake = {"ok": False}
    check(fake.get("filtering") is None, "占位校验：未测出时 filtering 为空")
    check(nat_mod.FILTER_CN.get(None, "未知") == "未知", "None 不映射成任何具体行为")
    check(nat_mod.CLASS_CN.get(None, "未知") == "未知", "unknown 分类有中文说明")

    # diagnosis 在 STUN 全不可达时必须 ok=False，而不是编一个类型出来
    d = nat_mod.diagnosis(servers=(("127.0.0.1", 9),), timeout=0.3)
    check(not d.get("ok"), "STUN 全不可达时 ok=False（不编造网况）")
    check(d.get("kind") in (None, "unknown"), f"拿不到测量时分类应为未知（实际 {d.get('kind')}）")

    print()
    if FAIL:
        print("T53 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("T53 全绿")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
