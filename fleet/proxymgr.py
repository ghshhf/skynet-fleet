"""天网 · 代理管理面（参考 Karing / sing-box 架构）

把 Karing「一套规则跨多订阅源、节点归一、延迟选优」的代理管理能力搬进天网。
注意：这是**管理面**（解析订阅、归一节点、加载规则、测速选优、生成分流配置），
不绑定某个核心二进制——天网现有 gitport 是克制型（只接管救得动的站），
本模块产出的节点/规则数据可喂给任意兼容核心（gitport 或 sing-box）。

数据格式直接照抄 sing-box：
- 节点：sing-box outbound 风格（type/server/server_port/uuid/password/sni/network…）
- 规则集：sing-box rule-set source JSON（{version,rules:[{domain_suffix,ip_cidr,…}]}）
- 规则应用：[{rule_set, outbound:"direct|proxy|block", name}]（抄 recommend/cn.json）

零强依赖：yaml 可用就用（解析 Clash 订阅），不可用退化为自写 mini 解析。
GBK 防线（2026-11-01 铁律）：输出可能含 ✓ ✗ → 等外字符，入口重配 UTF-8 兜底。
"""
import base64
import ipaddress
import json
import os
import re
import socket
import sys
import time

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# 已知协议 → sing-box outbound type 的归一映射
_PROTO_MAP = {
    "vmess": "vmess", "vless": "vless", "trojan": "trojan",
    "ss": "shadowsocks", "shadowsocks": "shadowsocks",
    "shadowsocksr": "shadowsocks", "ssr": "shadowsocks",
    "socks": "socks", "socks5": "socks", "http": "http",
    "https": "http", "tuic": "tuic", "hysteria": "hysteria2",
    "hysteria2": "hysteria2", "wireguard": "wireguard",
    "snell": "snell", "anytls": "anytls",
}

# karing-ruleset 默认位置（克隆到 E:/xmanbian/karing-ruleset 后自动加载完整国内直连规则）
_KARING_RULESET_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "karing-ruleset")
)
if not os.path.isdir(_KARING_RULESET_DIR):
    _KARING_RULESET_DIR = os.environ.get("FLEET_KARING_RULESET") or os.path.expanduser(
        "~/.fleet/karing-ruleset"
    )


class ProxyNode:
    """归一后的代理节点（sing-box outbound 风格）。

    所有来源的订阅最终都归一成这个对象，保证「一套模型跨多订阅源」。
    extra 装各协议专属字段（uuid/password/sni/method/path/host/flow…）。
    """

    def __init__(self, name, proto, server, port, extra=None, source=None):
        self.name = name
        self.proto = proto            # 归一后的类型（见 _PROTO_MAP）
        self.server = server
        self.port = int(port)
        self.extra = extra or {}
        self.source = source          # 来自哪个订阅（多源归一时区分）
        self.latency_ms = None       # 测速结果（None=未测 / -1=不可达）
        self.ok = None               # 三态：None 未测 / True 可达 / False 不可达

    def to_outbound(self):
        """导出 sing-box outbound JSON（直接可进 sing-box 配置）。"""
        ob = {"tag": self.name, "type": self.proto,
              "server": self.server, "server_port": self.port}
        ob.update(self.extra)
        return ob

    def to_dict(self):
        d = {"name": self.name, "proto": self.proto, "server": self.server,
             "port": self.port, "source": self.source,
             "latency_ms": self.latency_ms, "ok": self.ok}
        d.update({f"x_{k}": v for k, v in self.extra.items()})
        return d

    @classmethod
    def from_dict(cls, d):
        """to_dict() 的逆操作（深解/跨进程回传的节点 dict 还原成对象）。"""
        extra = {k[2:]: v for k, v in d.items() if k.startswith("x_")}
        n = cls(d.get("name"), d.get("proto"), d.get("server"),
                d.get("port", 0), extra, d.get("source"))
        n.latency_ms = d.get("latency_ms")
        n.ok = d.get("ok")
        return n

    def __repr__(self):
        lat = "?" if self.latency_ms is None else f"{self.latency_ms:.0f}ms"
        return f"<{self.proto} {self.name} {self.server}:{self.port} {lat}>"


