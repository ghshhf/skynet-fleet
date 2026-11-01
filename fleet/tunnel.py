"""天网 · 隧道编排层（tunnel）：先能用，再变快。

两个开源项目的核心思想，各取一半 —— 这两条是本层的全部设计依据。

  ① Warpgate（Python，8 插件级联）
     它的关键不是「会打洞」，是**级联 + 剪枝**：内建 NAT 分类器，
     对一对端只跑这对 NAT 真有可能成的策略，而不是八种全试一遍。
     以及「打洞时刻由会话 id 的 hash 推算」—— 两边各算一次就对齐，
     信令只花一轮，不用来回对表（多次 RTT 在打洞这种争分夺秒的场景里是纯浪费）。

  ② RavenFabric 的 DCUtR（libp2p 那套：Direct Connection Upgrade through Relay）
     一句话讲完：**永远不要等最优路径**。
     先连中继（总是成功、立刻可用），再在后台探直连；直连通了平滑迁移，
     旧中继留下做兜底。用户看到的是「秒连」，不是「转圈三十秒然后失败」。

合起来就是本文件的形状：

    cascade()   —— 一对 NAT 画像 → 该走哪几条、什么顺序、各自多大把握
    punch_at()  —— 由会话 id 推出双方一致的开打时刻（一轮信令对齐）
    Tunnel      —— 状态机：relayed → punching → direct（中继永不拆）

为什么中继是保底而不是丢人的降级：
  打洞失败在真实网络里是常态。本机实测是 APDF（端口限制锥），
  而对称 NAT 组合下成功率按机理就低。一个只在好网络里能用的功能等于没有。
  中继保证「任何两张网之间都能连上」，打洞负责「连上之后更快更便宜」。

为什么不在这里做加密：
  本层只搬运字节，不定义信任。加密与鉴权在 pairtls / device_auth 那一层。
  在这里塞一层自研加密，会让「中继看得到内容」变成「谁都不确定它看不到」。
"""
import hashlib
import ipaddress
import os
import socket
import threading
import time

# ---------------------------------------------------------------- 常量

#: 策略阶梯的固定顺序：**先便宜后贵**。
#: 顺序本身就是设计——直连零成本，打洞零外传流量，中继要过第三方。
#: 不能按成功率排序：成功率最高的是中继（100%），排前面就永远轮不到直连。
CASCADE_ORDER = ("ipv6", "direct", "portmap", "udp-punch", "birthday",
                 "tcp-punch", "relay")

#: 「锥形」家族。RFC 5780 里映射行为为 EIM 的三种，加上「过滤测不出来」。
#: 它们的共同点是**公网端口稳定**，这决定了能不能被对端瞄准。
CONE_CLASSES = ("open", "restricted", "port-restricted", "cone-filter-unknown")

#: 候选地址表的硬上限。信令是别人递给我们的数据，不设上限等于送一个
#: 「让我发一万个包」的按钮。8 条已经足够覆盖「公网映射 + 多网卡」的实际情况。
MAX_CANDIDATES = 8

#: 单条信令消息的字段白名单。多余字段一律丢弃而不是原样转发 ——
#: 中继与信令都可能被第三方经手，不能替别人背未知字段。
OFFER_FIELDS = ("sid", "nonce", "punch_at", "nat", "stability", "candidates", "ts")


# ---------------------------------------------------------------- 画像与级联

def _is_cone(cls):
    return cls in CONE_CLASSES


# ---------------------------------------------------------------- IPv6
#
# 这一节的存在，是为了让上面那整套 NAT 分析**在该失效的时候失效**。
#
# IPv4 世代里，「对端在哪」是个要反复猜的问题：私网地址对方够不着，
# 公网映射要问 STUN，端口会变要猜，两端都在变就只能走中继。
# IPv6 把这个问题整个删掉了 —— 每台机器的地址就是它在全球的唯一入口，
# 没有地址转换，也就没有映射行为、过滤行为、端口预测可言。
#
# 所以正确的顺序不是「v4 打洞失败再试 v6」，而是「先问有没有 v6，有就别折腾 v4」。
# 反过来做（先跑 40 秒 STUN 探测再发现根本用不上）是纯粹的浪费。

#: 判「全球可达」用的探针。UDP connect 只做路由选路、不真的发包，
#: 所以拿不到回包也不影响；用它只是为了问系统「你会从哪个 v6 地址出去」。
#: 选阿里 DNS 的 v6 地址，国内可达、不依赖境外。
_V6_PROBE = ("2400:3200::1", 53)


