"""域名库设备：把爬虫攒出来的 1.4 万条域名 + 分类接进天网总线。

为什么要有这一台（主人 2026-11-03）：
  「域名库接总线：无 domain.* / index 能力，14,755 条只躺在 md。」
  数据面花了几十小时爬出来、又人工判过一遍分类的资产，总线里一条能力都没有
  ⇒ agent 想找「有没有现成的开源语音合成站」只能重新上网搜，等于把已付的成本再付一次。

数据来源（两份，以 classified.json 为准，resources.json 补 url / 发现渠道）：
  github_runner/data/classified.json   域名 + 分类 + 标题/描述 + 判定依据
  github_runner/data/resources.json    域名 + url + found_via（谁发现了它）

设计：
  - 纯标准库、只读、零副作用；懒加载 + mtime 双判缓存（爬虫在持续追加）；
  - 检索按 domain/title/desc 子串打分，支持 category 过滤与分页；
  - 不写回、不改分类——分类的写入仍是 aiclassify/人工批次那条线（这里只读）。
"""
import json
import os
import re

from ..core import BaseDevice, DeviceResult as R

#: 仓库根（fleet/kinds/domain_device.py → 上三级）
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DATA = os.path.join(ROOT, "github_runner", "data")

TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+|[\u4e00-\u9fff]+")


def _tokens(q):
    return [t.lower() for t in TOKEN_RE.findall(q or "")][:8]


def _word_hit(text, w):
    """英文按词边界、中文按子串——否则 "tts" 会在 "bhatts"/"settings" 里假命中。"""
    if not text or not w:
        return False
    if re.fullmatch(r"[a-z0-9_.\-]+", w):
        return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text) is not None
    return w in text


