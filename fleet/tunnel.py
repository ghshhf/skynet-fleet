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
import json
import os
import socket
import subprocess
import threading
import time

from . import text as _fleet_text

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


# ---------------------------------------------------------- IPv6 诊断（四档）
#
# 上面 `ipv6_addresses()` 只回答「有没有地址」，这对隧道是不够的。
# 地址是运营商下发的，**转发和放通是另外两件事**：
#
#   有地址 + 出不去  = 默认路由缺失 / 运营商只给前缀不给转发
#   出得去 + 入站拦  = 主机防火墙或路由侧 v6 防火墙（默认全拦）
#
# 把这两种都归成「没有 IPv6」，后果是运维直接放弃这条路 —— 而这恰恰是
# 2026-11-01 主人指出的那个认知偏差：v6 是设备联网即分配的东西，
# 「没有」在国内几乎总是错的，真实情况只有「没开」「没放通」。
# 所以这里必须把「没有」拆开，每一档给一句能动手的建议。

#: 判「能不能出网」必须用真握手的 TCP 目标。不能用 UDP connect：
#: UDP connect 只做路由选路、不发包，通不通它根本不知道（最常见误判源）。
_V6_EGRESS = ("ipv6.baidu.com", 443)

_V6_CACHE = {"t": 0.0, "v": None}
_V6_TTL = 60.0


def ipv6_reach(timeout=4.0, target=_V6_EGRESS):
    """真发一次 TCP 握手，确认 IPv6 出网通不通。

    只取 AAAA 记录（AF_INET6），否则系统会悄悄回落 A 记录走 v4，
    测出来的是「v4 通」却被当成「v6 通」——这是最典型的假阳性。
    """
    host, port = target
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as e:
        return {"ok": False, "target": f"{host}:{port}", "ms": None,
                "err": f"解析不到 AAAA：{type(e).__name__}: {e}"}
    if not infos:
        return {"ok": False, "target": f"{host}:{port}", "ms": None,
                "err": "目标没有 AAAA 记录"}

    last = ""
    for fam, typ, proto, _, sa in infos[:3]:
        s = socket.socket(fam, typ, proto)
        s.settimeout(timeout)
        try:
            s.connect(sa)
            return {"ok": True, "target": f"[{sa[0]}]:{port}", "addr": sa[0],
                    "ms": round((time.time() - t0) * 1000), "err": ""}
        except OSError as e:
            last = f"{type(e).__name__}: {e}"
        finally:
            s.close()
    return {"ok": False, "target": f"{host}:{port}", "ms": None, "err": last}


