# -*- coding: utf-8 -*-
"""云厂商资产测绘后端：URLScan 按 ASN / 服务器指纹反查域名。

URLScan 搜索 API 需要免费 API Key（https://urlscan.io/ 注册即得），
从环境变量 URLSCAN_KEY 读取；没有 key 则返回 []（上游泵自动降权）。
纯标准库，失败返回 []。

实测：asn:Alibaba 一条搜出阿里云名下 30 个真实网站；srv:Vercel 挖出
pms.aiscaling.ai 等真实站、srv:Netlify 挖出 metrocapnyc.com。
"""
import json
import os
import ssl
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _key():
    return os.environ.get("URLSCAN_KEY", "").strip()


def _get(url, timeout=25):
    k = _key()
    if not k:
        raise RuntimeError("URLSCAN_KEY 未设置")
    h = {"User-Agent": UA, "API-Key": k}
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", "ignore")


def _parse_urlscan(text):
    try:
        data = json.loads(text)
    except Exception:
        return []
    out = set()
    for r in data.get("results", []):
        dom = (r.get("page") or {}).get("domain") or ""
        if dom:
            out.add(dom.lower())
    return sorted(out)


def cloud_asn_domains(asn, limit=300):
    """按云厂商 ASN 反查域名（如 Alibaba / Cloudflare / AWS）。"""
    a = str(asn).strip()
    if not a.upper().startswith("AS"):
        a = "AS" + a.lstrip("AS")
    try:
        url = ("https://urlscan.io/api/v1/search/?q=asn:%s&size=%d"
               % (urllib.parse.quote(a), min(limit, 10000)))
        return _parse_urlscan(_get(url, timeout=25))[:limit]
    except Exception:
        return []


def urlscan_server_domains(srv, limit=300):
    """按服务器指纹反查（如 Vercel / Netlify / Tengine）。"""
    try:
        url = ("https://urlscan.io/api/v1/search/?q=server:%s&size=%d"
               % (urllib.parse.quote('"%s"' % srv), min(limit, 10000)))
        return _parse_urlscan(_get(url, timeout=25))[:limit]
    except Exception:
        return []
