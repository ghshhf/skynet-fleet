"""打洞设备：把「两台机器怎么直接连上」变成天网能调的动作。

## 它在整个网络层里的位置

    netpath      本机有哪些地址 / 双栈能不能绑
    discovery    局域网里谁在
    lan_transfer 同网段直接传（LocalSend 协议）
    reach        谁现在可达（外圈账本）
    ─────────────────────────────────────
    nat ← 就是这个文件
    答案：隔着一层或几层 NAT 时，还能不能直连、该怎么连

前四层解决的都是「本来就在一张网里」。真实世界不是这样：一台机器在手机热点后面，
另一台在公司防火墙后面，中间隔着两层甚至三层 NAT。这时要么打洞，要么走中继。
打洞的价值在于——**它让流量不过第三方的服务器**，快、便宜、不被别人看见。

## 六条能力，按「一次真实连接」的次序排

    nat.profile   我在什么网里（映射行为 / 过滤行为 / 能跟谁打通）
    nat.map       跟网关要一个固定端口（UPnP / NAT-PMP，能要到就成功率高一大截）
    nat.open      开一个打洞会话：占住一个 UDP 端口，拿到自己的候选地址
    nat.punch     朝对端的候选地址猛发，同时收对方的（必须双方都调）
    nat.sessions  看现在开着哪些会话
    nat.close     关掉

为什么是「open + punch」两步而不是一个 poke(host) 就完事：
打洞是**双向同时**的动作。本机实测是端口限制锥形（APDF），过滤表只放行
「我先发过包的地址」——如果只有一方在打、另一方在等，等的那一方永远收不到，
因为它还没往对方那儿发过包。所以协议必须是：双方各自 open 拿到候选，
交换候选，然后**两边同时 punch**。少一边就打不通，这不是实现缺陷，是 NAT 的规则。

## 为什么没有内置信令

因为「两台机器怎么交换候选地址」这件事，天网里已经有至少三条路
（局域网发现、WS 隧道、relay），而每条路的凭据和生命周期都不一样，
硬塞一个进这层只会让下一层没法复用。这里只认一件事：
**你把对端候选给我，我就去打。** 交换候选是上层的事。
`nat.open` 返回的 candidates 就是给上层拿去交换的。

## 安全边界

打洞会话是**临时的**：默认 5 分钟不活动就回收，端口随之关闭。
不注册成常开监听，也不接受「谁都能往这个端口灌数据」——
`nat.punch` 只往你显式给的候选地址发，`recv` 只把带匹配 nonce 的包当对端。
"""
import threading
import time
import uuid

from .. import nat as nat_mod
from .. import portmap
from ..core import BaseDevice, DeviceResult as R

#: 会话默认存活时间。打洞是几分钟内的事，开一整天等于给自己挖一个洞。
SESSION_TTL = 300

_SESSIONS = {}
_LOCK = threading.Lock()


def _sweep():
    """清掉过期会话。每次调用顺手做，不起后台线程——
    后台线程与「不许常驻进程」的铁律冲突，而这种清理本来就不需要准时。"""
    now = time.time()
    with _LOCK:
        dead = [k for k, v in _SESSIONS.items() if now - v["seen"] > SESSION_TTL]
        for k in dead:
            try:
                v = _SESSIONS.pop(k)
                v["puncher"].close()
            except OSError:
                pass
    return dead