def _win_firewall_state():
    """Windows 三个防火墙配置档是否开启。查不到返回 None。

    存在的理由：v6 没有 NAT，全网段可直接寻址 —— 所以路由器和主机防火墙
    对 v6 入站的默认策略是**全拦**，而不是像 v4 那样「NAT 顺手挡了」。
    这就是「v6 地址明明有、对端就是连不进来」的头号原因。
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "show", "allprofiles", "state"],
            capture_output=True, timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None

    # netsh 的输出编码跟着系统区域走：中文 Windows 是 GBK，英文是 cp1252。
    # 统一交给 text 层：严格 UTF-8 优先，解不了才按评分在 gb18030 / utf-16 之间选。
    txt = _fleet_text.decode(out.stdout or b"")
    return _parse_fw_profiles(txt)


def _parse_fw_profiles(txt):
    """从 `netsh advfirewall show allprofiles state` 输出解析三个配置档开关。

    抽成纯函数是为了能单测：中文 Windows 的「关闭」包含字符「开」，
    只判「开」子串会把「已关闭」误判成「开启」——这条曾经让防火墙状态整条判反，
    进而错误建议「先开 pinhole」。否定词优先于肯定词；都不命中保守判 off
    （off 不触发多余 pinhole 建议，on 才会，所以默认方向要稳）。
    """
    prof, cur = {}, None
    for ln in (txt or "").splitlines():
        s = ln.strip()
        low = s.lower()
        if not s:
            continue
        if "domain" in low or s.startswith("域"):
            cur = "domain"
        elif "private" in low or s.startswith("专用"):
            cur = "private"
        elif "public" in low or s.startswith("公用"):
            cur = "public"
        elif cur and (low.startswith("state") or s.startswith("状态")):
            neg = ("off" in low or "关闭" in s or "禁用" in s
                   or "停用" in s or "未启用" in s or "未开启" in s)
            pos = ("on" in low or "启用" in s or "开启" in s)
            prof[cur] = "off" if neg else ("on" if pos else "off")
    return prof or None


def ipv6_diagnose(deep=False, timeout=4.0):
    """把「有没有 IPv6」拆成四档，每档一句话说清下一步做什么。

    返回 `verdict` 四选一：
      no-address   一个 v6 地址都没有（网卡禁了 v6 / 系统关掉了）
      local-only   只有 fe80 或 ULA，没有全球单播（路由器没下发前缀）
      egress-fail  有全球地址，但出不了网（缺默认路由 / 运营商未放通）
      ready        出得去。**注意：出得去 ≠ 对端进得来**，入站是另一道墙
    """
    glob = ipv6_addresses()
    all6 = ipv6_addresses(only_global=False)

    if not all6:
        return {"verdict": "no-address", "addresses": [], "egress": None,
                "firewall": None, "usable": False,
                "advice": "本机一个 v6 地址都没有：先看网卡属性里 IPv6 有没有被勾掉，"
                          "再看路由器有没有开 v6。这不是「国内没有 v6」，是这台机器没开。"}

    if not glob:
        return {"verdict": "local-only", "addresses": all6, "egress": None,
                "firewall": None, "usable": False,
                "advice": "只有链路本地/ULA，没有全球单播地址：路由器没下发 PD 前缀，"
                          "或运营商侧没给这个宽带开 v6。要问的是路由器，不是本机。"}

    eg = ipv6_reach(timeout=timeout)
    if not eg.get("ok"):
        return {"verdict": "egress-fail", "addresses": glob, "egress": eg,
                "firewall": None, "usable": False,
                "advice": "有全球地址但出不去：多半是缺默认路由，或运营商只给了前缀"
                          "没给转发。查 `netsh interface ipv6 show route` 里有没有 ::/0。"}

    fw = _win_firewall_state() if deep else None
    blocked = bool(fw and any(v == "on" for v in fw.values()))
    return {
        "verdict": "ready", "addresses": glob, "egress": eg, "firewall": fw,
        "usable": True,
        "advice": ("出网通，可以拿它做直连候选。但**出得去不等于进得来**："
                   + ("本机防火墙开着，v6 入站默认全拦，对端连进来之前要先开 pinhole。"
                      if blocked else
                      "防火墙未拦（或未检出），对端仍可能被路由侧 v6 防火墙挡住。")
                   + "另外隐私地址（RFC 4941）会按小时轮换，长连接要用稳定地址。"),
    }


def _win_temporary_ipv6():
    """Windows 上被标为「临时」的 v6 地址集合（RFC 4941 隐私地址）。

    为什么必须知道哪些是临时的：临时地址按小时轮换，**当下可用，明天就没了**。
    拿它当长期候选，对端存下来第二天连过来必然超时，而且会看起来像「网络坏了」。
    稳定的那一个（RFC 7217 / EUI-64）才适合做长期入口。

    拿不到就返回空集合 —— 明确「不知道」，而不是假装全都是稳定的。
    """
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(["ipconfig", "/all"], capture_output=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return set()

    # 与 _win_firewall_state 统一走 text 层解码：严格 UTF-8 优先，解不了再退 gb18030/utf-16。
    # 之前这里硬编码 ("gbk","utf-8","cp1252") 且 gbk 优先，会把 utf-8 系统上的中文
    # 假成功解出乱码，与上面 firewall 解析那处是同一类「编码顺序写反」的坑。
    raw = out.stdout or b""
    txt = _fleet_text.decode(raw) if raw else ""

    tmp = set()
    for ln in txt.splitlines():
        low = ln.lower()
        if "temporary" not in low and "临时" not in ln:
            continue
        for tok in ln.split():
            # ipconfig 会在地址后面缀状态标记，中文是「(首选)」、英文是「(Preferred)」，
            # 不剥掉它，下面那条字符白名单会把整个 token 判成非地址。
            t = tok.split("%")[0].split("(")[0].strip()
            if t.count(":") >= 2 and all(c in "0123456789abcdefABCDEF:." for c in t):
                tmp.add(t)
    return tmp


def ipv6_candidates(timeout=4.0):
    """给信令用的 v6 候选，**稳定的排前面**，并标出哪些会轮换。

    这是 `ipv6_addresses()` 面向「当候选」这件事的版本：
    地址列表本身不够用，因为两个地址一个明天还在、一个明天就没了，
    而对端看不出区别。把 temporary 标出来，上层才能决定要不要长连。
    """
    addrs = ipv6_addresses()
    if not addrs:
        return []
    tmp = _win_temporary_ipv6()
    out = []
    for ip in addrs:
        # 拿不到临时地址清单时（非 Windows / 解析失败）一律标 unknown，
        # 不标 False —— 那等于替对端担保「这个地址明天还在」。
        flag = None if not tmp else (ip in tmp)
        out.append({"ip": ip, "kind": "host6",
                    "temporary": flag,
                    "note": ("临时地址，几小时后会变，不适合长期候选"
                             if flag else
                             "" if flag is None else
                             "稳定地址，可做长期入口")})
    out.sort(key=lambda c: (bool(c["temporary"]) if c["temporary"] is not None else True))
    return out


def ipv6_usable(timeout=4.0, force=False):
    """「现在能不能靠 IPv6 直连」—— 有全球地址且出得去。带 60 秒缓存。

    为什么缓存：`tun.plan` / `tun.offer` 会被界面反复调用，
    每次都拨一次 TCP 会把面板拖成幻灯片，而且平白多出一堆出站连接。
    """
    now = time.time()
    if not force and _V6_CACHE["v"] is not None and now - _V6_CACHE["t"] < _V6_TTL:
        return _V6_CACHE["v"]
    d = ipv6_diagnose(timeout=timeout)
    v = {"usable": bool(d["usable"]), "verdict": d["verdict"],
         "addresses": d["addresses"], "ts": round(now, 1)}
    _V6_CACHE.update({"t": now, "v": v})
    return v


# ------------------------------------------- IPv6 入站放行（pinhole）
#: 规则名前缀。查、清、审计全靠它 —— 改了前缀等于让已经建好的规则失去撤销入口。
PINHOLE_PREFIX = "skynet.pinhole."

#: 一次最多开几个端口。防火墙是**全局状态**，传进来一个长列表就等于把机器敞开；
#: 上限不设，「批量开端口」本身就会变成一个可以被远程触发的洞。
PINHOLE_MAX_PORTS = 8

#: 只对 IPv6 源生效的地址前缀集合。
#:
#: 为什么不是 `::/0`：本机是 Win10 19045，实测 `-RemoteAddress '::/0'` 和
#: `netsh ... remoteip=::/0` 都报「一个或多个地址前缀无效」——**/0 这个前缀
#: 被这个版本拒了**，而 `::/1`、`::/2`、`240e::/16` 都正常接受。
#: `::/1` + `8000::/1` 并起来恰好覆盖整个 v6 空间，语义与 `::/0` 等价，
#: 而且能作为数组塞进同一条规则的地址过滤器。这不是绕开校验，
#: 是把「一个写不进去的表达式」换成「两个写得进去的等效表达式」。
#:
#: 为什么一定要限源、而不是干脆不限：不限 remoteip 的入站放行会**在 v4 上
#: 同时敞开同一个端口**。用户以为开了一个 v6 的洞，实际开了两个，
#: 而且 v4 那头没有 NAT 兜着。限源是这条能力敢自动跑的前提。
PINHOLE_V6_ANY = ("::/1", "8000::/1")

#: 合法协议白名单。`protocol=any` 加 `localport` 会被 netsh 直接拒
#: （「仅当协议是 TCP 或 UDP 时可以指定端口」），所以只能逐协议建。
PINHOLE_PROTOS = ("TCP", "UDP")

#: 最近一次规则列表的缓存。面板会反复问「开了没」，每次都起一个 PowerShell
#: 冷启动（实测 0.5~1.5 秒）会把界面拖成幻灯片。
_PINHOLE_CACHE = {"t": 0.0, "v": None}
_PINHOLE_TTL = 5.0


def is_admin():
    """当前进程是不是提权状态。拿不到就返回 False（保守：不假装有权限）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _ps(script, timeout=20):
    """跑一段 PowerShell，返回 (rc, 文本)。

    统一强制 UTF-8 输出再按 UTF-8 解 —— 中文 Windows 的控制台默认 GBK，
    而脚本里可能把中文错误消息原样抛回来，用默认编码解会得到乱码，
    上层就没法判断到底是不是「权限不足」。显式设定比猜编码可靠。
    """
    if os.name != "nt":
        return 1, "not windows"
    full = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "$ProgressPreference='SilentlyContinue';" + script)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", full],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)

    raw = (out.stdout or b"")
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        txt = raw.decode("utf-8", "replace")
    err = (out.stderr or b"").decode("utf-8", "replace")
    return out.returncode, (txt + ("\n" + err if err.strip() else "")).strip()


