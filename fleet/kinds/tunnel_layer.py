"""隧道设备：把「先连上、再变快」做成天网能调的动作。

## 它补的是 nat 那层留下的缺口

    nat.profile / nat.open / nat.punch   —— 会打洞，但**不打洞就什么都没有**
    tun.*                                —— 打不通也先能用；打得通就自动变快

这不是「给 nat 包一层」。两者的失败语义完全不同：
`nat.punch` 失败 = 会话废了，上层要么重试要么放弃；
`tun.upgrade` 失败 = 什么都没发生，业务一直跑在中继上，用户无感。

**一个只在好网络里能用的功能，等于没有。** 这就是本层存在的全部理由。

## 八条能力，按「一条真实连接」的次序排

    tun.ipv6      我在 IPv6 上的实况（这一条能让后面七条全部变得不必要）
    tun.plan      给定两端网况，我该按什么顺序试（只算，不连，无副作用）
    tun.offer     产一条信令 offer（候选 + 打洞时刻，由会话 id 定）
    tun.accept    校验对端 offer 并回一条（脏数据在这里被挡掉）
    tun.open      开会话。**先连中继，此刻就已经可用**
    tun.upgrade   后台试直连；失败不改可用性，成功则主路迁移
    tun.status    现在走的哪条路、历史怎么变的
    tun.sessions / tun.close

## 为什么 IPv6 排在最前面

IPv4 下「对端在哪」要反复猜：私网地址够不着、公网映射得问 STUN、端口会变、
两端都变就只能走中继。**IPv6 没有 NAT**，每台机器的地址就是它在全球的唯一入口，
上面那整套猜测全部不适用。
所以正确顺序是「先问有没有 v6，有就别折腾 v4」——反过来做（先跑 40 秒 STUN
再发现根本用不上）是纯粹的浪费。

## 信令为什么在这里定义格式

`nat_traversal` 的注释写了「交换候选是上层的事」。本层就是那个上层：
它给出消息**长什么样**（字段白名单、上限、必填项），但不规定**怎么送**——
MQTT、WS 隧道、局域网 HTTP 收件口都可以送。格式归这里，通道归调用方。
这样做的好处是：脏数据在进入打洞阶段之前就被校验掉，而不是拿脏地址去打几十万个包。

## 安全边界

隧道不自己造加密。本层只搬运字节，信任与鉴权在 pairtls / device_auth。
在这里塞一层自研加密，会让「中继看不看得到内容」从「有明确答案」变成「谁都不确定」。
"""
import os
import threading
import time

from .. import tunnel as tun_mod
from .. import nat as nat_mod
from ..core import BaseDevice, DeviceResult as R

#: 隧道会话寿命。比打洞会话（300s）长：隧道是**可用通路**，
#: 不是探测用的临时洞；太短会让「刚连上就断」，太长会攒一堆死会话。
SESSION_TTL = 1800

_SESSIONS = {}
_LOCK = threading.Lock()


def _sweep():
    """清掉过期会话。顺手做，不起后台线程（与「不许常驻进程」一致）。"""
    now = time.time()
    dead = []
    with _LOCK:
        for k, v in list(_SESSIONS.items()):
            if now - v.get("seen", now) > SESSION_TTL:
                try:
                    v["tunnel"].close()
                except Exception:
                    pass
                _SESSIONS.pop(k, None)
                dead.append(k)
    return dead


