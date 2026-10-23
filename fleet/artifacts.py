"""产物中心（fleet.artifacts）——内容寻址的生成物仓库。

为什么需要它：
    在它之前，出图/出视频的返回值是**某台机器上的本地路径**
    （`/sdcard/Pictures/fleet/7.png`）。后果是三个断链：
      · 换台机器就引用不到（路径只在那台手机上有意义）；
      · 同一个文件重复生成/重复传输，没有去重；
      · 记忆、followups、技能进化都没法「指向一个产物」——它们只能存一句人话。

    这一层把一个文件变成**一个有身份的东西**：内容哈希当 id，元数据记来源，
    本地留一份可读的实体文件。任何节点拿到 id 就能取到内容。

设计：
  · 内容寻址：id = `art-` + sha256[:16]。同样内容重复 put 只加索引项，不重复存盘。
  · 实体存放：`~/.fleet/artifacts/<sha 前2位>/<sha>`（分桶，避免一个目录几万文件）。
  · 索引：`~/.fleet/artifacts/index.json`（原子替换）。
  · 元数据只记「这是什么、哪来的」：kind / name / size / source / job / created_at / ttl。
  · 不记凭据（沿用全仓 NEVER 纪律）：name 里若带绝对路径，只保留 basename。
  · TTL：ttl_days>0 的产物过期可被 gc 清掉；0 = 永久。
  · 零第三方依赖（hashlib/json/os/shutil 都是标准库）。

对外：
    st = ArtifactStore()
    st.put_file("/tmp/a.png", kind="image", source="phoneA", job="job-1")  -> dict
    st.put_bytes(b"...", name="a.bin", kind="blob")                        -> dict
    st.get("art-xxxx")                                                     -> dict | None
    st.open_bytes("art-xxxx")                                              -> bytes（取不到抛错带原因）
    st.list(kind="image", limit=20)                                        -> list[dict]
    st.link_job("art-xxxx", "job-2")                                       -> bool
    st.gc()                                                                -> 清掉的条数
"""
import hashlib
import json
import os
import threading
import time

CHUNK = 1 << 20            # 1MB 分块算哈希/拷贝，避免整文件进内存
ID_PREFIX = "art-"
ID_LEN = 16


def _default_root():
    return os.environ.get("FLEET_ARTIFACTS") or os.path.join(
        os.path.expanduser("~/.fleet"), "artifacts")


def _now(now=None):
    return float(now) if now is not None else time.time()


