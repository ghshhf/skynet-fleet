# -*- coding: utf-8 -*-
"""天网 · 自研侦察引擎（recon_engine）
====================================
纯 Python 实现渗透侦察的「主动」部分，不依赖任何外部 Go 二进制工具。

背景（2026-11-02）：pentest.* 能力族原来是「能力接入」模式——封装 subfinder /
nuclei / ffuf / naabu / httpx 等 Go 二进制。但这些侦察动作（探活、扫端口、fuzz 目录、
查常见误配）Python 标准库 + 少量依赖就能写，根本不用把命运交给外部工具。

能力映射（原 Go 工具 → 自研函数）：
  httpx   存活探测     → probe_alive()          （urllib 探活 + Server/标题指纹）
  naabu   端口扫描     → port_scan()            （socket TCP connect + 线程池并发）
  ffuf    目录 fuzz    → content_discovery()    （urllib + 并发 + 内置字典）
  nuclei  漏洞扫描     → vuln_scan()            （轻量规则引擎，检测常见误配）
  被动部分（子域枚举 / DNS）→ 复用 osint.py 的 _crt / _dns（已自研，本模块不重复）

设计约束：
  · 只用 Python 标准库（urllib / socket / ssl / concurrent.futures）+ 已装的 dnspython
  · 每处网络 IO 都带超时、捕获异常、绝不抛出（侦察失败 = 空结果，不是崩溃）
  · 主动侦察会向目标发包，仅对「你拥有或明确授权」的目标使用（被动源只读，不触碰）
"""
from __future__ import annotations

import concurrent.futures
import re
import socket
import ssl
import urllib.error
import urllib.request

# 全局 opener：直连（不套系统代理的默认行为；需要代理时由调用方决定）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_UA = "fleet-recon/1.0 (+authorized security assessment)"

# 常用端口（探活后优先扫的 20 个高频服务端口）
COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 135, 139, 143, 443, 445,
                993, 995, 3306, 3389, 5432, 6379, 8080, 8443, 9200, 27017]

# 内置目录字典（精简实用，覆盖常见路径 + 敏感文件 + 备份文件）
COMMON_PATHS = [
    # 常见目录
    "admin", "login", "api", "upload", "uploads", "backup", "backups",
    "config", "db", "database", "tmp", "temp", "test", "dev", "static",
    "assets", "images", "css", "js", "docs", "doc", "readme", "install",
    # 敏感文件 / 元数据
    ".git/HEAD", ".env", ".DS_Store", "robots.txt", "sitemap.xml",
    "phpinfo.php", "info.php", "config.php", "wp-config.php", "wp-login.php",
    "composer.json", "package.json", "docker-compose.yml", "Dockerfile",
    ".htaccess", "crossdomain.xml", "server-status", "trace.axd", "web.config",
    "actuator", "actuator/health", "swagger-ui.html", "graphql",
    # 备份 / 数据库导出
    "backup.zip", "backup.tar.gz", "db.sql", "dump.sql", "database.sql",
    "www.zip", "site.zip", "web.zip", "backup.sql", ".bak", "config.bak",
]

# 漏洞 / 误配规则：每项 = (路径, 内容指纹正则, 漏洞名, 严重度)
# 只做「已知误配 / 敏感信息泄露」这一层，不做任何利用。
_VULN_RULES = [
    (".git/HEAD", r"refs?/", "Git 仓库暴露", "medium"),
    (".env", r"(APP_KEY|DB_PASSWORD|SECRET|API_KEY)", "环境变量泄露", "high"),
    ("phpinfo.php", r"PHP Version", "phpinfo 信息暴露", "medium"),
    ("info.php", r"PHP Version", "phpinfo 信息暴露", "medium"),
    ("server-status", r"Apache Server Status", "Apache 状态页暴露", "low"),
    ("actuator/health", r'"status"\s*:\s*"UP"', "Spring Boot Actuator 暴露", "medium"),
    ("swagger-ui.html", r"Swagger UI", "Swagger 文档暴露", "low"),
    (".DS_Store", r"Bud1", "macOS 元数据泄露", "low"),
]

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_SUB_RE = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", re.I)
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _clean_sub(s):
    """域名片段清洗：去通配符/端口/路径/邮箱，只留干净域名；不合规则返回 None。"""
    s = (s or "").strip().lower().lstrip("*.").rstrip(".")
    if not s or "@" in s or ":" in s or "/" in s or " " in s:
        return None
    if _IP_RE.match(s):
        return None
    if "." not in s:
        return None
    return s


