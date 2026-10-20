"""节点表：内存里的「在线」，磁盘上的「来过」。

为什么两套：在线是瞬时状态（socket 一断就没了），而「谁来过、它是什么能力、
多久没见」是历史——策略层（掉线降级、重活优先 GPU）和她的「记得谁」都要看历史。
原来节点表是纯内存 dict，控制面一重启历史全空，等于每天从零认识自己的设备。

两条纪律：
  · 绝不落凭据。sock（文件描述符）和 token/password/psk（认证凭据）永远不进磁盘；
    历史文件只留「这机器是什么」，不留「怎么登进去」。
  · 原子替换。先写 .tmp 再 os.replace，宁可丢最后一次心跳，也不留半个坏文件。

还有一个刻意不做的设计：**重启后不把历史里的节点当成在线**。假装在线比丢历史更糟——
nodes 会显示一堆连不上的幽灵，策略层会以为编队还在。
"""
import json
import os
import threading
import time

import common

# 落盘字段白名单：宁少不多，新增字段要显式加进来，避免哪天把凭据顺手写出去
KEEP = ("platform", "info", "capabilities", "models", "gpu", "ram_gb",
        "session", "tags")
NEVER = ("sock", "token", "password", "psk", "secret")

VERSION = 1


class NodeTable(common.LockedDict):
    """在线表（继承 LockedDict，用法不变）+ 影子历史（落盘）。"""

    def __init__(self, path=None, interval=10.0):
        super().__init__()
        self.path = path or ""
        self.interval = interval
        self._hist = {}
        self._lk2 = threading.RLock()      # 保护历史与落盘；可重入（observe 里要调 save）
        self._last_save = 0.0
        self._dirty = False
        if self.path:
            self.load()

    # ---------------- 历史 ----------------

    def _clean(self, rec):
        out = {}
        src = rec if isinstance(rec, dict) else {}
        for k in KEEP:
            if k in src and src[k] not in (None, ""):
                out[k] = src[k]
        return out

    def observe(self, name, rec, force=False):
        """记一笔「见过这个节点」。心跳也走这里，所以内部节流，别每次写盘。"""
        if not name:
            return {}
        clean = self._clean(rec)
        with self._lk2:
            old = self._hist.get(name) or {}
            merged = dict(old)
            merged.update(clean)
            merged["last_seen"] = time.time()
            merged["first_seen"] = old.get("first_seen") or merged["last_seen"]
            merged["count"] = int(old.get("count") or 0) + 1
            self._hist[name] = merged
            self._dirty = True
            saved = self.save(force=force)
        return merged

    def history(self):
        with self._lk2:
            return {k: dict(v) for k, v in self._hist.items()}

    def seen(self, order="recent"):
        """历史按「最近见过」倒序（默认）。给 nodes 之外的「谁来过」用。"""
        rows = [dict(v, name=k) for k, v in self.history().items()]
        rows.sort(key=lambda r: -(r.get("last_seen") or 0))
        return rows

    def forget(self, name):
        """把一台机器从历史里删掉（E-7 式：设备不该永远留在账上）。"""
        with self._lk2:
            had = self._hist.pop(name, None) is not None
            self._dirty = self._dirty or had
            if had:
                self.save(force=True)
        return had

    # ---------------- 落盘 ----------------

    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return 0                       # 坏了就当没有：不要因为一份坏账起不来
        nodes = d.get("nodes") if isinstance(d, dict) else None
        if not isinstance(nodes, dict):
            return 0
        with self._lk2:
            for name, rec in nodes.items():
                if isinstance(rec, dict):
                    # 再洗一遍：文件可能是老版本或被手改过，凭据字段一律剔掉
                    clean = self._clean(rec)
                    for k in ("last_seen", "first_seen", "count"):
                        if k in rec:
                            clean[k] = rec[k]
                    self._hist[name] = clean
        return len(self._hist)

    def save(self, force=False):
        """原子写。返回落盘路径，没写（节流中/没配路径）返回空串。"""
        if not self.path:
            return ""
        now = time.time()
        if not force and now - self._last_save < self.interval:
            return ""
        with self._lk2:
            nodes = {}
            for k, v in self._hist.items():
                rec = dict(self._clean(v))
                rec.update({"last_seen": v.get("last_seen"),
                            "first_seen": v.get("first_seen"),
                            "count": v.get("count")})
                nodes[k] = rec
            payload = {"version": VERSION, "at": now, "nodes": nodes}
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
                self._last_save = now
                self._dirty = False
                return self.path
            except OSError:
                return ""                  # 写不进去不该影响在线表工作


