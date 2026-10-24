"""L4 外部情报（OSINT）：把「世界模型」扩到互联网，靠拆解公开面拿数据。

这一层的意义（详见 docs/总纲_唯一推进依据_2026-10-25.md）：
很多东西你必须拆解才能得到——一个域名暴露了哪些子域、注册在谁名下、开了哪些端口，
这些都不在搜索引擎里直给，得去证书透明度日志 / RDAP / Shodan 里「拆」出来。

诚实边界：
- 全部走公开 / 被动源（证书透明度、RDAP、公共 DNS、Shodan(key 可选项)）。
- 只查「你拥有或明确授权」的资产；返回的是公开事实，不是入侵。
- Shodan 需 key，且只能用于自有资产自查——无 key 时如实说 unavailable，不假装能查。
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run(op, **args):
    """统一入口：op ∈ {crt, dns, rdap, shodan}。返回 dict（由 Osint kind 包成 DeviceResult）。"""
    fn = {"crt": _crt, "dns": _dns, "rdap": _rdap, "shodan": _shodan}.get(op)
    if fn is None:
        return {"ok": False, "err": f"未知 osint 操作: {op}（支持 crt/dns/rdap/shodan）"}
    try:
        return fn(**args)
    except Exception as e:  # 网络/解析异常都归一为「查询失败」，不让调用方崩
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def _get_json(url, timeout=25, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "fleet-osint/1.0"})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ----------------------------------------------------------- crt.sh 证书透明度

def _crt(domain, limit=200):
    """证书透明度：枚举某域的所有子域（crt.sh，无需 key，被动数据源）。

    这是「拆解」一个域名暴露面的核心手段——你会拿到从没猜过的子域。
    """
    try:
        data = _get_json(f"https://crt.sh/?q=%.{domain}&output=json", timeout=25)
    except Exception as e:
        return {"ok": False, "err": f"crt.sh 查询失败: {e}"}
    subs = set()
    for row in data or []:
        name = row.get("name_value") or ""
        for part in name.split("\n"):
            part = part.strip().lower()
            if part and domain in part:
                subs.add(part)
    subs = sorted(subs)[:limit]
    return {"ok": True, "domain": domain, "subdomains": subs, "count": len(subs),
            "source": "crt.sh (Certificate Transparency)",
            "note": "被动数据：仅含曾申请过证书的域名；未申请证书的子域不会出现在这里。"}


# ----------------------------------------------------------- 公共 DNS 解析

def _dns(domain, types=("A", "AAAA", "MX", "TXT", "NS", "CNAME")):
    """公共 DNS 解析（走 dnspython，keyless）。给出该域的解析记录面。"""
    try:
        import dns.resolver
    except Exception:
        return {"ok": False, "err": "未装 dnspython（pip install dnspython）"}
    out = {}
    for t in types:
        try:
            ans = dns.resolver.resolve(domain, t, lifetime=8)
            out[t] = [r.to_text().rstrip(".") for r in ans]
        except Exception:
            out[t] = []
    return {"ok": True, "domain": domain, "records": out,
            "note": "公共解析结果，可能因解析器/缓存而异。"}


# ----------------------------------------------------------- RDAP 注册信息

def _rdap(domain):
    """RDAP 注册信息（取代老 whois，keyless）：注册局/注册商/创建时间/DNS。"""
    try:
        data = _get_json(f"https://rdap.org/domain/{domain}", timeout=20)
    except Exception as e:
        return {"ok": False, "err": f"RDAP 查询失败: {e}"}
    ev = {e.get("eventAction"): e.get("eventDate") for e in (data.get("events") or [])}
    return {"ok": True, "domain": domain,
            "ldhName": data.get("ldhName"),
            "status": data.get("status"),
            "entities": [e.get("roles") for e in (data.get("entities") or [])],
            "events": ev,
            "nameservers": [n.get("ldhName") for n in (data.get("nameservers") or [])],
            "source": "rdap.org",
            "note": "注册公开信息；个人注册人信息按 GDPR 多已脱敏。"}


# ----------------------------------------------------------- Shodan（需 key，可选）

def _shodan(query, key=None, limit=20):
    """Shodan 资产情报（需 key，环境变量 SHODAN_KEY）。仅查你授权的资产。"""
    key = key or os.environ.get("SHODAN_KEY")
    if not key:
        return {"ok": False,
                "err": "未设 SHODAN_KEY（仅查自有资产时使用；不可用于未授权目标）"}
    try:
        url = (f"https://api.shodan.io/shodan/host/search?key={key}"
               f"&query={urllib.parse.quote(query)}&limit={int(limit)}")
        data = _get_json(url, timeout=25)
    except Exception as e:
        return {"ok": False, "err": f"Shodan 查询失败: {e}"}
    matches = [{"ip": m.get("ip_str"), "ports": m.get("ports"),
                "org": m.get("org"), "os": m.get("os")}
               for m in (data.get("matches") or [])]
    return {"ok": True, "query": query, "matches": matches,
            "total": data.get("total"),
            "note": "Shodan 是公开的暴露面测绘；只用于你拥有/授权的资产自查。"}


# ----------------------------------------------------------- 离线自测

def _selftest():
    checks, fails = [], []

    def check(name, cond):
        checks.append(name)
        if not cond:
            fails.append(name)

    # crt 子域抽取逻辑（不联网，直接复刻内部去重逻辑）
    subs = set()
    for row in [{"name_value": "www.example.com\napi.example.com"}]:
        for part in row["name_value"].split("\n"):
            part = part.strip().lower()
            if part and "example.com" in part:
                subs.add(part)
    check("crt 子域抽取", subs == {"www.example.com", "api.example.com"})

    # 无 key 时 shodan 诚实拒绝
    import os as _os
    old = _os.environ.pop("SHODAN_KEY", None)
    try:
        r = _shodan("example.com")
        check("shodan 无 key 拒绝", (not r.get("ok")) and "SHODAN_KEY" in r.get("err", ""))
    finally:
        if old is not None:
            _os.environ["SHODAN_KEY"] = old

    # 未知 op
    check("未知 op 拒绝", (not run("nope").get("ok")))

    print(f"[osint] {len(checks)-len(fails)}/{len(checks)} 通过"
          + (f"，失败: {fails}" if fails else " ✓"))
    return not fails


if __name__ == "__main__":
    _selftest()
