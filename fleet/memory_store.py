"""天网·大脑子持久记忆（fleet.memory_store）—— 零依赖本地记忆库。

为什么要有它：llm.py 的「她」每次启动都是空白世界模型，用户说过的话、
perceive/persona 上报的世界模型，重启就清零。融一个轻量本地记忆库，
让「她」能跨会话检索相关上下文、把本轮问答写回，不再每次从零开始。

设计：
  · 存储：sqlite（标准库），`~/.fleet/memory.db`（用户数据目录，不进仓库）。
  · 相似度：字符 3-gram 向量 + 余弦相似度，纯标准库，不引 numpy/scikit-learn。
  · 开关：FLEET_MEMORY=0 关闭，退回纯无记忆链路（不影响任何已有行为）。
  · 不静默：db 打不开时如实报错，不假装记住了。

接口：
    store = MemoryStore()                    # 默认开；FLEET_MEMORY=0 ⇒ enabled=False
    store.remember("user", "我叫阿强", tags=("profile",))   -> id
    store.remember("assistant", "好的，记住你叫阿强了", tags=("profile",))
    mems = store.recall("我叫什么", top_k=5)                -> list[str]
    store.recent(10)                                        -> list[dict]
    store.enabled                                           -> bool
"""
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path


def _default_path():
    d = Path(os.path.expanduser("~/.fleet"))
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "memory.db")


def _grams(text, n=3):
    """字符 n-gram（n=3），纯中文/英文都适用，无需分词。"""
    t = (text or "").lower()
    if len(t) < n:
        return [t] if t else []
    return [t[i:i + n] for i in range(len(t) - n + 1)]


def _tfvec(grams):
    """tf 向量：gram -> 频率/总数。"""
    c = Counter(grams)
    total = sum(c.values()) or 1
    return {g: v / total for g, v in c.items()}


