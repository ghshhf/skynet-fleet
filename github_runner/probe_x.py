"""从 GitHub Actions 的机器上打 X 的接口，看机房 IP 到底能不能用。

Kaggle 那边已经废了（未认证 = 无网）。这里的价值是换一个**出口 IP**再问一遍
同一个问题：

    这里的 IP 能不能拿到 X 的公开数据？

能，就把整条爬虫搬过来；不能，说明 X 只认住宅段，云端方案要重新设计。
只测三个真正要用的端点 + 两个对照组，几十秒出结果。
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
CTX = ssl.create_default_context()

TARGETS = [
    ("账号时间线", "https://syndication.twitter.com/srv/timeline-profile/screen-name/github"),
    ("单推补全", "https://cdn.syndication.twimg.com/tweet-result?id=2117001357672825165&token=8"),
    ("站点首页", "https://x.com/"),
    ("API 域", "https://api.x.com/2/tweets/search/recent?query=test"),
    ("对照 GitHub", "https://api.github.com/rate_limit"),
    ("对照 Google", "https://www.google.com/"),
]

TIMEOUT = 15


def probe(label, url):
    rec = {"label": label, "url": url, "code": None, "ms": None,
           "bytes": None, "err": None}
    t = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
            body = r.read(200000)
            rec["code"] = r.status
            rec["bytes"] = len(body)
            rec["ms"] = round((time.time() - t) * 1000)
    except urllib.error.HTTPError as e:
        rec["code"] = e.code
        rec["ms"] = round((time.time() - t) * 1000)
        rec["err"] = "http_error"
    except Exception as e:
        rec["ms"] = round((time.time() - t) * 1000)
        rec["err"] = type(e).__name__
    return rec


def main():
    print("出口 IP:", end=" ", flush=True)
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            print(r.read().decode().strip(), flush=True)
    except Exception as e:
        print("取不到 (%s)" % type(e).__name__, flush=True)

    rows = []
    for label, url in TARGETS:
        rec = probe(label, url)
        rows.append(rec)
        state = ("HTTP %s" % rec["code"]) if rec["code"] else rec["err"]
        print("%-14s %-10s %6sms %8sB" % (label, state, rec["ms"], rec["bytes"]), flush=True)

    ok = sum(1 for r in rows if r["code"] == 200)
    print("汇总: %d/%d 返回 200" % (ok, len(rows)), flush=True)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_x.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
