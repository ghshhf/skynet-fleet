"""执行包装器：给每次能力调用留下可回放的证据。

为什么必须有这个模块（2026-10-22 实测，不是推测）：
    `fleet_journal.db` 共 187 条流水，其中 `status='done'` 的有 **0 条**；
    160 条 task 全部停在「开始了」，`topic` 字段去重后是 **0 个**。
    ⇒ 不是没人写流水，是**没有任何一层在能力调用前后写**。
    后果是连锁的：
        followups 全是永远挂着的账 → 记忆的 evidence 表 0 行
        → 技能进化没有原料，自动长出来的技能必然是编的。
    这个模块补的就是那一行。

证据分级（技能进化按前缀分，**不许混**）：
    ``exec:<trace>``         能力真的跑过（本模块写的）—— 强证据
    ``mem:<file>#L<a>-<b>``  从记忆日志导入的事实陈述 —— 次级证据，可回查原文
    混在一起的坏处：以后没法回答「这条技能是真的跑通过，还是只是我写过」。

只记参数名，不记参数值：
    流水会持久化，还可能进模型上下文。参数值里可能有序列号、路径、用户数据。
    记「参数名 + 值的形状」（str 长度 / list 长度 / 类型）足以定位问题，
    又不把值写进盘。敏感值另有 `journal.redact()` 兜底。

失败也留痕：
    失败记 `status='open'`（**不是** done），body 里带 code 与原因。
    成功记 `done`。这样「开始了但没成」在账上永远看得见，
    而不是一个被静默吞掉的异常。
"""
import time
import uuid

import journal as J


# 证据前缀（与 skills_evolve 里的分级判定共用同一套常量）
EXEC_PREFIX = "exec:"
MEM_PREFIX = "mem:"


def _shape(v):
    """值的形状（不取值本身）。"""
    if v is None:
        return "none"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, str):
        return "str(%d)" % len(v)
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (list, tuple)):
        return "list(%d)" % len(v)
    if isinstance(v, dict):
        return "dict(%d)" % len(v)
    return type(v).__name__


def arg_summary(args):
    """参数摘要：只留名字与形状。"""
    if not args:
        return "[]"
    return "[" + ", ".join("%s:%s" % (k, _shape(args[k]))
                            for k in sorted(args)) + "]"


def is_exec_trace(trace):
    return bool(trace) and trace.startswith(EXEC_PREFIX)


def is_mem_trace(trace):
    return bool(trace) and trace.startswith(MEM_PREFIX)


class Executor:
    """把能力调用包起来，前后写流水。

    `journal` 传 None 就只调用不记录（测试与一次性脚本用）。
    宁可没证据，也不许假装有证据。
    """

    def __init__(self, reg, journal=None, actor="fleet", gate=None):
        self.reg = reg
        self.journal = journal
        self.actor = actor
        self.gate = gate

    # ---------- trace ----------

    def new_trace(self, label=""):
        """开一次新的执行。返回 `exec:<12位十六进制>`。"""
        return EXEC_PREFIX + uuid.uuid4().hex[:12]

    # ---------- 记非能力事件 ----------

    def note(self, body, topic=None, trace=None, kind="task", status=J.DONE_STATUS):
        """记一条与能力调用无关但属于这次执行的事件（例如「用户确认了」）。"""
        if not self.journal:
            return 0
        return self.journal.log(kind, self.actor, body, topic=topic, trace=trace,
                                 status=status)

    # ---------- 调用 ----------

    def call(self, cap, device="", args=None, trace=None, gate=None):
        """调一次能力，前后写流水。返回原始 `DeviceResult`（调用方拿到的还是它）。

        刻意**不**改调用语义：失败仍旧返回失败的 DeviceResult，
        由上层决定重试还是换设备——本模块只负责留痕，不负责决策。

        `gate` 不传就用构造时那个（闸门决定「要不要人点确认」，与留痕无关，
        但必须原样传下去，否则副作用能力的审批语义会变）。
        """
        args = dict(args or {})
        trace = trace or self.new_trace()
        gate = self.gate if gate is None else gate

        # 1) 能力名不存在：这是调用方的错，也得留痕（不然这一次的 trace 会缺一段）
        if self.reg.capability(cap) is None:
            self._write(cap, trace, ok=False, body="能力不存在：%s" % cap,
                        code="unsupported")
            return self.reg.call(cap, device, args, gate=gate)

        # 2) 开始
        self._write(cap, trace, ok=False,
                    body="开始 %s device=%s args=%s" % (cap, device or "-",
                                                      arg_summary(args)),
                    code="running")

        # 3) 真的调
        t0 = time.time()
        r = self.reg.call(cap, device, args, gate=gate)
        ms = (time.time() - t0) * 1000.0

        # 4) 结果
        if getattr(r, "ok", False):
            keys = ",".join(sorted((getattr(r, "data", None) or {}).keys()))
            self._write(cap, trace, ok=True,
                        body="完成 %s device=%s ms=%.0f side_effect=%s data=[%s]" % (
                            cap, getattr(r, "device", "") or "-", ms,
                            bool(getattr(r, "side_effect", False)), keys),
                        code="ok", ms=ms)
        else:
            self._write(cap, trace, ok=False,
                        body="失败 %s device=%s code=%s err=%s" % (
                            cap, getattr(r, "device", "") or "-",
                            getattr(r, "code", "error"), getattr(r, "err", "")),
                        code=getattr(r, "code", "error"), ms=ms)
        return r

    # ---------- 内部 ----------

    def _write(self, cap, trace, ok, body, code, ms=0.0):
        if not self.journal:
            return 0
        return self.journal.log(
            "task", self.actor, body,
            topic="cap:%s" % cap,
            trace=trace,
            status=J.DONE_STATUS if ok else J.OPEN_STATUS,
        )