def ipv6_addresses(only_global=True, probe=_V6_PROBE):
    """本机可用于隧道的 IPv6 地址，全球可达的排前面。

    过滤依据（RFC 6890）：
      `::1`        回环，只在本机有意义
      `fe80::/10`  链路本地，只在一个网段内有效，发给对端没有意义
      `fc00::/7`   唯一本地（ULA），相当于 IPv4 私网地址

    为什么**不**过滤隐私地址（RFC 4941）：
      它在功能上完全可用，只是会按小时轮换。这里保留它、由调用方按
      「要不要长期候选」自己决定；直接丢掉会让「只有临时地址」的机器
      被误判成「没有 IPv6」——那是把「能用但会变」错报成「不能用」。
    """
    out = []
    seen = set()

    def _add(ip):
        ip = str(ip or "").split("%")[0].strip()
        if not ip or ip in seen:
            return
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return
        if a.version != 6:
            return
        if only_global and not a.is_global:
            return
        seen.add(ip)
        out.append(ip)

    # ① 出口地址：让系统自己说「你会从哪个地址出网」，最接近真实可达
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            s.connect(probe)
            _add(s.getsockname()[0])
        finally:
            s.close()
    except (OSError, IndexError):
        pass

    # ② 主机名解析出来的其余地址（多网卡、多个前缀时补齐）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET6):
            _add(info[4][0])
    except (socket.gaierror, OSError):
        pass

    return out


def ipv6_state(mine_addrs, theirs_addrs):
    """把两端的 IPv6 掌握情况压成 cascade 要的那个三值。

    刻意只分三档而不是算概率：v6 通不通是**二值**事实，
    给它编一个「78% 成功率」只会让上层误以为需要重试策略。
    """
    a = bool(mine_addrs)
    b = bool(theirs_addrs)
    if a and b:
        return "both"
    if a or b:
        return "one"
    return "none"


