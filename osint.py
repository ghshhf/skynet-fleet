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
    """统一入口：op ∈ {crt, dns, rdap, shodan, exposure, phone, handle, identity}。
    返回 dict（由 Osint kind 包成 DeviceResult）。

    资产级（crt/dns/rdap/shodan/exposure）：拆解域名/IP 的公开暴露面；
        exposure 额外做 A-F 暴露面评级（内化 OpenOSINT 评分模型）。
    身份级（phone/handle/identity）：把「手机号/昵称/邮箱/IP」等标识聚合成
    一个数字身份的公开足迹画像——靠的是**各平台既有查询入口 + 关联拼装**，
    不做囤积式数据库：使用即拼接，可选留历史快照供下次对比
    （见 _exposure/_identity 的 history 参数；历史是运行态快照，不是库）。
    """
    fn = {"crt": _crt, "dns": _dns, "rdap": _rdap, "shodan": _shodan, "exposure": _exposure,
          "phone": _phone, "handle": _handle, "identity": _identity}.get(op)
    if fn is None:
        return {"ok": False, "err": f"未知 osint 操作: {op}（支持 crt/dns/rdap/shodan/exposure/phone/handle/identity）"}
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


# ----------------------------------------------------------- 手机号本地解析（身份级）

def _phone(number, region=None):
    """手机号公开情报（纯本地、零外部请求）：用 Google 维护的 libphonenumber 号段库
    解析归属地/运营商/号段/号码类型。这就是「大厂自己存了号段数据，直接借它的库查」——
    不需要我们建库，库里现成。

    region: 优先国家码（如 "CN"），用于无国际前缀的本地号码；number 带 + 前缀时忽略。
    """
    try:
        import phonenumbers
        from phonenumbers import carrier, geocoder
    except Exception as e:
        return {"ok": False, "err": f"未装 phonenumbers（pip install phonenumbers）：{e}"}
    try:
        p = phonenumbers.parse(number, region)
    except Exception as e:
        return {"ok": False, "err": f"号码解析失败: {e}"}
    valid = phonenumbers.is_valid_number(p)
    possible = phonenumbers.is_possible_number(p)
    ntype = phonenumbers.number_type(p)
    type_map = {
        phonenumbers.PhoneNumberType.MOBILE: "手机",
        phonenumbers.PhoneNumberType.FIXED_LINE: "固话",
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "固话或手机",
        phonenumbers.PhoneNumberType.TOLL_FREE: "免费号",
        phonenumbers.PhoneNumberType.VOIP: "VOIP",
        phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "个人号",
        phonenumbers.PhoneNumberType.PAGER: "寻呼机",
        phonenumbers.PhoneNumberType.UAN: "UAN",
        phonenumbers.PhoneNumberType.UNKNOWN: "未知",
    }
    return {
        "ok": True,
        "input": number,
        "e164": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164),
        "national": phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL),
        "country_code": p.country_code,
        "region": phonenumbers.region_code_for_number(p),
        "valid": valid,
        "possible": possible,
        "type": type_map.get(ntype, "未知"),
        "carrier": carrier.name_for_number(p, "zh") or None,
        "location": geocoder.description_for_number(p, "zh") or None,
        "source": "libphonenumber (Google, 内置号段库, 离线)",
        "note": "纯本地解析：归属地/运营商来自号段库，城市级；不含任何个人实名信息。",
    }


# ----------------------------------------------------------- 跨平台昵称存在性枚举（身份级）