def _selftest():
    """离线自测：用假注册表验证「前后都写、失败不写 done、参数值不落盘」。"""
    import os
    import sys
    import tempfile

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    from fleet.core import registry as RC
    from fleet.core import result as R
    from fleet.core.device import BaseDevice

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    j = J.Journal(path, cap=200)
    try:
        reg = RC.Registry(path="", ttl=0)
        reg.register(RC.Capability("probe.echo", "回声"))
        reg.register(RC.Capability("probe.boom", "会炸"))

        class FakeDev(BaseDevice):
            kind = "fake"

            def __init__(self, name):
                super().__init__(name, via="fake")

            def probe(self):
                return True, ""

            def caps(self):
                return ["probe.echo", "probe.boom"]

            def invoke(self, cap, args=None):
                if cap == "probe.boom":
                    return R.DeviceResult.error(cap, self.name, "炸了")
                return R.DeviceResult.success(cap, self.name, {"echo": (args or {}).get("x")},
                                             via=self.via)

        reg.add(FakeDev("f:fake"))
        ex = Executor(reg, journal=j)

        # 1) 成功：先 open 后 done，同一个 trace
        tr = ex.new_trace()
        r = ex.call("probe.echo", args={"x": 7}, trace=tr)
        assert r.ok, r
        seq = j.by_trace(tr)
        assert len(seq) == 2, seq
        assert seq[0]["status"] == J.OPEN_STATUS, seq
        assert seq[1]["status"] == J.DONE_STATUS, seq
        assert seq[0]["id"] < seq[1]["id"]
        assert seq[0]["topic"] == "cap:probe.echo"
        assert is_exec_trace(tr)
        print("[selftest] 成功调用：先 open 后 done（同 trace，顺序可回放） OK")

        # 2) 失败：只有 open，没有 done
        tr2 = ex.new_trace()
        r2 = ex.call("probe.boom", args={}, trace=tr2)
        assert not r2.ok
        seq2 = j.by_trace(tr2)
        assert len(seq2) == 2, seq2
        assert all(s["status"] == J.OPEN_STATUS for s in seq2), seq2
        assert "炸了" in seq2[-1]["body"], seq2[-1]["body"]
        print("[selftest] 失败调用：留 open + 原因，不写 done OK")

        # 3) 参数值不落盘（只留名字与形状）
        tr3 = ex.new_trace()
        ex.call("probe.echo", args={"x": "hunter2-secret"}, trace=tr3)
        bodies = " ".join(s["body"] for s in j.by_trace(tr3))
        assert "hunter2-secret" not in bodies, bodies
        assert "x:str(14)" in bodies, bodies
        print("[selftest] 参数值不落盘（只留名字与形状） OK")

        # 4) 能力不存在：也要留痕
        tr4 = ex.new_trace()
        ex.call("probe.nope", trace=tr4)
        assert len(j.by_trace(tr4)) == 1, j.by_trace(tr4)
        print("[selftest] 能力不存在也留痕 OK")

        # 5) 没有 journal 时只调用不记录（不许假装有证据）
        ex2 = Executor(reg, journal=None)
        r5 = ex2.call("probe.echo", args={"x": 1})
        assert r5.ok
        print("[selftest] 无 journal 时不记录 OK")

        # 6) 证据分级：exec / mem 前缀不混
        assert is_exec_trace("exec:abc") and not is_mem_trace("exec:abc")
        assert is_mem_trace("mem:2026-10-22.md#L1-5") and not is_exec_trace("mem:x")
        assert not is_exec_trace("") and not is_exec_trace(None)
        print("[selftest] 证据分级（exec / mem 不混） OK")
        print("[selftest] PASS")
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
