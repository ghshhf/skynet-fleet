"""X → 爬虫 的衔接层（xhunt）
=================================
为什么要有这一层
----------------
内置浏览器是唯一带登录态的东西，但它只该干一件事：**过登录墙，把推文 ID 吐出来**。
截屏、反复刷新都太贵 —— 一次截屏是图像算力，一次 DOM 快照只是文本。

拿到 ID 之后全部走 HTTP（`xfeed.tweet`，公开 syndication 接口），
不进浏览器、不动屏幕、不烧视觉算力。

完整链路：

    内置浏览器搜一次
        ↓ dom_cua_get_visible_dom（文本快照，不是截图）
    ids_from_snapshot()        ← 正则抽 status ID
        ↓
    harvest()                  ← 并发补全正文（纯 HTTP）
        ↓
    keywords() / extract()     ← 抽关键词、域名、仓库、handle
        ↓
    喂给 netforce 爬虫         ← 关键词回流，去更大的池子里捞

关键设计：**ID 是唯一的交接物**。浏览器不传内容，只传 ID。

用法
----
命令行：

    python -m fleet.xhunt ids < snapshot.txt      # 从快照抽 ID
    python -m fleet.xhunt harvest 2117001357672825165 ...   # 补全 + 出清单
    python -m fleet.xhunt kw 2117001357672825165 ...        # 只看关键词

代码：

    from fleet import xhunt
    ids = xhunt.ids_from_snapshot(snapshot_text)
    tweets = xhunt.harvest(ids)
    print(xhunt.report(tweets))
"""
from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import xfeed

__all__ = ["ids_from_snapshot", "harvest", "keywords", "extract", "report",
           "to_queries", "STOP"]

# 推文 ID：17~20 位数字，出现在 /user/status/<id> 里
_STATUS = re.compile(r"/status/(\d{10,25})")
# 裸 ID（18~20 位，X 的 snowflake 当前是这个量级）
_BARE_ID = re.compile(r"(?<!\d)(2[01]\d{17})(?!\d)")

STOP = {
    "the", "and", "for", "with", "that", "this", "from", "have", "your", "you",
    "are", "was", "were", "not", "but", "can", "will", "just", "now", "all",
    "out", "get", "got", "how", "what", "who", "why", "when", "our", "their",
    "its", "it's", "about", "into", "than", "then", "them", "they", "there",
    "here", "more", "most", "some", "any", "use", "using", "used", "make",
    "made", "like", "need", "needs", "want", "way", "one", "two", "new",
    "https", "http", "www", "com", "t.co", "amp",
}

_DOMAIN = re.compile(r"https?://([a-z0-9][a-z0-9\-\.]*\.[a-z]{2,})", re.I)
_GH_REPO = re.compile(r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", re.I)
_HANDLE = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{2,15})")
_HASHTAG = re.compile(r"#([\w\u4e00-\u9fff]{2,40})")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")


def ids_from_snapshot(text: str) -> list:
    """从 DOM 快照里抽出推文 ID，去重保序。

    不截图也能拿到的东西：快照里每条推文都带 /user/status/<id> 链接。
    """
    seen, out = set(), []
    for m in _STATUS.finditer(text or ""):
        tid = m.group(1)
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    if not out:  # 没链接时退化到裸 ID
        for m in _BARE_ID.finditer(text or ""):
            tid = m.group(1)
            if tid not in seen:
                seen.add(tid)
                out.append(tid)
    return out


def harvest(ids, workers: int = 6) -> list:
    """并发补全正文。任何一条失败都不影响其余。"""
    ids = [str(x) for x in ids if x]
    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 10))) as ex:
        got = list(ex.map(xfeed.tweet, ids))
    return [t for t in got if t and "_error" not in t]