# 主流平台的「用户主页/查找」入口模板。存在性枚举 = 构造 URL 看 HTTP 状态，
# 本质就是「用平台自带的搜索/查找功能」，不爬不建库。
_HANDLE_TEMPLATES = {
    "github": "https://github.com/{name}",
    "twitter": "https://twitter.com/{name}",
    "telegram": "https://t.me/{name}",
    "instagram": "https://instagram.com/{name}",
    "reddit": "https://www.reddit.com/user/{name}",
    "bilibili": "https://space.bilibili.com/{name}",
    "youtube": "https://www.youtube.com/@{name}",
    "medium": "https://medium.com/@{name}",
    "keybase": "https://keybase.io/{name}",
    "gitlab": "https://gitlab.com/{name}",
}

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _check_one(base, name, timeout):
    url = base.format(name=name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with _OPENER.open(req, timeout=timeout) as r:
            st = r.status
        # 200/30x = 可能存在；401/403 通常是「存在但私有」也算命中；404 = 大概率无
        hit = st in (200, 301, 302, 401, 403)
        return {"url": url, "status": st, "exists": hit}
    except urllib.error.HTTPError as e:
        hit = e.code in (401, 403)
        return {"url": url, "status": e.code, "exists": hit}
    except Exception as e:
        return {"url": url, "status": None, "exists": False, "err": str(e)[:80]}


def _handle(name, platforms=None, timeout=12):
    """跨平台昵称存在性枚举（利用各平台既有查找入口）。纯公开请求，不爬不建库。

    platforms: 逗号分隔平台名（见 _HANDLE_TEMPLATES）；空 = 全部。
    诚实边界：HTTP 200 是「该用户名对应的页面返回了内容」，不等于「一定有人」——
    部分平台对不存在的用户也返回 200 的占位页。据此判定存在时标注 confidence。
    """
    if not name:
        return {"ok": False, "err": "name 不能为空"}
    wanted = ([s.strip().lower() for s in platforms.split(",") if s.strip()]
              if platforms else list(_HANDLE_TEMPLATES.keys()))
    results = []
    for plat in wanted:
        tpl = _HANDLE_TEMPLATES.get(plat)
        if not tpl:
            results.append({"platform": plat,
                            "error": "未知平台（可选: " + "/".join(_HANDLE_TEMPLATES.keys()) + ")"})
            continue
        r = _check_one(tpl, name, timeout)
        r["platform"] = plat
        results.append(r)
    found = [r["platform"] for r in results if r.get("exists")]
    return {
        "ok": True, "handle": name, "checked": len(results),
        "found_on": found, "found_count": len(found),
        "details": results,
        "source": "平台公开主页/查找入口（存在性枚举）",
        "note": "200/301/302/401/403 视为可能存在；404 视为大概率无。部分平台对不存在用户也返 200 占位页，结果供参考。",
    }


# ----------------------------------------------------------- IP 公开地理情报（身份级）

def _ip_intel(ip):
    """IP 公开地理情报（城市级，免费源）。轻量、被动。"""
    try:
        data = _get_json(f"https://ipwho.is/{ip}", timeout=12)
    except Exception as e:
        return {"ok": False, "err": f"IP 查询失败: {e}"}
    if not data.get("success", True):
        return {"ok": False, "err": data.get("message") or "IP 查询被拒"}
    return {"ok": True, "ip": ip,
            "city": data.get("city"), "region": data.get("region"),
            "country": data.get("country"),
            "org": (data.get("connection") or {}).get("org"),
            "source": "ipwho.is (城市级, 免费)",
            "note": "城市/机房级；非实时精确定位，需运营商级权限才能做到。"}


# ----------------------------------------------------------- 多标识关联拼装（身份级）

def _identity(phone=None, handle=None, email=None, ip=None,
              handle_platforms=None, handle_timeout=12):
    """多标识关联拼装：把手机号/昵称/邮箱/IP 各自的公开查询结果，聚合成一个
    「数字身份公开足迹画像」。关键——**不建库**，每个标识都现查现拼；
    且**不做跨源实体消歧**（同名可能指向不同人，需要更多证据才能确认同一实体）。

    这是定位层「身份级」的主入口：你给一组标识，它借各平台既有查询入口分别取数，
    再按标识维度拼起来。AI 下游可据 correlation 提示决定是否进一步核实。
    """
    provided_map = {"phone": phone, "handle": handle, "email": email, "ip": ip}
    parts = {}
    if phone:
        parts["phone"] = _phone(phone)
    if handle:
        parts["handle"] = _handle(handle, platforms=handle_platforms, timeout=handle_timeout)
    if ip:
        parts["ip"] = _ip_intel(ip)
    if email:
        # 邮箱本身不在此处做第三方查重（有风控风险）；仅登记，供下游/人工核实。
        parts["email"] = {"ok": True, "email": email,
                          "note": "邮箱仅登记；跨平台注册查询需单独授权，此处不自动执行。"}
    return {
        "ok": True,
        "provided": [k for k, v in provided_map.items() if v],
        "identity": parts,
        "correlation": "各标识独立查询后按维度拼装；未做跨源实体消歧——"
                       "同名/同号可能指向不同人，确认同一实体需更多关联证据。",
        "source": "各平台公开查询入口 + 本地号段库（不建库）",
    }


# ----------------------------------------------------------- 历史快照（轻量，非库）
# 主人 11-02 方法论：不做「建库」，而是「使用即拼接」；拼接完留一份轻量快照，
# 下次查时读上一次快照做对比，数据变动时据此走，更准。这是运行态快照，不是囤积库。
_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", "osint_history.jsonl")


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_history(key, snapshot):
    try:
        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
        with open(_HISTORY_PATH, "a", encoding="utf-8") as f:
            rec = {"key": key, "ts": _now_iso()}
            rec.update(snapshot)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_history(key):
    try:
        if not os.path.exists(_HISTORY_PATH):
            return None
        last = None
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("key") == key:
                    last = rec
        return last
    except Exception:
        return None


# ----------------------------------------------------------- 暴露面评测（资产暴露面·A-F 评级）
# 内化 OpenOSINT exposure 的评分模型（A-F，critical 压 D），但全用免费公开源，不依赖要 key 的第三方。
# 评 4 类被动检查：dns_auth（SPF/DMARC/DKIM）/ registration（注册与隐私）/ tls（证书）/ http_headers（安全头）。
_SEV_PENALTY = {"critical": 40, "high": 20, "medium": 10, "low": 4, "info": 0}
_CAT_MULT = {"dns_auth": 1.0, "tls": 1.0, "registration": 0.75, "http_headers": 0.5}
_CAT_CAP = {"http_headers": 10, "registration": 20}
_GRADE_FLOOR = (("A", 90), ("B", 80), ("C", 65), ("D", 50))
_CRIT_CEIL = "D"
_REQUIRED = ("dns_auth", "tls")
_GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}


