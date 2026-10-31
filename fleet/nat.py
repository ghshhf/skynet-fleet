"""天网 · NAT 层：我在什么网里、对面在什么网里、两边能不能直接打通。

## 为什么单独一层

局域网直传（lan_transfer）解决的是「同一个网段」——那是运气好。
真实世界两台机器之间通常隔着 NAT：家用路由、手机热点（运营商 CGNAT）、公司防火墙。
这些 NAT 的共同点是「你出得去，别人进不来」，而天网要的是「随处可达」。

NAT 穿透不是玄学，是一串可测量的事实：
  · 我的公网映射是什么（STUN 告诉我）；
  · 我换一个目的地，映射会不会变（映射行为）；
  · 陌生地址发来的包我收不收（过滤行为）；
  · 这三种行为组合，决定打洞能不能成、该怎么打。
所以这一层的第一件事不是打洞，是**测量**。不知道自己在什么网里，
就只能把「打洞失败」和「对面根本不在这」混为一谈。

## 判定标准的选择

不用 RFC 3489 的四类（Full Cone / Restricted / Port Restricted / Symmetric）。
那份 2003 年的分类已被 RFC 5780 取代，原因写在 5780 里：真实 NAT 不按四类走，
四类算法把大量设备误判，最典型的是「映射端点无关、过滤很严」的组合被塞进
Restricted Cone，于是程序按错误的前提去打包，怎么打都不通。

RFC 5780 拆成两个**独立**维度，各自三档（RFC 4787 定义）：
  映射行为  端点无关(EIM) / 地址依赖(ADM) / 地址端口依赖(APDM)
  过滤行为  端点无关(EIF) / 地址依赖(ADF) / 地址端口依赖(APDF)
两者组合才是真正的网况。本机 2026-11-01 实测：
  映射 = EIM（发往 miwifi 和 bilibili 两个不同 IP，映射端口都是 42737）
  过滤 = APDF（change-ip 与 change-port 的响应都收不到）
  ⇒ 端口限制锥形（Port-Restricted Cone）
这类 NAT **能打洞，但必须双方同时发**：过滤表只放行「我先发过包的地址」，
所以只靠一方猛发没用，两边得对着发。

## 一个必须守住的测试顺序

RFC 5780 明确要求：过滤测试的前置条件是「尚未向备用地址建立过绑定」。
如果先做映射测试（往备用地址发过 binding），过滤测试就会被自己污染——
我第一次实测就是这样，得到「换 IP 通、换端口不通」这种自相矛盾的结果。
所以本模块**固定先过滤、后映射**，且两者用各自独立的 socket。

## 用法

    python -m fleet.nat                  # 体检：我是哪种 NAT，能跟谁打通
    python -m fleet.nat --servers a,b    # 指定 STUN 服务器
    python -m fleet.nat --selftest       # 离线自测（不依赖任何外部服务器）
"""
import json
import os
import random
import socket
import struct
import sys
import threading
import time

MAGIC = 0x2112A442                      # RFC 5389 固定魔数，用来识别「这是 STUN 包」
MAGIC_B = struct.pack("!I", MAGIC)

# ---- STUN 方法（RFC 5389 §6，方法在报文类型的高位）
BINDING_REQUEST = 0x0001
BINDING_SUCCESS = 0x0101
BINDING_ERROR = 0x0111

# ---- 属性类型（RFC 5389 §18 / RFC 5780 §7）
A_MAPPED = 0x0001
A_CHANGE_REQ = 0x0003
A_SOURCE = 0x0004
A_XOR_MAPPED = 0x0020
A_OTHER_ADDR = 0x802C
A_SOFTWARE = 0x8022
A_ERROR_CODE = 0x0009

#: 国内可达的 STUN 服务器。刻意只放实测通过的：
#: stun.l.google.com / stun1.l.google.com 在本机 100% 超时（Google 整段在 TLS 前被切），
#: 放进来只会让每次体检白等 2.5 秒并且给出「没有 NAT」这种假象。
DEFAULT_SERVERS = (
    ("stun.miwifi.com", 3478),          # 有 OTHER-ADDRESS，唯一能做完整 5780 判定的
    ("stun.chat.bilibili.com", 3478),
    ("stun.hitv.com", 3478),
    ("stun.cloudflare.com", 3478),      # 境外，200ms 左右，当交叉验证用
)

#: 配置端口映射用的默认本地 UDP 端口。固定端口比随机端口好在：
#: NAT 的映射老化是「按内部端点」记的，端口换个不停等于每次都从零开始。
DEFAULT_PUNCH_PORT = 0

#: NAT 绑定存活时间。RFC 4787 要求至少 2 分钟；实测家用路由多在 30~120 秒，
#: 运营商 CGNAT 常短到 30 秒。保活按最坏情况取。
BINDING_TTL_GUESS = 30
KEEPALIVE_INTERVAL = 15

# 行为枚举。刻意用字符串而不是 Enum：这些值要进 JSON、进结果信封、进日志，
# 字符串在哪儿都读得懂，Enum 还要多一层序列化。
MAP_EIM = "endpoint-independent"
MAP_ADM = "address-dependent"
MAP_APDM = "address-and-port-dependent"
FIL_EIF = "endpoint-independent"
FIL_ADF = "address-dependent"
FIL_APDF = "address-and-port-dependent"

MAPPING_CN = {MAP_EIM: "端点无关（换目的地端口不变）",
              MAP_ADM: "地址依赖（换目的地 IP 就换端口）",
              MAP_APDM: "地址端口依赖（换目的地端口就换端口）"}
