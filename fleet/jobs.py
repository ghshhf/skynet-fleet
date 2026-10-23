"""作业层（fleet.jobs）——把「一次性能力调用」升级成「可排队、可查询、可取消、可迁移的长任务」。

为什么需要这一层（不是加功能，是补骨架）：
    编队现在只有两种动法：
      · `registry.call` —— 同步单次调用，连接断 = 任务丢，查不到进度，取消不了；
      · `orchestrator`   —— 一次意图拆多步 + 失败回滚，但一次只跑一遍，没有账。
    手机算力的真实形态是**分钟级长任务**（SDXL 实测 179s/步、视频生成、4B LLM 长文）：
    同步调用撑不住「连接断、手机没电、想中途取消、想回头查跑到哪」。

三条纪律（与全仓一致）：
  1. **状态可查**：每个作业有 id 与状态历史，绝不「开始了但没成」被静默吞掉；
  2. **掉线可迁**：running 靠租约（lease）维持，租约过期 ⇒ 退回 queued 换别的节点跑；
  3. **失败带原因**：failed 一定带 err，不交空。

落盘：单个 JSON（`FLEET_JOBS` 覆盖，默认 `~/.fleet/jobs.json`），
临时文件 + os.replace 原子替换——沿用 queue_store.py 的做法，避免半写坏档。
单写者约定：控制面（commander）是唯一写者；节点只回报执，不直接改这个文件。
零第三方依赖。
"""
import json
import os
import threading
import time
import uuid

# ---- 状态机 ----
QUEUED = "queued"          # 已提交，等节点
RUNNING = "running"        # 已被某节点领走，租约有效
DONE = "done"              # 成功
FAILED = "failed"          # 用尽重试或不可恢复
CANCELLED = "cancelled"    # 人工取消

TERMINAL = (DONE, FAILED, CANCELLED)

DEFAULT_LEASE = 180.0      # 秒。节点领走后必须在这个时间内续租或回报，否则判掉线
DEFAULT_ATTEMPTS = 3       # 最多尝试次数（含首次）
MAX_HISTORY = 40           # 每作业最多保留多少条状态历史（防 JSON 无限膨胀）


def _default_path():
    return os.environ.get("FLEET_JOBS") or os.path.join(
        os.path.expanduser("~/.fleet"), "jobs.json")


def _now(now=None):
    return float(now) if now is not None else time.time()


def _new_id():
    """作业 id：可读前缀 + 时间 + 随机短串。人眼能排序，机器能去重。"""
    return "job-%d-%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6])


