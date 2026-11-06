"""能力索引：将能力发现与规格读取拆成两层。

完整规格（参数表、说明、审批档）仅在调用前需要；全量下发会随清单条数线性
增加上下文开销。索引常驻用于匹配，规格按名字按需读取。

索引项不含 params / note / check。两层均从同一份 Capability 列表实时生成，
目录表变更即时生效，无需同步第二份数据。采用两段式加载：元数据常驻触发，
正文用时加载。
"""
from __future__ import annotations

import json
import re

from .core import BaseDevice, DeviceResult as R

_TOKEN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+", re.I)
# 只过滤会同时出现在大量标题里、对区分能力毫无帮助的词
_STOP = frozenset((
    "的", "和", "与", "及", "或", "一个", "当前", "支持", "使用", "进行",
    "以及", "默认", "可以", "不限", "the", "and", "or", "a", "an", "to",
    "for", "of", "with", "from", "by", "in", "on",
))
_MAX_KEYWORDS = 8


def _caps():
    """惰性取清单：目录表要导入本模块的设备类，顶层导入会成环。"""
    from . import catalog
    return catalog.CAPABILITIES


def _tokens(text):
    out = []
    for m in _TOKEN.finditer(str(text or "")):
        t = m.group(0).lower()
        if t and t not in _STOP and len(t) > 1:
            out.append(t)
    return out


def _keywords(cap):
    """触发词只来自名字与标题；说明里的大段文字不进索引。"""
    domain, _, verb = cap.name.partition(".")
    words = [domain.lower(), verb.lower()]
    words += _tokens(cap.title)
    seen, out = set(), []
    for w in words:
        if w not in seen and w not in _STOP and len(w) > 1:
            seen.add(w)
            out.append(w)
        if len(out) >= _MAX_KEYWORDS:
            break
    return out


def _flags(cap):
    f = []
    if cap.destructive:
        f.append("destructive")
    elif cap.side_effect:
        f.append("side_effect")
    if cap.approval in ("ask", "deny"):
        f.append("approval:" + cap.approval)
    return f


def _row(cap):
    domain, _, verb = cap.name.partition(".")
    return {
        "name": cap.name,
        "domain": domain,
        "verb": verb,
        "title": cap.title,
        "keywords": _keywords(cap),
        "flags": _flags(cap),
    }


def _full(cap):
    """完整规格：只有命中之后才构造。"""
    return {
        "name": cap.name,
        "title": cap.title,
        "params": [p.as_dict() for p in cap.params],
        "side_effect": bool(cap.side_effect),
        "destructive": bool(cap.destructive),
        "approval": cap.approval,
        "note": cap.note,
        "has_check": cap.check is not None,
    }


def index(domain=None):
    """轻量索引。可按域收窄；不传则给全量目录。"""
    rows = [_row(c) for c in _caps()]
    if domain:
        d = str(domain).strip().lower()
        rows = [r for r in rows if r["domain"].lower() == d]
    return rows


