"""作业板（fleet.kinds.job_board）——把作业层与产物中心接进注册表。

为什么是一台「设备」而不是一堆函数：
    注册表是全仓唯一真相（终端补全 / MCP 工具表 / 「她」能看见的东西，全从这里长）。
    新增能力只要有一台设备声明 caps，就会自动出现在这三处——不需要再改三份清单。
    作业板是「编队中枢自己的一台设备」，不需要任何外部硬件，永远在线。

能力（名字一律 domain.verb）：
    job.submit  提交长任务（有副作用：写账本）
    job.list    列出作业（只读）
    job.get     查单个作业（只读）
    job.cancel  取消作业（有副作用）
    artifact.put   收一个产物（有副作用：写盘）
    artifact.get   取产物内容（只读；返回 base64，避免 MCP 层处理二进制）
    artifact.list  列产物（只读）

纪律：
  · 没装/坏档也如实说，不假装空账本；
  · 失败一律带原因；
  · artifact.get 返回 base64 而不是裸 bytes——MCP/JSON 层不认二进制。
"""
import base64

from ..core import BaseDevice, DeviceResult as R
from .. import artifacts as A
from .. import jobs as J

CAPS = ("job.submit", "job.list", "job.get", "job.cancel",
        "artifact.put", "artifact.get", "artifact.list")