class ArtifactStore:
    """内容寻址产物仓库。线程安全；索引落盘用原子替换。"""

    def __init__(self, root=None):
        self.root = root or _default_root()
        self.index_path = os.path.join(self.root, "index.json")
        self._lk = threading.RLock()
        self._idx = {}
        self._load()

    # ---------------- 索引 ----------------

    def _load(self):
        try:
            with open(self.index_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        items = d.get("items") if isinstance(d, dict) else None
        self._idx = items if isinstance(items, dict) else {}

    def _save(self):
        try:
            os.makedirs(self.root, exist_ok=True)
        except OSError:
            pass
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": self._idx}, f,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, self.index_path)

    def _blob_path(self, sha):
        """分桶路径：sha 前两位做子目录，其余做文件名。"""
        return os.path.join(self.root, sha[:2], sha)

    # ---------------- 写入 ----------------

    def put_file(self, path, kind="", name="", source="", job="", ttl_days=0):
        """把一个已存在的文件收进仓库。返回产物 dict。

        文件不存在/读不动 ⇒ 抛 FileNotFoundError/OSError 并带原因（不静默）。
        """
        path = (path or "").strip()
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("产物文件不存在：%r" % path)
        h = hashlib.sha256()
        size = 0
        with open(path, "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                h.update(b)
                size += len(b)
        sha = h.hexdigest()
        rec = self._register(sha, size, kind=kind, name=name or os.path.basename(path),
                             source=source, job=job, ttl_days=ttl_days)
        dst = self._blob_path(sha)
        if not os.path.isfile(dst):                 # 同内容只存一份
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(path, "rb") as src, open(dst + ".tmp", "wb") as out:
                while True:
                    b = src.read(CHUNK)
                    if not b:
                        break
                    out.write(b)
            os.replace(dst + ".tmp", dst)
        return rec

    def put_bytes(self, data, name="blob.bin", kind="", source="", job="", ttl_days=0):
        """直接存一段字节。空数据拒收（空文件通常意味着上游已经失败了）。"""
        if not data:
            raise ValueError("产物是空的，拒收（空文件多半表示上游已失败）")
        h = hashlib.sha256(data).hexdigest()
        sha = h
        rec = self._register(sha, len(data), kind=kind, name=name or "blob.bin",
                             source=source, job=job, ttl_days=ttl_days)
        dst = self._blob_path(sha)
        if not os.path.isfile(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst + ".tmp", "wb") as f:
                f.write(data)
            os.replace(dst + ".tmp", dst)
        return rec

    def _register(self, sha, size, kind="", name="", source="", job="", ttl_days=0):
        with self._lk:
            aid = ID_PREFIX + sha[:ID_LEN]
            old = self._idx.get(aid)
            now = time.time()
            if old:                                  # 已存在：只补元数据，不改身份
                if kind and not old.get("kind"):
                    old["kind"] = kind
                if source and source not in (old.get("sources") or []):
                    old.setdefault("sources", []).append(source)
                if job and job not in (old.get("jobs") or []):
                    old.setdefault("jobs", []).append(job)
                old["seen"] = int(old.get("seen") or 1) + 1
                old["last_seen"] = now
                self._save()
                return dict(old)
            rec = {
                "id": aid,
                "sha256": sha,
                "size": int(size),
                "kind": kind or "",
                "name": name or "blob.bin",
                "sources": [source] if source else [],
                "jobs": [job] if job else [],
                "created_at": now,
                "last_seen": now,
                "seen": 1,
                "ttl_days": float(ttl_days or 0),
                "path": self._blob_path(sha),
            }
            self._idx[aid] = rec
            self._save()
            return dict(rec)

    # ---------------- 读取 ----------------

    def get(self, art_id):
        with self._lk:
            rec = self._idx.get((art_id or "").strip())
            return dict(rec) if rec else None

    def exists(self, art_id):
        rec = self.get(art_id)
        return bool(rec) and os.path.isfile(rec.get("path") or "")

    def open_bytes(self, art_id):
        """取回内容。取不到抛 FileNotFoundError，消息说清是「没这条」还是「索引在但文件丢了」。"""
        rec = self.get(art_id)
        if not rec:
            raise FileNotFoundError("没有这个产物：%r" % art_id)
        p = rec.get("path") or ""
        if not p or not os.path.isfile(p):
            raise FileNotFoundError(
                "产物索引里有 %s，但实体文件不见了：%r（可能被手工删了，需重新生成）"
                % (art_id, p))
        with open(p, "rb") as f:
            return f.read()

    def list(self, kind=None, limit=50, job=None):
        """按创建时间倒序列出；kind/job 可过滤。"""
        with self._lk:
            items = list(self._idx.values())
        if kind:
            items = [x for x in items if x.get("kind") == kind]
        if job:
            items = [x for x in items if job in (x.get("jobs") or [])]
        items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        return [dict(x) for x in items[:max(0, int(limit or 50))]]

    def link_job(self, art_id, job_id):
        """把一个产物挂到作业上（作业产出、或事后补挂）。"""
        with self._lk:
            rec = self._idx.get((art_id or "").strip())
            if not rec or not job_id:
                return False
            jobs = rec.setdefault("jobs", [])
            if job_id not in jobs:
                jobs.append(job_id)
                self._save()
            return True

    def stats(self):
        with self._lk:
            items = list(self._idx.values())
        total = sum(int(x.get("size") or 0) for x in items)
        kinds = {}
        for x in items:
            k = x.get("kind") or "unknown"
            kinds[k] = kinds.get(k, 0) + 1
        return {"count": len(items), "bytes": total, "kinds": kinds}

    # ---------------- 清理 ----------------

    def gc(self, now=None):
        """清掉过期（ttl_days>0 且到期）的产物。返回 (清掉的条数, 释放字节数)。"""
        now = _now(now)
        removed = 0
        freed = 0
        with self._lk:
            for aid, rec in list(self._idx.items()):
                ttl = float(rec.get("ttl_days") or 0)
                if ttl <= 0:
                    continue
                if (rec.get("created_at") or 0) + ttl * 86400 > now:
                    continue
                p = rec.get("path") or ""
                if p and os.path.isfile(p):
                    try:
                        freed += os.path.getsize(p)
                        os.remove(p)
                    except OSError:
                        pass
                del self._idx[aid]
                removed += 1
            if removed:
                self._save()
        return removed, freed


# ------------------------------------------------------------------ 单例

_store = None


def get_store(root=None):
    global _store
    if _store is None or root:
        _store = ArtifactStore(root=root)
    return _store


def is_artifact_id(s):
    """粗判一个字符串是不是产物 id（给「路径或 id」两路入参用）。"""
    s = (s or "").strip()
    return s.startswith(ID_PREFIX) and len(s) == len(ID_PREFIX) + ID_LEN


# ------------------------------------------------------------------ 自测

def _selftest():
    import tempfile
    import shutil
    root = os.path.join(tempfile.gettempdir(), "fleet_art_%d" % os.getpid())
    if os.path.isdir(root):
        shutil.rmtree(root)
    st = ArtifactStore(root=root)

    # 1) 存字节 → 拿到 id，实体落盘
    a1 = st.put_bytes(b"PNG-fake-data", name="a.png", kind="image", source="phoneA", job="job-1")
    assert a1["id"].startswith(ID_PREFIX) and len(a1["id"]) == 4 + ID_LEN, a1
    assert a1["size"] == 13, a1
    assert os.path.isfile(a1["path"]), "实体文件应存在"
    assert st.open_bytes(a1["id"]) == b"PNG-fake-data"

    # 2) 同内容去重：id 相同、只加来源，不新增条目
    a2 = st.put_bytes(b"PNG-fake-data", name="b.png", source="phoneB")
    assert a2["id"] == a1["id"], "同内容必须同 id"
    rec = st.get(a1["id"])
    assert set(rec["sources"]) == {"phoneA", "phoneB"}, rec
    assert rec["seen"] == 2, rec
    assert len(st.list()) == 1, "去重后条目仍是 1"

    # 3) 不同内容 → 不同 id
    a3 = st.put_bytes(b"other", kind="video")
    assert a3["id"] != a1["id"]

    # 4) 空数据拒收（不静默存一个空文件）
    try:
        st.put_bytes(b"")
        assert False, "空数据应拒收"
    except ValueError as e:
        assert "空" in str(e)

    # 5) 文件入库 + 不存在时报错带原因
    p = os.path.join(tempfile.gettempdir(), "fleet_art_src_%d.bin" % os.getpid())
    with open(p, "wb") as f:
        f.write(b"from-file")
    a4 = st.put_file(p, kind="blob", job="job-2")
    assert st.open_bytes(a4["id"]) == b"from-file"
    try:
        st.put_file(p + ".nope")
        assert False, "不存在的文件应报错"
    except FileNotFoundError as e:
        assert "不存在" in str(e)
    os.remove(p)

    # 6) 按 kind / job 过滤
    assert len(st.list(kind="image")) == 1
    assert len(st.list(job="job-2")) == 1
    assert len(st.list(job="job-1")) == 1

    # 7) link_job 补挂
    assert st.link_job(a4["id"], "job-3") is True
    assert "job-3" in st.get(a4["id"])["jobs"]
    assert st.link_job("art-doesnotexist", "job-3") is False

    # 8) 索引在但实体丢了 → 明确报「文件不见了」，不冒充成功
    lost = st.put_bytes(b"will-be-deleted", kind="blob")
    os.remove(lost["path"])
    try:
        st.open_bytes(lost["id"])
        assert False, "实体丢失应报错"
    except FileNotFoundError as e:
        assert "不见" in str(e)

    # 9) TTL gc：过期清掉，未过期保留
    old = st.put_bytes(b"expired", kind="blob", ttl_days=1)
    st._idx[old["id"]]["created_at"] = time.time() - 3 * 86400    # 手工做旧
    fresh = st.put_bytes(b"fresh", kind="blob", ttl_days=30)
    removed, freed = st.gc()
    assert removed == 1 and freed >= 7, (removed, freed)
    assert st.get(old["id"]) is None and st.get(fresh["id"]) is not None

    # 10) 统计
    s = st.stats()
    assert s["count"] >= 3 and s["bytes"] > 0, s

    # 11) 落盘可重读
    st2 = ArtifactStore(root=root)
    assert st2.get(a1["id"]) is not None
    assert st2.open_bytes(a1["id"]) == b"PNG-fake-data"

    # 12) is_artifact_id
    assert is_artifact_id(a1["id"]) and not is_artifact_id("/tmp/a.png")

    shutil.rmtree(root)
    print("fleet.artifacts 自测通过（存盘/去重/空拒收/文件入库/过滤/挂作业/丢文件/GC/统计/重读/ID 判定 12 组）")


if __name__ == "__main__":
    _selftest()