def domains():
    """域名 → 该域能力条数，按条数降序。"""
    out = {}
    for r in index():
        out[r["domain"]] = out.get(r["domain"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def brief(domain=None):
    """给模型看的紧凑目录：一行一项，不含参数表。"""
    lines = []
    for r in index(domain):
        flag = (" [" + ",".join(r["flags"]) + "]") if r["flags"] else ""
        lines.append("%s | %s%s" % (r["name"], r["title"], flag))
    return "\n".join(lines)


def detail(name):
    """按名字取一条完整规格；找不到返回 None（不抛异常，便于批量处理）。"""
    key = str(name or "").strip().lower()
    for c in _caps():
        if c.name.lower() == key:
            return _full(c)
    return None


def bundle(names):
    """一次取多条完整规格。未知名单列在 missing 里，不静默丢弃。"""
    if isinstance(names, str):
        names = [s.strip() for s in names.split(",") if s.strip()]
    names = list(names or [])
    loaded, missing = {}, []
    for n in names:
        d = detail(n)
        if d is None:
            missing.append(n)
        else:
            loaded[d["name"]] = d
    return {"loaded": loaded, "missing": missing,
            "count": len(loaded), "requested": len(names)}


def find(query, limit=10):
    """按名字 / 域 / 触发词 / 标题打分检索，返回 (名字, 分数) 列表。"""
    q = str(query or "").strip().lower()
    if not q:
        return []
    try:
        limit = max(1, min(int(limit), len(_caps()) or 1))
    except (TypeError, ValueError):
        limit = 10
    scored = []
    for r in index():
        n, d = r["name"].lower(), r["domain"].lower()
        score = 0
        if n == q:
            score = 100
        elif n.startswith(q) or n.endswith("." + q):
            score = 60
        elif q in n:
            score = 45
        elif d == q:
            score = 50
        elif any(q == k for k in r["keywords"]):
            score = 40
        elif any(q in k for k in r["keywords"]):
            score = 20
        elif q in r["title"].lower():
            score = 10
        if score:
            scored.append((r["name"], score))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]


def stats():
    """两层的实际字节数与压缩比，用来判断这套分层是否还划算。"""
    rows = index()
    fulls = [_full(c) for c in _caps()]
    b_index = len(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    b_full = len(json.dumps(fulls, ensure_ascii=False).encode("utf-8"))
    return {
        "total": len(rows),
        "domains": len(domains()),
        "index_bytes": b_index,
        "full_bytes": b_full,
        "ratio": round(b_index / b_full, 4) if b_full else 0,
        "brief_bytes": len(brief().encode("utf-8")),
    }


class CapIndexOps(BaseDevice):
    """把索引层暴露成可调用能力，便于按名字只取需要的那几条规格。"""

    kind = "capindex"
    via = "local"

    def __init__(self, name="cap:index", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "meta")

    def probe(self):
        try:
            n = len(_caps())
        except Exception as exc:                              # noqa: BLE001
            return False, "能力清单读不出来：%s" % exc
        return (True, "") if n else (False, "能力清单为空")

    def caps(self):
        return ("cap.index", "cap.find", "cap.detail",
                "cap.bundle", "cap.stats")

    def invoke(self, cap, args=None):
        a = args or {}
        ok, why = self.probe()
        if not ok:
            return R.unavailable(cap, why, device=self.name)
        try:
            if cap == "cap.index":
                rows = index(a.get("domain"))
                lim = a.get("limit")
                if lim:
                    rows = rows[:max(0, int(lim))]
                return R.success(cap, self.name,
                                 {"items": rows, "count": len(rows),
                                  "layer": "index"},
                                 side_effect=False, via=self.via)
            if cap == "cap.find":
                q = a.get("q") or a.get("query") or ""
                if not str(q).strip():
                    return R.error(cap, "find 需要 q（检索词）", device=self.name)
                hits = find(q, a.get("limit", 10))
                return R.success(cap, self.name,
                                 {"query": q, "hits": [n for n, _ in hits],
                                  "scored": [{"name": n, "score": s}
                                             for n, s in hits]},
                                 side_effect=False, via=self.via)
            if cap == "cap.detail":
                name = a.get("name")
                d = detail(name)
                if d is None:
                    return R.error(cap, "没有这项能力：%s" % name,
                                   device=self.name)
                return R.success(cap, self.name, {"spec": d, "layer": "full"},
                                 side_effect=False, via=self.via)
            if cap == "cap.bundle":
                names = a.get("names")
                if not names:
                    return R.error(cap, "bundle 需要 names（列表或逗号串）",
                                   device=self.name)
                return R.success(cap, self.name, bundle(names),
                                 side_effect=False, via=self.via)
            if cap == "cap.stats":
                return R.success(cap, self.name, stats(),
                                 side_effect=False, via=self.via)
        except Exception as exc:                              # noqa: BLE001
            return R.error(cap, "%s: %s" % (type(exc).__name__, exc),
                           device=self.name)
        return R.unsupported(cap, "%s 没有这项能力" % self.name,
                             device=self.name)


def _selftest():
    fails, n = [], 0

    def ck(cond, label):
        nonlocal n
        n += 1
        if not cond:
            fails.append(label)

    rows = index()
    caps = _caps()
    ck(len(rows) == len(caps) and len(rows) > 400, "索引条数与清单一致")
    ck(all(set(r) == {"name", "domain", "verb", "title", "keywords", "flags"}
           for r in rows), "索引字段固定且最小")
    ck(all("params" not in r and "note" not in r and "check" not in r
           for r in rows), "索引不含完整规格")
    ck(len({r["name"] for r in rows}) == len(rows), "能力名无重复")
    ck(all(r["domain"] and r["verb"] for r in rows), "域与动词都已拆出")
    ck(all(r["keywords"] for r in rows), "每条都有触发词")
    ck(all(len(r["keywords"]) <= _MAX_KEYWORDS for r in rows), "触发词有上限")

    d = domains()
    ck(sum(d.values()) == len(rows), "域统计合计等于总数")
    ck("chart" in d and d["chart"] >= 5, "chart 域条数合理")
    ck(list(d.values()) == sorted(d.values(), reverse=True), "域统计按条数降序")
    sub = index("chart")
    ck(sub and all(r["domain"] == "chart" for r in sub), "按域收窄生效")
    ck(index("NO_SUCH_DOMAIN") == [], "未知域返回空")

    lines = brief().splitlines()
    ck(len(lines) == len(rows), "brief 一行一项")
    ck(all(r["name"] in b for r, b in zip(rows, lines)), "brief 含能力名")
    ck("params" not in brief(), "brief 不含参数表")
    ck(len(brief("chart").splitlines()) == len(sub), "brief 可按域收窄")

    one = detail("chart.bar")
    ck(one is not None, "detail 命中已知能力")
    ck(one and one["name"] == "chart.bar", "detail 名字正确")
    ck(one and "params" in one and "note" in one, "detail 含完整规格")
    src = next(c for c in caps if c.name == "chart.bar")
    ck(one and len(one["params"]) == len(src.params), "detail 参数条数与源一致")
    ck(detail("chart.bar".upper()) is not None, "detail 忽略大小写")
    ck(detail("no.such.cap") is None, "detail 未知返回 None")
    ck(detail("") is None and detail(None) is None, "detail 空输入安全")

    b = bundle(["chart.bar", "chart.line"])
    ck(b["count"] == 2 and b["requested"] == 2, "bundle 计数正确")
    ck(set(b["loaded"]) == {"chart.bar", "chart.line"}, "bundle 只含指定项")
    ck(b["missing"] == [], "bundle 无缺失")
    ck(bundle("chart.bar,chart.line")["count"] == 2, "bundle 接受逗号串")
    bm = bundle(["chart.bar", "no.such.cap"])
    ck(bm["missing"] == ["no.such.cap"] and bm["count"] == 1,
       "bundle 未知名单列不丢弃")
    ck(bundle([]) == {"loaded": {}, "missing": [], "count": 0,
                      "requested": 0}, "bundle 空输入安全")
    ck(bundle(["chart.bar", "CHART.BAR"])["requested"] == 2, "bundle 不擅自去重")

    ck(find("chart.bar")[0][0] == "chart.bar", "find 精确名排第一")
    ck(find("chart.bar")[0][1] == 100, "find 精确名满分")
    ck(any(x[0].startswith("chart.") for x in find("chart")), "find 命中域")
    ck(any(x[0].startswith("media.") for x in find("视频")), "find 命中中文标题")
    ck(any(x[0].startswith("code.") for x in find("语法")), "find 命中中文关键词")
    ck(find("") == [] and find(None) == [], "find 空检索返回空")
    ck(len(find("chart", 3)) <= 3, "find 遵守 limit")
    ck(all(s > 0 for _, s in find("chart")), "find 分数为正")
    ck(find("zzzz_no_match_zzzz") == [], "find 无匹配返回空")
    ck(len(find("chart", 1)) == 1, "find limit=1 生效")

    st = stats()
    ck(st["total"] == len(rows), "stats 总数一致")
    ck(st["domains"] == len(d), "stats 域数一致")
    ck(st["index_bytes"] < st["full_bytes"], "索引比全量小")
    ck(0 < st["ratio"] < 1, "压缩比在 0 与 1 之间")
    ck(st["brief_bytes"] < st["full_bytes"], "brief 比全量小")
    ck(index() == rows, "index 幂等（同一份真源）")

    dev = CapIndexOps()
    ok, why = dev.probe()
    ck(ok, "设备 probe 通过：" + why)
    ck(set(dev.caps()) == {"cap.index", "cap.find", "cap.detail",
                           "cap.bundle", "cap.stats"}, "设备能力名齐全")
    ck(all(dev.supports(x) for x in dev.caps()), "设备 supports 一致")
    ck(dev.invoke("cap.stats").ok, "cap.stats 调用通")
    ck(dev.invoke("cap.index", {"domain": "chart"}).data["count"] >= 5,
       "cap.index 按域调用通")
    ck(dev.invoke("cap.find", {"q": "chart"}).data["hits"], "cap.find 有命中")
    ck(dev.invoke("cap.detail", {"name": "chart.bar"}).data["spec"]["name"]
       == "chart.bar", "cap.detail 调用通")
    ck(dev.invoke("cap.bundle", {"names": ["chart.bar"]}).data["count"] == 1,
       "cap.bundle 调用通")
    ck(not dev.invoke("cap.find", {}).ok, "cap.find 缺 q 报错")
    ck(not dev.invoke("cap.detail", {"name": "nope"}).ok, "cap.detail 未知报错")
    ck(not dev.invoke("cap.bundle", {}).ok, "cap.bundle 缺 names 报错")
    ck(not dev.invoke("cap.nope").ok, "未知能力返回失败")
    ck(all(not r.ok or r.code == "ok" for r in
           [dev.invoke(x, {"q": "chart", "name": "chart.bar",
                           "names": ["chart.bar"]}) for x in dev.caps()]),
       "成功结果 code 恒为 ok")

    print("capindex 自测：%d/%d 通过" % (n - len(fails), n))
    for f in fails:
        print("  ✗ " + f)
    print("分层实测：索引 %d B / 全量 %d B，压缩到 %.1f%%"
          % (st["index_bytes"], st["full_bytes"], st["ratio"] * 100))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