def pinhole_rule_name(port, proto):
    """规则名 = 前缀 + 协议 + 端口。**端口必须进名字**。

    理由：撤销要能按端口点名。只用 `skynet.pinhole.1`、`.2` 这种序号，
    事后没人知道 `.2` 对应哪个端口，`tun.pinhole.close` 就变成了「全清」
    或者「不敢清」——两种都不是好结果。
    """
    return "%s%s.%d" % (PINHOLE_PREFIX, str(proto).upper(), int(port))


def pinhole_parse(name):
    """从规则名反解 (proto, port)。解析不出来返回 None，不猜。"""
    if not name.startswith(PINHOLE_PREFIX):
        return None
    rest = name[len(PINHOLE_PREFIX):]
    if "." not in rest:
        return None
    proto, _, port = rest.rpartition(".")
    if proto.upper() not in PINHOLE_PROTOS:
        return None
    try:
        return proto.upper(), int(port)
    except ValueError:
        return None


def pinhole_list(force=False):
    """列出本模块建过的入站规则。

    只认前缀，不列全表 —— 用户机器上可能有几百条别人的规则，
    把全表倒出来既拖时间又让人找不到重点。
    """
    now = time.time()
    if not force and _PINHOLE_CACHE["v"] is not None \
            and now - _PINHOLE_CACHE["t"] < _PINHOLE_TTL:
        return _PINHOLE_CACHE["v"]

    if os.name != "nt":
        v = {"ok": False, "platform": "non-windows", "rules": [], "count": 0,
             "err": "pinhole 只处理 Windows 防火墙；这台机器不是 Windows"}
        _PINHOLE_CACHE.update({"t": now, "v": v})
        return v

    script = (
        "$o=@();"
        "Get-NetFirewallRule -DisplayName '%s*' -ErrorAction SilentlyContinue"
        " | ForEach-Object {"
        " $pf=$_ | Get-NetFirewallPortFilter;"
        " $af=$_ | Get-NetFirewallAddressFilter;"
        " $o += @{name=$_.DisplayName; enabled=[string]$_.Enabled;"
        " action=[string]$_.Action; dir=[string]$_.Direction;"
        " proto=[string]$pf.Protocol; port=[string]$pf.LocalPort;"
        " remote=@($af.RemoteAddress)} };"
        "$o | ConvertTo-Json -Compress -Depth 5"
    ) % PINHOLE_PREFIX

    rc, txt = _ps(script)
    rules = []
    if txt and txt.strip().startswith(("[", "{")):
        try:
            data = json.loads(txt)
            rules = data if isinstance(data, list) else [data]
        except ValueError:
            rules = []
    v = {"ok": rc == 0, "platform": "windows", "count": len(rules),
         "rules": rules, "err": "" if rc == 0 else txt[:300]}
    _PINHOLE_CACHE.update({"t": now, "v": v})
    return v


