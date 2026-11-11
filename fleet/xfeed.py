"""X(Twitter) 内容抓取 —— 走公开 syndication 通道，不需要登录、不需要 cookie。

为什么要这个模块
----------------
2026 年 X 官方 API 已经没有免费读额度（$0.005/条），Nitter 也被法务逼停。
但 X 自己给**站外嵌入**留了两个公开接口，不需要任何认证：

    /tweet-result?id=<id>&token=<t>      单条推文，完整正文
    /srv/timeline-profile/screen-name/<u> 账号时间线，一次约 100 条

token 不是密钥，是 id 的公开换算：
    token = base36(floor(id/1e15*pi)) 去掉所有 0 和小数点

能力边界
--------
- **账号时间线**：拿到任意公开账号最近约 100 条，无需登录  ← 扒资源主力
- **单条详情**：知道 id 就能拿完整正文
- **关键词搜索**：**拿不到**。X 把搜索锁在登录墙后面，只能走内置浏览器
  （已登录的会话），本模块不碰这条路

用法
----
    from fleet import xfeed
    tw = xfeed.tweet("2117001357672825165")
    ts = xfeed.timeline("huggingface", limit=50)
    xfeed.pick_links(ts)        # 只看带外链的
"""

from __future__ import annotations

import json
import math
import re
import ssl
import time
import urllib.error
import urllib.request

__all__ = ["tweet", "timeline", "pick_links", "token", "tweet_id_from_url",
           "search_url", "build_query", "PRESETS"]

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"}
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _ctx():
    return ssl.create_default_context()


def token(tweet_id) -> str:
    """id → syndication 需要的 token。JS 原式：
    ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\\.)/g, '')
    """
    v = (int(tweet_id) / 1e15) * math.pi
    ip = int(v)
    frac = v - ip
    s = ""
    while ip:
        ip, r = divmod(ip, 36)
        s = _DIGITS[r] + s
    s = s or "0"
    if frac > 0:
        f = ""
        for _ in range(24):
            frac *= 36
            d = int(frac)
            f += _DIGITS[d]
            frac -= d
            if frac <= 0:
                break
        s += "." + f
    return re.sub(r"0+|\.", "", s)


def tweet_id_from_url(url: str) -> str:
    m = re.search(r"/status/(\d{10,25})", url or "")
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 搜索查询构造
# X 搜索默认就是按最新排序，不需要额外设置。决定结果质量的是 min_faves /
# filter:links —— 不加这两个，搜出来前排全是零粉小号。

PRESETS = {
    # 带外链的高质量帖，扒资源主力
    "links": 'filter:links min_faves:{faves} -filter:replies',
    # 有人在推荐 / 在找的东西
    "ask": '("looking for" OR "recommend" OR "anyone know")',
    # 高赞精选
    "top": "min_faves:{faves} -filter:replies",
    # 只找链到 GitHub 的
    "github": "filter:links url:github.com min_faves:{faves}",
}


def build_query(keyword: str, preset: str = "", faves: int = 20,
                since: str = "", until: str = "", lang: str = "en") -> str:
    """按预设拼一条 X 查询串。since/until 用 YYYY-MM-DD。"""
    parts = []
    if preset and preset in PRESETS:
        p = PRESETS[preset]
        if "{faves}" in p:
            p = p.format(faves=faves)
        if preset == "ask":
            parts.append(p)
            parts.append(keyword)
        else:
            parts.append(keyword)
            parts.append(p)
    else:
        parts.append(keyword)
    if since:
        parts.append("since:" + since)
    if until:
        parts.append("until:" + until)
    if lang:
        parts.append("lang:" + lang)
    return " ".join(x for x in parts if x)


def search_url(keyword: str, preset: str = "", faves: int = 20,
               since: str = "", until: str = "", lang: str = "en",
               latest: bool = True) -> str:
    """生成可直接在浏览器打开的搜索地址。latest=True 走 Latest 标签。"""
    from urllib.parse import quote
    q = build_query(keyword, preset, faves, since, until, lang)
    return "https://x.com/search?q=%s&src=typed_query%s" % (
        quote(q, safe=""), "&f=live" if latest else "")



def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return r.read().decode("utf-8", "replace")


def expand_tco(url: str, timeout: int = 6) -> str:
    """展开 t.co 短链 → 真实地址。

    实测坑：syndication 的 entities.urls 经常是空的，正文里却留着
    `https://t.co/xxxx`。不展开就永远看不到资源链接。跟随重定向即可，
    不读 body，省流量。
    """
    if "t.co/" not in (url or ""):
        return url or ""
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            final = r.geturl()
        return "" if final.startswith("https://t.co/") else final
    except Exception:
        return ""



