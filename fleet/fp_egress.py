"""出口体检：我现在到底从哪根线出去、落在哪个国家。

为什么这个模块必须存在
----------------------
沙箱有一条最狠的规则：**出口漂移 = 历史作废**。
下面这两句话是同一件事的两面：

    · profile 里攒的 cookie/历史，是给「某个国家的人」攒的
    · 如果出口换成另一个国家，对方看到的是「同一台机器突然搬家」

后者比一个全新访客危险得多。所以 `fp_sandbox.check()` 里那条 `egress_drift`
是养号唯一的死因 —— 而它能不能生效，**全看这里能不能自动探**。
靠人记得传 live_country，等于没有这条规则。

三条纪律
--------
1. **探不出来不许当没问题。** 网络不通时返回 ok=False，
   调用方必须把它当成「未知」单独报，不能因为「没发现不一致」就放行。
2. **强制指定代理，不吃环境变量。** 本机注入过 HTTP_PROXY=127.0.0.1:3067，
   urllib 默认会吃到它 —— 那会让「探出口」探成一个谁都不知道的线路。
   所以永远显式传 ProxyHandler，直连就传 `{}`。
3. **两个源互为冗余。** 一家挂了换另一家，全挂才报错。
   按主人的规矩：真正的价值在供货源冗余度，不在调度技巧。
"""

from __future__ import annotations

import json
import urllib.request

# 两个源：(名字, URL, 归一化函数)。字段名各家不同，归一到 ip/country/asn/isp。
_SOURCES = (
    ("ifconfig.co", "https://ifconfig.co/json",
     lambda d: {"ip": d.get("ip"), "country": d.get("country_iso") or d.get("country"),
                "asn": d.get("asn"), "isp": d.get("asn_org"), "city": d.get("city")}),
    ("ip-api.com", "http://ip-api.com/json/?fields=status,country,countryCode,isp,as,query",
     lambda d: {"ip": d.get("query"), "country": d.get("countryCode"),
                "asn": d.get("as"), "isp": d.get("isp"), "city": d.get("city")}),
)

# 有些站会看 UA，给一个最常见、最不像脚本的
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


def _json(url, timeout, proxy):
    """取 JSON。proxy 为 None 表示直连（**显式**直连，不吃环境变量）。"""
    if proxy:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "application/json"})
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def resolve_proxy():
    """按家底选路。失败就返回 (None, 原因)，直连也返回 None。"""
    try:
        from . import net as N
        return N.resolve_proxy()
    except Exception as e:
        return None, "net.resolve_proxy 不可用（%s）→ 直连" % e


def probe(timeout=8, proxy="auto"):
    """探一次出口。

    返回 {'ok': True, 'ip', 'country', 'asn', 'isp', 'city', 'source', 'proxy', 'why'}
      或 {'ok': False, 'error': '...', 'proxy': ...}

    **ok=False 不等于「没问题」** —— 它意味着不知道。调用方必须区别对待。
    """
    if proxy == "auto":
        proxy, why = resolve_proxy()
    else:
        why = "调用方指定 %s" % (proxy or "直连")
    errors = []
    for name, url, parse in _SOURCES:
        try:
            d = _json(url, timeout, proxy)
            r = parse(d or {})
            if r.get("ip"):
                r["ok"] = True
                r["country"] = (r.get("country") or "").upper() or None
                r["source"] = name
                r["proxy"] = proxy
                r["why"] = why
                return r
            errors.append("%s: 返回里没有 ip（%r）" % (name, str(d)[:100]))
        except Exception as e:
            errors.append("%s: %s" % (name, e))
    return {"ok": False, "error": "; ".join(errors), "proxy": proxy, "why": why}


def looks_residential(asn=None, isp=None):
    """粗判这条线是住宅还是机房。

    只做**粗判**，不装精确：判据是 ISP 名里有没有云厂商字样。
    判不出来的返回 None（不知道就说不知道，不猜）。
    """
    s = ("%s %s" % (asn or "", isp or "")).lower()
    if not s.strip():
        return None
    cloud = ("amazon", "aws", "google", "microsoft", "azure", "alibaba", "aliyun",
             "tencent", "huawei cloud", "digitalocean", "linode", "vultr",
             "hetzner", "ovh", "oracle", "cloudflare", "akamai", "fastly",
             "choopa", "leaseweb", "contabo", "hostinger", "hostwinds")
    return "datacenter" if any(k in s for k in cloud) else "residential"


def _selftest():
    """离线自测：不联网，只验归一化与粗判。真探一次用 `python -m fleet.fp_egress`。"""
    okc = failc = 0

    def chk(cond, msg):
        nonlocal okc, failc
        if cond:
            okc += 1
        else:
            failc += 1
            print("  FAIL:", msg)

    # 两家源的字段名不同，归一化必须都能出 ip + country
    for name, url, parse in _SOURCES:
        r = parse({"ip": "1.2.3.4", "country_iso": "JP", "country": "Japan",
                   "countryCode": "JP", "asn": 2516, "as": "AS2516 KDDI",
                   "asn_org": "KDDI", "isp": "KDDI", "query": "1.2.3.4", "city": "Tokyo"})
        chk(r.get("ip") == "1.2.3.4", "%s 归一化后应有 ip" % name)
        chk(r.get("country") == "JP", "%s 归一化后应有 country=JP，实得 %r" % (name, r.get("country")))

    # 住宅 / 机房 粗判
    chk(looks_residential("AS16509 Amazon.com, Inc.", "Amazon") == "datacenter", "AWS 应判机房")
    chk(looks_residential("AS2516 KDDI CORPORATION", "KDDI") == "residential", "KDDI 应判住宅")
    chk(looks_residential("AS4134 CHINANET", "China Telecom") == "residential", "电信应判住宅")
    chk(looks_residential("AS45090 Shenzhen Tencent", "Tencent") == "datacenter", "腾讯云应判机房")
    chk(looks_residential(None, None) is None, "什么都不知道时必须返回 None，不许猜")

    # 直连分支不许吃环境变量：源码里必须**显式**构造空 ProxyHandler。
    # 不用运行时检查 —— 那种检查依赖 build_opener 的内部结构，
    # 换个 python 版本就可能假红假绿。这里直接钉代码形状，跟 t69 的做法一致。
    import io
    import os as _os
    src = io.open(_os.path.abspath(__file__), encoding="utf-8").read()
    chk('ProxyHandler({"http": proxy, "https": proxy})' in src,
        "走代理时必须显式构造 ProxyHandler（否则 urllib 会去读环境变量）")
    chk("ProxyHandler({})" in src,
        "直连时必须传空 ProxyHandler —— 本机注入过 HTTP_PROXY=127.0.0.1:3067，"
        "不显式传空就会探成一个谁都不知道的线路")

    print("fp_egress 自测：%d 过 / %d 败" % (okc, failc))
    return failc == 0


def main():
    import sys
    if "--selftest" in sys.argv:
        return 0 if _selftest() else 1
    r = probe()
    if r.get("ok"):
        print("出口 %s（%s）" % (r["ip"], r["country"]))
        print("  ASN/ISP : %s / %s" % (r.get("asn"), r.get("isp")))
        print("  城市    : %s" % r.get("city"))
        print("  线路    : %s（%s）" % (r.get("proxy") or "直连", r.get("why")))
        print("  线路性质: %s" % (looks_residential(r.get("asn"), r.get("isp")) or "判不出来"))
        print("  取自    : %s" % r["source"])
        return 0
    print("探不出来（**这不等于没问题，等于不知道**）：")
    print("  " + r.get("error", ""))
    print("  线路: %s" % (r.get("why") or ""))
    return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
