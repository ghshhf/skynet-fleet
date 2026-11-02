"""论文库设备：把 G:\\_papers 里已经爬下来的论文接进天网总线。

为什么要有这一台（主人 2026-11-03）：
  「论文库接总线：439 条能力里 paper.* 只有 web.papers（抓取列表），没有检索，
    paper_harvest 是独立脚本，agent 调不到。」
  数据面已经把 500+ 篇 PDF + 元数据攒在本地，但总线里没有任何一条能力能读它
  ⇒ agent 想引用一篇论文只能重新上网搜。这台设备就是把「已经付过带宽成本的数据」
  变成可调能力。

🔴 边界（主人明确要求，不许自作聪明）：
  **不建全文索引、不预先解析 PDF。**
  「PDF 解析是使用时需要什么知识才专门去解析」——所以这里只做**元数据检索**
  （题目/摘要/作者/编号/方向标签），另给 `paper.path` 把本地文件位置交出去，
  谁要用正文谁自己按需解析（pypdf/pdfminer 之类到时再内化，不进本文件）。

设计：
  - 纯标准库、零依赖、只读（不改任何文件，probe 恒可用）；
  - 懒加载 + mtime/size 双判缓存：papers.jsonl 由落盘器持续追加，变了就重读；
  - 检索是**朴素子串打分**（title×3 / id×5 / abstract×1），几百到几万条足够快，
    不引入索引文件——索引一建就有一致性问题，而这里的数据每天都在长。
"""
import json
import os
import re

from ..core import BaseDevice, DeviceResult as R

#: 论文库根：环境变量优先，其次 G:\_papers（落盘器默认），再退用户目录。
DEFAULT_ROOTS = (r"G:\_papers", os.path.join(os.path.expanduser("~"), "_papers"))

TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+|[\u4e00-\u9fff]+")


def _tokens(q):
    """查询分词：英文按词、中文按连续块（不引分词库，够用且零依赖）。"""
    return [t.lower() for t in TOKEN_RE.findall(q or "")][:8]