def cascade(mine, theirs, my_stability=1.0, their_stability=1.0,
            router_upnp=False, has_relay=True, same_lan=False, ipv6="none"):
    """按两端 NAT 画像给出**该走的策略阶梯**，而不是一个孤立结论。

    为什么必须返回阶梯：`nat.punch_chance` 给的是「最可能的那一招」，
    但真实网络上那一招会失败。失败之后要往哪走，必须在动手之前就排好，
    否则现场只能干等超时、或者直接报错——那等于把「打洞失败」变成「功能不可用」。

    每条返回 {step, name, chance, feasible, why, cost}：
      feasible=False 的条目留在表里但标明不可行。**不删除**是刻意的：
      上层界面要能回答「为什么没试直连」，删掉就只剩一个空白。

    参数口径：
      mine/theirs       —— nat.classify() 的输出（open/restricted/port-restricted/
                           cone-filter-unknown/symmetric-addr/symmetric/unknown）
      my_stability      —— 本侧映射端口实测稳定率（0~1），缺省按 1.0 乐观处理
      router_upnp       —— 路由器是否支持 NAT-PMP / UPnP 端口映射
      has_relay         —— 有没有可用的中继（没有时 relay 标不可行，而不是假装有）
      same_lan          —— 两端是否已在同一网段（此时直连无需公网）
      ipv6              —— 公网 IPv6 的掌握情况："both"（两端都有）/ "one"（只有一端）
                           / "none"。**这是唯一能让上面的 NAT 分析整段失效的变量**：
                           IPv6 没有 NAT，两端都有公网 v6 时根本不需要打洞，
                           也就没有映射行为、过滤行为、端口预测这一整套东西可言。
    """
    a_cone, b_cone = _is_cone(mine), _is_cone(theirs)
    stab = max(0.1, min(1.0, float(my_stability or 1.0)))
    stab2 = max(0.1, min(1.0, float(their_stability or 1.0)))
    pair_stab = min(stab, stab2)

    out = []

    # ---- 0. IPv6 直连：唯一能让下面整套 NAT 分析失效的一步
    # 为什么排第 0 而不是第 1：它不是「另一条打洞路线」，它是**根本不需要打洞**。
    # 两端都有公网 v6 时，对端地址就是真实地址，映射/过滤/端口预测全部不适用。
    # 给 0.9 而不是 1.0，是因为还剩一道真实的墙：主机防火墙默认拦入站。
    if ipv6 == "both":
        out.append(_step(0, "ipv6", 0.9, True,
                         "两端都有公网 IPv6，地址即真实地址，无 NAT 可打；"
                         "剩下只可能卡在主机防火墙，开一个 pinhole 即可", "零"))
    elif ipv6 == "one":
        out.append(_step(0, "ipv6", 0.0, False,
                         "只有一端有公网 IPv6，另一端够不着这个地址", "零"))
    else:
        out.append(_step(0, "ipv6", 0.0, False,
                         "两端都没有公网 IPv6（或未探测），回落到 IPv4 路径", "零"))

    # ---- 1. 直连：零成本，只要有一条路能直接摸到对端就该先试
    if same_lan:
        out.append(_step(1, "direct", 0.99, True,
                         "同一网段，直接走局域网地址，连 NAT 都不碰", "零"))
    elif mine == "open" or theirs == "open":
        out.append(_step(1, "direct", 0.99, True,
                         "至少一端无 NAT 过滤（全锥或公网地址），对端能直接连进来", "零"))
    else:
        out.append(_step(1, "direct", 0.05, False,
                         "两端都在 NAT 后面，公网地址够不着对方", "零"))

    # ---- 2. 端口映射：把 NAT 自己拆掉一点，比打洞稳
    if router_upnp:
        out.append(_step(2, "portmap", round(min(0.9, 0.85 * stab), 2), True,
                         "路由器支持 NAT-PMP/UPnP，开一条映射后对端等同直连",
                         "改路由器状态，用完要撤"))
    else:
        out.append(_step(2, "portmap", 0.0, False,
                         "路由器没报出 NAT-PMP/UPnP，映射这条路走不通", "零"))

    # ---- 3. UDP 打洞：双方端口都稳定，同时发包必成
    if a_cone and b_cone:
        out.append(_step(3, "udp-punch", round(0.95 * pair_stab, 2), True,
                         "两端映射都稳定，同时向对方公网端口发包即可打通", "零外传流量"))
    elif a_cone or b_cone:
        out.append(_step(3, "udp-punch", round(0.45 * pair_stab, 2), True,
                         "一端端口稳定、另一端在变；稳定侧可被瞄准，但对面端口会漂",
                         "零外传流量"))
    else:
        out.append(_step(3, "udp-punch", 0.08, False,
                         "两端端口都在变，没有可瞄准的固定入口", "零"))

    # ---- 4. 生日攻击：对端口在变的那一侧做窗口扫描
    if a_cone or b_cone:
        out.append(_step(4, "birthday", round(0.55 * pair_stab, 2), True,
                         "对稳定侧猛敲一批相邻端口，靠生日悖论撞上对称 NAT 分到的端口",
                         "包量大，可能触发对方防火墙告警"))
    else:
        # 双对称：两边端口都在变，要**同时**撞中双方的随机端口，是乘积不是加法。
        # 单向 256 个包只覆盖约 1/256，双向同时命中再乘一次 —— 实测量级远低于
        # 直觉里的「三成」。给 0.12 不是保守，是按机理算的；给高了会让
        # 后台升级白转一整天，用户只看到「一直在升级、一直没升上去」。
        out.append(_step(4, "birthday", 0.12, True,
                         "两端都在变端口，只能双向同时撞；机理上成功率极低，仅作理论备选",
                         "包量极大，实际不值得试"))

    # ---- 5. TCP 同时打开（RFC 5128）：UDP 被整段封时的备选
    # 双对称下这一步同样要求「端口可预测」，而双对称恰恰不满足 —— 给 0.08。
    # 之前写 0.25 是只看「TCP 比 UDP 容易被放行」，漏了端口前提。
    out.append(_step(5, "tcp-punch", 0.25 if (a_cone or b_cone) else 0.08,
                     mine != "open" and theirs != "open",
                     "UDP 不通时改走 TCP 同时打开；很多网络封 UDP 不封 TCP",
                     "需要两端严格同时 SYN，时序要求苛刻"))

    # ---- 6. 中继：永远能成，代价是流量过第三方
    out.append(_step(6, "relay", 1.0 if has_relay else 0.0, bool(has_relay),
                     "字节经中继转发，任何两张网之间都能连上" if has_relay
                     else "没有可用中继；这一步不可行，只能接受打洞失败",
                     "走第三方带宽，延迟增加"))

    return out


def _step(n, name, chance, feasible, why, cost):
    return {"step": n, "name": name, "chance": round(float(chance), 2),
            "feasible": bool(feasible), "why": why, "cost": cost}