def _grade(score):
    for g, f in _GRADE_FLOOR:
        if score >= f:
            return g
    return "F"


def _F(fid, cat, sev, title, evidence, rec):
    return {"id": fid, "category": cat, "severity": sev, "title": title,
            "evidence": evidence, "recommendation": rec}


def _opener_with_proxy():
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _get_json_proxy(url, timeout=25, headers=None):
    opener = _opener_with_proxy()
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "fleet-osint/1.0",
                                                          "Accept": "application/dns-json"})
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _exp_dns_auth(domain, timeout):
    findings = []
    try:
        data = _get_json_proxy(f"https://dns.google/resolve?name={domain}&type=TXT", timeout=timeout)
        txts = []
        for ans in (data.get("Answer") or []):
            if ans.get("type") == 16:
                raw = str(ans.get("data", ""))
                for chunk in raw.split('"'):
                    chunk = chunk.strip()
                    if chunk:
                        txts.append(chunk)
    except Exception as e:
        return [_F("dns_query_failed", "dns_auth", "info", "DNS TXT 查询失败",
                   str(e)[:80], "检查出网/域名是否解析")]
    spf = [t for t in txts if t.lower().startswith("v=spf1")]
    dmarc = [t for t in txts if t.lower().startswith("v=dmarc1")]
    dkim = [t for t in txts if t.lower().startswith("v=dkim1")]
    if not spf:
        findings.append(_F("spf_missing", "dns_auth", "high", "缺 SPF 记录",
                           "无 v=spf1 TXT", "添加 SPF 防止邮件伪造"))
    elif "+all" in spf[0].lower():
        findings.append(_F("spf_plus_all", "dns_auth", "critical", "SPF 含 +all（允许任意伪造）",
                           spf[0][:80], "改为 ~all 或 -all"))
    if not dmarc:
        findings.append(_F("dmarc_missing", "dns_auth", "high", "缺 DMARC 记录",
                           "无 v=dmarc1 TXT", "添加 DMARC p=quarantine/none 起步"))
    if not dkim:
        findings.append(_F("dkim_not_found", "dns_auth", "info", "未发现 DKIM 记录",
                           "常见 selector 未命中", "部署 DKIM 签名"))
    if not findings:
        findings.append(_F("dns_auth_ok", "dns_auth", "info", "SPF/DMARC 配置存在", "", "保持"))
    return findings