class DomainDevice(BaseDevice):
    kind = "domainlib"
    via = "local"

    def __init__(self, name="domain:lib", data_dir=None, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "data")
        self._dir = data_dir
        self._cache = {"key": None, "rows": []}

    # ---------------- 基础 ----------------

    def data_dir(self):
        return self._dir or os.environ.get("DOMAIN_LIB") or DEFAULT_DATA

    def _p(self, name):
        return os.path.join(self.data_dir(), name)

    def _load_json(self, name):
        try:
            with open(self._p(name), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _rows(self):
        """classified.json 为主，resources.json 补 url / found_via。"""
        key = []
        for n in ("classified.json", "resources.json"):
            try:
                st = os.stat(self._p(n))
                key.append((n, int(st.st_mtime), st.st_size))
            except OSError:
                key.append((n, 0, 0))
        key = tuple(key)
        if self._cache["key"] == key:
            return self._cache["rows"]
        res = {}
        for r in self._load_json("resources.json"):
            d = (r.get("domain") or "").strip().lower()
            if d:
                res[d] = r
        rows = []
        seen = set()
        for c in self._load_json("classified.json"):
            d = (c.get("domain") or "").strip().lower()
            if not d or d in seen:
                continue
            seen.add(d)
            base = res.get(d) or {}
            rows.append({
                "domain": d,
                "category": c.get("category") or "其他/待定",
                "title": c.get("title") or base.get("title") or "",
                "desc": c.get("desc") or base.get("desc") or "",
                "basis": c.get("basis") or "",
                "url": c.get("url") or base.get("url") or ("https://" + d),
                "found_via": base.get("found_via") or c.get("found_via") or "",
            })
        for d, r in res.items():                 # 还没分类的新域名也让人看得到
            if d not in seen:
                rows.append({
                    "domain": d, "category": "其他/待定",
                    "title": r.get("title") or "", "desc": r.get("desc") or "",
                    "basis": "未分类", "url": r.get("url") or ("https://" + d),
                    "found_via": r.get("found_via") or "",
                })
        self._cache = {"key": key, "rows": rows}
        return rows

    # ---------------- 设备契约 ----------------

    def probe(self):
        p = self._p("classified.json")
        if not os.path.exists(p):
            return True, "分类库还没建（%s 不存在）" % p
        return True, ""

    def caps(self):
        return ("domain.stats", "domain.search", "domain.get", "domain.categories",
                "domain.by_category", "domain.recent")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "domain.stats":
            return self._stats()
        if cap == "domain.search":
            return self._search(args)
        if cap == "domain.get":
            return self._get(args)
        if cap == "domain.categories":
            return self._categories()
        if cap == "domain.by_category":
            return self._by_category(args)
        if cap == "domain.recent":
            return self._recent(args)
        return super().invoke(cap, args)

    # ---------------- 只读能力 ----------------

    def _stats(self):
        rows = self._rows()
        cats = {}
        for r in rows:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        tail = ("其他/待定", "平台/大厂服务（非资源站）", "疑似失效（保留待复查）",
                "访问失败（保留待复查）", "JS渲染/无静态内容（保留待复查）")
        useful = sum(n for k, n in cats.items() if k not in tail)
        return R.success("domain.stats", self.name, via=self.via, data={
            "data_dir": self.data_dir(),
            "domains": len(rows),
            "classified": useful,
            "classified_pct": round(useful * 100.0 / len(rows), 1) if rows else 0.0,
            "categories": len(cats),
            "top": dict(sorted(cats.items(), key=lambda kv: -kv[1])[:12]),
        })

    def _hit(self, row, terms):
        """域名按**标签边界**算命中：`bhatts.org` 不该被 "tts" 命中。
        英文标题按词边界，中文按子串（中文没有空格分词）。"""
        parts = re.split(r"[.\-_]", row["domain"])
        t = (row["title"] or "").lower()
        desc = (row["desc"] or "").lower()
        cat = row["category"].lower()
        score = 0
        for w in terms:
            if any(p == w or p.startswith(w) for p in parts):
                score += 5
            if w in cat:
                score += 2
            if _word_hit(t, w):
                score += 3
            if _word_hit(desc, w):
                score += 1
        return score

    def _search(self, args):
        q = args.get("q") or args.get("query") or ""
        cat = (args.get("category") or "").strip()
        limit = max(1, min(int(args.get("limit") or 20), 200))
        offset = max(0, int(args.get("offset") or 0))
        terms = _tokens(q)
        if not terms and not cat:
            return R.error("domain.search", "给个 q 或 category", device=self.name, via=self.via)
        scored = []
        for r in self._rows():
            if cat and cat not in r["category"]:
                continue
            sc = self._hit(r, terms) if terms else 1
            if sc <= 0:
                continue
            scored.append((sc, r))
        scored.sort(key=lambda x: -x[0])
        return R.success("domain.search", self.name, via=self.via, data={
            "q": q, "category": cat, "total": len(scored), "offset": offset,
            "results": [dict(r, score=sc) for sc, r in scored[offset:offset + limit]],
        })

    def _get(self, args):
        d = (args.get("domain") or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
        if not d:
            return R.error("domain.get", "缺 domain", device=self.name, via=self.via)
        for r in self._rows():
            if r["domain"] == d:
                return R.success("domain.get", self.name, via=self.via, data=r)
        return R.error("domain.get", "库里没有 %s" % d, device=self.name, via=self.via)

    def _categories(self):
        cats = {}
        for r in self._rows():
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        return R.success("domain.categories", self.name, via=self.via, data={
            "categories": dict(sorted(cats.items(), key=lambda kv: -kv[1])),
        })

    def _by_category(self, args):
        cat = (args.get("category") or "").strip()
        if not cat:
            return R.error("domain.by_category", "缺 category", device=self.name, via=self.via)
        limit = max(1, min(int(args.get("limit") or 50), 500))
        rows = [r for r in self._rows() if cat in r["category"]]
        return R.success("domain.by_category", self.name, via=self.via, data={
            "category": cat, "total": len(rows), "results": rows[:limit],
        })

    def _recent(self, args):
        limit = max(1, min(int(args.get("limit") or 20), 200))
        rows = self._rows()
        return R.success("domain.recent", self.name, via=self.via, data={
            "results": list(reversed(rows[-limit:])),
        })