def pinhole_open(ports, protos=("TCP", "UDP"), profile="any"):
    """按端口开 v6 入站放行。**幂等**：已存在的规则不重建、不报错。

    幂等不是礼貌，是必需：这个方法会被后台循环反复调用。
    不幂等的版本每轮都在防火墙里追加一批同名规则，几十轮之后
    机器上会有几百条一模一样的放行 —— 而且没人看得出哪条是哪条。

    返回逐条结果，**不把「部分成功」折叠成一个 ok**：
    一半成功时报 ok=True 会让上层以为入站已经通了，
    然后去等一个永远连不进来的连接。
    """
    if os.name != "nt":
        return {"ok": False, "err": "pinhole 只处理 Windows 防火墙",
                "need_admin": False, "results": []}

    clean, seen = [], set()
    for p in ports or []:
        try:
            n = int(p)
        except (TypeError, ValueError):
            continue
        if 0 < n < 65536 and n not in seen:
            seen.add(n)
            clean.append(n)
    if not clean:
        return {"ok": False, "err": "没有合法端口（1-65535）",
                "need_admin": False, "results": []}
    if len(clean) > PINHOLE_MAX_PORTS:
        return {"ok": False, "need_admin": False, "results": [],
                "err": "一次最多放行 %d 个端口（给了 %d 个）："
                       "防火墙是全局状态，批量开口不该被随手触发"
                       % (PINHOLE_MAX_PORTS, len(clean))}

    prot = [str(x).upper() for x in (protos or PINHOLE_PROTOS)
            if str(x).upper() in PINHOLE_PROTOS] or ["TCP"]

    # 没权限就别去撞墙：直接报 need_admin，让调用方给出「提权后再试」，
    # 而不是把 PowerShell 的「拒绝访问」翻译成「开失败」——
    # 后者会让人以为防火墙坏了。
    if not is_admin():
        return {"ok": False, "need_admin": True, "results": [],
                "err": "当前进程没有管理员权限：改防火墙必须先提权。"
                       "这不是失败，是还没开始。"}

    items = ";".join("@(@{p='%s';port=%d})" % (p, n) for p in prot for n in clean)
    addrs = ",".join("'%s'" % a for a in PINHOLE_V6_ANY)
    script = (
        "$want=@(%s);$o=@();"
        "foreach($w in $want){"
        " $n='%s'+$w.p+'.'+$w.port;"
        " $ex=Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue;"
        " if($ex){$o+=@{name=$n;port=$w.port;proto=$w.p;ok=$true;existed=$true};continue};"
        " try{"
        "  New-NetFirewallRule -DisplayName $n -Direction Inbound -Action Allow"
        " -Protocol $w.p -LocalPort $w.port -RemoteAddress @(%s) -Profile %s"
        " -Description 'skynet fleet pinhole (IPv6 inbound)'"
        " -ErrorAction Stop | Out-Null;"
        "  $o+=@{name=$n;port=$w.port;proto=$w.p;ok=$true;existed=$false}"
        " }catch{"
        "  $o+=@{name=$n;port=$w.port;proto=$w.p;ok=$false;existed=$false;err=$_.Exception.Message}"
        " } };"
        "$o | ConvertTo-Json -Compress -Depth 4"
    ) % (items, PINHOLE_PREFIX, addrs, profile)

    rc, txt = _ps(script, timeout=40)
    results = []
    if txt and txt.strip().startswith(("[", "{")):
        try:
            data = json.loads(txt)
            results = data if isinstance(data, list) else [data]
        except ValueError:
            results = []

    if not results:
        return {"ok": False, "need_admin": False, "results": [],
                "err": txt[:400] or "PowerShell 没有返回结果"}

    made = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    _PINHOLE_CACHE.update({"t": 0.0, "v": None})  # 让下一次 list 重新读
    return {
        "ok": bool(made) and not failed,
        "partial": bool(made) and bool(failed),
        "need_admin": False,
        "ports": clean, "protos": prot,
        "created": [r["name"] for r in made if not r.get("existed")],
        "existed": [r["name"] for r in made if r.get("existed")],
        "failed": [{"name": r.get("name"), "err": r.get("err")} for r in failed],
        "results": results,
        "err": "" if not failed else "有 %d 条规则没建成，见 failed" % len(failed),
    }