class JobBoard(BaseDevice):
    kind = "job_board"
    via = "local"

    def __init__(self, name="fleet:jobs", job_store=None, art_store=None, **kw):
        super().__init__(name, via="local", **kw)
        self._jobs = job_store
        self._arts = art_store

    # 延迟取单例：构造这台设备时不要碰磁盘（probe 之前不许有副作用）
    @property
    def jobs(self):
        return self._jobs if self._jobs is not None else J.get_store()

    @property
    def arts(self):
        return self._arts if self._arts is not None else A.get_store()

    def probe(self):
        """账本与产物目录能不能落盘——只看可写性，不新建文件。"""
        import os
        try:
            for path in (self.jobs.path, os.path.dirname(self.arts.index_path)):
                d = path if os.path.isdir(path) else os.path.dirname(path)
                if d and not os.path.isdir(d):
                    # 目录还没建不算不可用（首次使用会建），但要能追溯到父目录
                    parent = os.path.dirname(d) or "."
                    if not os.path.isdir(parent):
                        return False, "作业账本目录不可达：%s" % parent
            return True, ""
        except Exception as e:
            return False, "作业板探测失败：%s: %s" % (type(e).__name__, e)

    def caps(self):
        return CAPS

    # ---------------- 派发 ----------------

    def invoke(self, cap, args=None):
        args = dict(args or {})
        try:
            if cap == "job.submit":
                return self._submit(args)
            if cap == "job.list":
                return R.success(cap, self.name,
                                 data={"jobs": self.jobs.list(
                                     state=args.get("state") or None,
                                     limit=args.get("limit") or 50)},
                                 via=self.via)
            if cap == "job.get":
                jid = (args.get("id") or "").strip()
                if not jid:
                    return R.error(cap, "缺少 id 参数", device=self.name, via=self.via)
                job = self.jobs.get(jid)
                if not job:
                    return R.error(cap, "没有这个作业：%s" % jid,
                                   device=self.name, via=self.via)
                return R.success(cap, self.name, data={"job": job}, via=self.via)
            if cap == "job.cancel":
                jid = (args.get("id") or "").strip()
                if not jid:
                    return R.error(cap, "缺少 id 参数", device=self.name, via=self.via)
                ok, err = self.jobs.cancel(jid, reason=args.get("reason") or "人工取消")
                if not ok:
                    return R.error(cap, err, device=self.name, via=self.via)
                return R.success(cap, self.name, data={"ok": True, "id": jid,
                                                       "state": J.CANCELLED},
                                 via=self.via)
            if cap == "artifact.put":
                return self._put(args)
            if cap == "artifact.get":
                aid = (args.get("id") or "").strip()
                if not aid:
                    return R.error(cap, "缺少 id 参数", device=self.name, via=self.via)
                rec = self.arts.get(aid)
                if not rec:
                    return R.error(cap, "没有这个产物：%s" % aid,
                                   device=self.name, via=self.via)
                data = self.arts.open_bytes(aid)
                return R.success(cap, self.name,
                                 data={"id": aid, "name": rec.get("name"),
                                       "kind": rec.get("kind"), "size": rec.get("size"),
                                       "b64": base64.b64encode(data).decode("ascii")},
                                 via=self.via)
            if cap == "artifact.list":
                return R.success(cap, self.name,
                                 data={"artifacts": self.arts.list(
                                     kind=args.get("kind") or None,
                                     job=args.get("job") or None,
                                     limit=args.get("limit") or 50),
                                       "stats": self.arts.stats()},
                                 via=self.via)
            return R.unsupported(cap, "作业板没有这项能力", device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 两个有副作用的分支 ----------------

    def _submit(self, args):
        cap = (args.get("cap") or "").strip()
        if not cap:
            return R.error("job.submit", "缺少 cap（要执行哪项能力）",
                           device=self.name, via=self.via)
        job = self.jobs.submit(cap,
                               args=args.get("args") or {},
                               node=args.get("node") or "",
                               max_attempts=int(args.get("max_attempts") or J.DEFAULT_ATTEMPTS),
                               note=args.get("note") or "")
        return R.success("job.submit", self.name,
                         data={"ok": True, "job": job}, via=self.via)

    def _put(self, args):
        """两种入参：path（本机已有文件）或 b64（MCP 传上来的字节）。"""
        kind = args.get("kind") or ""
        name = args.get("name") or ""
        job = args.get("job") or ""
        b64 = args.get("b64") or ""
        if b64:
            try:
                data = base64.b64decode(b64)
            except Exception as e:
                return R.error("artifact.put", "b64 解不开：%s" % e,
                               device=self.name, via=self.via)
            rec = self.arts.put_bytes(data, name=name or "blob.bin", kind=kind, job=job,
                                      source=args.get("source") or "")
        else:
            path = (args.get("path") or "").strip()
            if not path:
                return R.error("artifact.put", "缺 path 或 b64（二者必给其一）",
                               device=self.name, via=self.via)
            rec = self.arts.put_file(path, kind=kind, name=name, job=job,
                                     source=args.get("source") or "",
                                     ttl_days=float(args.get("ttl_days") or 0))
        return R.success("artifact.put", self.name,
                         data={"ok": True, "artifact": rec}, via=self.via)


# ------------------------------------------------------------------ 自测

def _selftest():
    import os
    import shutil
    import tempfile

    tmp = os.path.join(tempfile.gettempdir(), "fleet_jobboard_%d" % os.getpid())
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    jp = os.path.join(tmp, "jobs.json")
    ar = os.path.join(tmp, "arts")
    board = JobBoard(job_store=J.JobStore(path=jp), art_store=A.ArtifactStore(root=ar))

    ok, why = board.probe()
    assert ok, why

    # 1) 提交
    res = board.invoke("job.submit", {"cap": "image.gen", "args": {"prompt": "蓝鲸"}})
    assert res.ok, res.err
    jid = res.data["job"]["id"]
    assert res.data["job"]["state"] == J.QUEUED

    # 2) list / get
    lst = board.invoke("job.list", {})
    assert lst.ok and len(lst.data["jobs"]) == 1, lst.err
    got = board.invoke("job.get", {"id": jid})
    assert got.ok and got.data["job"]["cap"] == "image.gen"
    assert board.invoke("job.get", {"id": "job-nope"}).ok is False

    # 3) cancel + 终态再取消要如实拒
    c1 = board.invoke("job.cancel", {"id": jid})
    assert c1.ok and c1.data["state"] == J.CANCELLED
    c2 = board.invoke("job.cancel", {"id": jid})
    assert c2.ok is False, "终态再取消不该假成功"

    # 4) 参数缺失必须带原因
    bad = board.invoke("job.submit", {})
    assert bad.ok is False and "cap" in bad.err

    # 5) artifact.put（b64 路径）+ get + list
    p1 = board.invoke("artifact.put", {"b64": base64.b64encode(b"hello-art").decode(),
                                       "name": "a.txt", "kind": "text", "job": "job-9"})
    assert p1.ok, p1.err
    aid = p1.data["artifact"]["id"]
    g1 = board.invoke("artifact.get", {"id": aid})
    assert g1.ok and base64.b64decode(g1.data["b64"]) == b"hello-art", g1.err
    l1 = board.invoke("artifact.list", {"kind": "text"})
    assert l1.ok and len(l1.data["artifacts"]) == 1

    # 6) artifact.put（文件路径）
    fp = os.path.join(tmp, "b.bin")
    with open(fp, "wb") as f:
        f.write(b"\x00\x01\x02")
    p2 = board.invoke("artifact.put", {"path": fp, "kind": "blob"})
    assert p2.ok and p2.data["artifact"]["size"] == 3

    # 7) 缺 path/b64 → 明确拒
    p3 = board.invoke("artifact.put", {})
    assert p3.ok is False and ("path" in p3.err or "b64" in p3.err)

    # 8) 未知能力 → unsupported
    u = board.invoke("job.nope", {})
    assert u.ok is False

    shutil.rmtree(tmp)
    print("fleet.kinds.job_board 自测通过（提交/列出/查/取消/产物 b64+文件/参数校验 8 组）")


if __name__ == "__main__":
    _selftest()
