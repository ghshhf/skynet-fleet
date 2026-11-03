"""Notion 公开页抓取（notion）
=============================
问题：Notion 是纯前端渲染，`urllib` 拿到的 HTML 里只有加载动画，
正文一个字都没有（实测 20KB 全是 CSS）。

解法：走它的公开数据接口 `POST /api/v3/loadPageChunk`，公开页不需要登录。

    请求体 {"pageId": "<8-4-4-4-12>", "limit": 200,
            "cursor": {"stack": []}, "chunkNumber": 0, "verticalColumns": False}

坑（踩过一次）：返回的 recordMap.block 是 **双层 value** ——
`blocks[k]["value"]["value"]` 才是真正的块，少剥一层就永远是 0 条文字。

用法
----
    python -m fleet.notion https://app.notion.com/p/xxxx?source=copy_link
    python -m fleet.notion <url> -o out.md

    from fleet import notion
    md = notion.to_markdown(url)
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request

__all__ = ["page_id", "load", "to_markdown"]

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Content-Type": "application/json",
}

# 32 位 hex，可能带连字符，可能后面跟 ?query
_HEX32 = re.compile(r"([0-9a-f]{32})", re.I)


def page_id(url: str) -> str:
    """从任意 Notion 链接里取出 pageId，格式化成 8-4-4-4-12。"""
    s = url.strip()
    m = _HEX32.search(s.replace("-", ""))
    if not m:
        # 已经是带连字符的形式
        m2 = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", s, re.I)
        if m2:
            return m2.group(1)
        raise ValueError("链接里找不到 pageId：%s" % url)
    h = m.group(1).lower()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def load(url_or_id: str) -> dict:
    """取回 recordMap.block。失败抛异常，不静默。"""
    pid = url_or_id if "-" in url_or_id and len(url_or_id) == 36 else page_id(url_or_id)
    body = json.dumps({
        "pageId": pid, "limit": 300,
        "cursor": {"stack": []}, "chunkNumber": 0, "verticalColumns": False,
    }).encode()
    ctx = ssl.create_default_context()
    last = None
    for host in ("https://www.notion.so", "https://app.notion.com"):
        try:
            req = urllib.request.Request(host + "/api/v3/loadPageChunk",
                                         data=body, headers=_UA, method="POST")
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                d = json.loads(r.read().decode())
            return d.get("recordMap", {}).get("block", {}) or {}
        except Exception as e:
            last = e
    raise last


def _val(blocks: dict, key: str) -> dict:
    """剥掉双层 value —— 这是本模块最容易踩的坑。"""
    b = blocks.get(key, {}) or {}
    v = b.get("value", {}) or {}
    return v.get("value", v) or {}


def _text(v: dict) -> str:
    props = v.get("properties") or {}
    segs = props.get("title") or props.get("Name") or []
    out = []
    for seg in segs:
        if isinstance(seg, list) and seg and isinstance(seg[0], str):
            out.append(seg[0])
    return "".join(out).strip()


def to_markdown(url_or_id: str, depth: int = 0) -> str:
    """整页 → Markdown。嵌套子页面会展开（最多 3 层）。"""
    blocks = load(url_or_id)
    pid = url_or_id if "-" in url_or_id and len(url_or_id) == 36 else page_id(url_or_id)
    root = _val(blocks, pid)
    lines = []

    def walk(keys, level):
        for k in keys:
            v = _val(blocks, k)
            ty = v.get("type", "")
            s = _text(v)
            if ty == "page" and s:
                if level <= 3 and v.get("content"):
                    lines.append("\n" + "#" * min(6, level + 1) + " " + s)
                    walk(v["content"], level + 1)
                elif s:
                    lines.append("\n" + "#" * min(6, level + 1) + " " + s)
                continue
            if not s:
                # 空文本块也可能有子块
                if v.get("content"):
                    walk(v["content"], level)
                continue
            if ty == "header":
                lines.append("\n## " + s)
            elif ty == "sub_header":
                lines.append("\n### " + s)
            elif ty == "sub_sub_header":
                lines.append("\n#### " + s)
            elif ty in ("bulleted_list", "numbered_list", "toggle"):
                lines.append("- " + s)
            elif ty == "quote":
                lines.append("> " + s)
            elif ty == "code":
                lines.append("```\n" + s + "\n```")
            else:
                lines.append(s)
            if v.get("content") and ty not in ("code",):
                walk(v["content"], level + 1)

    walk(root.get("content", []), depth)
    title = _text(root)
    head = "# " + title + "\n" if title and not lines[:1] == ["# " + title] else ""
    return head + "\n".join(lines).strip() + "\n"


def _main(argv):
    import sys
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    url = argv[0]
    out = None
    if "-o" in argv:
        out = argv[argv.index("-o") + 1]
    md = to_markdown(url)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        print("已写入 %s（%d 字）" % (out, len(md)))
    else:
        print(md)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
