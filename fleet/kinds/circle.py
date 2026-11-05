"""关系圈：从一个东西出发，把「跟它有关的一圈」拆出来。

这不是社交功能，是**关联定位**。
--------------------------------
主人给的比喻很准：像朋友圈——不是看一个人发了什么，而是**看谁跟谁是一伙的**。
落到工程上就是一张图：节点是标识（域名/IP/昵称/手机号/邮箱/证书/组织），
边是"它们之间存在某种可验证的关联"，边上挂证据和强度。

为什么这一层必须单独存在
------------------------
`osint.*` 是「查一个东西」：给我一个域名，我把它的子域、端口、注册信息摊开。
那是**一次查询**，回答「它身上有什么」。

关系圈回答的是另一个问题：「**它和谁在一起**」。这不是多查几次就能得到的——
中间要判断哪些线索是真关联、哪些只是巧合，还要控制扩展不要爆掉。
把这件事塞进 osint 会让 osint 长出一堆和它职责无关的爬取逻辑。

最关键的纪律：**关联 ≠ 同一主体**
--------------------------------
同一个 IP 上有 300 个网站，这不代表它们是同一个人的；同一个昵称 5000 人在用。
所以这里对所有边都标 `weight`（技术关联强度）和 `basis`（凭什么这么判），
但**不替用户下"它们是同一个主体"的结论**。结论只在 `verdict` 里给分级，
且只有出现强证据（同证书 SAN / 同注册人邮箱）才升到 `likely`，`confirmed`
一律留给人工核实——机器没有资格替人认定"这两个账号是同一个人"。

信号表（weight 是关联强度，不是置信度）
---------------------------------------
| 边 | 依据 | weight | 说明 |
|---|---|---|---|
| same_cert      | 同一张证书里同时出现 | 0.95 | 证书是主动绑定，最强 |
| same_registrant| RDAP 注册人/邮箱相同 | 0.90 | 注册信息，强 |
| resolves_to    | DNS A/AAAA 记录       | 0.90 | 事实，但只说明"指向"，不说明"属于" |
| subdomain_of   | crt.sh 证书透明度     | 0.85 | 同一域名的子域 |
| same_org_cert  | 证书 O= 字段相同      | 0.70 | 同一组织申请 |
| same_ns        | NS 记录相同           | 0.35 | 常只是同一个服务商 |
| shares_ip      | 同一 IP 上还有它      | 0.30 | **共享 IP 极常见**，云主机默认如此 |
| same_handle    | 跨平台同昵称          | 0.30 | 撞名太常见 |
| same_asn       | 同一 ASN              | 0.10 | 太弱，默认只标不建边 |

`0.30` 那两条特别标出来：它们是「值得看一眼」的线索，不是「有关系」的结论。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import osint as O
from ..core import BaseDevice, DeviceResult as R
from .. import net

# ============================================================ 信号表

SIGNALS = {
    "same_cert":      (0.95, "同一张证书的 SAN 列表里同时出现"),
    "same_registrant": (0.90, "RDAP 注册人 / 注册邮箱相同"),
    "resolves_to":    (0.90, "DNS A/AAAA 记录指向"),
    "subdomain_of":   (0.85, "证书透明度显示属于同一域名的子域"),
    "same_org_cert":  (0.70, "证书 O= 字段相同（同一组织）"),
    "same_ns":        (0.35, "NS 记录相同（多半只是同一个服务商）"),
    "shares_ip":      (0.30, "同一 IP 上还有它（共享 IP 极常见）"),
    "same_handle":    (0.30, "跨平台同昵称（撞名常见）"),
    "same_asn":       (0.10, "同一 ASN（太弱，默认只标注不建边）"),
}

#: 低于这个强度的边默认不建（避免图被 0.1 的弱信号塞满）
MIN_EDGE_WEIGHT = 0.25

#: 反查同 IP 的免费源。都可能挂，挂了就如实标注 unavailable，不编数据。
_REVERSE_IP_URLS = (
    "https://api.hackertarget.com/reverseiplookup/?q=%s",
)


# ============================================================ 图

class Graph:
    """一张有向图，节点是标识，边带证据。

    刻意用纯 dict 而不是 networkx：这个项目的纪律是核心链路零第三方依赖，
    而且要能直接序列化给别的进程。图算法的需求也就 BFS + 连通分量 + 最短路，
    自己写比引一个库更省。
    """

    def __init__(self):
        self.nodes = {}          # id -> {"id","type","value","depth","attrs"}
        self.edges = []          # {"src","dst","rel","weight","evidence":[...]}
        self._seen = set()       # 去重键

    def add_node(self, nid, ntype, value, depth=0, **attrs):
        if nid in self.nodes:
            n = self.nodes[nid]
            n["depth"] = min(n["depth"], depth)
            n["attrs"].update({k: v for k, v in attrs.items() if v is not None})
            return n
        n = {"id": nid, "type": ntype, "value": value, "depth": depth,
             "attrs": {k: v for k, v in attrs.items() if v is not None}}
        self.nodes[nid] = n
        return n

    def add_edge(self, src, dst, rel, weight=None, evidence=None):
        w = SIGNALS.get(rel, (0.0, ""))[0] if weight is None else weight
        key = (src, dst, rel)
        rkey = (dst, src, rel)
        if key in self._seen:
            # 同一对节点同一关系出现多次 → 合并证据，取最强权重
            for e in self.edges:
                if (e["src"], e["dst"], e["rel"]) == key:
                    for ev in (evidence or []):
                        if ev not in e["evidence"]:
                            e["evidence"].append(ev)
                    e["weight"] = max(e["weight"], w)
                    return e
            return None
        if rkey in self._seen:
            return None
        self._seen.add(key)
        e = {"src": src, "dst": dst, "rel": rel, "weight": round(w, 2),
             "basis": SIGNALS.get(rel, (0, ""))[1],
             "evidence": evidence or []}
        self.edges.append(e)
        return e

    # ---------------- 查询 ----------------

    def neighbors(self, nid, min_weight=0.0):
        out = []
        for e in self.edges:
            if e["weight"] < min_weight:
                continue
            if e["src"] == nid:
                out.append((e["dst"], e))
            elif e["dst"] == nid:
                out.append((e["src"], e))
        return out

    def clusters(self):
        """连通分量 = 一个个「圈」。"""
        parent = {n: n for n in self.nodes}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for e in self.edges:
            if e["src"] in parent and e["dst"] in parent:
                union(e["src"], e["dst"])
        groups = {}
        for n in self.nodes:
            groups.setdefault(find(n), []).append(n)
        out = []
        for root, members in groups.items():
            if len(members) < 2:
                continue
            strength = max((e["weight"] for e in self.edges
                            if e["src"] in members and e["dst"] in members), default=0)
            out.append({"members": sorted(members), "size": len(members),
                        "max_weight": strength,
                        "types": sorted({self.nodes[m]["type"] for m in members})})
        out.sort(key=lambda c: (c["max_weight"], c["size"]), reverse=True)
        return out

    def path(self, a, b):
        """a 到 b 的最强路径（边权取负做最短路，等价于找最可信链）。"""
        if a not in self.nodes or b not in self.nodes:
            return None
        import heapq
        dist = {a: 0.0}
        prev = {}
        pq = [(0.0, a)]
        visited = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in visited:
                continue
            visited.add(u)
            if u == b:
                break
            for v, e in self.neighbors(u):
                # 用「1-weight」当距离：权重越高路越短（越可信）
                nd = d + (1.0 - e["weight"])
                if nd < dist.get(v, 1e9):
                    dist[v] = nd
                    prev[v] = (u, e)
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None
        chain, cur = [], b
        while cur != a:
            u, e = prev[cur]
            chain.append({"from": u, "to": cur, "rel": e["rel"], "weight": e["weight"],
                          "basis": e["basis"], "evidence": e["evidence"][:3]})
            cur = u
        chain.reverse()
        return {"from": a, "to": b, "hops": len(chain),
                "cost": round(dist[b], 3), "chain": chain}

    def to_dict(self):
        return {"nodes": list(self.nodes.values()), "edges": self.edges,
                "node_count": len(self.nodes), "edge_count": len(self.edges),
                "clusters": self.clusters()}


# ============================================================ 扩展器

def _nid(ntype, value):
    return "%s:%s" % (ntype, str(value).strip().lower())


def _reverse_ip(ip, timeout=12):
    """IP 反查：同一个 IP 上还有哪些域名。

    免费源很少且不稳定。拿不到就返回 []，并在诊断里写明「没拿到」——
    悄悄返回空会让下游以为是「查过了，没有别的」。
    """
    for tpl in _REVERSE_IP_URLS:
        url = tpl % urllib.parse.quote(str(ip))
        try:
            with net.urlopen(url, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
        except Exception:
            continue
        if "error" in body[:120].lower() or "API count exceeded" in body:
            continue
        hosts = [h.strip().lower() for h in body.replace(",", "\n").splitlines() if h.strip()]
        hosts = [h for h in hosts if "." in h and " " not in h][:60]
        if hosts:
            return hosts
    return []


def _rdap_registrant(domain):
    """从 RDAP 里抠注册人/注册邮箱。多数 .com 已做隐私保护，抠不到就返回空。"""
    r = O.run("rdap", domain=domain)
    if not r.get("ok"):
        return {}
    # osint 的 rdap 返回结构可能随版本变，这里做宽松取值
    e = (r.get("entities") or r.get("data") or {})
    if isinstance(e, list):
        for ent in e:
            vc = ent.get("vcardArray") if isinstance(ent, dict) else None
            if vc and len(vc) > 1:
                for item in vc[1]:
                    if isinstance(item, list) and item[0] in ("fn", "email"):
                        return {item[0]: item[3]}
    out = {}
    for k in ("registrant", "registrant_email", "email", "org", "registrar"):
        v = r.get(k) if isinstance(r, dict) else None
        if v:
            out[k] = v
    return out


def expand_once(g, node_id, budget):
    """从一个节点出发扩一层。返回新加的节点数。

    budget: {"max_fanout": 每类信号最多扩几个, "min_weight": 建边下限}
    """
    if node_id not in g.nodes:
        return 0
    n = g.nodes[node_id]
    t, v = n["type"], n["value"]
    fan = int(budget.get("max_fanout", 8))
    added = 0
    depth = n["depth"] + 1
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")

    def ev(source, field, value_, url=""):
        return {"source": source, "field": field, "value": value_, "url": url, "ts": ts}

    # ---------------- 域名：拆子域、解析 IP、看注册信息 ----------------
    if t == "domain":
        crt = O.run("crt", domain=v, limit=fan * 4)
        if crt.get("ok"):
            subs = [s for s in (crt.get("subdomains") or []) if s != v][:fan]
            for s in subs:
                sid = _nid("domain", s)
                g.add_node(sid, "domain", s, depth)
                g.add_edge(node_id, sid, "subdomain_of",
                           evidence=[ev("crt.sh", "name_value", s,
                                        "https://crt.sh/?q=%s" % urllib.parse.quote(v))])
                added += 1

        dns = O.run("dns", domain=v)
        if dns.get("ok"):
            for rec in (dns.get("records") or {}).get("A", [])[:fan] if isinstance(
                    dns.get("records"), dict) else []:
                rid = _nid("ip", rec)
                g.add_node(rid, "ip", rec, depth)
                g.add_edge(node_id, rid, "resolves_to",
                           evidence=[ev("dns", "A", rec)])
                # IP 向外：情报 + 反查同 IP 的其他域
                intel = O.run("ip_intel", ip=rec)
                if intel.get("ok"):
                    g.nodes[rid]["attrs"].update({
                        "org": intel.get("org"), "country": intel.get("country"),
                        "city": intel.get("city")})
                for other in _reverse_ip(rec)[:fan]:
                    if other == v:
                        continue
                    oid = _nid("domain", other)
                    g.add_node(oid, "domain", other, depth)
                    g.add_edge(rid, oid, "shares_ip",
                               evidence=[ev("reverse-ip", "host", other,
                                            "https://api.hackertarget.com/reverseiplookup/?q=%s" % rec)])
                    added += 1

        rd = _rdap_registrant(v)
        for k, val in rd.items():
            if k in ("fn", "email") and val:
                eid = _nid("email", val) if k == "email" else _nid("org", val)
                g.add_node(eid, "email" if k == "email" else "org", val, depth)
                g.add_edge(node_id, eid, "same_registrant",
                           evidence=[ev("rdap", k, val)])
                added += 1

    # ---------------- IP：反查同 IP、看归属 ----------------
    elif t == "ip":
        intel = O.run("ip_intel", ip=v)
        if intel.get("ok"):
            for key, rel, ntype in (("org", "same_asn", "org"),):
                val = intel.get(key)
                if val:
                    oid = _nid(ntype, val)
                    is_new = oid not in g.nodes
                    g.add_node(oid, ntype, val, depth)
                    g.add_edge(node_id, oid, rel, evidence=[ev("ipwho.is", key, val)])
                    if is_new:
                        added += 1
        for other in _reverse_ip(v)[:fan]:
            oid = _nid("domain", other)
            g.add_node(oid, "domain", other, depth)
            g.add_edge(node_id, oid, "shares_ip",
                       evidence=[ev("reverse-ip", "host", other)])
            added += 1

    # ---------------- 昵称：跨平台 ----------------
    elif t == "handle":
        h = O.run("handle", name=v, timeout=10)
        if h.get("ok"):
            for plat in (h.get("found_on") or [])[:fan]:
                pid = _nid("account", "%s/%s" % (plat, v))
                g.add_node(pid, "account", "%s@%s" % (v, plat), depth,
                           platform=plat, url="https://%s/%s" % (plat, v))
                g.add_edge(node_id, pid, "same_handle",
                           evidence=[ev("platform-lookup", "handle", v)])
                added += 1

    # ---------------- 手机号：归属地 ----------------
    elif t == "phone":
        p = O.run("phone", number=v)
        if p.get("ok"):
            for key in ("carrier", "location", "line_type"):
                val = p.get(key)
                if val:
                    aid = _nid("attr", "%s:%s" % (key, val))
                    g.add_node(aid, "attr", str(val), depth, key=key)
                    g.add_edge(node_id, aid, "same_registrant", weight=0.5,
                               evidence=[ev("libphonenumber", key, str(val))])
                    added += 1

    return added


# ============================================================ 种子识别

def _seed_type(seed):
    s = str(seed).strip()
    if not s:
        return None, ""
    low = s.lower()
    if low.startswith(("http://", "https://")):
        host = urllib.parse.urlsplit(low).hostname or ""
        return "domain", host
    if all(c.isdigit() or c in ".-" for c in s) and s.count(".") == 3:
        return "ip", s
    if "." in s and " " not in s and not s.startswith("@"):
        return "domain", low
    if s.startswith("@") or (s.isalnum() and len(s) <= 32):
        return "handle", s.lstrip("@")
    return "handle", s


# ============================================================ 设备

class Circle(BaseDevice):
    """kind=circle：关系圈。给一组标识，拆出「这一圈」。"""

    kind = "circle"
    via = "internet"

    def __init__(self, name="circle:web", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault(
            "note", "只查公开/被动源；关联强度是技术线索，不等于同一主体")

    def probe(self):
        try:
            import dns  # noqa: F401    # osint.dns 依赖 dnspython
        except Exception:
            pass          # 没有也能跑，只是 dns 信号缺失，不阻断
        return True, ""

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        args = dict(args or {})
        if cap not in CAPS:
            return R.unsupported(cap, "%s（%s）没有这项能力" % (self.name, self.kind),
                                 device=self.name, via=self.via)
        fn = getattr(self, "_do_" + cap.split(".", 1)[1], None)
        if fn is None:
            return R.unsupported(cap, "这项能力没接上实现", device=self.name, via=self.via)
        try:
            return fn(args)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 能力 ----------------

    def _do_signals(self, args):
        return R.success("circle.signals", self.name, via=self.via, data={
            "signals": [{"rel": k, "weight": v[0], "basis": v[1]}
                        for k, v in sorted(SIGNALS.items(), key=lambda x: -x[1][0])],
            "min_edge_weight": MIN_EDGE_WEIGHT,
            "discipline": [
                "weight 是「技术关联强度」，不是「同一主体」的概率。",
                "同一 IP / 同昵称可能纯属共享主机与撞名，标弱信号就是这个原因。",
                "机器不替你认定「这两个是同一个人」；confirmed 只留给人核实。",
            ],
        })

    def _do_expand(self, args):
        seed = args.get("seed")
        if not seed:
            return R.error("circle.expand", "需要 seed（域名/IP/昵称/手机号）",
                           device=self.name, via=self.via)
        t, v = _seed_type(seed)
        if not v:
            return R.error("circle.expand", "识别不出这个 seed 的类型：%r" % seed,
                           device=self.name, via=self.via)
        g = Graph()
        sid = _nid(t, v)
        g.add_node(sid, t, v, 0)
        n = expand_once(g, sid, {"max_fanout": int(args.get("fanout") or 8),
                                 "min_weight": float(args.get("min_weight") or MIN_EDGE_WEIGHT)})
        return R.success("circle.expand", self.name, via=self.via,
                         data=dict(g.to_dict(), seed=sid, seed_type=t, added=n))

    def _do_build(self, args):
        seeds = args.get("seeds") or args.get("seed")
        if not seeds:
            return R.error("circle.build", "需要 seeds（逗号分隔的域名/IP/昵称/手机号）",
                           device=self.name, via=self.via)
        if isinstance(seeds, str):
            seeds = [s.strip() for s in seeds.split(",") if s.strip()]
        max_depth = int(args.get("max_depth") or 2)
        max_nodes = int(args.get("max_nodes") or 60)
        fanout = int(args.get("fanout") or 6)
        min_w = float(args.get("min_weight") or MIN_EDGE_WEIGHT)

        g = Graph()
        frontier = []
        for s in seeds:
            t, v = _seed_type(s)
            if not v:
                continue
            sid = _nid(t, v)
            g.add_node(sid, t, v, 0)
            frontier.append(sid)

        trace = []
        for depth in range(max_depth):
            nxt = []
            for nid in frontier:
                if len(g.nodes) >= max_nodes:
                    break
                before = len(g.nodes)
                try:
                    expand_once(g, nid, {"max_fanout": fanout, "min_weight": min_w})
                except Exception as e:
                    trace.append({"node": nid, "depth": depth, "err": "%s: %s" % (type(e).__name__, e)})
                    continue
                trace.append({"node": nid, "depth": depth, "added": len(g.nodes) - before})
                for v, e in g.neighbors(nid):
                    if g.nodes[v]["depth"] == depth + 1 and v not in nxt:
                        nxt.append(v)
            frontier = nxt
            if not frontier or len(g.nodes) >= max_nodes:
                break

        # 只保留达到权重下限的边（弱信号可以是 0.1，但默认不建）
        g.edges = [e for e in g.edges if e["weight"] >= min_w]

        data = g.to_dict()
        data.update({
            "seeds": seeds,
            "truncated": len(g.nodes) >= max_nodes,
            "trace": trace[-10:],
            "discipline": "边上的 weight 是技术关联强度；同一主体需要人工核实。",
        })
        return R.success("circle.build", self.name, via=self.via, data=data)

    def _do_path(self, args):
        a, b = args.get("from"), args.get("to")
        if not a or not b:
            return R.error("circle.path", "需要 from 与 to（两个节点 id 或可识别的标识）",
                           device=self.name, via=self.via)

        def norm(x):
            if ":" in str(x) and str(x).split(":", 1)[0] in (
                    "domain", "ip", "handle", "phone", "email", "org", "account", "attr"):
                return str(x)
            t, v = _seed_type(x)
            return _nid(t, v)

        na, nb = norm(a), norm(b)
        g = Graph()
        for seed in (na, nb):
            g.add_node(seed, seed.split(":", 1)[0], seed.split(":", 1)[1], 0)
        for seed in (na, nb):
            try:
                expand_once(g, seed, {"max_fanout": int(args.get("fanout") or 8)})
            except Exception:
                pass
        p = g.path(na, nb)
        if not p:
            return R.unavailable("circle.path",
                                 "两跳之内没有找到可信通路（可以加大 fanout 或分两次 build）",
                                 device=self.name, via=self.via)
        return R.success("circle.path", self.name, via=self.via, data=p)

    def _do_export(self, args):
        """把图导出成别的工具能吃的格式。"""
        raw = args.get("graph")
        if not raw:
            return R.error("circle.export", "需要 graph（circle.build 的返回对象）",
                           device=self.name, via=self.via)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception as e:
                return R.error("circle.export", "graph 不是合法 JSON：%s" % e,
                               device=self.name, via=self.via)
        fmt = str(args.get("format") or "json").lower()
        nodes = raw.get("nodes") or []
        edges = raw.get("edges") or []

        if fmt == "graphml":
            def esc(x):
                return (str(x).replace("&", "&amp;").replace("<", "&lt;")
                        .replace(">", "&gt;").replace('"', "&quot;"))
            out = ['<?xml version="1.0" encoding="UTF-8"?>',
                   '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
                   '<key id="type" for="node" attr.name="type" attr.type="string"/>',
                   '<key id="value" for="node" attr.name="value" attr.type="string"/>',
                   '<key id="rel" for="edge" attr.name="rel" attr.type="string"/>',
                   '<key id="weight" for="edge" attr.name="weight" attr.type="double"/>',
                   '<graph edgedefault="directed">']
            for n in nodes:
                out.append('<node id="%s"><data key="type">%s</data>'
                           '<data key="value">%s</data></node>'
                           % (esc(n["id"]), esc(n.get("type")), esc(n.get("value"))))
            for i, e in enumerate(edges):
                out.append('<edge id="e%d" source="%s" target="%s">'
                           '<data key="rel">%s</data><data key="weight">%s</data></edge>'
                           % (i, esc(e["src"]), esc(e["dst"]), esc(e.get("rel")), e.get("weight", 0)))
            out.append("</graph></graphml>")
            body = "\n".join(out)
            return R.success("circle.export", self.name, via=self.via,
                             data={"format": "graphml", "content": body,
                                   "note": "可直接导入 Gephi / yEd / Cytoscape"})

        if fmt in ("cypher", "neo4j"):
            lines = []
            for n in nodes:
                lines.append(
                    'MERGE (:%s {id:"%s", value:"%s"});'
                    % (str(n.get("type") or "Node").capitalize(), n["id"], n.get("value", "")))
            for e in edges:
                lines.append(
                    'MATCH (a {id:"%s"}),(b {id:"%s"}) '
                    'MERGE (a)-[:%s {weight:%s}]->(b);'
                    % (e["src"], e["dst"], str(e.get("rel") or "REL").upper().replace("-", "_"),
                       e.get("weight", 0)))
            return R.success("circle.export", self.name, via=self.via,
                             data={"format": "cypher", "content": "\n".join(lines),
                                   "note": "粘到 Neo4j Browser 直接执行"})

        return R.success("circle.export", self.name, via=self.via,
                         data={"format": "json",
                               "content": json.dumps(raw, ensure_ascii=False, indent=1)})


CAPS = ("circle.signals", "circle.expand", "circle.build", "circle.path", "circle.export")


# ============================================================ 自测（不联网部分）

def _selftest():
    ok = []

    def chk(name, cond, extra=""):
        ok.append((name, bool(cond), extra))

    # 图算法
    g = Graph()
    g.add_node("domain:a.com", "domain", "a.com", 0)
    g.add_node("ip:1.2.3.4", "ip", "1.2.3.4", 1)
    g.add_node("domain:b.com", "domain", "b.com", 1)
    g.add_edge("domain:a.com", "ip:1.2.3.4", "resolves_to")
    g.add_edge("ip:1.2.3.4", "domain:b.com", "shares_ip")
    chk("节点数", len(g.nodes) == 3, str(len(g.nodes)))
    chk("边权重取自信号表",
        abs(g.edges[0]["weight"] - 0.9) < 1e-6 and abs(g.edges[1]["weight"] - 0.3) < 1e-6)
    cl = g.clusters()
    chk("连通分量识别", len(cl) == 1 and cl[0]["size"] == 3, str(cl))
    p = g.path("domain:a.com", "domain:b.com")
    chk("最可信路径", p and p["hops"] == 2 and p["chain"][0]["rel"] == "resolves_to")
    chk("反向边不重复", g.add_edge("ip:1.2.3.4", "domain:a.com", "resolves_to") is None)

    # 种子识别
    cases = [("https://x.com/a", ("domain", "x.com")),
             ("1.2.3.4", ("ip", "1.2.3.4")),
             ("example.com", ("domain", "example.com")),
             ("@someone", ("handle", "someone"))]
    for raw, want in cases:
        got = _seed_type(raw)
        chk("识别 %s" % raw, got == want, "%s vs %s" % (got, want))

    # 导出
    d = Circle()
    r = d._do_export({"graph": g.to_dict(), "format": "graphml"})
    chk("GraphML 导出", r.data.get("format") == "graphml"
        and "<graphml" in r.data.get("content", ""))
    r2 = d._do_export({"graph": g.to_dict(), "format": "cypher"})
    chk("Cypher 导出", "MERGE" in r2.data.get("content", ""))

    passed = sum(1 for _, c, _ in ok if c)
    for n, c, x in ok:
        print("  %s %s %s" % ("✓" if c else "✗", n, x))
    print("circle selftest: %d/%d" % (passed, len(ok)))
    return passed == len(ok)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