def _exp_registration(domain, timeout):
    from datetime import datetime
    findings = []
    try:
        data = _get_json_proxy(f"https://rdap.org/domain/{domain}", timeout=timeout)
    except Exception as e:
        return [_F("rdap_query_failed", "registration", "info", "RDAP 查询失败",
                   str(e)[:80], "检查出网")]
    if data.get("error") or data.get("status") == 404:
        return [_F("domain_not_registered", "registration", "info", "域名未注册/RDAP 无记录",
                   "", "确认域名拼写")]
    expire_dt = None
    for ev in (data.get("events") or []):
        if ev.get("eventAction") in ("expiration", "registrar expiration"):
            try:
                expire_dt = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
            except Exception:
                pass
    if expire_dt:
        days = (expire_dt - datetime.now(expire_dt.tzinfo)).days
        if days < 0:
            findings.append(_F("domain_expired", "registration", "critical", "域名已过期",
                               f"过期 {abs(days)} 天", "立即续费"))
        elif days < 30:
            findings.append(_F("domain_expires_lt_30d", "registration", "high", "30 天内过期",
                               f"剩 {days} 天", "尽快续费"))
        elif days < 60:
            findings.append(_F("domain_expires_lt_60d", "registration", "medium", "60 天内过期",
                               f"剩 {days} 天", "安排续费"))
    entities = data.get("entities") or []
    privacy = any("privac" in (e.get("roles") or []) or "redact" in json.dumps(e, ensure_ascii=False).lower()
                 for e in entities)
    if not privacy:
        findings.append(_F("no_privacy", "registration", "info", "注册信息未启用隐私保护",
                           "WHOIS 可能暴露注册人", "考虑注册商隐私保护服务"))
    if not findings:
        findings.append(_F("registration_ok", "registration", "info", "注册信息正常", "", "保持"))
    return findings