def keywords(tweets, top: int = 25, min_len: int = 3) -> list:
    """词频统计 —— 看这批推文到底在说什么。

    单词（unigram）只用来排序，**真正能拿去搜的是双词（bigram）**：
    `api` 搜不出东西，`dataforseo api`、`free gpu` 才有指向。
    所以返回的是 bigram 优先、unigram 补位的混合榜。
    """
    uni, bi = Counter(), Counter()
    for t in tweets:
        words = [w.lower() for w in _WORD.findall(t.get("text", ""))]
        words = [w for w in words if w not in STOP and len(w) >= min_len]
        uni.update(words)
        for a, b in zip(words, words[1:]):
            bi["%s %s" % (a, b)] += 1
    # bigram 出现 >=2 次才算数，单次的多半是巧合搭配
    strong = [(k, v) for k, v in bi.most_common() if v >= 2]
    out = strong + [("%s" % k, v) for k, v in uni.most_common(top)]
    seen, res = set(), []
    for k, v in out:
        if k in seen:
            continue
        seen.add(k)
        res.append((k, v))
        if len(res) >= top:
            break
    return res


def extract(tweets) -> dict:
    """抽出可行动的四类东西：GitHub 仓库、外链域名、账号、标签。"""
    repos, domains, handles, tags = Counter(), Counter(), Counter(), Counter()
    for t in tweets:
        text = t.get("text", "")
        # 正文里直接出现的链接 + t.co 展开后的原地址
        blob = text + " " + " ".join(t.get("urls", []))
        for m in _GH_REPO.finditer(blob):
            repos[m.group(1).rstrip(".").lower()] += 1
        for m in _DOMAIN.finditer(blob):
            d = m.group(1).lower()
            if d in ("t.co", "x.com", "twitter.com"):
                continue
            domains[d] += 1
        for m in _HANDLE.finditer(text):
            handles[m.group(1)] += 1
        for m in _HASHTAG.finditer(text):
            tags[m.group(1)] += 1
    return dict(
        repos=repos.most_common(20),
        domains=domains.most_common(20),
        handles=handles.most_common(20),
        tags=tags.most_common(20),
    )


def to_queries(kws, template: str = "{kw}") -> list:
    """关键词 → 爬虫查询串。下一棒交给 netforce 的 search 层。

    双词（带空格）直接当查询；单词才需要考虑加限定词。
    """
    items = [k for k, _ in kws] if kws and isinstance(kws[0], tuple) else list(kws)
    return [template.format(kw=k) for k in items]


def report(tweets, show_text: int = 200) -> str:
    """生成人看的清单：正文 + 外链，按有没有链接排序（有链接的优先）。"""
    if not tweets:
        return "没有内容"
    tw = sorted(tweets, key=lambda t: (0 if t.get("urls") else 1, -t.get("likes", 0)))
    lines = []
    for t in tw:
        lines.append("### @%s · %s" % (t["author"] or "?", (t.get("created") or "")[:16]))
        txt = (t.get("text") or "").replace("\n", " ").strip()
        lines.append(txt[:show_text] + ("…" if len(txt) > show_text else ""))
        for u in t.get("urls", [])[:3]:
            lines.append("- " + u)
        lines.append("")
    return "\n".join(lines)


def _main(argv):
    import sys
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]

    if cmd == "ids":
        text = sys.stdin.read() if len(argv) == 1 else " ".join(argv[1:])
        ids = ids_from_snapshot(text)
        print("\n".join(ids))
        print("\n共 %d 条" % len(ids), file=sys.stderr)
        return 0

    if cmd == "harvest" and len(argv) > 1:
        tw = harvest(argv[1:])
        print(report(tw))
        ex = extract(tw)
        print("\n## 关键词")
        print(", ".join("%s(%d)" % (k, c) for k, c in keywords(tw)[:20]))
        print("\n## GitHub 仓库")
        print("\n".join("%s  x%d" % (r, c) for r, c in ex["repos"]) or "无")
        print("\n## 域名")
        print("\n".join("%s  x%d" % (d, c) for d, c in ex["domains"]) or "无")
        return 0

    if cmd == "kw" and len(argv) > 1:
        tw = harvest(argv[1:])
        print("共 %d 条推文\n" % len(tw))
        for k, c in keywords(tw):
            print("%-24s %d" % (k, c))
        print("\n--- 建议的爬虫查询 ---")
        for q in to_queries(keywords(tw)[:8]):
            print(q)
        return 0

    print("未知命令：%s" % cmd, file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