class NatTraversal(BaseDevice):
    kind = "nat"
    via = "wan"

    def __init__(self, name="nat:traversal", **kw):
        super().__init__(name, via="wan", **kw)
        self.meta.setdefault("note", "跨 NAT 打洞：先测网况，再要端口，最后对打")

    def probe(self):
        """能不能干这活，取决于本机有没有非回环地址。

        不去 ping STUN：probe 会在界面刷新时被反复调用，
        每次跑一遍 3 秒的体检会让面板变成幻灯片。
        真正的可达性由 nat.profile 按需测。
        """
        ips = nat_mod.local_ipv4()
        if not ips:
            return False, "本机没有非回环 IPv4，打洞无从谈起"
        return True, ""

    def caps(self):
        return ("nat.profile", "nat.map", "nat.open", "nat.punch",
                "nat.sessions", "nat.close")

    def invoke(self, cap, args=None):
        args = args or {}
        _sweep()

        if cap == "nat.profile":
            d = nat_mod.diagnosis(local_port=int(args.get("local_port") or 0))
            if not d.get("ok"):
                return R.unavailable("nat.profile",
                                     "STUN 全部不可达，测不出网况；"
                                     "这不代表你没有 NAT，只代表问不到",
                                     device=self.name, via=self.via)
            return R.success("nat.profile", self.name, via=self.via, data=d)

        if cap == "nat.map":
            port = int(args.get("port") or 0)
            if not port:
                return R.error("nat.map", "缺少 port 参数（要映射的本地端口）",
                               device=self.name, via=self.via)
            r = portmap.try_map(port, protocol=(args.get("protocol") or "udp").lower(),
                                lifetime=int(args.get("lifetime") or 3600))
            if r.get("ok"):
                return R.success("nat.map", self.name, via=self.via, data=r)
            # 映射失败是常态（网关不支持 / 上层还有 CGNAT），不是错误
            return R.unavailable("nat.map", r.get("err") or "网关不支持端口映射",
                                 device=self.name, data=r, via=self.via)

        if cap == "nat.open":
            return self._open(args)

        if cap == "nat.punch":
            return self._punch(args)

        if cap == "nat.sessions":
            with _LOCK:
                items = [{"id": k, "local_port": v["puncher"].local_port,
                          "public": v["puncher"].public,
                          "nonce": v["puncher"].nonce.hex(),
                          "peer": v.get("peer"),
                          "age": round(time.time() - v["born"], 1),
                          "idle": round(time.time() - v["seen"], 1)}
                         for k, v in _SESSIONS.items()]
            return R.success("nat.sessions", self.name, via=self.via,
                             data={"count": len(items), "sessions": items,
                                   "ttl": SESSION_TTL})

        if cap == "nat.close":
            sid = args.get("id")
            with _LOCK:
                if sid:
                    v = _SESSIONS.pop(sid, None)
                else:
                    v = None
                    for k in list(_SESSIONS):
                        v = _SESSIONS.pop(k)
                        sid = k
                        break
            if v is None:
                return R.success("nat.close", self.name, via=self.via,
                                 data={"closed": False, "note": "没有这个会话，什么都没关"})
            v["puncher"].close()
            return R.success("nat.close", self.name, via=self.via,
                             data={"closed": True, "id": sid})

        return R.DeviceResult.unsupported(cap, f"{self.name} 没有这项能力",
                                          device=self.name, via=self.via)

    # ------------------------------------------------------------ 开

    def _open(self, args):
        local_port = int(args.get("local_port") or 0)
        p = nat_mod.Puncher(local_port=local_port)
        # 允许上层指定 nonce：信令里先约定好，两侧用同一个，
        # 否则互相认不出对方的打洞包（这个坑自测抓过一次）
        nonce = args.get("nonce")
        if nonce:
            try:
                p.nonce = bytes.fromhex(str(nonce))
            except ValueError:
                return R.error("nat.open", "nonce 必须是十六进制字符串",
                               device=self.name, via=self.via)
        if not p.prepare():
            p.close()
            return R.unavailable("nat.open", "打不开 UDP 端口或绑定失败",
                                 device=self.name, via=self.via)
        sid = uuid.uuid4().hex[:12]
        with _LOCK:
            _SESSIONS[sid] = {"puncher": p, "born": time.time(),
                              "seen": time.time(), "peer": None}
        return R.success("nat.open", self.name, via=self.via, data={
            "id": sid, "local_port": p.local_port,
            "nonce": p.nonce.hex(),
            "candidates": p.candidates(),
            "public": p.public,
            "public_via": getattr(p, "public_via", None),
            "ttl": SESSION_TTL,
            "need": "把这个 candidates 交给对端；同时把对端的 candidates 拿来调 nat.punch。"
                    "双方都要 punch，只有一边打不通。",
        })

    # ------------------------------------------------------------ 打

    def _punch(self, args):
        sid = args.get("id")
        with _LOCK:
            sess = _SESSIONS.get(sid)
        if sess is None:
            return R.error("nat.punch", f"没有会话 {sid}（先 nat.open 或 nat.sessions 看看）",
                           device=self.name, via=self.via)
        peers = args.get("peer") or args.get("candidates") or []
        if isinstance(peers, dict):
            peers = peers.get("candidates") or []
        if not peers:
            return R.error("nat.punch", "缺少 peer 参数（对端的 candidates）",
                           device=self.name, via=self.via)
        extra = []
        # 对端如果开了端口映射，它给的 srflx 可能不是真外部门，这里允许显式补
        for p in args.get("extra_ports") or []:
            try:
                extra.append((p["ip"], int(p["port"])))
            except (KeyError, TypeError, ValueError):
                continue
        res = sess["puncher"].punch(
            peers,
            burst=int(args.get("burst") or 20),
            interval=float(args.get("interval") or 0.5),
            seconds=float(args.get("seconds") or 6.0),
            extra_ports=extra,
        )
        with _LOCK:
            if sid in _SESSIONS:
                _SESSIONS[sid]["seen"] = time.time()
                if res.get("ok"):
                    _SESSIONS[sid]["peer"] = res.get("peer")
        if res.get("ok"):
            return R.success("nat.punch", self.name, via=self.via, data={
                "id": sid, "peer": res["peer"], "sent": res.get("sent"),
                "targets": res.get("targets"),
                "note": "洞已打通。之后这条 UDP 通道可直接收发；"
                        f"约 {nat_mod.KEEPALIVE_INTERVAL} 秒内没流量要发保活，否则 NAT 会把洞填上。",
            })
        return R.unavailable("nat.punch",
                             res.get("err") or "打洞超时",
                             device=self.name, data=res, via=self.via)