def pinhole_close(ports=None):
    """撤销放行。不给 ports 就是全清（只清本模块前缀的规则）。

    「只清前缀」是这条能力的边界：用户机器上其他程序（远程桌面、
    局域网共享、杀毒）的规则不在射程内。做全量清理的工具没人敢用第二回。
    """
    if os.name != "nt":
        return {"ok": False, "err": "pinhole 只处理 Windows 防火墙",
                "removed": []}
    if not is_admin():
        return {"ok": False, "need_admin": True, "removed": [],
                "err": "当前进程没有管理员权限：改防火墙必须先提权"}

    if ports:
        names = []
        for p in ports:
            try:
                n = int(p)
            except (TypeError, ValueError):
                continue
            if 0 < n < 65536:
                names += [pinhole_rule_name(n, proto) for proto in PINHOLE_PROTOS]
        if not names:
            return {"ok": False, "removed": [], "err": "没有合法端口"}
    else:
        script = ("Get-NetFirewallRule -DisplayName '%s*' -ErrorAction SilentlyContinue"
                  " | ForEach-Object { $_.DisplayName }") % PINHOLE_PREFIX
        rc, txt = _ps(script, timeout=30)
        names = [ln.strip() for ln in txt.splitlines()
                 if ln.strip().startswith(PINHOLE_PREFIX)]

    names = [n for n in names if n.startswith(PINHOLE_PREFIX)]
    if not names:
        _PINHOLE_CACHE.update({"t": 0.0, "v": None})
        return {"ok": True, "removed": [], "count": 0,
                "note": "本来就没有这类规则"}

    # 先问「这些名字里哪些真的存在」，再删。
    # 为什么不直接 `Remove-NetFirewallRule -DisplayName @(全部名字)`：
    # 点名要删的东西里，有的可能本来就不在（例如只开过 TCP，没开 UDP）。
    # 一次性删一批不存在的规则，PowerShell 在中文环境下会走到错误分支，
    # 于是「删成功了一条」被报成「撤销失败」—— 事实相反，最难查的那种错。
    quoted = ",".join("'%s'" % n.replace("'", "''") for n in names)
    _, txt0 = _ps(
        "Get-NetFirewallRule -DisplayName @(%s) -ErrorAction SilentlyContinue"
        " | ForEach-Object { $_.DisplayName }" % quoted, timeout=30)
    exist = [ln.strip() for ln in txt0.splitlines()
             if ln.strip().startswith(PINHOLE_PREFIX)]
    if not exist:
        _PINHOLE_CACHE.update({"t": 0.0, "v": None})
        return {"ok": True, "removed": [], "count": 0,
                "note": "这些端口本来就没有放行规则"}

    quoted = ",".join("'%s'" % n.replace("'", "''") for n in exist)
    rc2, txt2 = _ps(
        "Remove-NetFirewallRule -DisplayName @(%s) -Confirm:$false "
        "-ErrorAction SilentlyContinue" % quoted, timeout=30)
    _PINHOLE_CACHE.update({"t": 0.0, "v": None})
    left = pinhole_list(force=True)
    alive = {r.get("name") for r in left.get("rules") or []}

    # 判据是「**这一批**里还有没有活的」，不是「全局还剩几条」。
    # 用全局计数当判据时，机器上只要还留着别的端口，删对了也会报失败。
    still = [n for n in exist if n in alive]
    return {"ok": not still, "removed": [n for n in exist if n not in alive],
            "count": len(exist), "still": still, "left": left["count"],
            "err": "" if not still else "有 %d 条规则没删掉" % len(still)}