# ---------------------------------------------------------------- 自测

def _selftest():
    import tempfile
    path = os.path.join(tempfile.gettempdir(), f"fleet_nodes_st_{os.getpid()}.json")
    for p in (path, path + ".tmp"):
        if os.path.exists(p):
            os.remove(p)

    t = NodeTable(path, interval=0)
    # ① 在线表语义不变
    t.with_lock(lambda d: d.__setitem__("PC-A", {"sock": object(), "token": "tok-abc",
                                                 "platform": "windows",
                                                 "capabilities": ["exec"]}))
    assert t.with_lock(lambda d: "PC-A" in d)

    # ② 历史：凭据绝不落盘
    t.observe("PC-A", {"platform": "windows", "capabilities": ["exec", "push"],
                       "token": "tok-abc", "password": "hunter2", "info": {"os": "win10"}})
    t.save(force=True)
    raw = open(path, encoding="utf-8").read()
    assert "tok-abc" not in raw and "hunter2" not in raw, raw
    assert "windows" in raw and "exec" in raw

    # ③ 重启后历史还在，但**不会**被当成在线
    t2 = NodeTable(path, interval=0)
    assert t2.with_lock(lambda d: len(d)) == 0, "重启后不许把历史当在线"
    h = t2.history()
    assert "PC-A" in h and h["PC-A"]["platform"] == "windows", h
    assert h["PC-A"]["capabilities"] == ["exec", "push"], h
    assert h["PC-A"]["count"] == 1 and h["PC-A"]["last_seen"], h

    # ④ 合并：第二次上报能力变了，历史要跟上，但首次时间不能变
    first = h["PC-A"]["first_seen"]
    t2.observe("PC-A", {"platform": "windows", "capabilities": ["exec"]})
    t2.observe("PC-A", {"platform": "windows", "capabilities": ["exec", "ctx"]})
    h2 = t2.history()["PC-A"]
    assert h2["capabilities"] == ["exec", "ctx"], h2
    assert h2["count"] == 3, h2
    assert h2["first_seen"] == first, h2

    # ⑤ 节流：间隔内不重复写（别让心跳每秒打一次盘）
    slow = NodeTable(os.path.join(tempfile.gettempdir(), f"fleet_nodes_slow_{os.getpid()}.json"),
                     interval=60)
    slow.observe("N1", {"platform": "android"}, force=True)
    mt = os.path.getmtime(slow.path)
    slow.observe("N1", {"platform": "android"})
    assert os.path.getmtime(slow.path) == mt, "间隔内不该写盘"
    slow.observe("N1", {"platform": "android"}, force=True)
    assert os.path.getmtime(slow.path) >= mt

    # ⑥ 排序：最近见过的在前
    slow.observe("N2", {"platform": "linux"}, force=True)
    names = [r["name"] for r in slow.seen()]
    assert names[0] == "N2", names

    # ⑦ 坏文件不影响启动（fail-open 到空历史，而不是起不来）
    bad = os.path.join(tempfile.gettempdir(), f"fleet_nodes_bad_{os.getpid()}.json")
    open(bad, "w", encoding="utf-8").write("{这不是 json")
    t3 = NodeTable(bad, interval=0)
    assert t3.history() == {}, t3.history()

    # ⑧ 没有路径时静默不落盘（自检/临时场景）
    t4 = NodeTable(None)
    t4.observe("X", {"platform": "?"})
    assert t4.save(force=True) == "" and t4.history()["X"]

    # ⑨ E-7：能真删
    assert t2.forget("PC-A") is True
    assert t2.history() == {} and t2.forget("PC-A") is False

    # ⑩ 原子写不留 .tmp
    assert not os.path.exists(path + ".tmp"), "残留 .tmp 说明不是原子替换"

    for p in (path, slow.path, bad):
        if os.path.exists(p):
            os.remove(p)
    print("registry \u81ea\u6d4b\uff1a10 \u9879\u5168\u8fc7")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    print(__doc__)