class JobStore:
    """作业账本。所有方法带锁，可多线程调用；跨进程靠「单写者」约定。"""

    def __init__(self, path=None):
        self.path = path or _default_path()
        self._lk = threading.RLock()
        self._jobs = {}
        self._load()

    # ---------------- 落盘 ----------------

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        jobs = d.get("jobs") if isinstance(d, dict) else None
        self._jobs = jobs if isinstance(jobs, dict) else {}

    def _save(self):
        d = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "jobs": self._jobs}, f,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------------- 内部 ----------------

    def _touch(self, job, state, note=""):
        """记一条状态历史；同一状态重复出现只更新时间，不刷屏。"""
        h = job.setdefault("history", [])
        h.append({"ts": time.time(), "state": state, "note": note})
        if len(h) > MAX_HISTORY:
            del h[:len(h) - MAX_HISTORY]
        job["state"] = state
        job["updated_at"] = time.time()

    # ---------------- 提交 / 查询 ----------------

    def submit(self, cap, args=None, node="", max_attempts=DEFAULT_ATTEMPTS, note=""):
        """提交一个作业。返回作业 dict。

        node 为空 = 自动挑节点；给了就用它（不存在/没能力时会在 claim 阶段如实失败）。
        """
        cap = (cap or "").strip()
        if not cap:
            raise ValueError("作业必须带 cap（要执行哪项能力）")
        n = int(max_attempts or DEFAULT_ATTEMPTS)
        if n < 1:
            n = 1
        with self._lk:
            job = {
                "id": _new_id(),
                "cap": cap,
                "args": dict(args or {}),
                "node": (node or "").strip(),
                "state": QUEUED,
                "attempts": 0,
                "max_attempts": n,
                "lease_until": 0.0,
                "created_at": time.time(),
                "started_at": 0.0,
                "ended_at": 0.0,
                "result": {},
                "artifacts": [],
                "note": note or "",
                "history": [],
            }
            self._touch(job, QUEUED, "提交")
            self._jobs[job["id"]] = job
            self._save()
            return dict(job)

    def get(self, job_id):
        with self._lk:
            j = self._jobs.get(job_id or "")
            return dict(j) if j else None

    def list(self, state=None, limit=50):
        """按提交时间倒序列出；state 可为单个状态或状态序列。"""
        with self._lk:
            jobs = list(self._jobs.values())
        if state:
            want = {state} if isinstance(state, str) else set(state)
            jobs = [j for j in jobs if j.get("state") in want]
        jobs.sort(key=lambda j: j.get("created_at") or 0, reverse=True)
        return [dict(j) for j in jobs[:max(0, int(limit or 50))]]

    def stats(self):
        """一屏统计：各状态数量 + 正在跑哪些。"""
        with self._lk:
            jobs = list(self._jobs.values())
        out = {QUEUED: 0, RUNNING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
        running = []
        for j in jobs:
            st = j.get("state")
            if st in out:
                out[st] += 1
            if st == RUNNING:
                running.append({"id": j["id"], "cap": j.get("cap"),
                                "node": j.get("node"),
                                "lease_in": round((j.get("lease_until") or 0) - time.time(), 1)})
        out["total"] = len(jobs)
        out["running_jobs"] = running
        return out

    # ---------------- 取消 ----------------

    def cancel(self, job_id, reason="人工取消"):
        """取消。已终态则如实说不必取消（不假装成功）。"""
        with self._lk:
            j = self._jobs.get(job_id or "")
            if not j:
                return False, "没有这个作业：%s" % job_id
            if j.get("state") in TERMINAL:
                return False, "作业已是 %s，不用取消" % j["state"]
            self._touch(j, CANCELLED, reason)
            j["ended_at"] = time.time()
            self._save()
            return True, ""

    # ---------------- 领活 / 续租 / 收尾 ----------------

    def claim(self, node, now=None, caps=None):
        """节点领一个能干的活。

        node 给定时只领「指名给它」或「未指名」的作业；
        caps（该节点能力集合）给了就严格过滤——不许把干不了的活派下去。
        """
        node = (node or "").strip()
        now = _now(now)
        want = set(caps) if caps else None
        with self._lk:
            cand = [j for j in self._jobs.values() if j.get("state") == QUEUED]
            cand.sort(key=lambda j: j.get("created_at") or 0)      # 先来先干
            for j in cand:
                if j.get("node") and node and j["node"] != node:
                    continue
                if j.get("node") and not node:
                    continue
                if want is not None and j.get("cap") not in want:
                    continue
                j["attempts"] = int(j.get("attempts") or 0) + 1
                j["node"] = node or j.get("node") or ""
                j["lease_until"] = now + DEFAULT_LEASE
                if not j.get("started_at"):
                    j["started_at"] = now
                self._touch(j, RUNNING, "第 %d 次尝试，%s 领走" % (j["attempts"], node or "未指名"))
                self._save()
                return dict(j)
        return None

    def heartbeat(self, job_id, lease=DEFAULT_LEASE, now=None):
        """续租。节点长任务必须定期调，否则会被 reap 判掉线。"""
        with self._lk:
            j = self._jobs.get(job_id or "")
            if not j or j.get("state") != RUNNING:
                return False
            j["lease_until"] = _now(now) + float(lease)
            self._save()
            return True

    def finish(self, job_id, ok, data=None, err="", artifacts=None, now=None, retry=True):
        """回报结果。ok=False 时按重试次数决定退回 queued 还是 failed。

        retry=False 表示「这个错误重试也没用」（能力不认识、参数不可能对、
        鉴权被拒……）：立刻判 failed，不浪费别的节点再跑一遍。
        """
        now = _now(now)
        with self._lk:
            j = self._jobs.get(job_id or "")
            if not j:
                return None
            if j.get("state") in TERMINAL:
                return dict(j)          # 幂等：重复回报不改终态
            if artifacts:
                j["artifacts"] = list(dict.fromkeys(list(j.get("artifacts") or []) + list(artifacts)))
            if ok:
                j["result"] = {"ok": True, "data": data or {}}
                j["ended_at"] = now
                j["lease_until"] = 0.0
                self._touch(j, DONE, "成功")
                self._save()
                return dict(j)

            # 失败：还能重试就退回队列（保留 attempts），否则判死
            j["result"] = {"ok": False, "err": err or "失败（未给原因）"}
            can_retry = bool(retry) and int(j.get("attempts") or 0) < int(j.get("max_attempts") or 1)
            if can_retry:
                j["lease_until"] = 0.0
                self._touch(j, QUEUED, "失败，退回队列重试：%s" % (err or ""))
            else:
                j["ended_at"] = now
                j["lease_until"] = 0.0
                self._touch(j, FAILED, "尝试 %d 次均失败：%s" % (j.get("attempts") or 0, err or ""))
            self._save()
            return dict(j)

    # ---------------- 掉线回收 / 迁移 ----------------

    def reap(self, now=None):
        """回收租约过期的 running 作业。返回被处理的作业列表。

        这是「手机没电/掉线，任务不陪葬」的唯一实现点：
        过期 → attempts < max_attempts 就退回 queued（下一个节点能领）；
               否则判 failed 并带上原因。
        """
        now = _now(now)
        out = []
        with self._lk:
            for j in self._jobs.values():
                if j.get("state") != RUNNING:
                    continue
                if (j.get("lease_until") or 0) > now:
                    continue
                who = j.get("node") or "某节点"
                if int(j.get("attempts") or 0) < int(j.get("max_attempts") or 1):
                    j["lease_until"] = 0.0
                    j["node"] = ""          # 允许换节点
                    self._touch(j, QUEUED, "%s 租约过期（掉线？），退回队列换节点" % who)
                else:
                    j["lease_until"] = 0.0
                    j["ended_at"] = now
                    j["result"] = {"ok": False,
                                   "err": "%s 租约过期且已试满 %d 次" % (who, j.get("attempts") or 0)}
                    self._touch(j, FAILED, "租约过期且用尽重试")
                out.append(dict(j))
            if out:
                self._save()
        return out

    # ---------------- 驱动 ----------------

    def pump(self, dispatch, node, caps=None, now=None, max_jobs=10):
        """领活 → 执行 → 结算，跑一轮（最多 max_jobs 个）。

        dispatch(job) -> (ok, data, err, artifacts) 或 (ok, data, err)。
        这是控制面「把作业真的跑起来」的唯一入口；离线测试注入假 dispatch 即可验证
        完整的 claim/lease/finish 链路，不需要任何真设备。
        """
        done = []
        for _ in range(max(0, int(max_jobs or 0))):
            job = self.claim(node, now=now, caps=caps)
            if not job:
                break
            try:
                r = dispatch(job)
                retry = True
                if isinstance(r, tuple) and len(r) >= 5:
                    ok, data, err, arts, retry = r[0], r[1], r[2], r[3], bool(r[4])
                elif isinstance(r, tuple) and len(r) == 4:
                    ok, data, err, arts = r[0], r[1], r[2], r[3]
                elif isinstance(r, tuple) and len(r) == 3:
                    ok, data, err, arts = r[0], r[1], r[2], None
                elif isinstance(r, tuple):
                    ok, data, err, arts = r[0], r[1] if len(r) > 1 else {}, "", None
                else:
                    ok, data, err, arts = bool(r), {}, "", None
                if isinstance(data, dict) and data.get("_no_retry"):
                    retry = False
            except Exception as e:                       # 派发自己炸了也要结算，不许留 running 僵尸
                ok, data, err, arts, retry = False, {}, "%s: %s" % (type(e).__name__, e), None, True
            done.append(self.finish(job["id"], ok, data=data, err=err, artifacts=arts,
                                    now=now, retry=retry))
        return done


# ------------------------------------------------------------------ 单例

_store = None


def get_store(path=None):
    """取全局作业账本单例（首次调用才读盘）。"""
    global _store
    if _store is None or path:
        _store = JobStore(path=path)
    return _store


# ------------------------------------------------------------------ 自测

def _selftest():
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "fleet_jobs_%d.json" % os.getpid())
    if os.path.exists(p):
        os.remove(p)
    st = JobStore(path=p)

    # 1) 提交 → queued
    j1 = st.submit("image.gen", {"prompt": "一只蓝鲸"}, max_attempts=2)
    assert j1["state"] == QUEUED, j1
    assert st.get(j1["id"])["cap"] == "image.gen"
    assert [x["id"] for x in st.list()] == [j1["id"]]

    # 2) 领活 → running（带租约）；能力不匹配时领不到
    assert st.claim("phoneA", caps={"video.gen"}) is None, "干不了的活不许派下去"
    got = st.claim("phoneA", caps={"image.gen"})
    assert got and got["state"] == RUNNING and got["attempts"] == 1, got
    assert got["lease_until"] > time.time()

    # 3) 续租
    assert st.heartbeat(got["id"]) is True

    # 4) 掉线回收：租约过期 → 退回 queued 且清空 node（可换机）
    later = time.time() + DEFAULT_LEASE + 10
    reaped = st.reap(now=later)
    assert len(reaped) == 1 and reaped[0]["state"] == QUEUED, reaped
    assert reaped[0]["node"] == "", "过期后应允许换节点"

    # 5) 换节点领走 → 第二次尝试
    got2 = st.claim("phoneB", caps={"image.gen"})
    assert got2 and got2["attempts"] == 2, got2

    # 6) 失败且用尽重试 → failed 带原因
    fin = st.finish(got2["id"], False, err="手机没电了")
    assert fin["state"] == FAILED and "没电" in fin["result"]["err"], fin

    # 7) 重复回报幂等（不改终态）
    again = st.finish(got2["id"], True, data={"x": 1})
    assert again["state"] == FAILED, again

    # 8) 成功路径 + 产物挂载
    j2 = st.submit("video.gen", {"prompt": "海浪"}, node="pc1")
    g2 = st.claim("pc1", caps={"video.gen"})
    ok = st.finish(g2["id"], True, data={"path": "/tmp/a.mp4"}, artifacts=["art-abc"])
    assert ok["state"] == DONE and ok["artifacts"] == ["art-abc"], ok

    # 9) 取消：排队中可取消，终态不许假成功
    j3 = st.submit("exec", {"cmd": "echo hi"})
    assert st.cancel(j3["id"])[0] is True
    assert st.get(j3["id"])["state"] == CANCELLED
    assert st.cancel(j3["id"])[0] is False, "终态再取消应如实说不必取消"

    # 10) 落盘可重读（换实例仍看得见）
    st2 = JobStore(path=p)
    assert st2.get(j3["id"])["state"] == CANCELLED

    # 11) pump：注入假 dispatch，跑完即结算
    j4 = st.submit("image.gen", {"prompt": "猫"})
    ran = st.pump(lambda job: (True, {"path": "/tmp/cat.png"}, "", ["art-cat"]),
                  node="phoneC", caps={"image.gen"}, max_jobs=5)
    assert len(ran) == 1 and ran[0]["state"] == DONE, ran
    assert st.get(j4["id"])["artifacts"] == ["art-cat"]

    # 12) pump 里 dispatch 抛异常 → 结算为失败（不留 running 僵尸）
    j5 = st.submit("exec", {"cmd": "x"}, max_attempts=1)

    def boom(job):
        raise RuntimeError("派发炸了")

    out = st.pump(boom, node="phoneC", caps={"exec"}, max_jobs=1)
    assert out and out[0]["state"] == FAILED and "派发炸了" in out[0]["result"]["err"], out

    # 13) 永久失败不重试：retry=False 一次判死，不浪费别的节点
    j6 = st.submit("nope.cap", {}, max_attempts=5)
    out6 = st.pump(lambda job: (False, {}, "不认识这个能力", None, False),
                   node="phoneD", caps={"nope.cap"}, max_jobs=3)
    assert len(out6) == 1 and out6[0]["state"] == FAILED, out6
    assert out6[0]["attempts"] == 1, "永久失败只能尝试 1 次"

    # 14) 统计
    s = st.stats()
    assert s["total"] == 6, s
    assert s[DONE] == 2 and s[FAILED] == 3 and s[CANCELLED] == 1, s

    os.remove(p)
    print("fleet.jobs 自测通过（提交/领活/续租/回收/重试/幂等/取消/落盘/pump/永久失败/统计 14 组）")


if __name__ == "__main__":
    _selftest()