FILTER_CN = {FIL_EIF: "端点无关（谁都能发进来）",
             FIL_ADF: "地址依赖（发过包的那个 IP 能进）",
             FIL_APDF: "地址端口依赖（发过包的那个 IP+端口才能进）"}


# ============================================================ STUN 报文

def _attr(atype, value):
    """构造一个 TLV 属性。值必须补 4 字节对齐（RFC 5389 §15）。"""
    pad = (-len(value)) % 4
    return struct.pack("!HH", atype, len(value)) + value + b"\x00" * pad


def build_binding(txid=None, change_ip=False, change_port=False):
    """构造 Binding Request，可选带 CHANGE-REQUEST（RFC 5780 过滤测试用）。"""
    txid = txid or os.urandom(12)
    body = b""
    if change_ip or change_port:
        flags = (0x04 if change_ip else 0) | (0x02 if change_port else 0)
        body += _attr(A_CHANGE_REQ, struct.pack("!I", flags))
    return struct.pack("!HHI", BINDING_REQUEST, len(body), MAGIC) + txid + body, txid


def _decode_addr(v):
    """解 XOR-MAPPED-ADDRESS / OTHER-ADDRESS 的地址段。

    这两类属性的 8 字节布局一致：保留(1) 家族(1) 端口(2) 地址(4/16)。
    OTHER-ADDRESS 用的是**明文**地址，XOR-MAPPED 才是异或过的——两者不能共用
    同一个解码路径，混用会把端口解成一段垃圾数字，看起来像「NAT 端口随机变化」。
    """
    if len(v) < 8:
        return None
    fam = v[1]
    port = struct.unpack("!H", v[2:4])[0]
    if fam == 0x01:
        return socket.inet_ntoa(v[4:8]), port
    if fam == 0x02:
        return socket.inet_ntop(socket.AF_INET6, v[4:20]), port
    return None


def _decode_xor_addr(v):
    if len(v) < 8:
        return None
    fam = v[1]
    xport = struct.unpack("!H", v[2:4])[0] ^ (MAGIC >> 16)
    if fam == 0x01:
        raw = bytes(a ^ b for a, b in zip(v[4:8], MAGIC_B))
        return socket.inet_ntoa(raw), xport
    if fam == 0x02:
        # IPv6 的 XOR 是与 (magic ++ txid) 共 16 字节异或
        key = MAGIC_B + b"\x00" * 12
        raw = bytes(a ^ b for a, b in zip(v[4:20], key))
        return socket.inet_ntop(socket.AF_INET6, raw), xport
    return None