# ---------------------------------------------------------------- 底层 HTTP

def _http_get(url, timeout=10, follow=True):
    """urllib GET，返回 (status, headers, body_bytes, final_url)。任何失败返回 (None,*,*,*)。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            body = r.read(256 * 1024)  # 最多读 256KB（探活/指纹够用）
            return (r.status, dict(r.headers), body, r.geturl())
    except urllib.error.HTTPError as e:
        # HTTP 错误码也是「有响应」，状态码本身有价值（403/404/500 都说明活着）
        try:
            return (e.code, dict(e.headers), e.read(4096), url)
        except Exception:
            return (e.code, {}, b"", url)
    except Exception:
        return (None, {}, b"", url)


def _get_text(url, timeout=20):
    """urllib GET 返回文本（被动源用）。失败返回空串。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.read(2 * 1024 * 1024).decode("utf-8", "replace")
    except Exception:
        return ""


def _get_json(url, timeout=20):
    """urllib GET 返回 JSON。失败返回 []。"""
    import json
    try:
        return json.loads(_get_text(url, timeout))
    except Exception:
        return []


# ---------------------------------------------------------------- ① 存活探测


def probe_alive(targets, timeout=10, tech_detect=True, title=True):
    """存活探测 + 技术栈指纹（原 httpx）。

    对每个 target（url 或 host）探活，返回状态码 / Server / 标题 / 跳转。
    自动补协议（无 scheme 先试 https 再退 http）。
    """
    def _probe_one(t):
        t = t.strip()
        if not t:
            return None
        if not re.match(r"^https?://", t, re.I):
            # 无协议：先 https 后 http（现代站点多为 https）
            for scheme in ("https", "http"):
                st, hd, body, fu = _http_get(f"{scheme}://{t}", timeout)
                if st is not None:
                    break
            url = f"{scheme}://{t}"
        else:
            st, hd, body, fu = _http_get(t, timeout)
            url = t
        if st is None:
            return {"target": t, "alive": False}
        rec = {"target": t, "alive": True, "status": st, "url": fu or url}
        if tech_detect:
            server = hd.get("Server") or hd.get("server")
            powered = hd.get("X-Powered-By") or hd.get("x-powered-by")
            tech = []
            if server:
                tech.append(f"server={server}")
            if powered:
                tech.append(f"powered-by={powered}")
            if "x-aspnet-version" in hd or "X-AspNet-Version" in hd:
                tech.append("aspnet")
            rec["tech"] = tech
        if title:
            m = _TITLE_RE.search(body.decode("utf-8", "replace"))
            if m:
                rec["title"] = " ".join(m.group(1).split())[:120]
        return rec

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
        for r in ex.map(_probe_one, targets):
            if r is not None:
                results.append(r)
    return {"count": len(results),
            "alive": [r for r in results if r.get("alive")],
            "hosts": results}


# ---------------------------------------------------------------- ② 端口扫描