class PaperDevice(BaseDevice):
    kind = "paperlib"
    via = "local"

    def __init__(self, name="paper:lib", root=None, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "data")
        self._root = root
        self._cache = {"key": None, "recs": []}

    # ---------------- 基础 ----------------

    def root(self):
        if self._root:
            return self._root
        env = os.environ.get("PAPER_LIB")
        if env:
            return env
        for p in DEFAULT_ROOTS:
            if os.path.isdir(p):
                return p
        return DEFAULT_ROOTS[0]

    def _meta_path(self):
        return os.path.join(self.root(), "papers.jsonl")

    def _recs(self):
        """读 papers.jsonl（坏行跳过，不因一行脏数据整库不可用）。"""
        p = self._meta_path()
        try:
            st = os.stat(p)
            key = (p, int(st.st_mtime), st.st_size)
        except OSError:
            return []
        if self._cache["key"] == key:
            return self._cache["recs"]
        out = []
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("id"):
                        out.append(r)
        except OSError:
            return []
        # 同一个 id 可能被追加过两次（断点续跑），后写的覆盖先写的
        dedup = {}
        for r in out:
            dedup[str(r["id"])] = r
        recs = list(dedup.values())
        self._cache = {"key": key, "recs": recs}
        return recs

    def _pdf_path(self, rec):
        pid = str(rec.get("id") or "").replace("/", "_")
        return rec.get("pdf") or os.path.join(self.root(), "pdf", pid + ".pdf")

    # ---------------- 设备契约 ----------------

    def probe(self):
        """恒可用：库不存在时逐条能力如实返回空，而不是让整台设备下线。"""
        p = self._meta_path()
        if not os.path.exists(p):
            return True, "库还没建（%s 不存在）" % p
        return True, ""

    def caps(self):
        return ("paper.stats", "paper.search", "paper.get", "paper.recent",
                "paper.tags", "paper.path", "paper.text")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "paper.stats":
            return self._stats()
        if cap == "paper.search":
            return self._search(args)
        if cap == "paper.get":
            return self._get(args)
        if cap == "paper.recent":
            return self._recent(args)
        if cap == "paper.tags":
            return self._tags()
        if cap == "paper.path":
            return self._path(args)
        if cap == "paper.text":
            return self._text(args)
        return super().invoke(cap, args)

    # ---------------- 只读能力 ----------------

    def _stats(self):
        recs = self._recs()
        tags, sources = {}, {}
        for r in recs:
            for t in r.get("tags") or []:
                tags[t] = tags.get(t, 0) + 1
            s = r.get("source") or "?"
            sources[s] = sources.get(s, 0) + 1
        pdf_dir = os.path.join(self.root(), "pdf")
        npdf = 0
        try:
            npdf = sum(1 for n in os.listdir(pdf_dir) if n.lower().endswith(".pdf"))
        except OSError:
            pass
        return R.success("paper.stats", self.name, via=self.via, data={
            "root": self.root(),
            "papers": len(recs),
            "pdf_files": npdf,
            "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
            "sources": dict(sorted(sources.items(), key=lambda kv: -kv[1])),
            "note": "只统计元数据，不解析 PDF 正文（按需解析由调用方决定）",
        })

    def _hit(self, rec, terms):
        """朴素打分：编号权重最高（精确引用），题目次之，摘要兜底。"""
        pid = str(rec.get("id") or "").lower()
        title = (rec.get("title") or "").lower()
        abstract = (rec.get("abstract") or "").lower()
        authors = (rec.get("authors") or "").lower()
        score = 0
        for t in terms:
            if t in pid:
                score += 5
            if t in title:
                score += 3
            if t in authors:
                score += 2
            if t in abstract:
                score += 1
        return score

    def _search(self, args):
        q = args.get("q") or args.get("query") or ""
        tag = (args.get("tag") or "").strip()
        limit = max(1, min(int(args.get("limit") or 20), 200))
        offset = max(0, int(args.get("offset") or 0))
        terms = _tokens(q)
        if not terms and not tag:
            return R.error("paper.search", "给个 q（关键词）或 tag（方向标签）", device=self.name, via=self.via)
        scored = []
        for r in self._recs():
            if tag and tag not in (r.get("tags") or []):
                continue
            sc = self._hit(r, terms) if terms else 1
            if sc <= 0:
                continue
            scored.append((sc, r))
        scored.sort(key=lambda x: (-x[0], str(x[1].get("date") or "")))
        window = scored[offset:offset + limit]
        return R.success("paper.search", self.name, via=self.via, data={
            "q": q, "tag": tag, "total": len(scored), "offset": offset,
            "results": [{
                "id": r.get("id"),
                "title": r.get("title"),
                "date": r.get("date"),
                "tags": r.get("tags") or [],
                "score": sc,
                "snippet": (r.get("abstract") or "")[:200],
                "pdf": self._pdf_path(r),
            } for sc, r in window],
        })

    def _get(self, args):
        pid = str(args.get("id") or "").strip()
        if not pid:
            return R.error("paper.get", "缺 id", device=self.name, via=self.via)
        for r in self._recs():
            if str(r.get("id")) == pid:
                out = dict(r)
                out["pdf"] = self._pdf_path(r)
                out["pdf_exists"] = os.path.exists(out["pdf"])
                return R.success("paper.get", self.name, via=self.via, data=out)
        return R.error("paper.get", "库里没有 %s" % pid, device=self.name, via=self.via)

    def _recent(self, args):
        limit = max(1, min(int(args.get("limit") or 20), 200))
        recs = sorted(self._recs(), key=lambda r: str(r.get("date") or ""), reverse=True)
        return R.success("paper.recent", self.name, via=self.via, data={
            "results": [{
                "id": r.get("id"), "title": r.get("title"),
                "date": r.get("date"), "tags": r.get("tags") or [],
            } for r in recs[:limit]],
        })

    def _tags(self):
        tags = {}
        for r in self._recs():
            for t in r.get("tags") or []:
                tags[t] = tags.get(t, 0) + 1
        return R.success("paper.tags", self.name, via=self.via, data={
            "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])),
        })

    def _path(self, args):
        pid = str(args.get("id") or "").strip()
        if not pid:
            return R.error("paper.path", "缺 id", device=self.name, via=self.via)
        for r in self._recs():
            if str(r.get("id")) == pid:
                p = self._pdf_path(r)
                return R.success("paper.path", self.name, via=self.via, data={
                    "id": pid, "pdf": p, "exists": os.path.exists(p),
                    "bytes": (os.path.getsize(p) if os.path.exists(p) else 0),
                    "hint": "正文解析按需做：谁要用谁读这个路径，本设备不预建索引",
                })
        return R.error("paper.path", "库里没有 %s" % pid, device=self.name, via=self.via)

    def _text(self, args):
        """按需读正文：走 MarkItDown（开源）转 Markdown，**不缓存、不预建索引**。

        主人定的边界：「解析是使用时需要什么知识才专门去解析」。
        所以这里只在被调用的那一刻读一个文件，读完就返回；不落 .txt、不建倒排。
        """
        pid = str(args.get("id") or "").strip()
        if not pid:
            return R.error("paper.text", "缺 id", device=self.name, via=self.via)
        max_chars = max(1000, min(int(args.get("max_chars") or 20000), 200000))
        for r in self._recs():
            if str(r.get("id")) == pid:
                p = self._pdf_path(r)
                if not os.path.exists(p):
                    return R.error("paper.text", "PDF 不在本地: %s" % p,
                                   device=self.name, via=self.via)
                try:
                    from markitdown import MarkItDown
                    md = MarkItDown().convert(p).text_content or ""
                except Exception as e:
                    return R.error("paper.text", "解析失败（需要 markitdown）: %s: %s"
                                   % (type(e).__name__, e), device=self.name, via=self.via)
                return R.success("paper.text", self.name, via=self.via, data={
                    "id": pid,
                    "title": r.get("title"),
                    "pdf": p,
                    "chars": len(md),
                    "truncated": len(md) > max_chars,
                    "text": md[:max_chars],
                    "engine": "MarkItDown",
                })
        return R.error("paper.text", "库里没有 %s" % pid, device=self.name, via=self.via)