# ───────────────────────── 订阅解析 ─────────────────────────

def _b64decode(s):
    """容错 base64：严格校验字母表+长度，解码结果必须是合法 UTF-8 文本，否则返回空串。

    用于「可能是 base64 包裹，也可能就是明文」的场景（节点名 / vmess JSON / 订阅）。
    validate=True 拒绝含 `-`、空格等非 base64 字符的明文；strict UTF-8 拒绝解码出
    乱码的短字符串（如节点名 "test" 恰好是合法 base64 但解码是二进制）。
    """
    s = (s or "").strip()
    if not s:
        return ""
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except Exception:
        return ""
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return ""


def _try_yaml():
    try:
        import yaml  # 环境已有 6.0.3
        return yaml
    except ModuleNotFoundError:
        return None


def parse_clash_yaml(text, source="clash"):
    """解析 Clash / Clash.Meta YAML 订阅，抽出 proxies 列表。

    有 yaml 走标准库；没有则退化到 mini 解析（只认 proxies 下的扁平字段）。
    返回 ProxyNode 列表。
    """
    nodes = []
    y = _try_yaml()
    data = None
    if y:
        try:
            data = y.safe_load(text)
        except Exception:
            data = None
    if isinstance(data, dict) and isinstance(data.get("proxies"), list):
        for p in data["proxies"]:
            if not isinstance(p, dict):
                continue
            nodes.append(_clash_item_to_node(p, source))
    else:
        nodes = _mini_clash(text, source)
    return nodes


def _clash_item_to_node(p, source):
    proto = _PROTO_MAP.get((p.get("type") or "").lower())
    name = p.get("name", "")
    server = p.get("server", "")
    port = p.get("port", 0)
    extra = {}
    # 各协议专属字段归一
    for k in ("uuid", "password", "cipher", "method", "sni", "servername",
              "network", "path", "host", "flow", "tls", "skip-cert-verify",
              "alterId", "encryption", "plugin", "plugin-opts"):
        if k in p:
            extra[k] = p[k]
    # Clash 的 ws/grpc 传输常嵌在 ws-opts/grpc-opts
    for opt_key, dst in (("ws-opts", "ws"), ("grpc-opts", "grpc")):
        if opt_key in p and isinstance(p[opt_key], dict):
            extra[dst] = p[opt_key]
    if proto is None:
        proto = (p.get("type") or "unknown").lower()
    return ProxyNode(name, proto, server, port, extra, source)


def _mini_clash(text, source):
    """无 yaml 时的退化解析：只认 proxies: 下 `- key: value` 扁平结构。"""
    nodes = []
    lines = text.splitlines()
    cur = None
    in_proxies = False
    for ln in lines:
        s = ln.rstrip()
        if re.match(r"^proxies:\s*$", s):
            in_proxies = True
            continue
        if in_proxies and re.match(r"^\S", s):  # 退出了 proxies 块
            in_proxies = False
        if not in_proxies:
            continue
        m = re.match(r"^\s{2,}-\s*(\w+):\s*(.*)$", s)  # 列表项首项
        if m:
            if cur:
                nodes.append(_mini_node(cur, source))
            cur = {m.group(1): _strip(m.group(2))}
            continue
        m2 = re.match(r"^\s{4,}(\w[\w-]*):\s*(.*)$", s)
        if m2 and cur is not None:
            cur[m2.group(1)] = _strip(m2.group(2))
    if cur:
        nodes.append(_mini_node(cur, source))
    return nodes


def _strip(v):
    return v.strip().strip('"').strip("'")


def _mini_node(d, source):
    proto = _PROTO_MAP.get((d.get("type") or "").lower(),
                           (d.get("type") or "unknown").lower())
    return ProxyNode(d.get("name", ""), proto, d.get("server", ""),
                     int(d.get("port", 0) or 0), d, source)