def _norm(d: dict) -> dict:
    """把 syndication 返回的推文对象整成统一结构。"""
    u = d.get("user") or {}
    ents = d.get("entities") or {}
    urls = []
    for x in (ents.get("urls") or []):
        if x.get("expanded_url"):
            urls.append(x["expanded_url"])
    photos = [p.get("media_url_https") for p in ((d.get("mediaDetails") or []))]
    return dict(
        id=str(d.get("id_str") or d.get("id") or ""),
        author=u.get("screen_name") or "",
        name=u.get("name") or "",
        followers=u.get("followers_count") or 0,
        created=d.get("created_at") or "",
        text=d.get("text") or "",
        urls=urls,
        photos=[p for p in photos if p],
        likes=d.get("favorite_count") or 0,
        replies=d.get("conversation_count") or 0,
        lang=d.get("lang") or "",
        url="https://x.com/%s/status/%s" % (u.get("screen_name") or "i",
                                            d.get("id_str") or d.get("id") or ""),
    )


def tweet(tweet_id, retries: int = 2) -> dict:
    """单条推文的完整内容。失败返回 {}。"""
    tid = str(tweet_id)
    url = ("https://cdn.syndication.twimg.com/tweet-result"
           "?id=%s&token=%s&lang=en" % (tid, token(tid)))
    for i in range(retries + 1):
        try:
            return _norm(json.loads(_get(url)))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {}
            time.sleep(0.6 * (i + 1))
        except Exception:
            time.sleep(0.6 * (i + 1))
    return {}


def timeline(screen_name: str, limit: int = 100) -> list:
    """账号最近推文（约 100 条上限，接口本身限制，不是分页）。

    不需要登录。screen_name 不带 @。
    """
    name = screen_name.lstrip("@")
    url = "https://syndication.twitter.com/srv/timeline-profile/screen-name/" + name
    try:
        html = _get(url, timeout=25)
    except Exception as e:
        return [dict(_error="%s: %s" % (type(e).__name__, str(e)[:80]))]
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return [dict(_error="页面结构变了，没找到 __NEXT_DATA__（长度 %d）" % len(html))]
    try:
        data = json.loads(m.group(1))
        entries = data["props"]["pageProps"]["timeline"]["entries"]
    except Exception as e:
        return [dict(_error="解析失败：%s" % e)]

    out = []
    for ent in entries:
        c = ent.get("content") or {}
        tw = c.get("tweet") or c.get("itemContent", {}).get("tweet_results", {}).get("result")
        if not tw:
            continue
        # 新结构可能是 tweet_results.result.legacy
        if "legacy" in tw and "full_text" in tw.get("legacy", {}):
            lg = tw["legacy"]
            core = (tw.get("core") or {}).get("user_results", {}).get("result", {}).get("legacy", {})
            merged = dict(
                id_str=lg.get("id_str") or tw.get("rest_id"),
                text=lg.get("full_text") or "",
                created_at=lg.get("created_at") or "",
                favorite_count=lg.get("favorite_count") or 0,
                conversation_count=lg.get("reply_count") or 0,
                lang=lg.get("lang") or "",
                user=dict(screen_name=core.get("screen_name") or "",
                          name=core.get("name") or "",
                          followers_count=core.get("followers_count") or 0),
                entities=lg.get("entities") or {},
                mediaDetails=((lg.get("extended_entities") or {}).get("media") or []),
            )
            out.append(_norm(merged))
        else:
            out.append(_norm(tw))
        if len(out) >= limit:
            break
    return out


def pick_links(tweets, only_external: bool = True) -> list:
    """筛出带外链的推文，外链去掉 t.co 展开后的原地址。"""
    res = []
    for t in tweets:
        if "_error" in t:
            continue
        urls = [u for u in t.get("urls", [])]
        if only_external:
            urls = [u for u in urls if "x.com/" not in u and "twitter.com/" not in u]
        if urls:
            res.append(dict(author=t["author"], created=t["created"],
                            text=t["text"], urls=urls, url=t["url"],
                            likes=t.get("likes", 0)))
    return res


def _main(argv):
    import sys
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "q" and len(argv) > 1:
        # q <keyword> [preset] [faves] [since]
        kw = argv[1]
        preset = argv[2] if len(argv) > 2 else ""
        faves = int(argv[3]) if len(argv) > 3 else 20
        since = argv[4] if len(argv) > 4 else ""
        print("查询串:", build_query(kw, preset, faves, since))
        print("地址  :", search_url(kw, preset, faves, since))
        return 0
    if cmd == "tweet" and len(argv) > 1:
        t = tweet(tweet_id_from_url(argv[1]) or argv[1])
        print(json.dumps(t, ensure_ascii=False, indent=1) if t else "拿不到")
        return 0 if t else 1
    if cmd == "timeline" and len(argv) > 1:
        limit = int(argv[2]) if len(argv) > 2 else 100
        ts = timeline(argv[1], limit)
        if ts and "_error" in ts[0]:
            print("失败：", ts[0]["_error"])
            return 1
        for t in ts:
            print("%s | %s | %s" % (t["created"][:16], "@" + t["author"],
                                    t["text"][:90].replace("\n", " ")))
        print("\n共 %d 条" % len(ts))
        return 0
    if cmd == "links" and len(argv) > 1:
        limit = int(argv[2]) if len(argv) > 2 else 100
        for x in pick_links(timeline(argv[1], limit)):
            print("%s | @%s" % (x["created"][:16], x["author"]))
            print("   " + " ".join(x["urls"]))
            print("   " + x["text"][:100].replace("\n", " "))
        return 0
    print("未知命令：%s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