def pinhole_status(deep=False):
    """入站放行现在什么状态，以及**要不要开**。

    这一条要回答的其实不是「开了没」，是「不开会怎样、开了会多出什么」：
      v6 不可用      → 开了也没用，先修出网
      防火墙全关      → 不必开（开了也是空转）
      防火墙开 + 无规则 → 这是「地址明明有、对端就是连不进来」的头号原因
      有规则          → 报清楚哪几个端口、哪个协议
    """
    fw = _win_firewall_state() if deep else None
    rules = pinhole_list(force=deep)
    v6 = ipv6_usable()
    ports = sorted({p for p in (pinhole_parse(r.get("name"))
                                for r in rules.get("rules") or []) if p})
    blocked = bool(fw and any(v == "on" for v in fw.values()))

    if not v6.get("usable"):
        advice = "IPv6 当前不可用（%s），开 pinhole 没有意义：先让地址出得去" % v6.get("verdict")
        need = False
    elif fw is not None and not blocked:
        advice = "本机防火墙没开，入站不经过主机过滤，不需要 pinhole"
        need = False
    elif rules.get("count"):
        advice = "已放行 %d 个端口的 v6 入站（%s）；对端仍连不进来就查路由侧 v6 防火墙" % (
            len(ports), ",".join(str(x[1]) for x in ports) or "?")
        need = False
    else:
        advice = ("IPv6 出得去，但本机防火墙对 v6 入站默认全拦 —— "
                  "这正是「地址明明有、对端就是连不进来」的头号原因。"
                  "开 pinhole 只放指定端口，且只对 v6 源生效，v4 侧不受影响")
        need = True

    return {"v6_usable": bool(v6.get("usable")), "verdict": v6.get("verdict"),
            "firewall": fw, "firewall_blocking": blocked,
            "admin": is_admin(), "need": need,
            "count": rules.get("count", 0),
            "ports": [{"proto": p, "port": n} for p, n in ports],
            "rules": rules.get("rules") or [],
            "v6_any": list(PINHOLE_V6_ANY),
            "advice": advice}


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
            # 中继端点的约定是「返回发出字节数，发不出去返回 0」。
            # 不检查这个 0，就会把「已经断了」报成 ok=True —— 上层拿到一个
            # 假成功，等到需要重试时才发现根本没发出去。
            if isinstance(r, int) and r == 0:
                self.path = None
                return {"ok": False, "err": "中继发送失败：连接已断开"}
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

    print("【防火墙状态解析：中文「关闭」含「开」字不能误判为开启】")
    chk(_parse_fw_profiles(
            "Domain Profile Settings:\nState                           ON\n"
            "Private Profile Settings:\nState                           OFF\n"
            "Public Profile Settings:\nState                           ON")
        == {"domain": "on", "private": "off", "public": "on"},
        "英文 ON/OFF 三个档解析正确")
    chk(_parse_fw_profiles(
            "域配置文件 设置:\n状态                            启用\n"
            "专用配置文件 设置:\n状态                            关闭\n"
            "公用配置文件 设置:\n状态                            启用")
        == {"domain": "on", "private": "off", "public": "on"},
        "中文 启用/关闭 解析正确（「关闭」含「开」不误判为开启）")
    chk(_parse_fw_profiles("") is None, "空输出返回 None")

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
