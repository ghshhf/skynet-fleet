# -*- coding: utf-8 -*-
"""GitHub 代码挖域名后端：从全站代码里挖自建站 / NAS / 免费主机域名。

依赖 GitHub 代码搜索 API。无 token 也能跑（限流更严）；若环境变量有
NF_PAT / GITHUB_TOKEN / GH_TOKEN / OFM_KEY 则带上，抬高限流、解锁更多结果。
纯标准库，失败返回 []。

典型命中：sfx:duckdns.org 一次挖出 18 个真实自建站（betahowto.duckdns.org 等）；
cfg:server_name（nginx 配置）泛挖自建站域名。
"""
import json
import os
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


def _token():
    for k in ("NF_PAT", "GITHUB_TOKEN", "GH_TOKEN", "OFM_KEY"):
        v = os.environ.get(k, "").strip()
        if v:
            return v
    return ""


def _get(url, timeout=25):
    h = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    t = _token()
    if t:
        h["Authorization"] = "Bearer " + t
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", "ignore")


def _parse_fragments(text):
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = set()
    for it in data.get("items", []):
        for tm in it.get("text_matches", []) or []:
            frag = tm.get("fragment") or ""
            for m in _HOST_RE.finditer(frag):
                out.add(m.group(1).lower())
    return sorted(out)


def gh_code_domains(suffix, limit=40):
    """按后缀挖代码里的域名（如 duckdns.org / pages.dev）。"""
    q = "%s in:file" % suffix
    try:
        url = ("https://api.github.com/search/code?q=%s&per_page=%d"
               % (urllib.parse.quote(q), min(limit * 3, 100)))
        return _parse_fragments(_get(url, timeout=25))[:limit]
    except Exception:
        return []


def gh_code_server_names(limit=40):
    """按 nginx server_name 指令泛挖自建站域名。"""
    q = "server_name in:file language:nginx"
    try:
        url = ("https://api.github.com/search/code?q=%s&per_page=%d"
               % (urllib.parse.quote(q), min(limit * 3, 100)))
        return _parse_fragments(_get(url, timeout=25))[:limit]
    except Exception:
        return []