def parse_binding(data, txid=None):
    """解析 Binding 响应。返回 dict；不是合法响应时返回 None。

    校验三件事：长度够、类型是 success/error、txid 对得上。
    少了 txid 校验，就会把上一轮的迟到响应当成这一轮的结果——
    NAT 测试里这种串包会让判定结果随机跳，而且极难复现。
    """
    if len(data) < 20:
        return None
    mtype, mlen = struct.unpack("!HH", data[:4])
    if mtype not in (BINDING_SUCCESS, BINDING_ERROR):
        return None
    if data[4:8] != MAGIC_B:
        return None
    if txid is not None and data[8:20] != txid:
        return None
    out = {"type": mtype, "raw_len": len(data)}
    off, end = 20, min(20 + mlen, len(data))
    while off + 4 <= end:
        atype, alen = struct.unpack("!HH", data[off:off + 4])
        val = data[off + 4:off + 4 + alen]
        if atype == A_XOR_MAPPED:
            out["mapped"] = _decode_xor_addr(val)
        elif atype == A_MAPPED:
            out.setdefault("mapped", _decode_addr(val))
        elif atype == A_OTHER_ADDR:
            out["other"] = _decode_addr(val)
        elif atype == A_SOURCE:
            out["source"] = _decode_addr(val)
        elif atype == A_ERROR_CODE and alen >= 4:
            out["error"] = 100 * val[2] + val[3]
        elif atype == A_SOFTWARE:
            out["software"] = val.decode("utf-8", "replace")
        off += 4 + ((alen + 3) // 4) * 4
    return out


# ============================================================ 单次探测

def _new_sock(local_port=0, family=socket.AF_INET):
    s = socket.socket(family, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", local_port) if family == socket.AF_INET else ("::", local_port))
    except OSError:
        s.close()
        return None
    return s


def stun_once(sock, dest, change_ip=False, change_port=False, timeout=2.0, retries=2):
    """发一次 Binding 请求，等对应 txid 的响应。

    重试是必须的：UDP 无重传，国内到 STUN 服务器偶发丢包很常见，
    单发一次就把丢包误判成「端口被过滤」，整个过滤行为的结论就反了。
    """
    dest = (dest[0], dest[1]) if sock.family == socket.AF_INET else dest
    for attempt in range(max(1, retries)):
        pkt, txid = build_binding(change_ip=change_ip, change_port=change_port)
        try:
            sock.sendto(pkt, dest)
        except OSError:
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            remain = deadline - time.time()
            if remain <= 0:
                break
            sock.settimeout(remain)
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                break
            r = parse_binding(data, txid)
            if r is None:
                continue
            r["from"] = addr[:2]
            r["rtt_ms"] = round((time.time() - (deadline - timeout)) * 1000, 1)
            return r
    return None


def stun_resolve(host, port=3478):
    """域名 → (ip, port)。解析失败返回 None 而不是抛异常：
    一个服务器挂了不该让整次体检中断。"""
    try:
        return socket.gethostbyname(host), port
    except OSError:
        return None


def stun_multi(sock, dests, timeout=1.0):
    """一次向多个目的地址发 Binding 请求，按 txid 收齐响应。

    为什么要并发：串行探 4 个服务器，每个等 1~2 秒，一轮就是 5 秒；
    三轮映射采样加过滤测试能跑到 40 秒（本机实测确实跑到 39.8 秒）。
    「随处可测」如果测一次要等半分钟，这个能力等于没有——人不会用。

    同一 socket 并发发是安全的：响应靠 txid 区分，不同服务器的回包
    不会串。NAT 侧看到的是同一个内部端点对多个目的地，正好是
    判定映射行为需要的形态。
    """
    pending = {}
    for dest in dests:
        if not dest:
            continue
        pkt, txid = build_binding()
        try:
            sock.sendto(pkt, dest)
        except OSError:
            continue
        pending[txid] = {"dest": dest, "t0": time.time()}
    out = {}
    deadline = time.time() + max(0.2, timeout)
    while pending and time.time() < deadline:
        remain = deadline - time.time()
        if remain <= 0:
            break
        sock.settimeout(remain)
        try:
            data, addr = sock.recvfrom(2048)
        except (socket.timeout, OSError):
            break
        if len(data) < 20:
            continue
        txid = data[8:20]
        meta = pending.get(txid)
        if meta is None:
            continue                      # 迟到/别人的包，忽略
        r = parse_binding(data, txid)
        if r is None:
            continue
        r["from"] = addr[:2]
        r["rtt_ms"] = round((time.time() - meta["t0"]) * 1000, 1)
        out[meta["dest"]] = r
        del pending[txid]
    return out


# ============================================================ 行为判定

def _other_endpoint_alive(server, other, timeout=2.0):
    """验证备用端点真的在服务（RFC 5780 的 OTHER-ADDRESS 是否名副其实）。

    为什么需要这一步：过滤测试的「收不到」有两种成因——
      ① 服务器支持 CHANGE-REQUEST，但从备用地址回来的包被我方 NAT 挡了（真 APDF）
      ② 服务器压根不支持 CHANGE-REQUEST，什么都不回（测不出来）
    两者表现完全一样。只用 CHANGE-REQUEST 的话，遇到②会把「测不了」报成
    「过滤最严」，然后打洞策略全按错误前提走。

    区分办法：**主动**向备用端点发一个普通 Binding 请求。
    我方主动发 ⇒ 自己的过滤表放行 ⇒ 能收到就证明备用端点确实在服务。
    这时再「收不到 CHANGE-REQUEST 的响应」，就只能是被动了 ⇒ 结论可信。
    """
    if not other:
        return False
    s = _new_sock()
    if s is None:
        return False
    try:
        r = stun_once(s, (other[0], other[1]), timeout=timeout)
        return bool(r and r.get("mapped"))
    finally:
        s.close()


def probe_filtering(server, timeout=2.0, sock=None):
    """判定过滤行为（RFC 5780 §4.3/4.4）。

    必须在**干净**的 socket 上做，且之前不能向备用地址发过任何包。
    顺序：Test I → Test II(change ip+port) → Test III(change port)
      II 通           ⇒ EIF（端点无关，最宽松）
      II 不通 III 通  ⇒ ADF（地址依赖）
      II 不通 III 不通 ⇒ APDF（地址端口依赖，最严）
    """
    own = sock is None
    s = sock or _new_sock()
    if s is None:
        return {"ok": False, "err": "开不出 socket"}
    try:
        base = stun_once(s, server, timeout=timeout, retries=2)
        if not base or "mapped" not in base:
            return {"ok": False, "err": "基础 Binding 都没通，无法判定过滤"}
        # t2/t3 **不重试**：CHANGE-REQUEST 是「服务器要么执行要么不执行」，
        # 不回就是不看，重发一遍只是白等一个 timeout。原来用 retries=2 +
        # timeout=2.0，两个测试白烧 8 秒，整次体检被拖到 40 秒。
        t2 = stun_once(s, server, change_ip=True, change_port=True,
                       timeout=min(timeout, 1.2), retries=1)
        t3 = stun_once(s, server, change_port=True,
                       timeout=min(timeout, 1.2), retries=1)

        # 服务器到底认不认 CHANGE-REQUEST：看回包来源有没有真的换地方
        honored = bool(t2 and base.get("from") and t2.get("from") != base.get("from"))
        if t2 is None and t3 is None and base.get("other") is None:
            return {"ok": False, "err": "服务器既无 OTHER-ADDRESS 也不执行 CHANGE-REQUEST",
                    "mapped": base.get("mapped")}
        if not honored and (t2 is not None or t3 is not None):
            # 有回包但来源没变：服务器把 CHANGE-REQUEST 当未知属性忽略了。
            # 这时**不能**报 EIF。报 EIF 等于把「测不出来」说成「很宽松」，
            # 上层会据此认为不需要中继，实际打洞时才发现打不通。
            # 测不了就如实说测不了（项目铁律：不拿近似结果充数）。
            return {"ok": False, "filtering": None, "confident": False,
                    "mapped": base.get("mapped"), "other": base.get("other"),
                    "err": "服务器忽略 CHANGE-REQUEST（回包来源未变），过滤行为无法判定",
                    "test2": bool(t2), "test3": bool(t3)}

        if t2 is not None:
            behavior = FIL_EIF
        elif t3 is not None:
            behavior = FIL_ADF
        else:
            behavior = FIL_APDF
        # 判成 APDF 时，必须排除「服务器不支持」这个替代解释
        confident = honored
        if behavior == FIL_APDF and not honored:
            confident = _other_endpoint_alive(server, base.get("other"), timeout=timeout)
        return {"ok": True, "filtering": behavior, "confident": bool(confident),
                "mapped": base.get("mapped"), "other": base.get("other"),
                "test2": bool(t2), "test3": bool(t3),
                "other_alive": None if behavior != FIL_APDF else bool(confident)}
    finally:
        if own:
            s.close()


def probe_mapping(servers, local_port=0, timeout=2.0, rounds=3):
    """判定映射行为（RFC 5780 §4.1/4.2），**多轮采样**。

    核心手法：**同一个内部端点**发往**不同目的地**，看外部端口变不变。
      miwifi 与 bilibili 拿到同一端口 ⇒ EIM
      同 IP 不同端口                    ⇒ ADM
      两个都变                          ⇒ APDM

    这里必须用同一个 socket。每次新建 socket 会因为内核分配不同本地端口而
    得到不同外部端口，那不是 NAT 对称，是自己换了端点——我第一次实测就差点
    踩这个坑（四个服务器四个端口，差点判成对称 NAT）。

    ## 为什么要多轮

    单轮采样会骗人。本机 2026-11-01 实测：一次采到 [0,0,0]（EIM），
    紧接着另一次采到 [0,3,0]（看起来像对称）。手工连采 6 轮才看清真相——
    **6/6 轮内部完全一致**，那次 [0,3,0] 是 CGNAT 的瞬时抖动（丢包重传
    触发映射重建，或负载均衡瞬时切出口）。

    只测一次就下结论，会把「偶尔抖一下的锥形」判成「对称」，
    然后所有连接请求都被送上中继——功能能用，但白白多绕一跳，
    而且再也发现不了本可以直接打通这件事。所以这里按轮采样，
    用**轮内一致率**说话，抖动用 stability 如实标出来，不静默吸收。
    """
    if rounds < 1:
        rounds = 1
    # DNS 只解析一次，别每轮重来（cloudflare 那种境外域名解析本身就要几百毫秒）
    dests = []
    for host, port in servers:
        d = stun_resolve(host, port)
        if d:
            dests.append((d, f"{host}:{port}"))
    if len(dests) < 2:
        return {"ok": False, "err": f"可解析的 STUN 服务器不足（{len(dests)} 个）", "seen": []}

    samples = []
    for _ in range(rounds):
        s = _new_sock(local_port)
        if s is None:
            return {"ok": False, "err": f"本地端口 {local_port} 绑不上"}
        try:
            got = stun_multi(s, [d for d, _ in dests], timeout=timeout)
            row = []
            for dest, label in dests:
                r = got.get(dest)
                if r and r.get("mapped"):
                    row.append({"server": label, "mapped": r["mapped"],
                                "from": r.get("from"), "rtt_ms": r.get("rtt_ms")})
            if row:
                samples.append({"local": s.getsockname()[1], "seen": row})
        finally:
            s.close()

    if not samples:
        return {"ok": False, "err": "一个可用映射都没探到", "seen": []}
    usable = [x for x in samples if len(x["seen"]) >= 2]
    if not usable:
        return {"ok": False, "err": f"只探到 {len(samples[0]['seen'])} 个映射，无法比较",
                "seen": samples}

    # 轮内一致率：同一内部端点对多个目的地，外部端口是否相同
    consistent = 0
    for x in usable:
        ips = {e["mapped"][0] for e in x["seen"]}
        ports = {e["mapped"][1] for e in x["seen"]}
        if len(ips) == 1 and len(ports) == 1:
            consistent += 1
    stability = round(consistent / len(usable), 3)

    # 以最后一轮为准做判定（NAT 状态会变，远的样本参考价值低）
    last = usable[-1]["seen"]
    ips = {e["mapped"][0] for e in last}
    ports = [e["mapped"][1] for e in last]
    if len(ips) > 1:
        behavior = MAP_APDM
        note = "出口 IP 不一致（多出口或负载均衡），按最严处理"
    elif len(set(ports)) == 1:
        behavior = MAP_EIM
        note = ""
    else:
        behavior = MAP_APDM
        note = ""
    deltas = [ports[i + 1] - ports[i] for i in range(len(ports) - 1)]
    return {
        "ok": True, "mapping": behavior, "seen": last,
        "samples": [x["seen"] for x in samples],
        "rounds": len(usable),
        "stability": stability,
        "unstable": stability < 1.0,
        "deltas": deltas,
        "predictable": bool(deltas) and len(set(deltas)) == 1 and deltas[0] >= 0,
        "note": note,
    }


def classify(mapping, filtering):
    """把两个维度合回一个通俗名字。只为「人能一眼看懂」，判定逻辑仍看原始维度。

    过滤维度测不出来时（国内 STUN 普遍不执行 CHANGE-REQUEST），不硬猜三档，
    只按映射行为给出大类，并以 cone-filter-unknown 标明「过滤未知」。
    映射是锥形就已经说明「对方能稳定找到你的公网端口」，这是能否打洞的
    主因；过滤严不严只影响「要不要同时发」，而同时发本来就是默认打法。
    """
    if mapping is None:
        return "unknown"
    if filtering is None:
        if mapping == MAP_EIM:
            return "cone-filter-unknown"
        return "symmetric-addr" if mapping == MAP_ADM else "symmetric"
    if mapping == MAP_EIM and filtering == FIL_EIF:
        return "open"            # 无 NAT，或全锥
    if mapping == MAP_EIM and filtering == FIL_ADF:
        return "restricted"
    if mapping == MAP_EIM and filtering == FIL_APDF:
        return "port-restricted"
    if mapping == MAP_ADM:
        return "symmetric-addr"
    if mapping == MAP_APDM:
        return "symmetric"
    return "unknown"


CLASS_CN = {"open": "全锥 / 无 NAT", "restricted": "限制锥形",
            "port-restricted": "端口限制锥形", "symmetric-addr": "地址依赖对称",
            "symmetric": "对称（最难）", "cone-filter-unknown": "锥形（过滤未知）",
            "unknown": "未知", None: "未知"}


def punch_chance(mine, theirs, my_stability=1.0):
    """给定两端的 NAT 类型，判断打洞策略与大致成功率。

    这套概率不是拍脑袋，来自各类 NAT 组合的机理：
      · 只要**双方映射都是 EIM**，两边都拿得到稳定公网端口 ⇒ 直接对打，几乎必成
        （过滤再严也没关系，因为「同时发」正好骗过过滤表）；
      · 一方对称一方锥形 ⇒ 锥形侧地址稳定，对称侧端口要猜 ⇒ 生日攻击/端口预测，
        成功率随端口随机度下降；
      · 双方对称 ⇒ 两边端口都在变，只能靠高密度生日攻击或直接走中继。

    my_stability 是映射端口的实测稳定率。它必须参与计算：信令交换给对端的是
    「那一刻的映射端口」，如果 NAT 有三分之一的概率把这个端口换掉，
    成功率就不可能还是 95%。乘上去是最粗但最诚实的近似。
    返回 (策略, 成功率估计, 说明)。
    """
    a_cone = mine in ("open", "restricted", "port-restricted", "cone-filter-unknown")
    b_cone = theirs in ("open", "restricted", "port-restricted", "cone-filter-unknown")
    stab = max(0.1, min(1.0, float(my_stability or 1.0)))
    if mine == "open" or theirs == "open":
        return ("direct", 0.99, "至少一端无 NAT 过滤，可直连（若一端是公网地址甚至无需打洞）")
    if a_cone and b_cone:
        c = 0.95 * stab
        why = "两端映射都稳定，同时发包即可打通"
        if stab < 1.0:
            why += f"（本侧映射稳定率 {stab:.0%}，端口抖动会拉低成功率）"
        return ("udp-punch", round(c, 2), why)
    if a_cone or b_cone:
        return ("birthday", round(0.55 * stab, 2),
                "一端端口在变，需要对稳定侧做端口扫描（生日攻击），失败则降级中继")
    return ("relay", 0.15, "两端端口都在变，打洞基本不可行，应直接走中继")


# ============================================================ 端口预测

def predict_ports(base_port, count=12):
    """对称 NAT 的端口增量预测。

    实测本机：42737 → 42739（+2）。这类「端口按固定步长分配」的 NAT
    就是 EasyTier 所称的 EasySymmetric，只要猜中步长就能打。
    猜不出来时退化成相邻区间试探——命中率取决于 NAT 的实现，
    所以这只是降级手段，不是主路径。
    """
    if base_port is None:
        return list(range(1024, 1024 + count))
    out = []
    for step in (2, 1, 3, 4):           # 常见步长，按经验排序
        for i in range(1, count // 4 + 1):
            p = base_port + step * i
            if 1024 <= p <= 65535 and p not in out:
                out.append(p)
        if len(out) >= count:
            break
    return out[:count]


# ============================================================ 打洞

#: 打洞包的前缀。用一个天网自己的魔数而不是随便的字节：
#: 打洞期间会收到大量来自陌生地址的扫描包，没有标记就无法区分「对端回应」
#: 和「别人在扫我的端口」，只能靠超时判断失败——那是把成功判成失败。
PUNCH_TAG = b"FLEET-PUNCH:"


def _punch_payload(nonce, kind=b"H"):
    return PUNCH_TAG + nonce + kind


class Puncher:
    """一条打洞通道。

    用法（双方各自）：
        p = Puncher(local_port=0)
        p.prepare()                      # 开 socket、拿本地与公网映射
        ... 通过信令交换 p.candidates() ...
        p.punch(peer_candidates)         # 对每个候选地址同时猛发
        p.wait_peer(timeout=10)          # 等对端的包进来
        p.sock.sendto(b"data", p.peer)   # 之后就是普通 UDP

    为什么「发」和「等」要分两个循环、而不是发一发等一等：
    本机是 APDF（端口限制锥），过滤表的放行条件是「我先向这个目的地址发过包」。
    如果对方也在等我先开口，双方就会互相干等到超时。真正解法是**双方都主动发**，
    这要求两边各自的发送节奏足够密，覆盖住对方开始发的那一刻。
    """

    def __init__(self, local_port=0, nonce=None):
        self.local_port = local_port
        # nonce 是**会话 token**，必须双方一致，由信令在打洞前交换。
        # 早期版本让两端各自随机生成，结果收到对端的包时拿自己的 nonce 去比，
        # 永远不匹配 —— 表现为「明明打通了却报超时」。这是自测抓出来的。
        self.nonce = nonce or os.urandom(8)
        self.sock = None
        self.peer = None
        self.public = None            # (ip, port) 经 STUN 得到的映射
        self.local_ips = []
        self._stop = threading.Event()
        self.events = []

    # ---- 准备
    def prepare(self, servers=DEFAULT_SERVERS, timeout=2.0):
        self.sock = _new_sock(self.local_port)
        if self.sock is None:
            return False
        self.local_port = self.sock.getsockname()[1]
        self.local_ips = local_ipv4()
        for host, port in servers:
            d = stun_resolve(host, port)
            if not d:
                continue
            r = stun_once(self.sock, d, timeout=timeout)
            if r and r.get("mapped"):
                self.public = r["mapped"]
                self.public_via = f"{host}:{port}"
                self.public_rtt = r.get("rtt_ms")
                break
        return True

    def candidates(self):
        """本端候选地址表。公网映射排最前——它是对端最可能打通的入口。"""
        out = []
        if self.public:
            out.append({"ip": self.public[0], "port": self.public[1],
                        "kind": "srflx", "note": "STUN 反射地址"})
        for ip in self.local_ips:
            out.append({"ip": ip, "port": self.local_port,
                        "kind": "host", "note": "本地网卡"})
        return out

    # ---- 打洞
    def punch(self, peer_candidates, burst=20, interval=0.5, seconds=6.0,
              extra_ports=()):
        """向对端所有候选地址猛发打洞包，同时收对方的。

        burst=20 / interval=0.5 是实测出来的量级：单个包在 CGNAT 上丢包常见，
        一次只发 1 个然后等 1 秒，等于把成功率交给运气。20 个一批既能压住丢包，
        又不至于把防火墙的「端口扫描」告警打出来。
        """
        if self.sock is None:
            return {"ok": False, "err": "还没 prepare"}
        targets = []
        for c in peer_candidates:
            ip, port = c.get("ip"), int(c.get("port") or 0)
            if not ip or not port:
                continue
            targets.append((ip, port))
        for ip, port in extra_ports:
            if (ip, port) not in targets:
                targets.append((ip, port))
        if not targets:
            return {"ok": False, "err": "没有可打的候选地址"}

        payload = _punch_payload(self.nonce)
        sent = 0
        got = None
        deadline = time.time() + max(1.0, seconds)
        self.sock.settimeout(0.02)
        while time.time() < deadline and got is None:
            for t in targets:
                for _ in range(max(1, burst)):
                    try:
                        self.sock.sendto(payload, t)
                        sent += 1
                    except OSError:
                        pass
            # 发完这一轮后立刻收一小会儿
            slot = time.time() + interval
            while time.time() < slot and got is None:
                try:
                    data, addr = self.sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                if data.startswith(PUNCH_TAG) and data[len(PUNCH_TAG):len(PUNCH_TAG) + 8] == self.nonce:
                    got = addr[:2]
        if got:
            self.peer = got
            # 收到即回一个：确认给对方，也让对方的过滤表把我们也放行
            for _ in range(3):
                try:
                    self.sock.sendto(_punch_payload(self.nonce, b"A"), got)
                except OSError:
                    pass
            self._drain()
            self.sock.settimeout(5.0)
            return {"ok": True, "peer": got, "sent": sent,
                    "targets": len(targets)}
        return {"ok": False, "err": "打洞超时，对端没回", "sent": sent,
                "targets": len(targets)}

    def _drain(self):
        """把缓冲区里已到的打洞包读掉，交给业务前腾干净。"""
        if self.sock is None:
            return 0
        n = 0
        self.sock.settimeout(0.05)
        for _ in range(500):
            try:
                data, _addr = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                break
            if data.startswith(PUNCH_TAG):
                n += 1
        return n

    # ---- 之后的使用
    def send(self, data):
        if not self.peer:
            return False
        try:
            self.sock.sendto(data, self.peer)
            return True
        except OSError:
            return False

    def recv(self, timeout=5.0):
        """收业务数据。会跳过打洞残留包。

        为什么必须跳过：打洞是「猛发一批」，那一批里有相当一部分还在路上。
        打通后立刻当业务通道用，前几个 recvfrom 拿到的全是 PUNCH 包——
        上层如果直接把它当数据解析，就会在「刚连通时恰好出错」，然后就恢复了，
        这种偶发 bug 极难复现。过滤放在这里，一次解决。
        """
        if self.sock is None:
            return None
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            self.sock.settimeout(remain)
            try:
                data, addr = self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                return None
            if data.startswith(PUNCH_TAG):
                continue          # 打洞/保活的控制包，不是数据
            return data, addr[:2]

    def keepalive(self, times=1, interval=KEEPALIVE_INTERVAL):
        """保活。NAT 绑定会老化，不发热它就把洞填上了。

        默认 15 秒一次，因为运营商 CGNAT 实测常在 30 秒左右过期；
        按 RFC 4787 的「至少 2 分钟」来设会大面积掉线。
        """
        if not self.peer:
            return 0
        ok = 0
        for _ in range(max(1, times)):
            try:
                self.sock.sendto(_punch_payload(self.nonce, b"K"), self.peer)
                ok += 1
            except OSError:
                break
            if times > 1:
                time.sleep(interval)
        return ok

    def close(self):
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def local_ipv4():
    """本机所有非回环 IPv4。

    不用「UDP connect 一个外部地址再读 getsockname」那套技巧——它在断网或
    只有单一路由时会撒谎（返回一个其实连不通的地址）。这里走网卡枚举。
    """
    out = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for item in infos:
        ip = item[4][0]
        if ip and not ip.startswith("127.") and ip not in out:
            out.append(ip)
    # 兜底：gethostname 在部分 Windows 配置下只返回一条，补上所有网卡
    if not out:
        try:
            import subprocess
            r = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            import re
            for m in re.finditer(r"IPv4[^:]*:\s*((?:\d{1,3}\.){3}\d{1,3})", r.stdout):
                ip = m.group(1)
                if not ip.startswith("127.") and ip not in out:
                    out.append(ip)
        except (OSError, subprocess.SubprocessError):
            pass
    return out


# ============================================================ 体检

def diagnosis(servers=DEFAULT_SERVERS, local_port=0, timeout=2.0):
    """一次完整体检：过滤 → 映射 → 分类 → 对各类对端的成功率预测。

    顺序固定：**先过滤，后映射**。理由见模块文档——映射测试会污染过滤测试。
    """
    t0 = time.time()
    # 过滤测试要用支持 CHANGE-REQUEST 的服务器；miwifi 是唯一实测支持的
    filt = None
    filt_err = ""
    for host, port in servers:
        d = stun_resolve(host, port)
        if not d:
            continue
        r = probe_filtering(d, timeout=timeout)
        if r.get("ok"):
            filt = r
            filt["server"] = f"{host}:{port}"
            break
        filt_err = r.get("err") or ""
    # 映射测试要多个服务器交叉比
    mapping = probe_mapping(servers, local_port=local_port, timeout=timeout)

    m = mapping.get("mapping") if mapping.get("ok") else None
    f = filt.get("filtering") if filt and filt.get("ok") else None
    kind = classify(m, f)
    stability = mapping.get("stability", 1.0) if mapping.get("ok") else 1.0

    matrix = {}
    for peer_kind in ("open", "restricted", "port-restricted", "symmetric"):
        strat, chance, why = punch_chance(kind, peer_kind, stability)
        matrix[peer_kind] = {"strategy": strat, "chance": chance, "why": why}

    return {
        "ok": bool(m or f),
        "kind": kind, "kind_cn": CLASS_CN.get(kind, "未知"),
        "mapping": m, "mapping_cn": MAPPING_CN.get(m, "未知"),
        "filtering": f, "filtering_cn": FILTER_CN.get(f, "未知"),
        "stability": stability,
        "confident": bool(filt and filt.get("confident")),
        # public 取映射测试的结果：那是多个服务器交叉验证过的一致映射，
        # 比过滤测试那一个 socket 的单次结果更可信（两者端口必然不同，
        # 因为是两个不同的本地 socket；写清楚来源，免得看的人以为数据打架）
        "public": (mapping.get("seen") or [{}])[0].get("mapped"),
        "public_source": "mapping-test",
        "public_filtering_socket": (filt or {}).get("mapped"),
        "mapping_detail": mapping, "filtering_detail": filt,
        "filtering_err": filt_err,
        "matrix": matrix,
        "local_ips": local_ipv4(),
        "ms": round((time.time() - t0) * 1000, 1),
        "notes": _notes(kind, m, f, mapping),
    }


def _notes(kind, mapping, filtering, mapping_detail):
    """把这些数字翻译成「那我该怎么办」。空话不说，只说会影响动作的。"""
    out = []
    if mapping == MAP_EIM:
        out.append("映射端口稳定：发往多个不同服务器都落在同一外部端口，"
                   "对端能可靠地定位到你")
    elif mapping == MAP_APDM:
        delta = mapping_detail.get("deltas") or []
        out.append(f"映射端口随目的地变化，增量 {delta}；"
                   f"{'有规律，可用端口预测' if mapping_detail.get('predictable') else '无规律，只能靠生日攻击'}")
    if mapping_detail.get("unstable"):
        st = mapping_detail.get("stability")
        out.append(f"⚠ 映射有抖动：{st:.0%} 的采样轮次内部一致。"
                   f"CGNAT 负载均衡或丢包重建映射都会这样，打洞会偶发失败，"
                   f"失败后重试一次往往就好了")
    if filtering == FIL_APDF:
        out.append("过滤严格：必须双方**同时**发包才能打通，单方面猛发无效")
    if filtering == FIL_EIF:
        out.append("过滤宽松：对端不需要你先发包也能直接进，打洞最省事")
    if kind in ("symmetric", "symmetric-addr"):
        out.append("对称 NAT：打洞成功率显著下降，应优先走中继，打洞只当加速尝试")
    if kind == "open":
        out.append("无 NAT 或全锥：对方可直接连你，不需要打洞")
    if not out:
        out.append("未取得足够测量，先检查 STUN 服务器是否可达")
    return out


# ============================================================ 自测

def _selftest():
    """离线自测：不依赖任何外部 STUN 服务器。

    验的是报文编解码与判定逻辑本身——这两块出错会静默给出错误结论
    （比如把对称 NAT 判成锥形，然后所有打洞都失败却不知道为什么），
    所以必须能在没有网络的机器上重复验证。
    """
    ok = fail = 0

    def chk(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {msg}")

    # --- 报文构造
    pkt, txid = build_binding()
    chk(len(pkt) == 20, "无属性请求应为 20 字节")
    chk(struct.unpack("!H", pkt[:2])[0] == BINDING_REQUEST, "类型应为 Binding Request")
    chk(pkt[4:8] == MAGIC_B, "魔数位置错误")
    chk(pkt[8:20] == txid, "txid 未写入")

    pkt2, _ = build_binding(change_ip=True, change_port=True)
    # 20(头) + 4(属性头) + 4(标志) = 28；长度字段只算属性部分 = 8
    chk(len(pkt2) == 28, f"带 CHANGE-REQUEST 应为 28 字节，实际 {len(pkt2)}")
    chk(struct.unpack("!H", pkt2[2:4])[0] == 8, "长度字段应为 8（只含属性）")
    chk(struct.unpack("!I", pkt2[24:28])[0] == 0x06, "change ip+port 标志应为 0x06")
    pkt3, _ = build_binding(change_port=True)
    chk(struct.unpack("!I", pkt3[24:28])[0] == 0x02, "change port 标志应为 0x02")

    # --- 报文解析（手工造一个成功响应）
    tx = os.urandom(12)
    ip_b = socket.inet_aton("203.0.113.9")
    port = 54321
    xor_ip = bytes(a ^ b for a, b in zip(ip_b, MAGIC_B))
    xor_port = struct.pack("!H", port ^ (MAGIC >> 16))
    v = struct.pack("!BBH", 0, 1, 0) + b""      # 占位
    xor_val = b"\x00\x01" + xor_port + xor_ip
    other_val = b"\x00\x01" + struct.pack("!H", 3479) + socket.inet_aton("198.51.100.7")
    body = _attr(A_XOR_MAPPED, xor_val) + _attr(A_OTHER_ADDR, other_val)
    resp = struct.pack("!HHI", BINDING_SUCCESS, len(body), MAGIC) + tx + body
    r = parse_binding(resp, tx)
    chk(r is not None, "合法响应应能解析")
    chk(r["mapped"] == ("203.0.113.9", port), f"XOR-MAPPED 解错: {r and r.get('mapped')}")
    chk(r["other"] == ("198.51.100.7", 3479), f"OTHER-ADDRESS 解错: {r and r.get('other')}")

    # --- txid 不匹配必须拒绝（否则串包导致结论随机）
    chk(parse_binding(resp, os.urandom(12)) is None, "txid 不匹配应拒绝")

    # --- 拒绝垃圾
    chk(parse_binding(b"\x00" * 10) is None, "短包应拒绝")
    chk(parse_binding(b"\xde\xad" + b"\x00" * 18) is None, "非 STUN 包应拒绝")

    # --- 分类
    chk(classify(MAP_EIM, FIL_EIF) == "open", "EIM+EIF 应为 open")
    chk(classify(MAP_EIM, FIL_APDF) == "port-restricted", "EIM+APDF 应为端口限制锥")
    chk(classify(MAP_APDM, FIL_APDF) == "symmetric", "APDM+APDF 应为对称")

    # --- 打洞概率矩阵的单调性：锥形对锥形必须比对称对对称高
    c1 = punch_chance("port-restricted", "port-restricted")[1]
    c2 = punch_chance("symmetric", "symmetric")[1]
    chk(c1 > c2, "锥形对锥形成功率应高于对称对对称")
    chk(punch_chance("open", "symmetric")[0] == "direct", "一端无 NAT 应可直接连")

    # --- 端口预测
    ps = predict_ports(42737, 8)
    chk(42739 in ps, "对称端口预测应包含 +2")
    chk(len(ps) == len(set(ps)), "预测端口不能重复")
    chk(all(1024 <= p <= 65535 for p in ps), "端口必须在合法区间")

    # --- 打洞包标记可识别
    n = os.urandom(8)
    pl = _punch_payload(n)
    chk(pl.startswith(PUNCH_TAG), "打洞包应有标记")
    chk(pl[len(PUNCH_TAG):len(PUNCH_TAG) + 8] == n, "nonce 位置错误")
    chk(not _punch_payload(os.urandom(8)).endswith(n), "nonce 应随实例变化")

    # --- Puncher 在回环上能真打通（本机到本机，不依赖 NAT）
    # 必须**双向同时打**：单侧发包、另一侧只在听，本机这种 APDF 过滤下
    # 对方根本不会回，测出来的「失败」是测试方式错，不是代码错。
    tok = os.urandom(8)
    p = Puncher(nonce=tok)
    q = Puncher(nonce=tok)
    if p.prepare(servers=()) and q.prepare(servers=()):
        results = {}

        def _run(who, me, peer):
            results[who] = me.punch([{"ip": "127.0.0.1", "port": peer.local_port}],
                                    burst=5, interval=0.2, seconds=5.0)

        t1 = threading.Thread(target=_run, args=("p", p, q), daemon=True)
        t2 = threading.Thread(target=_run, args=("q", q, p), daemon=True)
        t1.start()
        t2.start()
        t1.join(8)
        t2.join(8)
        chk(results.get("p", {}).get("ok"), f"P 侧打洞失败: {results.get('p', {}).get('err')}")
        chk(results.get("q", {}).get("ok"), f"Q 侧打洞失败: {results.get('q', {}).get('err')}")
        # 打通后应当能真的互发数据
        if results.get("p", {}).get("ok") and results.get("q", {}).get("ok"):
            p.send(b"hello-from-p")
            got = q.recv(timeout=3)
            chk(got is not None and got[0] == b"hello-from-p",
                f"打通后数据不通: {got}")
    else:
        chk(False, "Puncher.prepare 在回环上就失败了")
    q.close()
    p.close()

    print(f"\n自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _selftest()
    servers = list(DEFAULT_SERVERS)
    for i, a in enumerate(argv):
        if a == "--servers" and i + 1 < len(argv):
            servers = []
            for item in argv[i + 1].split(","):
                item = item.strip()
                if not item:
                    continue
                if ":" in item:
                    h, p = item.rsplit(":", 1)
                    servers.append((h, int(p)))
                else:
                    servers.append((item, 3478))
    d = diagnosis(servers=servers)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