def _parse_vless_trojan(uri, proto):
    """vless:// / trojan:// / socks:// / http:// 这类 `scheme://user@host:port?query#name`"""
    m = re.match(r"^(?P<scheme>\w+)://(?P<auth>[^@]*)@(?P<host>[^:/?#]+):(?P<port>\d+)", uri)
    if not m:
        return None
    auth = m.group("auth")
    extra = {}
    query = ""
    if "?" in uri:
        query = uri.split("?", 1)[1].split("#")[0]
    for kv in query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            extra[k] = v
    if proto == "vless" and auth:
        extra.setdefault("uuid", auth)
    elif proto == "trojan" and auth:
        extra.setdefault("password", auth)
    elif proto in ("socks", "http") and auth and ":" in auth:
        u, p = auth.split(":", 1)
        extra["username"], extra["password"] = u, p
    name = uri.split("#", 1)[1] if "#" in uri else f"{proto}-{m.group('host')}"
    return ProxyNode(_b64decode(name) or name, proto, m.group("host"),
                     int(m.group("port")), extra, "v2ray-link")


def _parse_hysteria2(uri):
    """hysteria2:// / hy2:// 链接：`hysteria2://password@server:port?sni=...&insecure=1&obfs=...#name`

    hy2:// 是 hysteria2 的旧式短名（yaml 与链接里都常见），一并处理。
    常用参数：sni / insecure / obfs(salamander) / obfs-password / upmbps / downmbps /
    ports(多端口) / alpn / pinSHA256 / ip_version。
    """
    m = re.match(r"^(?P<scheme>hysteria2|hy2)://(?P<password>[^@/?#]+)@(?P<host>[^:/?#]+):(?P<port>\d+)", uri)
    if not m:
        return None
    extra = {"password": m.group("password")}
    query = uri.split("?", 1)[1].split("#")[0] if "?" in uri else ""
    for kv in query.split("&"):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        extra[k] = v
    # 归一：sni→servername、insecure→skip-cert-verify、obfs-password→obfs 密码
    if extra.get("sni"):
        extra.setdefault("servername", extra["sni"])
    if extra.get("insecure") in ("1", "true", "True"):
        extra["skip-cert-verify"] = True
    name = uri.split("#", 1)[1] if "#" in uri else f"hy2-{m.group('host')}"
    return ProxyNode(_b64decode(name) or name, "hysteria2", m.group("host"),
                     int(m.group("port")), extra, "v2ray-link")


def parse_v2ray_links(text, source="v2ray"):
    """逐行解析 vmess:// vless:// trojan:// ss:// hysteria2:// socks:// http:// 链接。"""
    nodes = []
    for raw in text.splitlines():
        u = raw.strip()
        if not u or "://" not in u:
            continue
        scheme = u.split("://", 1)[0].lower()
        if scheme == "vmess":
            n = _parse_vmess(u)
        elif scheme in ("vless", "trojan", "socks", "socks5", "http", "https"):
            n = _parse_vless_trojan(u, _PROTO_MAP.get(scheme, scheme))
        elif scheme in ("ss", "shadowsocks"):
            n = _parse_ss(u)
        elif scheme in ("hysteria2", "hy2"):
            n = _parse_hysteria2(u)
        else:
            continue
        if n:
            n.source = source
            nodes.append(n)
    return nodes


def _parse_vmess(uri):
    """vmess:// 是 base64 的 JSON：`{"v":"2","ps":"名","add":"host","port":123,"id":"uuid",...}`

    额外提取 ws/tls 关键字段：net(传输)/tls/sni/host/path/alpn，否则 ws+tls 节点会缺字段。
    """
    b = uri.split("://", 1)[1].split("#")[0]
    j = _b64decode(b)
    try:
        d = json.loads(j)
    except Exception:
        return None
    name = d.get("ps") or d.get("remark") or "vmess"
    extra = {"uuid": d.get("id"), "alterId": d.get("aid", 0),
             "security": d.get("scy", "auto"), "network": d.get("net", "tcp")}
    if d.get("tls") in ("tls", "true", "1", True):
        extra["tls"] = True
    if d.get("sni"):
        extra["servername"] = d["sni"]
    if d.get("host"):
        extra["host"] = d["host"]
    if d.get("path"):
        extra["path"] = d["path"]
    if d.get("alpn"):
        extra["alpn"] = d["alpn"]
    return ProxyNode(name, "vmess", d.get("add", ""), int(d.get("port", 0) or 0),
                     extra, "v2ray-link")