def _cosine(a, b):
    """两个稀疏向量的余弦相似度（0..1，0 表示无共享 gram）。"""
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    na = 0.0
    nb = 0.0
    for g, av in a.items():
        na += av * av
        bv = b.get(g)
        if bv:
            dot += av * bv
    for bv in b.values():
        nb += bv * bv
    if na == 0 or nb == 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class MemoryStore:
    """本地持久记忆：remember 写入，recall 按相关性检索。"""

    def __init__(self, path=None, enabled=None):
        if enabled is None:
            enabled = os.environ.get("FLEET_MEMORY", "1") != "0"
        self.enabled = bool(enabled)
        self.path = path or _default_path()
        self._con = None
        if self.enabled:
            self._open()

    def _open(self):
        self._con = sqlite3.connect(self.path)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS memory ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "ts REAL NOT NULL,"
            "role TEXT NOT NULL,"
            "content TEXT NOT NULL,"
            "tags TEXT NOT NULL DEFAULT '',"
            "emb TEXT NOT NULL DEFAULT '{}')")
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_role_ts ON memory(role, ts DESC)")
        self._con.commit()

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None

    def __del__(self):
        self.close()

    # ---------------- 写入 ----------------

    def remember(self, role, content, tags=()):
        """写一条记忆。返回 id；记忆关闭时返回 -1 且不写。"""
        if not self.enabled:
            return -1
        content = (content or "").strip()
        if not content:
            return -1
        tags_s = ",".join(tags) if tags else ""
        emb = json.dumps(_tfvec(_grams(content)), ensure_ascii=False)
        cur = self._con.execute(
            "INSERT INTO memory(ts, role, content, tags, emb) VALUES(?,?,?,?,?)",
            (time.time(), role, content, tags_s, emb))
        self._con.commit()
        return cur.lastrowid

    # ---------------- 检索 ----------------

    def recall(self, query, top_k=5, min_sim=0.05):
        """按与查询的相关性返回 top_k 条记忆文本（最相关的在前）。

        纯标准库实现：把库里全部条目载入内存算余弦（个人使用量级
        几千条完全可接受；如需千万级再换专用向量库）。
        """
        if not self.enabled or top_k <= 0:
            return []
        if self._con is None:
            self._open()
        qv = _tfvec(_grams(query))
        if not qv:
            return []
        rows = self._con.execute(
            "SELECT id, role, content, tags, emb FROM memory ORDER BY ts DESC").fetchall()
        if not rows:
            return []
        # idf 近似：含该 gram 的条目数（越稀有权重越高）
        n = len(rows)
        df = Counter()
        vecs = []
        for rid, role, content, tags_s, emb_s in rows:
            try:
                ev = json.loads(emb_s or "{}")
            except Exception:
                ev = {}
            vecs.append((rid, role, content, tags_s, ev))
            for g in ev:
                df[g] += 1

        def weight(g):
            d = df.get(g, 1)
            return math.log(n / d) if d < n else 0.1

        scored = []
        for rid, role, content, tags_s, ev in vecs:
            # 加权余弦：query gram 权重 × idf
            dot = 0.0
            nq = 0.0
            for g, qval in qv.items():
                w = weight(g)
                nq += (qval * w) ** 2
                evv = ev.get(g)
                if evv:
                    dot += (qval * w) * (evv * w)
            ne = 0.0
            for g, evv in ev.items():
                ne += (evv * weight(g)) ** 2
            sim = dot / math.sqrt(nq * ne) if nq and ne else 0.0
            if sim >= min_sim:
                scored.append((sim, rid, role, content))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored:
            return [c for _, _, _, c in scored[:top_k]]
        # 短查询（<3 字）按 3-gram 几乎命中不了：兜底做子串包含匹配，
        # 让「代号」这种短词也能翻出相关记忆。
        q = (query or "").strip()
        if len(q) < 3:
            sub = [content for _, _, content, _ in (
                (r[0], r[1], r[2], r[3]) for r in rows)]
            hits = [c for c in sub if q.lower() in c.lower()]
            # 越短的条目匹配越精确，先返回短的
            hits.sort(key=len)
            return hits[:top_k]
        return []

    def recent(self, limit=10):
        """最近 limit 条（id 降序），每条 dict。"""
        if not self.enabled:
            return []
        if self._con is None:
            self._open()
        rows = self._con.execute(
            "SELECT id, ts, role, content, tags FROM memory ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"id": r[0], "ts": r[1], "role": r[2], "content": r[3],
                 "tags": r[4]} for r in rows]

    def clear(self):
        """清空全部记忆（慎用）。"""
        if not self.enabled or self._con is None:
            return
        self._con.execute("DELETE FROM memory")
        self._con.commit()


# 单例：llm.py 与其他模块复用这一个实例
_store = None


def get_store():
    """取全局记忆库单例（首次调用才打开 db）。"""
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


# ---------------------------------------------------------------- 自测

def _selftest():
    import tempfile, os as _os
    p = _os.path.join(tempfile.gettempdir(), f"fleet_mem_{_os.getpid()}.db")
    for f in (p, p + "-journal", p + "-wal", p + "-shm"):
        if _os.path.exists(f):
            _os.remove(f)
    s = MemoryStore(path=p)
    assert s.enabled
    assert s.remember("user", "我喜欢喝普洱茶", tags=("profile",)) > 0
    assert s.remember("assistant", "普洱茶不错，是云南的特产", tags=("profile",)) > 0
    assert s.remember("user", "今天天气怎么样", tags=("weather",)) > 0
    assert s.remember("assistant", "外面在下雨", tags=("weather",)) > 0

    mems = s.recall("我喜欢喝什么茶", top_k=3)
    assert mems, "应该能检索到喝茶相关记忆"
    assert any("普洱" in m for m in mems), mems

    # 无关查询应返回空或低相关（不强制空，只强制不崩）
    s.recall("quantum computing", top_k=2)

    rec = s.recent(5)
    assert len(rec) == 4, len(rec)
    assert rec[0]["role"] == "assistant" and "下雨" in rec[0]["content"], rec

    s.clear()
    assert s.recall("普洱茶", top_k=3) == []
    s.close()
    for f in (p, p + "-journal", p + "-wal", p + "-shm"):
        if _os.path.exists(f):
            _os.remove(f)
    print("fleet.memory_store 自测通过（写入/检索/最近/清空/关闭）")


if __name__ == "__main__":
    _selftest()