def _tcp_connect(host, port, timeout=2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def port_scan(hosts, ports=None, timeout=2, workers=100):
    """TCP connect 端口扫描（原 naabu）。

    hosts: 主机/IP 列表；ports: 端口列表或范围字符串（'80,443' 或 '1-1000'），默认常用端口。
    """
    ports = ports or COMMON_PORTS
    if isinstance(ports, str):
        pts = []
        for seg in ports.split(","):
            seg = seg.strip()
            if not seg:
                continue
            if "-" in seg:
                a, b = seg.split("-", 1)
                pts.extend(range(int(a), int(b) + 1))
            else:
                pts.append(int(seg))
        ports = pts
    ports = [int(p) for p in ports]

    result = {}
    tasks = [(h, p) for h in hosts for p in ports]
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_tcp_connect, h, p, timeout): (h, p) for h, p in tasks}
        for fut in concurrent.futures.as_completed(futs):
            h, p = futs[fut]
            try:
                if fut.result():
                    result.setdefault(h, []).append(p)
            except Exception:
                pass
    for h in result:
        result[h].sort()
    return {"open_ports": result, "hosts_scanned": len(hosts),
            "ports_probed": len(ports)}


# ---------------------------------------------------------------- ③ 目录 fuzz


def content_discovery(url, wordlist=None, extensions="", match_codes="200,204,301,302,307,401,403,405,500", threads=40, timeout=8):
    """Web 内容/目录 fuzz（原 ffuf）。

    wordlist: 字典文件路径，缺省用内置 COMMON_PATHS；url 末尾自动补 /。
    """
    url = url.rstrip("/") + "/"
    if wordlist and wordlist != "builtin":
        try:
            with open(wordlist, encoding="utf-8", errors="replace") as f:
                words = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        except Exception:
            words = COMMON_PATHS
    else:
        words = COMMON_PATHS
    mc = {int(c) for c in match_codes.split(",") if c.strip()}

    def _fuzz_one(w):
        st, hd, body, fu = _http_get(url + w, timeout)
        if st in mc:
            return {"path": w, "status": st,
                    "length": len(body) if body else 0, "url": fu or url + w}
        return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        for r in ex.map(_fuzz_one, words):
            if r:
                found.append(r)
    return {"url": url, "match_codes": list(mc), "found": found[:200],
            "count": len(found), "wordlist": wordlist or "builtin"}


# ---------------------------------------------------------------- ④ 漏洞 / 误配检测


def vuln_scan(targets, rules=None, timeout=10, threads=10):
    """轻量漏洞/误配检测（原 nuclei 的侦察子集）。

    对每个 target 探测常见敏感路径，按内容指纹判定已知误配。
    只「检测」不「利用」，覆盖：Git 仓库暴露、环境变量泄露、phpinfo、Actuator、
    Swagger、Apache 状态页、.DS_Store 等常见误配。
    """
    rules = rules or _VULN_RULES

    def _check_one(t):
        t = t.rstrip("/")
        if not re.match(r"^https?://", t, re.I):
            base = f"https://{t}"
        else:
            base = t
        hits = []
        for path, fp, name, sev in rules:
            st, hd, body, fu = _http_get(f"{base}/{path.lstrip('/')}", timeout)
            if st is None:
                continue
            text = body.decode("utf-8", "replace")
            if re.search(fp, text):
                hits.append({"target": t, "path": path, "finding": name,
                             "severity": sev, "status": st})
        return hits

    findings = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as ex:
        for hits in ex.map(_check_one, targets):
            findings.extend(hits)
    by_sev = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    return {"count": len(findings), "by_severity": by_sev,
            "findings": findings[:100], "truncated": len(findings) > 100,
            "note": "仅检测已知误配/敏感信息暴露，不利用"}


# ---------------------------------------------------------------- ⑤ 被动子域枚举（自研版 subfinder）


def _src_crtsh(domain, timeout=25):
    """crt.sh 证书透明度日志：%.domain 前缀匹配挖全部子域（免费无 key）。"""
    names = []
    data = _get_json(f"https://crt.sh/?q=%.{domain}&output=json", timeout)
    for row in data:
        for part in (row.get("name_value") or "").split("\n"):
            n = _clean_sub(part)
            if n and (n == domain or n.endswith("." + domain)):
                names.append(n)
    return names