def _parse_ss(uri):
    """ss:// 两种：① base64(method:password)@host:port#name ② ss://method:password@host:port#name"""
    body = uri.split("://", 1)[1].split("#")[0]
    name = uri.split("#", 1)[1] if "#" in uri else "ss"
    if "@" in body:
        left, host = body.rsplit("@", 1)
        cred = _b64decode(left) or left
        m = re.match(r"^(?P<host>[^:/?#]+):(?P<port>\d+)", host)
        if not m:
            return None
        method, _, password = cred.partition(":")
        extra = {"method": method, "password": password}
        return ProxyNode(_b64decode(name) or name, "shadowsocks",
                         m.group("host"), int(m.group("port")), extra, "v2ray-link")
    return None


def parse_singbox_json(obj_or_text, source="sing-box"):
    """解析 sing-box 配置里的 outbounds（type 属代理协议的那些）。"""
    if isinstance(obj_or_text, str):
        try:
            obj = json.loads(obj_or_text)
        except Exception:
            return []
    else:
        obj = obj_or_text
    nodes = []
    outs = obj.get("outbounds", []) if isinstance(obj, dict) else []
    proxy_types = set(_PROTO_MAP.values())
    for o in outs:
        if not isinstance(o, dict):
            continue
        t = o.get("type", "")
        if t not in proxy_types:
            continue
        nodes.append(ProxyNode(
            o.get("tag", o.get("server", "")), t,
            o.get("server", ""), o.get("server_port", 0),
            {k: v for k, v in o.items() if k not in ("tag", "type", "server", "server_port")},
            source))
    return nodes