def first_feasible(steps):
    """阶梯上第一个可行的策略。全是不可行时返回 None —— 由上层如实报「无路可走」。"""
    for s in steps or ():
        if s.get("feasible"):
            return s
    return None


def direct_worth_trying(steps):
    """在已经走中继的前提下，还值不值得在后台试直连。

    判据是「有没有一条比中继更便宜的路，且把握不低于 15%」。
    不设这个门，弱网下会陷入「永远在后台打洞、永远打不通」的空转，
    白白耗电耗流量，还让状态栏一直闪「正在升级」。
    """
    for s in steps or ():
        if s.get("name") in ("relay", "portmap"):
            continue
        if s.get("feasible") and s.get("chance", 0) >= 0.15:
            return True
    return False


# ---------------------------------------------------------------- 打洞时刻

def punch_at(session_id, lead=1.2, spread=0.8, now=None):
    """由会话 id 推出**双方一致**的开打时刻（Unix 时间戳）。

    为什么要算而不是约定：约定就要多一轮信令（「我准备好了」「我也准备好了」），
    而打洞本身的窗口只有几百毫秒，多一轮就多一份被 NAT 超时掉的风险。
    两边拿同一个 session_id 各算一次，结果必然相同，信令一轮就够。

    为什么加抖动（spread）：固定的整数秒开打，会被中间设备当成周期性扫描的特征。
    抖动由 hash 决定，对双方仍然一致，但对旁观者不可预测。

    时钟前提：依赖两端系统时钟大致同步（Windows/Linux 默认都开 NTP，
    误差通常在几百毫秒内）。所以 spread 必须大于典型时钟偏差，
    否则两边算出的同一时刻在各自时钟上并不重合 —— 这是这套方案唯一的坑。
    """
    now = time.time() if now is None else float(now)
    h = hashlib.blake2b(str(session_id).encode("utf-8"), digest_size=4).digest()
    frac = int.from_bytes(h, "big") / 0xFFFFFFFF
    return round(now + float(lead) + frac * float(spread), 3)


def punch_window(target_at, now=None, tolerance=3.0):
    """距离开打时刻还有多久，以及是否已经错得离谱。

    返回 (wait, ok)。ok=False 表示对端给的时刻已经过去太多 —— 多半是两端
    时钟不同步。此时不该傻等到负数，应立刻开打并拉长窗口，用时间覆盖不确定。
    """
    now = time.time() if now is None else float(now)
    wait = float(target_at) - now
    return round(wait, 3), wait > -float(tolerance)


# ---------------------------------------------------------------- 信令消息

def new_session_id():
    return os.urandom(8).hex()


def make_offer(mine_profile, candidates, session_id, nonce=None,
               stability=1.0, lead=1.2, now=None):
    """组一条 offer。字段固定为 OFFER_FIELDS，多一个都不放。"""
    if not session_id:
        raise ValueError("session_id 不能为空")
    cands = []
    for c in (candidates or [])[:MAX_CANDIDATES]:
        ip, port = c.get("ip"), c.get("port")
        if not ip or not port:
            continue
        cands.append({"ip": str(ip)[:64], "port": int(port),
                      "kind": str(c.get("kind") or "host")[:16]})
    return {
        "sid": str(session_id),
        # nonce 必须两端一致：早期版本让各自随机，收到对方包永远验不过，
        # 表现为「明明打通了却报超时」。这里由发起方定，应答方照抄。
        "nonce": nonce or os.urandom(8).hex(),
        "punch_at": punch_at(session_id, lead=lead, now=now),
        "nat": str(mine_profile or "unknown")[:32],
        "stability": round(float(stability or 1.0), 3),
        "candidates": cands,
        "ts": round(time.time() if now is None else float(now), 3),
    }