def _exp_tls(domain, timeout):
    import ssl, socket
    from datetime import datetime
    findings = []
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((domain, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
        not_after = None
        na = cert.get("notAfter")
        if isinstance(na, str):
            try:
                not_after = datetime.strptime(na, "%b %d %H:%M:%S %Y %Z")
                not_after = not_after.replace(tzinfo=datetime.now().astimezone().tzinfo)
            except Exception:
                pass
        san = cert.get("subjectAltName") or []
        names = [v for k, v in san if k == "DNS"]
        match = domain in names or any(n.startswith("*.") and domain.endswith(n[2:]) for n in names)
        if not match:
            findings.append(_F("tls_hostname_mismatch", "tls", "critical", "证书主机名不匹配",
                               str(san)[:80], "使用匹配域名的证书"))
        if not_after:
            days = (not_after - datetime.now(not_after.tzinfo)).days
            if days < 0:
                findings.append(_F("tls_cert_expired", "tls", "critical", "证书已过期",
                                   f"过期 {abs(days)} 天", "续签证书"))
            elif days < 14:
                findings.append(_F("tls_cert_expires_lt_14d", "tls", "high", "14 天内过期",
                                   f"剩 {days} 天", "尽快续签"))
            elif days < 30:
                findings.append(_F("tls_cert_expires_lt_30d", "tls", "medium", "30 天内过期",
                                   f"剩 {days} 天", "安排续签"))
    except ssl.SSLCertVerificationError as e:
        findings.append(_F("tls_cert_invalid_chain", "tls", "high", "证书校验失败",
                           str(e)[:80], "修复证书链"))
    except (ssl.SSLError, OSError) as e:
        findings.append(_F("tls_unreachable_443", "tls", "info", "443 无法建立 TLS",
                           str(e)[:80], "确认是否提供 HTTPS 服务"))
    if not findings:
        findings.append(_F("tls_ok", "tls", "info", "TLS 配置正常", "", "保持"))
    return findings


def _exp_http_headers(domain, timeout):
    findings = []
    try:
        opener = _opener_with_proxy()
        req = urllib.request.Request(f"https://{domain}", headers={"User-Agent": "fleet-osint/1.0"})
        with opener.open(req, timeout=timeout) as r:
            hdrs = {k.lower(): v for k, v in r.headers.items()}
    except Exception as e:
        return [_F("http_unreachable", "http_headers", "info", "HTTPS 不可达",
                   str(e)[:80], "确认是否提供 Web 服务")]
    checks = [
        ("strict-transport-security", "hsts_missing", "low", "缺 HSTS", "启用 HSTS"),
        ("content-security-policy", "csp_missing", "low", "缺 CSP", "添加内容安全策略"),
        ("x-content-type-options", "x_content_type_options_missing", "low", "缺 X-Content-Type-Options", "设为 nosniff"),
        ("x-frame-options", "frame_protection_missing", "low", "缺 X-Frame-Options", "设为 DENY/SAMEORIGIN"),
    ]
    for hname, fid, sev, title, rec in checks:
        if hname not in hdrs:
            findings.append(_F(fid, "http_headers", sev, title, f"响应头无 {hname}", rec))
    if not findings:
        findings.append(_F("http_headers_ok", "http_headers", "info", "安全头齐备", "", "保持"))
    return findings


def _exposure(domain, history=False, timeout=20):
    """域名暴露面评测（A-F 评级）。内化 OpenOSINT exposure 的评分模型，全用免费公开源。
    history: 读上一次快照对比（主人方法论：使用即拼接，留历史，下次据变动走更准）。
    仅查你拥有/授权的域名。
    """
    domain = domain.strip().lower().split("/")[0].split(":")[0]
    cats = {
        "dns_auth": _exp_dns_auth(domain, timeout),
        "registration": _exp_registration(domain, timeout),
        "tls": _exp_tls(domain, timeout),
        "http_headers": _exp_http_headers(domain, timeout),
    }
    totals = {}
    all_findings = []
    for cat, fs in cats.items():
        for f in fs:
            all_findings.append(f)
            pen = _SEV_PENALTY[f["severity"]] * _CAT_MULT.get(cat, 1.0)
            totals[cat] = totals.get(cat, 0.0) + pen
    penalty = sum(min(t, _CAT_CAP.get(cat, t)) for cat, t in totals.items())
    score = max(0, round(100 - penalty))
    if set(cats.keys()) >= set(_REQUIRED):
        grade = _grade(score)
    else:
        grade = "incomplete"
    has_crit = any(f["severity"] == "critical" for f in all_findings)
    capped = has_crit and grade != "incomplete" and _GRADE_RANK[grade] < _GRADE_RANK[_CRIT_CEIL]
    if capped:
        grade = _CRIT_CEIL
    prev = _load_history("exposure:" + domain) if history else None
    delta = None
    if prev:
        prev_ids = {f.get("id") for f in (prev.get("findings") or [])}
        cur_ids = {f.get("id") for f in all_findings}
        delta = {"prev_ts": prev.get("ts"), "new": sorted(cur_ids - prev_ids),
                 "resolved": sorted(prev_ids - cur_ids)}
    _save_history("exposure:" + domain, {
        "domain": domain, "score": score, "grade": grade,
        "findings": [{"id": f["id"], "severity": f["severity"]} for f in all_findings],
    })
    return {
        "ok": True, "domain": domain, "score": score, "grade": grade,
        "capped_by_critical": capped, "findings": all_findings, "categories": cats,
        "source": "免费公开源（DoH/rdap.org/socket TLS/HTTP 头），内化 OpenOSINT exposure 评分模型",
        "note": "A≥90 B≥80 C≥65 D≥50；critical 发现压到 D。仅查你拥有/授权的域名。",
        "prev": ({"ts": prev.get("ts"), "score": prev.get("score"), "grade": prev.get("grade")} if prev else None),
        "delta": delta,
    }


# ----------------------------------------------------------- 离线自测

def _selftest():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    checks, fails = [], []

    def check(name, cond):
        checks.append(name)
        if not cond:
            fails.append(name)

    # 暴露面评分边界（不联网）
    check("exposure 评分边界", _grade(95) == "A" and _grade(85) == "B"
          and _grade(60) == "D" and _grade(40) == "F")

    # 手机号本地解析（离线，不联网）
    rp = _phone("+8613800138000")
    check("phone 离线解析", rp.get("ok") and rp.get("type") == "手机" and bool(rp.get("carrier")))
    check("phone 无效号识别", not _phone("12345").get("valid"))

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