def parse_subscription(raw, fmt=None, source="subscription"):
    """自动判断格式并解析：Clash YAML / sing-box JSON / V2ray 链接串 / 混合。

    返回 ProxyNode 列表。多格式混合时取并集（Karing 的「一套规则跨多源」前提）。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    # sing-box JSON
    if raw.lstrip().startswith("{"):
        try:
            return parse_singbox_json(raw, source)
        except Exception:
            pass
    # 含 vmess:// vless:// hysteria2:// 等链接 → 链接串
    if re.search(r"^(vmess|vless|trojan|ss|socks|http|hysteria2|hy2)s?://", raw, re.M):
        return parse_v2ray_links(raw, source)
    # 否则当 Clash YAML
    if fmt == "clash" or raw.lstrip().startswith("proxies:") or "proxies:" in raw[:4000]:
        return parse_clash_yaml(raw, source)
    return parse_clash_yaml(raw, source)


# ───────────────────────── 逆向解析兜底 ─────────────────────────

def _try_full_b64(raw):
    """尝试把整段当 base64 解码（很多订阅是 base64 包裹的自定义格式）。

    只接受「解码后像文本」的结果，避免把恰好能 base64 解的二进制误判。
    """
    s = (raw or "").strip()
    if not s or "\n" in s:
        return None
    try:
        d = base64.b64decode(s + "=" * (-len(s) % 4))
    except Exception:
        return None
    if d and all(32 <= b < 127 or b in (9, 10, 13) for b in d[:200]):
        return d
    return None


def _samples_from_text(text):
    """文本 → proto_reverse 的样本列表（每行一帧，限前 50 行，太长无意义）。"""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            out.append(ln.encode("utf-8", "replace"))
        if len(out) >= 50:
            break
    if not out and text:
        out.append(text.encode("utf-8", "replace"))
    return out


def deep_analyze(raw):
    """常规解析不成的兜底：先解 base64 包裹再回喂解析器，仍不成再逆向拆字节结构。

    免费订阅最常见的隐藏形式是「整段 base64 包裹的自定义链接串/JSON/YAML」——
    常规 parse_subscription 因首字符不是 {/vmess:// 而漏判。这里先解包重解析，
    救回节点（method="reversed" 但 nodes 非空）；实在救不回才交给 proto_reverse
    拆结构（method="reversed" 且 deep 有报告），拆不出则如实 method="failed"。

    返回 {method, nodes, deep?}：
      - method="standard"：常规解析已识别，nodes 非空（不走兜底）
      - method="reversed"：常规未识别，但 base64 解包后救回节点（nodes 非空），
                           或已用 proto_reverse 拆出结构（deep 非空）
      - method="failed"：都未给出结论（如实说明）
    """
    nodes = parse_subscription(raw)
    if nodes:
        return {"method": "standard", "nodes": [n.to_dict() for n in nodes], "deep": None}
    # 第一层兜底：base64 解包 → 再回喂解析器（救回节点）
    decoded = _try_full_b64(raw)
    if decoded:
        try:
            payload = decoded.decode("utf-8", "replace") if isinstance(decoded, (bytes, bytearray)) else decoded
            rescued = parse_subscription(payload)
            if rescued:
                return {"method": "reversed", "nodes": [n.to_dict() for n in rescued],
                        "b64_decoded": True,
                        "deep": None,
                        "note": "整段 base64 包裹，解包后救回节点"}
        except Exception:
            pass
        payload = decoded.decode("utf-8", "replace") if isinstance(decoded, (bytes, bytearray)) else raw
    else:
        payload = raw
    # 第二层兜底：proto_reverse 拆字节结构（不假装解析成节点，拆得出就报，拆不出如实说）
    samples = _samples_from_text(payload)
    if not samples:
        return {"method": "failed", "nodes": [], "err": "无可用样本"}
    try:
        from . import proto_reverse as PR
        report = PR.proto_report(samples, hunt_checksum=True)
        return {"method": "reversed", "nodes": [], "b64_decoded": decoded is not None,
                "deep": report,
                "note": "常规解析与 base64 解包都未识别，已用天网协议逆向拆解字节结构（见 deep 字段）"}
    except Exception as e:  # 逆向引擎自身异常也不能让调用方崩
        return {"method": "failed", "nodes": [], "err": str(e)}


# ───────────────────────── 延迟探测 ─────────────────────────

def probe_node(node, timeout=3.0):
    """TCP connect 到 server:port 测 RTT。

    这是「节点可达性 + 基础延迟」探针（Karing 测的是 outbound 端到端延迟，
    但天网克制场景下先测 TCP 连通性，足够支撑选优与多机出口健康度判断）。
    ok=True 可达；ok=False 不可达（latency_ms=-1）。
    """
    t0 = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((node.server, node.port))
        node.latency_ms = round((time.monotonic() - t0) * 1000, 1)
        node.ok = True
    except (OSError, socket.timeout):
        node.latency_ms = -1
        node.ok = False
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return node


def probe_all(nodes, timeout=3.0, max_workers=16):
    """对节点列表逐个测速（纯顺序，避免引入线程依赖；节点多时可改并发）。"""
    for n in nodes:
        probe_node(n, timeout)
    return nodes


# ───────────────────────── 规则集 + 路由 ─────────────────────────

class RuleSet:
    """sing-box rule-set source 格式规则集：{version,rules:[{domain,domain_suffix,domain_keyword,ip_cidr,geoip,geosite}]}"""

    def __init__(self, tag, data=None):
        self.tag = tag
        self.domains = set()        # 精确域名
        self.domain_suffix = set()  # 后缀（*.example.com）
        self.domain_keyword = set()
        self.ip_cidrs = []          # ipaddress 网络对象
        if data:
            self.load(data)

    def load(self, data):
        for rule in (data.get("rules") or []):
            self.domains.update(rule.get("domain", []))
            self.domain_suffix.update(rule.get("domain_suffix", []))
            self.domain_keyword.update(rule.get("domain_keyword", []))
            for c in rule.get("ip_cidr", []):
                try:
                    self.ip_cidrs.append(ipaddress.ip_network(c, strict=False))
                except ValueError:
                    pass

    def matches_domain(self, host):
        host = (host or "").lower().split(":")[0]
        if host in self.domains:
            return True
        for suf in self.domain_suffix:
            s = suf.lstrip("*.")
            if host == s or host.endswith("." + s):
                return True
        for kw in self.domain_keyword:
            if kw.lower() in host:
                return True
        return False

    def matches_ip(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.ip_cidrs)


class RouteTable:
    """规则应用表（抄 recommend/cn.json 的 [{rule_set, outbound, name}] 结构）。

    outbound 取值：direct（直连）/ proxy（走代理）/ block（拦截）。
    多条规则按顺序匹配，先匹配先生效；无命中走默认 outbound。
    """

    def __init__(self, default="direct"):
        self.default = default
        self.rules = []          # [(RuleSet, outbound, name)]
        self._index = {}         # tag -> RuleSet（便于动态增删）

    def add_ruleset(self, ruleset, outbound, name=None):
        self._index[ruleset.tag] = ruleset
        self.rules.append((ruleset, outbound, name or ruleset.tag))

    def from_spec(self, spec):
        """spec 形如 recommend/cn.json 的 rules 数组：
        [{rule_set:["geosite:apple-cn"], outbound:"direct", name:"苹果"}]
        其中 geosite:/geoip: 前缀映射到已加载的 _index。
        """
        for r in spec:
            outbound = r.get("outbound", self.default)
            name = r.get("name")
            for rs_tag in r.get("rule_set", []):
                tag = rs_tag.split(":", 1)[1] if ":" in rs_tag else rs_tag
                rs = self._index.get(tag)
                if rs:
                    self.rules.append((rs, outbound, name))
                # 未加载的 rule_set 跳过（不报错，规则是数据不是代码）

    def route(self, host=None, ip=None):
        for rs, outbound, name in self.rules:
            if host and rs.matches_domain(host):
                return outbound, name
            if ip and rs.matches_ip(ip):
                return outbound, name
        return self.default, None

    def decision(self, host=None, ip=None):
        out, name = self.route(host, ip)
        return {"outbound": out, "rule": name}


# ───────────────────────── 选优 ─────────────────────────

def pick_best(nodes, prefer="latency", only_ok=True):
    """按延迟选最优节点。only_ok 时只从可达节点里选。"""
    cand = [n for n in nodes if (not only_ok) or (n.ok is True)]
    if not cand:
        cand = [n for n in nodes if n.latency_ms is not None and n.latency_ms >= 0]
    if not cand:
        return None
    if prefer == "latency":
        cand.sort(key=lambda n: n.latency_ms if n.latency_ms is not None else 1e9)
    return cand[0]


# ───────────────────────── 内置 + 外部规则加载 ─────────────────────────

# 内置精简国内直连（无外部依赖也能跑；完整版靠 load_karing_ruleset）
BUILTIN_CN_DOMAINS = [
    "baidu.com", "qq.com", "taobao.com", "jd.com", "tmall.com", "163.com",
    "sina.com.cn", "weibo.com", "163.com", "aliyun.com", "alicdn.com",
    "bilibili.com", "iqiyi.com", "youku.com", "tencent.com", "tencentcloud.com",
    "hao123.com", "sohu.com", "ifeng.com", "xinhuanet.com", "people.com.cn",
    "gov.cn", "edu.cn", "chinatax.gov.cn", "icbc.com.cn", "ccb.com",
    "abchina.com", "bankcomm.com", "cmbchina.com", "cnki.net", "cas.cn",
]
BUILTIN_CN_SUFFIX = [
    "*.baidu.com", "*.qq.com", "*.taobao.com", "*.jd.com", "*.tmall.com",
    "*.163.com", "*.sina.com.cn", "*.weibo.com", "*.aliyun.com", "*.alicdn.com",
    "*.bilibili.com", "*.iqiyi.com", "*.youku.com", "*.tencent.com",
    "*.tencentcloud.com", "*.gov.cn", "*.edu.cn", "*.cn",
]
BUILTIN_CN_CIDR = ["10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16",
                   "127.0.0.0/8", "1.0.1.0/24", "1.2.4.0/22", "1.4.4.0/22"]


def builtin_cn_ruleset():
    rs = RuleSet("cn-direct")
    rs.domains.update(BUILTIN_CN_DOMAINS)
    rs.domain_suffix.update(BUILTIN_CN_SUFFIX)
    for c in BUILTIN_CN_CIDR:
        try:
            rs.ip_cidrs.append(ipaddress.ip_network(c, strict=False))
        except ValueError:
            pass
    return rs


def load_karing_ruleset(dir_path=None):
    """从 karing-ruleset 目录加载完整国内直连规则（ChinaDomain/ChinaIp），覆盖内置精简版。

    直接引用外部目录，不复制数据。返回 RuleSet 或 None（目录不存在）。
    """
    d = dir_path or _KARING_RULESET_DIR
    if not os.path.isdir(d):
        return None
    rs = RuleSet("cn-direct")
    for rel in ("ACL4SSR/ChinaDomain.json", "ACL4SSR/ChinaIp.json"):
        fp = os.path.join(d, rel)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, "r", encoding="utf-8") as f:
                rs.load(json.load(f))
        except Exception:
            pass
    return rs if (rs.domains or rs.domain_suffix or rs.ip_cidrs) else None


def default_routetable():
    """默认路由表：国内直连，其余代理；广告/隐私类可后续挂 block。"""
    rt = RouteTable(default="proxy")
    cn = load_karing_ruleset() or builtin_cn_ruleset()
    rt.add_ruleset(cn, "direct", "国内直连")
    return rt


# ───────────────────────── 自测 ─────────────────────────

def _selftest():
    print("== proxymgr 自测 ==")
    fails = []

    def chk(cond, msg):
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    # 1) Clash YAML 解析
    clash = """
proxies:
  - name: "HK-1"
    type: vmess
    server: hk.example.com
    port: 443
    uuid: 11111111-2222-3333-4444-555555555555
    alterId: 0
    cipher: auto
    network: ws
    ws-opts:
      path: /path
  - name: "JP-ss"
    type: ss
    server: jp.example.com
    port: 8388
    cipher: aes-256-gcm
    password: secret123
"""
    nodes = parse_clash_yaml(clash, "test-clash")
    chk(len(nodes) == 2, f"Clash 解析出 2 个节点（实际 {len(nodes)}）")
    chk(nodes[0].proto == "vmess" and nodes[0].extra.get("uuid") ==
        "11111111-2222-3333-4444-555555555555", "vmess 字段归一正确")
    chk(nodes[0].extra.get("ws", {}).get("path") == "/path", "ws-opts 嵌进去")
    # Clash 的 ss 加密字段叫 cipher（部分实现也用 method），两者都进 extra
    _ss_cipher = nodes[1].extra.get("cipher") or nodes[1].extra.get("method")
    chk(nodes[1].proto == "shadowsocks" and _ss_cipher == "aes-256-gcm", "ss 协议归一 + cipher")

    # 2) V2ray 链接解析
    vless = "vless://abcd-ef01-2345@sg.example.com:443?type=ws&security=tls#Singapore-1"
    trojan = "trojan://pass@hk2.example.com:443?sni=hk2.example.com#HK-2"
    vnodes = parse_v2ray_links(vless + "\n" + trojan)
    chk(len(vnodes) == 2, f"V2ray 链接解析出 2 个（实际 {len(vnodes)}）")
    chk(vnodes[0].proto == "vless" and vnodes[0].extra.get("uuid") ==
        "abcd-ef01-2345", "vless uuid 提取")
    chk(vnodes[1].proto == "trojan" and vnodes[1].extra.get("password") ==
        "pass", "trojan password 提取")
    chk(vnodes[0].name == "Singapore-1", "链接 #name 解码为节点名")

    # 3) sing-box JSON 解析
    sb = {"outbounds": [
        {"tag": "direct", "type": "direct"},
        {"tag": "gateway", "type": "socks", "server": "127.0.0.1", "server_port": 7890,
         "username": "u", "password": "p"},
    ]}
    snodes = parse_singbox_json(sb)
    chk(len(snodes) == 1 and snodes[0].proto == "socks", "sing-box 只抽代理 outbound")
    chk(snodes[0].extra.get("username") == "u", "sing-box extra 保留")

    # 4) 自动判断格式
    auto = parse_subscription(clash, source="auto")
    chk(len(auto) == 2, "parse_subscription 自动判 Clash")
    auto2 = parse_subscription(vless, source="auto")
    chk(len(auto2) == 1 and auto2[0].proto == "vless", "parse_subscription 自动判链接")

    # 5) 延迟探测（不依赖外网：用确定超时 + 确定拒绝的本地目标）
    t = ProxyNode("loopback-deny", "socks", "127.0.0.1", 1)
    probe_node(t, timeout=1.0)
    chk(t.ok is False and t.latency_ms == -1, "不可达节点 ok=False")
    t2 = ProxyNode("unreach", "socks", "198.51.100.1", 1)  # TEST-NET-2，必超时
    probe_node(t2, timeout=0.5)
    chk(t2.ok is False, "超时节点 ok=False")
    # 选优：两个不可达里挑不出可达，返回 None 不崩
    best = pick_best([t, t2])
    chk(best is None, "全不可达时 pick_best 返回 None 不崩")

    # 6) 规则集 + 路由
    rt = default_routetable()
    d_cn = rt.route("www.baidu.com")
    chk(d_cn[0] == "direct", f"baidu 走直连（实际 {d_cn}）")
    d_github = rt.route("github.com")
    chk(d_github[0] == "proxy", f"github 走代理（实际 {d_github}）")
    # 内置规则（无 karing 目录时）也应生效
    rt2 = RouteTable(default="proxy")
    rt2.add_ruleset(builtin_cn_ruleset(), "direct", "国内直连")
    chk(rt2.route("www.qq.com")[0] == "direct", "内置 *.qq.com 直连")
    chk(rt2.route(ip="192.168.1.1")[0] == "direct", "私有网段直连")

    # 7) 外部规则集加载（若 karing-ruleset 存在）
    kr = load_karing_ruleset()
    if kr:
        chk(len(kr.domain_suffix) > 100, f"karing 国内域名规则已加载（{len(kr.domain_suffix)} 后缀）")
        print(f"  · 已引用 karing-ruleset：{len(kr.domain_suffix)} 域名后缀 / {len(kr.ip_cidrs)} 网段")
    else:
        print("  · 未找到 karing-ruleset 目录，使用内置精简规则（已 clone 到 E:/xmanbian/karing-ruleset 后会自动加载完整版）")

    # 8) 逆向解析兜底：无法识别的格式交给 proto_reverse 拆结构（不崩、不假装成功）
    weird = "ZGZne2NvbnRlbnR9"  # base64("dfg{content}")，伪数据，常规解析识别不了
    da = deep_analyze(weird)
    chk(da["method"] in ("reversed", "failed"), f"无法识别格式走 deepen 兜底（实际 {da['method']}）")
    chk(da["method"] != "standard" or len(da["nodes"]) > 0, "兜底不会把失败包装成成功")
    # 常规可识别的不走 deepest
    da2 = deep_analyze(clash)
    chk(da2["method"] == "standard" and len(da2["nodes"]) == 2, "可识别格式走 standard，不误触发逆向")
    print(f"  · deep_analyze 兜底路径：method={da['method']}")

    # 9) 阶段4：hysteria2 链接解析
    hy2 = "hysteria2://pass123@hy2.example.com:443?sni=hy2.example.com&insecure=1&obfs=salamander&obfs-password=obfs123&upmbps=100#HY2"
    hnodes = parse_v2ray_links(hy2)
    chk(len(hnodes) == 1 and hnodes[0].proto == "hysteria2", "hysteria2 链接解析")
    chk(hnodes[0].extra.get("password") == "pass123", "hysteria2 password")
    chk(hnodes[0].extra.get("obfs") == "salamander" and
        hnodes[0].extra.get("obfs-password") == "obfs123", "hysteria2 obfs 字段")
    chk(hnodes[0].extra.get("skip-cert-verify") is True, "hysteria2 insecure 归一")

    # 10) 阶段5：deep_analyze base64 解包救回节点
    wrapped = base64.b64encode(vless.encode()).decode()
    da3 = deep_analyze(wrapped)
    chk(da3["method"] == "reversed" and len(da3.get("nodes", [])) == 1,
        "base64 包裹订阅深解救回节点")

    # 11) 短明文名不被 base64 误判
    chk(_b64decode("JP-ss") == "", "短明文名不误判 base64")
    _cn_b64 = base64.b64encode("香港".encode()).decode()
    chk(_b64decode(_cn_b64) == "香港", "合法 base64 中文名正确解码")

    print("\n自测结果：", "全部通过 ✓" if not fails else f"{len(fails)} 项失败 ✗")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest())