def validate_offer(msg):
    """校验对方递来的 offer。返回 (ok, 清洗后的消息, 原因)。

    信令可能来自局域网里任何一个会说话的进程，也可能经中继转手。
    所以在**用它之前**就把结构钉死：类型不对、字段越界、候选超量的，
    一律拒绝并说明原因，而不是「凑合着用」—— 凑合的结果是打洞阶段
    拿着脏地址打几十万个包出去。
    """
    if not isinstance(msg, dict):
        return False, None, "offer 不是对象"
    clean = {}
    for k in OFFER_FIELDS:
        if k in msg:
            clean[k] = msg[k]

    sid = clean.get("sid")
    if not isinstance(sid, str) or not sid or len(sid) > 64:
        return False, None, "sid 缺失或超长"

    nonce = clean.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 8 or len(nonce) > 64:
        return False, None, "nonce 必须是 8~64 字符的十六进制串"

    try:
        pa = float(clean.get("punch_at"))
    except (TypeError, ValueError):
        return False, None, "punch_at 不是数字"
    if pa <= 0 or pa > 4102444800:      # 2100-01-01，明显离谱的直接拒
        return False, None, "punch_at 超出合理范围"

    cands = clean.get("candidates")
    if not isinstance(cands, list):
        return False, None, "candidates 必须是数组"
    if len(cands) > MAX_CANDIDATES:
        return False, None, f"候选地址超过 {MAX_CANDIDATES} 条（收到 {len(cands)}）"

    ok_cands = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        ip, port = c.get("ip"), c.get("port")
        if not isinstance(ip, str) or not ip or len(ip) > 64:
            continue
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        if not (1 <= port <= 65535):
            continue
        ok_cands.append({"ip": ip, "port": port,
                         "kind": str(c.get("kind") or "host")[:16]})
    if not ok_cands:
        return False, None, "清洗后没有一条可用候选地址"

    clean["candidates"] = ok_cands
    clean["punch_at"] = pa
    clean["nat"] = str(clean.get("nat") or "unknown")[:32]
    try:
        clean["stability"] = max(0.1, min(1.0, float(clean.get("stability") or 1.0)))
    except (TypeError, ValueError):
        clean["stability"] = 1.0
    return True, clean, ""


def make_answer(offer, mine_profile, candidates, stability=1.0, now=None):
    """应答方：照抄 sid 与 nonce，换成自己的候选与画像。

    nonce 照抄是硬要求，不是习惯 —— 见 make_offer 的注释。
    """
    if not isinstance(offer, dict) or not offer.get("sid"):
        raise ValueError("应答必须先有一个合法的 offer")
    return make_offer(mine_profile, candidates, offer["sid"],
                      nonce=offer.get("nonce"), stability=stability, now=now)


# ---------------------------------------------------------------- 隧道状态机

#: 状态取值。故意不用数字，日志和界面里直接读得懂。
S_IDLE = "idle"          # 还没建立任何通路
S_RELAYED = "relayed"    # 中继已通，**此刻就能用**
S_PUNCHING = "punching"  # 中继仍可用，后台正在试直连
S_DIRECT = "direct"      # 直连已通，主路切换；中继保留为兜底
S_CLOSED = "closed"      # 已收摊