def _src_certspotter(domain, timeout=20):
    """Cert Spotter（sslmate）：证书透明度，expand=dns_names 直接给子域（免费无 key）。"""
    names = []
    u = ("https://api.certspotter.com/v1/issuances?domain=%s"
         "&include_subdomains=true&expand=dns_names" % domain)
    for it in _get_json(u, timeout):
        for n in (it.get("dns_names") or []):
            c = _clean_sub(n)
            if c and (c == domain or c.endswith("." + domain)):
                names.append(c)
    return names


def _src_hackertarget(domain, timeout=20):
    """HackerTarget hostsearch API：返回 host,ip 每行（免费无 key）。"""
    names = []
    text = _get_text(f"https://api.hackertarget.com/hostsearch/?q={domain}", timeout)
    for line in text.splitlines():
        host = _clean_sub(line.split(",")[0])
        if host and (host == domain or host.endswith("." + domain)):
            names.append(host)
    return names


def _src_rapiddns(domain, timeout=20):
    """RapidDNS 被动 DNS：HTML 页面，正则提取 xxx.domain（免费无 key）。"""
    text = _get_text(f"https://rapiddns.io/subdomain/{domain}?full=1", timeout)
    pat = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+" + re.escape(domain), re.I)
    names = []
    for m in pat.finditer(text):
        n = _clean_sub(m.group(0))
        if n:
            names.append(n)
    return names


def passive_enum(domain, sources=None, limit=500, timeout=20):
    """被动子域枚举（自研版 subfinder）：聚合多个免费无 key 的公开数据源。

    domain: 目标根域名。只读公开源，不向目标发包（零痕迹，目标无感知）。
    sources: 限定数据源子集（默认全部：crtsh/certspotter/hackertarget/rapiddns）。
    """
    domain = _clean_sub(domain)
    if not domain:
        return {"ok": False, "err": "域名不合法"}
    srcs = {
        "crtsh": _src_crtsh,
        "certspotter": _src_certspotter,
        "hackertarget": _src_hackertarget,
        "rapiddns": _src_rapiddns,
    }
    if sources:
        srcs = {k: v for k, v in srcs.items() if k in sources}
    merged, seen, by_src = [], set(), {}
    for name, fn in srcs.items():
        try:
            got = fn(domain, timeout)
        except Exception:
            got = []
        by_src[name] = len(got)
        for n in got:
            if n not in seen:
                seen.add(n)
                merged.append(n)
    merged.sort()
    return {"ok": True, "domain": domain, "subdomains": merged[:limit],
            "count": len(merged), "sources": by_src,
            "note": "被动数据：仅含公开源历史记录里出现过的子域，可能含已下线记录"} 


# ---------------------------------------------------------------- 离线自测


def _selftest():
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [ok] {name}")
        else:
            fail += 1
            print(f"  [FAIL] {name}")

    # 1. 端口解析（离线）
    check("端口范围字符串解析", _parse_ports("80-82") == [80, 81, 82])
    check("端口列表解析", _parse_ports("80,443,8080") == [80, 443, 8080])

    # 2. 域名清洗（离线）
    check("域名清洗", _clean_sub("*.api.example.com") == "api.example.com")
    check("IP 被过滤", _clean_sub("192.168.1.1") is None)
    check("邮箱被过滤", _clean_sub("x@example.com") is None)

    # 3. 内置字典非空
    check("内置目录字典", len(COMMON_PATHS) >= 30)

    # 4. 漏洞规则表完整
    check("漏洞规则表", len(_VULN_RULES) >= 6)

    # 5. 被动枚举的非法域名拒绝（离线）
    check("非法域名拒绝", passive_enum("not a domain").get("ok") is False)

    print(f"[recon_engine] 自测：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


def _parse_ports(s):
    out = []
    for seg in s.split(","):
        seg = seg.strip()
        if "-" in seg:
            a, b = seg.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif seg:
            out.append(int(seg))
    return out


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
