# -*- coding: utf-8 -*-
"""天网 · 自建发现引擎：证书透明度 + Common Crawl + 互联网档案馆。

完全不经过搜索引擎——这是 resource_finder.selfindex_pump 背后真正干活的引擎。
纯标准库，失败一律返回 []（上游泵会 catch 并计入"零产出泵"自动降权）。

数据来源（均无需 API key）：
  · crt.sh          —— 证书透明度日志，按域名/后缀挖子域（含 *.cn 一次 2665 个）
  · Common Crawl    —— 全网快照索引，按关键词挖主机（搜索引擎难收录的新站）
  · Wayback (IA)    —— 互联网档案馆 CDX，按域名挖存档主机
"""
import json
import re
import ssl
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_HOST_RE = re.compile(
    r'https?://([\w.-]+\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2,})?)', re.I)


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", "ignore")


def _clean_domain(d):
    d = (d or "").strip().lower()
    if d.startswith("*."):
        d = d[2:]
    if d.startswith("."):
        d = d[1:]
    if " " in d or "/" in d or not d or "." not in d:
        return ""
    return d


# ---- crt.sh（证书透明度日志）----
def _parse_crtsh(text):
    try:
        arr = json.loads(text)
    except Exception:
        return []
    out = set()
    for it in arr if isinstance(arr, list) else []:
        nv = it.get("name_value") or ""
        for part in nv.replace("\n", ",").split(","):
            dom = _clean_domain(part)
            if dom:
                out.add(dom)
    return sorted(out)


def ct_subdomains(domain, limit=200):
    """crt.sh 按域名挖全部子域（含 *.cn 这类备案域名）。"""
    try:
        txt = _get("https://crt.sh/?q=%s&output=json" % urllib.parse.quote(domain),
                   timeout=25)
        return _parse_crtsh(txt)[:limit]
    except Exception:
        return []


def ct_suffix(suffix, limit=500):
    """crt.sh 按后缀挖（如 edu.cn / io / org）。"""
    q = "%." + suffix.lstrip("%.")
    try:
        txt = _get("https://crt.sh/?q=%s&output=json" % urllib.parse.quote(q),
                   timeout=25)
        return _parse_crtsh(txt)[:limit]
    except Exception:
        return []


# ---- Common Crawl（全网快照索引）----
def _cc_latest_index():
    try:
        info = json.loads(_get("https://index.commoncrawl.org/collinfo.json",
                               timeout=25))
        if isinstance(info, list) and info:
            return info[-1].get("cdx-api")
    except Exception:
        pass
    return "https://index.commoncrawl.org/CC-MAIN-2024-51-index"


def _parse_cc_lines(text):
    out = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        m = _HOST_RE.search(rec.get("url") or "")
        if m:
            out.add(m.group(1).lower())
    return sorted(out)


def ct_keyword(keyword, limit=200):
    """Common Crawl 按关键词挖主机（搜不到的新站，搜索引擎难收录）。"""
    try:
        idx = _cc_latest_index()
        url = "%s?url=%s&output=json&limit=%d" % (
            idx, urllib.parse.quote("*%s*" % keyword), min(limit, 1000))
        return _parse_cc_lines(_get(url, timeout=30))[:limit]
    except Exception:
        return []


# ---- 互联网档案馆 Wayback CDX ----
def _parse_wayback(text):
    try:
        rows = json.loads(text)
    except Exception:
        return []
    if not isinstance(rows, list) or not rows:
        return []
    if isinstance(rows[0], list):
        try:
            oi = rows[0].index("original")
        except Exception:
            oi = 2
        body = rows[1:]
    else:
        oi = 2
        body = rows
    out = set()
    for r in body:
        if not isinstance(r, list):
            continue
        orig = r[oi] if oi < len(r) else (r[2] if len(r) > 2 else "")
        m = _HOST_RE.search(orig or "")
        if m:
            out.add(m.group(1).lower())
    return sorted(out)


def archive_hosts(domain, limit=200):
    """互联网档案馆：挖某域名下所有存档主机。"""
    try:
        url = ("https://web.archive.org/cdx/search/cdx?url=%s/*&output=json"
               "&collapse=host&limit=%d" % (urllib.parse.quote(domain),
                                            min(limit, 1000)))
        return _parse_wayback(_get(url, timeout=30))[:limit]
    except Exception:
        return []