class Tunnel:
    """一条会话的完整生命周期：先中继能用，再后台升级直连。

    这是 DCUtR 的最小可信实现，刻意只做三件事：

      1. `open_relay()` 立刻可用 —— 不等任何探测；
      2. `upgrade()` 在后台试直连，**失败不改变任何可用性**；
      3. `send()` 永远按当前最优路径发，路径切换对调用方透明。

    为什么不把「打洞」做成阻塞调用：
    阻塞意味着「打不通就什么都没有」。而中继在这里的意义正是
    「无论打不打洞，业务都能先跑起来」—— 顺序反了，整个设计就退回
    到「运气好才能用」。
    """

    def __init__(self, session_id=None, steps=None, relay_factory=None,
                 punch_factory=None, on_state=None):
        self.sid = session_id or new_session_id()
        self.steps = steps or []
        self.state = S_IDLE
        self.path = None                 # 当前实际在用的路径名
        self.relay = None                # 中继对象（一旦建立就不拆）
        self.sock = None                 # 直连成功后的 UDP socket
        self.peer = None
        self.history = []                # [(ts, from, to, 原因)]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._relay_factory = relay_factory
        self._punch_factory = punch_factory
        self._on_state = on_state

    # ---- 状态
    def _set(self, new, why=""):
        with self._lock:
            old, self.state = self.state, new
            if old != new:
                self.history.append((round(time.time(), 3), old, new, why))
        if old != new and self._on_state:
            try:
                self._on_state(old, new, why)
            except Exception:
                # 回调是别人的代码，不能让它把状态机带崩
                pass
        return old != new

    def status(self):
        with self._lock:
            return {"sid": self.sid, "state": self.state, "path": self.path,
                    "has_relay": self.relay is not None,
                    "has_direct": self.sock is not None,
                    "peer": list(self.peer) if self.peer else None,
                    "closed": self.state == S_CLOSED,
                    "history": list(self.history[-8:])}

    # ---- 第一步：先用上
    def open_relay(self):
        """建立中继通路。这是**唯一保证成功**的一步（前提是给了 relay_factory）。"""
        if self.state in (S_CLOSED,):
            return {"ok": False, "err": "隧道已关闭"}
        if self.relay is not None:
            return {"ok": True, "already": True, "state": self.state}
        if self._relay_factory is None:
            self._set(S_IDLE, "没有中继工厂")
            return {"ok": False, "err": "没有配置中继，无法保证可用性"}
        try:
            self.relay = self._relay_factory(self.sid)
        except Exception as e:
            return {"ok": False, "err": f"中继建立失败：{type(e).__name__}: {e}"}
        self.path = "relay"
        self._set(S_RELAYED, "中继已通，业务可立即开始")
        return {"ok": True, "state": self.state, "path": self.path}

    # ---- 第二步：后台升级
    def upgrade(self, peer_candidates=None, seconds=6.0):
        """后台打洞。**任何失败都不影响当前可用性**，这是本方法的契约。"""
        if self.state in (S_CLOSED, S_DIRECT):
            return {"ok": False, "err": f"当前状态 {self.state} 不需要升级"}
        if not direct_worth_trying(self.steps):
            return {"ok": False, "err": "阶梯上没有比中继更划算的路（不折腾）",
                    "state": self.state}
        if self._punch_factory is None:
            return {"ok": False, "err": "没有配置打洞工厂"}
        if self._thread is not None and self._thread.is_alive():
            return {"ok": False, "err": "已经在升了"}

        self._set(S_PUNCHING, "后台试直连，中继不受影响")
        self._stop.clear()

        def _run():
            try:
                got = self._punch_factory(self.sid, peer_candidates or [], seconds)
            except Exception as e:
                got = {"ok": False, "err": f"{type(e).__name__}: {e}"}
            if got and got.get("ok"):
                self.sock = got.get("sock")
                self.peer = got.get("peer")
                self.path = "direct"
                self._set(S_DIRECT, "直连打通，主路已切换；中继保留为兜底")
            else:
                # 关键：退回 relayed，而不是退回 idle。
                # 打洞失败不代表隧道坏了 —— 业务一直在中继上跑着。
                self._set(S_RELAYED, "打洞未成功，继续走中继")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        return {"ok": True, "state": self.state, "async": True}

    def wait_upgrade(self, timeout=10.0):
        """等后台升级结束（测试与 CLI 用；业务代码不该等这个）。"""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return self.status()

    # ---- 收发
    def send(self, data):
        """按当前最优路径发送，调用方不需要知道走的是哪条。"""
        if self.state == S_CLOSED:
            return {"ok": False, "err": "隧道已关闭"}
        if self.state == S_DIRECT and self.sock is not None and self.peer:
            try:
                self.sock.sendto(data, tuple(self.peer))
                return {"ok": True, "via": "direct", "bytes": len(data)}
            except OSError as e:
                # 直连断了就**自动回落中继**，而不是把错误抛给业务。
                # 这是 DCUtR 的另一半：升级失败要能降级。
                self.path = "relay"
                self._set(S_RELAYED, f"直连失效（{e}），自动回落中继")
        if self.relay is not None:
            r = None
            try:
                sender = getattr(self.relay, "send", None)
                if sender:
                    r = sender(data)
            except Exception as e:
                return {"ok": False, "err": f"中继发送失败：{e}"}
            return {"ok": True, "via": "relay", "result": r, "bytes": len(data)}
        return {"ok": False, "err": "既没有直连也没有中继"}

    # ---- 收摊
    def close(self):
        """按次序收：先停后台线程，再关直连 socket，最后拆中继。"""
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(2.0)
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        if self.relay is not None:
            closer = getattr(self.relay, "close", None)
            if closer:
                try:
                    closer()
                except Exception:
                    pass
            self.relay = None
        self.path = None
        self.peer = None
        self._set(S_CLOSED, "已关闭")
        return self.status()