class TunnelLayer(BaseDevice):
    kind = "tunnel"
    via = "wan"

    def __init__(self, name="tunnel:layer", **kw):
        super().__init__(name, via="wan", **kw)
        self._relay_why = ""
        self.meta.setdefault(
            "note", "先中继可用，再后台升级直连；IPv6 能连就不折腾 NAT")

    def probe(self):
        """本地判断，不联网。

        probe 会被面板反复调用，不能在这里跑 STUN 或拨号——那会把界面拖成幻灯片。
        真正的可达性由 tun.plan / tun.ipv6 按需测。
        """
        if nat_mod.local_ipv4() or tun_mod.ipv6_addresses():
            return True, ""
        return False, "本机既没有非回环 IPv4 也没有公网 IPv6，隧道无从谈起"

    def caps(self):
        return ("tun.ipv6", "tun.plan", "tun.offer", "tun.accept",
                "tun.open", "tun.upgrade", "tun.status",
                "tun.sessions", "tun.close")

    # ---------------------------------------------------------- 分派
    def invoke(self, cap, args=None):
        args = args or {}
        _sweep()

        if cap == "tun.ipv6":
            return self._ipv6(args)

        if cap == "tun.plan":
            return self._plan(args)

        if cap == "tun.offer":
            return self._offer(args)

        if cap == "tun.accept":
            return self._accept(args)

        if cap == "tun.open":
            return self._open(args)

        if cap == "tun.upgrade":
            return self._upgrade(args)

        if cap == "tun.status":
            return self._status(args)

        if cap == "tun.sessions":
            return self._sessions()

        if cap == "tun.close":
            return self._close(args)

        return R.unsupported(cap, "%s 没有这项能力" % self.name,
                             device=self.name, via=self.via)

    # ---------------------------------------------------------- IPv6
    def _ipv6(self, args=None):
        """IPv6 实况：**四档，不是「有/没有」两档**。

        「没有 IPv6」这个结论在国内几乎总是错的 —— v6 是设备联网即分配的东西，
        探不到通常意味着没开、没下发前缀、或缺默认路由。把这三者和真正的
        「一个地址都没有」混在一起报，等于劝人放弃一条本来能走的路。
        """
        args = args or {}
        d = tun_mod.ipv6_diagnose(deep=bool(args.get("deep")))
        cands = tun_mod.ipv6_candidates() if d["addresses"] else []
        return R.success("tun.ipv6", self.name, via=self.via, data={
            "verdict": d["verdict"],
            "usable": d["usable"],
            "addresses": d["addresses"],
            "candidates": cands,
            "egress": d["egress"],
            "firewall": d["firewall"],
            "advice": d["advice"],
            "note": ("有可用公网 IPv6：与同样有 v6 的对端直连时不需要打洞、"
                     "不需要 STUN、不需要中继，NAT 判定整段可跳过"
                     if d["usable"] else "当前不能靠 IPv6 直连，原因见 verdict / advice"),
        })

    # ---------------------------------------------------------- 规划
    def _plan(self, args):
        mine = str(args.get("mine") or "unknown")
        theirs = str(args.get("theirs") or "unknown")
        # IPv6 状态可以由调用方给，也可以现场探（本端能探，对端只能靠信令里带）
        v6 = args.get("ipv6")
        if v6 not in ("both", "one", "none"):
            mine6 = tun_mod.ipv6_addresses()
            theirs6 = args.get("theirs_ipv6") or []
            v6 = tun_mod.ipv6_state(mine6, theirs6)

        steps = tun_mod.plan(
            mine, theirs, ipv6=v6,
            my_stability=args.get("stability", 1.0),
            their_stability=args.get("their_stability", 1.0),
            router_upnp=bool(args.get("upnp")),
            has_relay=args.get("has_relay", True) is not False,
            same_lan=bool(args.get("same_lan")),
        )
        steps["ipv6"] = v6
        return R.success("tun.plan", self.name, via=self.via, data=steps)

    # ---------------------------------------------------------- 信令
    def _offer(self, args):
        sid = args.get("sid") or tun_mod.new_session_id()
        cands = args.get("candidates") or []
        if not cands:
            # 没给候选就现探：本机地址 + （有 STUN 时）公网映射
            cands = [{"ip": ip, "port": int(args.get("port") or 0) or 9,
                      "kind": "host"} for ip in tun_mod.ipv6_addresses()]
            cands += [{"ip": ip, "port": int(args.get("port") or 0) or 9,
                       "kind": "host"} for ip in nat_mod.local_ipv4()]
        if not cands:
            return R.unavailable("tun.offer", "本机没有任何可用候选地址",
                                 device=self.name, via=self.via)
        try:
            msg = tun_mod.make_offer(
                args.get("mine") or "unknown", cands, sid,
                nonce=args.get("nonce"),
                stability=args.get("stability", 1.0),
                lead=float(args.get("lead") or 1.2))
        except ValueError as e:
            return R.error("tun.offer", str(e), device=self.name, via=self.via)
        return R.success("tun.offer", self.name, via=self.via, data={
            "offer": msg,
            "note": "把这条原样交给对端；对端用 tun.accept 校验并回应答",
        })

    def _accept(self, args):
        peer = args.get("offer")
        ok, clean, why = tun_mod.validate_offer(peer)
        if not ok:
            # 关键：脏数据在这里挡住，不往下走到打洞阶段
            return R.error("tun.accept", f"对端 offer 不合法：{why}",
                           device=self.name, via=self.via)
        cands = [{"ip": ip, "port": int(args.get("port") or 0) or 9, "kind": "host"}
                 for ip in tun_mod.ipv6_addresses()]
        cands += [{"ip": ip, "port": int(args.get("port") or 0) or 9, "kind": "host"}
                  for ip in nat_mod.local_ipv4()]
        try:
            ans = tun_mod.make_answer(clean, args.get("mine") or "unknown", cands,
                                      stability=args.get("stability", 1.0))
        except ValueError as e:
            return R.error("tun.accept", str(e), device=self.name, via=self.via)
        wait, synced = tun_mod.punch_window(clean["punch_at"])
        return R.success("tun.accept", self.name, via=self.via, data={
            "answer": ans,
            "peer": clean,
            "punch_in": wait,
            "clock_ok": synced,
            "note": ("时钟基本同步，按 punch_in 等待后开打" if synced else
                     "对端给的开打时刻已过去太多：多半两端时钟不同步，"
                     "应立刻开打并拉长窗口"),
        })

    # ---------------------------------------------------------- 会话
    def _open(self, args):
        sid = args.get("sid") or tun_mod.new_session_id()
        relay_factory = self._relay_factory(args)
        if relay_factory is None:
            return R.unavailable(
                "tun.open",
                self._relay_why or ("没有可用中继。隧道的第一要求是「无论打洞成不成"
                                    "都能用」，没有中继就不该开"),
                device=self.name, via=self.via)

        # IPv6 状态现场探，别信调用方传的 "none"：
        # 传 "none" 会让阶梯把直连判成不可行，然后只能走中继 —— 明明有 v6 却不用。
        v6 = args.get("ipv6")
        if v6 not in ("both", "one", "none"):
            v6 = "both" if tun_mod.ipv6_usable().get("usable") else "none"
            if v6 == "both" and not args.get("theirs_ipv6", True):
                v6 = "one"

        steps = tun_mod.cascade(
            args.get("mine") or "unknown", args.get("theirs") or "unknown",
            ipv6=v6,
            router_upnp=bool(args.get("upnp")),
            has_relay=True, same_lan=bool(args.get("same_lan")))

        t = tun_mod.Tunnel(sid, steps=steps, relay_factory=relay_factory,
                           punch_factory=self._punch_factory(args))
        r = t.open_relay()
        if not r.get("ok"):
            return R.unavailable("tun.open", r.get("err") or "中继建立失败",
                                 device=self.name, via=self.via)

        with _LOCK:
            _SESSIONS[sid] = {"tunnel": t, "seen": time.time(),
                              "created": time.time()}
        return R.success("tun.open", self.name, via=self.via, data={
            "sid": sid, "state": t.state, "path": t.path,
            "ipv6": v6,
            "plan": tun_mod.plan(args.get("mine") or "unknown",
                                 args.get("theirs") or "unknown",
                                 ipv6=v6),
            "note": "中继已通，此刻就能传数据；要更快再调 tun.upgrade",
        })

    def _upgrade(self, args):
        sid = args.get("sid")
        with _LOCK:
            ent = _SESSIONS.get(sid)
        if not ent:
            return R.error("tun.upgrade", f"没有会话 {sid}", device=self.name,
                           via=self.via)
        r = ent["tunnel"].upgrade(peer_candidates=args.get("peer") or [],
                                  seconds=float(args.get("seconds") or 6.0))
        ent["seen"] = time.time()
        if not r.get("ok"):
            return R.unavailable("tun.upgrade", r.get("err") or "升级未开始",
                                 device=self.name, via=self.via)
        if args.get("wait"):
            return R.success("tun.upgrade", self.name, via=self.via,
                             data=ent["tunnel"].wait_upgrade(10))
        return R.success("tun.upgrade", self.name, via=self.via, data={
            "state": ent["tunnel"].state, "async": True,
            "note": "后台进行中；失败不影响当前可用性",
        })

    def _status(self, args):
        sid = args.get("sid")
        with _LOCK:
            ent = _SESSIONS.get(sid)
        if not ent:
            return R.unavailable("tun.status", f"没有会话 {sid}",
                                 device=self.name, via=self.via)
        return R.success("tun.status", self.name, via=self.via,
                         data=ent["tunnel"].status())

    def _sessions(self):
        out = []
        with _LOCK:
            for sid, ent in _SESSIONS.items():
                st = ent["tunnel"].status()
                out.append({"sid": sid, "state": st["state"], "path": st["path"],
                            "age": round(time.time() - ent["created"], 1)})
        return R.success("tun.sessions", self.name, via=self.via, data={
            "count": len(out), "sessions": out})

    def _close(self, args):
        sid = args.get("sid")
        with _LOCK:
            ent = _SESSIONS.pop(sid, None)
        if not ent:
            return R.success("tun.close", self.name, via=self.via, data={
                "closed": False, "note": "本来就没有这个会话"})
        st = ent["tunnel"].close()
        return R.success("tun.close", self.name, via=self.via, data={
            "closed": True, "final": st})

    # ---------------------------------------------------------- 工厂
    def _relay_factory(self, args):
        """把「中继怎么建」收敛成一个**可注入**的函数。

        默认实现走 `tools/cloudnode/wsrelay.py` 的 `/pair`：两台机器各自连进
        同一个房间（房间号 = 会话 id），由中继转发彼此的字节。房间号同时是
        打洞时刻的种子，一个值办两件事，少一轮信令。

        三种来源，按优先级：
          args["relay_factory"]  —— 测试替身 / 自定义实现
          args["relay_url"]      —— 显式给中继地址
          FLEET_WSRELAY          —— 与 netaccel.gateway 共用的开关

        拿不到就返回 None，由 `_open` 如实报「没有中继」。**不造占位对象**：
        以前这里塞过一个 send 直接抛错的假中继，结果是 open 报成功、
        发数据才炸，调用方会以为连上了 —— 那比直接说不可用坏得多。
        """
        f = args.get("relay_factory")
        if callable(f):
            return f

        try:
            from ..relay_pair import make_factory
        except Exception:
            return None

        factory, why = make_factory(args.get("relay_url") or "",
                                    args.get("relay_secret") or "")
        self._relay_why = why
        return factory

    def _punch_factory(self, args):
        """真正打洞的实现。默认走 nat.Puncher；测试可注入替身。"""
        factory = args.get("punch_factory")
        if callable(factory):
            return factory

        def _punch(sid, peers, seconds):
            p = nat_mod.Puncher(nonce=sid.encode("utf-8")[:8].ljust(8, b"0"))
            if not p.prepare():
                return {"ok": False, "err": "开不了 UDP 端口"}
            r = p.punch(peers, seconds=seconds)
            if r.get("ok"):
                return {"ok": True, "sock": p.sock, "peer": r["peer"]}
            p.close()
            return {"ok": False, "err": r.get("err")}
        return _punch