def plan(mine, theirs, **kw):
    """一步到位算完「造一条隧道要做什么」。

    这是给人和给机器的同一份答案：人看 `steps` 知道因果，
    机器拿 `first` 直接开工，拿 `worth_upgrade` 决定要不要后台折腾。
    """
    steps = cascade(mine, theirs, **kw)
    first = first_feasible(steps)
    return {
        "mine": mine, "theirs": theirs,
        "steps": steps,
        "first": first,
        "worth_upgrade": direct_worth_trying(steps),
        "verdict": (f"先走 {first['name']}（把握 {first['chance']:.0%}）"
                    if first else "无路可走"),
    }


# ---------------------------------------------------------------- 自测

def _selftest():
    """只测本文件能自己负责的东西：策略裁剪、时钟一致、消息校验、状态机。

    不测真实打洞 —— 那要两台机器和两个 NAT，属于 t53 与环境实测的职责。
    """
    ok = fail = 0

    def chk(c, m):
        nonlocal ok, fail
        if c:
            ok += 1
            print(f"  ✓ {m}")
        else:
            fail += 1
            print(f"  ✗ {m}")

    print("【IPv6：能让 NAT 分析整段失效的那一步】")
    chk(ipv6_state(["240e::1"], ["2400::1"]) == "both", "两端都有公网 v6 → both")
    chk(ipv6_state(["240e::1"], []) == "one", "只有一端有 → one")
    chk(ipv6_state([], []) == "none", "两端都没有 → none")
    s6 = cascade("symmetric", "symmetric", ipv6="both")
    chk(first_feasible(s6)["name"] == "ipv6",
        "双对称但有 IPv6 → 首选 IPv6，不去硬打对称 NAT")
    chk(s6[0]["chance"] >= 0.85, "IPv6 直连把握高（只剩主机防火墙一道墙）")
    chk(s6[0]["step"] == 0, "IPv6 是第 0 步（不是「另一条打洞路线」而是「不需要打洞」）")
    chk(not cascade("port-restricted", "port-restricted", ipv6="one")[0]["feasible"],
        "只有一端有 v6 → 如实标不可行（不假装能连）")
    chk(not direct_worth_trying(cascade("symmetric", "symmetric", ipv6="none"))
        and direct_worth_trying(cascade("symmetric", "symmetric", ipv6="both")),
        "有 IPv6 时值得升级，没有时不折腾")
    local6 = ipv6_addresses()
    chk(isinstance(local6, list), "ipv6_addresses 恒返回列表（没 v6 也不抛异常）")
    chk(all(ipaddress.ip_address(x).is_global for x in local6),
        f"返回地址全部全球可达（滤掉 fe80/ULA；本机 {len(local6)} 个）")

    print("【级联裁剪】")
    s = cascade("port-restricted", "port-restricted")
    names = [x["name"] for x in s]
    chk(names == list(CASCADE_ORDER), "策略顺序严格按 先便宜后贵 排列")
    f = first_feasible(s)
    chk(f["name"] == "udp-punch", "双锥形首选 UDP 打洞")
    chk(first_feasible(cascade("open", "symmetric"))["name"] == "direct",
        "一端全锥 → 直连是首个可行步（无 v6 时）")
    chk(not cascade("symmetric", "symmetric")[2]["feasible"],
        "双对称判 UDP 打洞不可行（不假装能打）")
    chk(cascade("symmetric", "symmetric")[-1]["chance"] == 1.0, "双对称时中继标记必成")
    chk(not any(x["feasible"] for x in cascade("symmetric", "symmetric",
                                               has_relay=False)[-1:]),
        "没有中继时 relay 如实标不可行")

    print("【中继优先与升级判断】")
    chk(not direct_worth_trying(cascade("symmetric", "symmetric")),
        "双对称不折腾后台升级（省电省流量）")
    chk(direct_worth_trying(cascade("port-restricted", "port-restricted")),
        "双锥形值得后台升级")
    chk(plan("open", "port-restricted")["first"]["name"] == "direct",
        "plan 给出的第一步是直连")

    print("【打洞时刻一致性】")
    t1 = punch_at("abc123", now=1000.0)
    t2 = punch_at("abc123", now=1000.0)
    t3 = punch_at("abc124", now=1000.0)
    chk(t1 == t2, "同会话 id 两次算出同一时刻")
    chk(t1 != t3, "不同会话 id 算出不同时刻（有抖动）")
    chk(1001.2 <= t1 <= 1002.0, "时刻落在 lead+spread 窗口内")
    w, good = punch_window(1005.0, now=1000.0)
    chk(w == 5.0 and good, "未来时刻等待为正且判定正常")
    w2, bad = punch_window(900.0, now=1000.0)
    chk(w2 < 0 and not bad, "对端时刻已过去太多 → 判定时钟不同步")

    print("【信令校验】")
    off = make_offer("port-restricted",
                     [{"ip": "1.2.3.4", "port": 5000, "kind": "srflx"},
                      {"ip": "192.168.1.3", "port": 5000}],
                     "s1", nonce="0123456789abcdef", now=1000.0)
    good_ok, clean, why = validate_offer(off)
    chk(good_ok and len(clean["candidates"]) == 2, "正常 offer 通过并清洗")
    chk(validate_offer(None)[0] is False, "非对象被拒")
    chk(validate_offer({"sid": "x", "nonce": "0123456789abcdef",
                        "punch_at": 1, "candidates": []})[0] is False,
        "空候选被拒（不拿脏数据去打洞）")
    bad = dict(off)
    bad["candidates"] = [{"ip": "1.1.1.1", "port": i} for i in range(50)]
    chk(validate_offer(bad)[0] is False, f"候选超 {MAX_CANDIDATES} 条被拒")
    bad2 = dict(off)
    bad2["punch_at"] = "不是数字"
    chk(validate_offer(bad2)[0] is False, "punch_at 非数字被拒")
    bad3 = dict(off)
    bad3["candidates"] = [{"ip": "1.1.1.1", "port": 70000}]
    chk(validate_offer(bad3)[0] is False, "端口越界被拒")
    ans = make_answer(off, "symmetric", [{"ip": "5.6.7.8", "port": 6000}],
                      now=1000.0)
    chk(ans["sid"] == off["sid"] and ans["nonce"] == off["nonce"],
        "应答照抄 sid 与 nonce（双方 nonce 必须一致）")

    print("【隧道状态机：先中继，再升级】")

    class FakeRelay:
        def __init__(self):
            self.sent = []
            self.closed = False
        def send(self, data):
            self.sent.append(data)
            return {"ok": True}
        def close(self):
            self.closed = True

    # 场景一：打洞成功
    t = Tunnel(steps=cascade("port-restricted", "port-restricted"),
               relay_factory=lambda sid: FakeRelay(),
               punch_factory=lambda sid, c, s: {"ok": True,
                                                "sock": type("S", (), {"sendto": lambda *a: None, "close": lambda *a: None})(),
                                                "peer": ("1.2.3.4", 5000)})
    chk(t.open_relay()["ok"] and t.state == S_RELAYED, "open_relay 后立刻可用")
    chk(t.send(b"hi")["via"] == "relay", "升级前发数据走中继")
    t.upgrade()
    st = t.wait_upgrade(5)
    chk(st["state"] == S_DIRECT and st["path"] == "direct", "打洞成功后切到直连")
    chk(st["has_relay"] is True, "切直连后中继仍保留（兜底不拆）")
    chk(t.send(b"yo")["via"] == "direct", "升级后发数据走直连")
    t.close()
    chk(t.state == S_CLOSED and t.relay is None, "close 收干净")

    # 场景二：打洞失败 —— 必须仍然是「可用」的
    t2 = Tunnel(steps=cascade("port-restricted", "port-restricted"),
                relay_factory=lambda sid: FakeRelay(),
                punch_factory=lambda sid, c, s: {"ok": False, "err": "超时"})
    t2.open_relay()
    t2.upgrade()
    st2 = t2.wait_upgrade(5)
    chk(st2["state"] == S_RELAYED, "打洞失败退回 relayed（不是 idle — 业务没断）")
    chk(t2.send(b"x")["via"] == "relay", "打洞失败后仍能正常发数据")
    t2.close()

    # 场景三：没有中继，也不该假装能连
    t3 = Tunnel(steps=cascade("symmetric", "symmetric", has_relay=False))
    chk(t3.open_relay()["ok"] is False, "没中继时 open_relay 如实失败")
    chk(t3.send(b"x")["ok"] is False, "没中继没直连时发送如实失败")
    t3.close()

    # 场景四：双对称不该浪费电去升级
    t4 = Tunnel(steps=cascade("symmetric", "symmetric"),
                relay_factory=lambda sid: FakeRelay(),
                punch_factory=lambda sid, c, s: {"ok": True, "sock": None,
                                                 "peer": None})
    t4.open_relay()
    r = t4.upgrade()
    chk(r["ok"] is False and "不折腾" in r["err"], "双对称跳过后台升级")
    t4.close()

    print(f"\n结果：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    raise SystemExit(_selftest())
