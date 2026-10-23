"""P0 门禁：证据层（执行留痕 + 记忆导入）。

这个门禁钉的是「技能进化有没有燃料」这一件事。

背景（2026-10-22 实测，不是推测）：
    `fleet_journal.db` 187 条流水里 `status='done'` 有 **0 条**，
    160 条 task 全部停在「开始了」。于是 followups 空转、记忆 evidence 空表、
    技能进化没有原料。根因是**没有任何一层在能力调用前后写流水**。

判据（每条都要能指向具体证据）：
    ① journal 有 trace 列；老库打开能自动补列，历史行 trace 为 NULL（不是空串）
    ② by_trace 按时间正序取回一次执行，且不串组
    ③ execute 成功调用 ⇒ 同 trace 一条 open + 一条 done
    ④ execute 失败调用 ⇒ 只有 open，没有 done（失败不许被吞成成功）
    ⑤ execute 不把参数**值**写进盘（只留名字与形状）
    ⑥ memory_import 解析：只取列表项，跳表格/代码块/短条目
    ⑦ memory_import 幂等：重复导入不新增
    ⑧ memory_import 溯源：trace 指到「文件+行号」，且是**原文件**行号
    ⑨ 密码/令牌不落明文
    ⑩ 证据分级：exec: 与 mem: 不混（不许把「我写过」冒充「我跑过」）
    ⑪ 证据库与对话流水库分离：记忆证据不许挤掉真实对话流水

全离线：只读本地文件与本地 sqlite，不联网、不调模型。
"""
import os
import sqlite3
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RESULTS = []


def check(no, what, fn, timeout=60):
    """带超时跑一项（卡住就是这项没通过）。"""
    box = {}

    def run():
        try:
            box["ok"], box["ev"] = fn()
        except Exception as e:
            box["ok"], box["ev"] = False, "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        ok, evidence = False, "超过 %d 秒没返回" % timeout
    else:
        ok, evidence = box.get("ok", False), box.get("ev", "(无输出)")
    RESULTS.append({"no": no, "what": what, "ok": bool(ok), "evidence": str(evidence)})
    print(("PASS " if ok else "FAIL ") + "#%-2s %s" % (no, what))
    for line in str(evidence).splitlines():
        print("        " + line)
    return ok


def _journal():
    import journal as J
    return J


def _exec_mod():
    from fleet import execute as X
    return X


# ---------------------------------------------------------------- #1

def t1_trace_column():
    """journal 有 trace 列；老库能自动补列，历史行 trace 为 NULL。"""
    J = _journal()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts REAL, kind TEXT, actor TEXT, topic TEXT, body TEXT, "
                "ref INTEGER, status TEXT)")
    raw.execute("INSERT INTO journal(ts,kind,actor,body,status) "
                "VALUES(1,'task','PC-X','老记录','open')")
    raw.commit()
    raw.close()
    j = J.Journal(path)
    try:
        con = sqlite3.connect(path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(journal)").fetchall()}
        finally:
            con.close()
        assert "trace" in cols, cols
        old = j.recent(hours=10 ** 7, now=time_now())
        assert len(old) == 1, old
        assert old[0]["trace"] is None, "老行的 trace 应为 NULL（不是空串）：%r" % old[0]["trace"]
        return True, "trace 列已存在；老行 trace=%r（NULL 表示「没归到任何一次执行」）" % old[0]["trace"]
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)


def time_now():
    import time
    return time.time()


# ---------------------------------------------------------------- #2

def t2_by_trace_order():
    """by_trace 按时间正序取回一次执行，且不串组。"""
    J = _journal()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    j = J.Journal(path, cap=200)
    try:
        a = j.log("task", "PC-A", "第1步", topic="cap:x", trace="exec:T1",
                  status=J.DONE_STATUS, ts=1000)
        b = j.log("task", "PC-A", "第2步", topic="cap:y", trace="exec:T1",
                  status=J.DONE_STATUS, ts=1002)
        j.log("task", "PC-B", "别人的", topic="cap:x", trace="exec:T2",
              status=J.DONE_STATUS, ts=1001)
        seq = j.by_trace("exec:T1")
        assert [r["id"] for r in seq] == [a, b], seq
        assert len(j.by_trace("exec:T2")) == 1
        assert j.by_trace("") == [] and j.by_trace("exec:none") == []
        return True, "exec:T1 取回 %d 条（顺序 %s），别的组未串入" % (
            len(seq), [r["id"] for r in seq])
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------- #3 / #4 / #5

def _fake_exec(call_it):
    """造一个假注册表 + 执行器，跑一次调用，返回 (journal, result)。"""
    from fleet.core import registry as RC
    from fleet.core import result as R
    from fleet.core.device import BaseDevice
    J = _journal()
    X = _exec_mod()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    j = J.Journal(path, cap=200)
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
    ex = X.Executor(reg, journal=j)
    try:
        return j, call_it(ex)
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)


def t3_success_open_done():
    def run(ex):
        tr = ex.new_trace()
        r = ex.call("probe.echo", args={"x": 7}, trace=tr)
        assert r.ok, r
        return ex.journal.by_trace(tr)
    j, seq = _fake_exec(run)
    assert len(seq) == 2, seq
    assert seq[0]["status"] == "open" and seq[1]["status"] == "done", seq
    return True, "成功调用留下 open→done 两条（id %d→%d）" % (seq[0]["id"], seq[1]["id"])


def t4_failure_no_done():
    def run(ex):
        tr = ex.new_trace()
        r = ex.call("probe.boom", trace=tr)
        assert not r.ok, r
        return ex.journal.by_trace(tr)
    j, seq = _fake_exec(run)
    assert seq, seq
    assert all(s["status"] == "open" for s in seq), seq
    assert any("炸了" in s["body"] for s in seq), seq
    return True, "失败调用留下 %d 条 open、无 done，原因写在 body 里" % len(seq)


def t5_no_arg_values():
    def run(ex):
        tr = ex.new_trace()
        ex.call("probe.echo", args={"x": "hunter2-secret", "n": 5}, trace=tr)
        return " ".join(s["body"] for s in ex.journal.by_trace(tr))
    j, bodies = _fake_exec(run)
    assert "hunter2-secret" not in bodies, bodies
    assert "x:str(14)" in bodies, bodies
    assert "n:int" in bodies, bodies
    return True, "参数值未落盘；只留 x:str(14) / n:int 这样的形状"


# ---------------------------------------------------------------- #6 / #7 / #8 / #9

def _mem_setup():
    """造一份记忆目录 + 证据库，返回 (dir, journal)。"""
    from fleet import memory_import as MI
    J = _journal()
    d = tempfile.mkdtemp()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    with open(os.path.join(d, "2026-10-22.md"), "w", encoding="utf-8") as f:
        f.write("# 2026-10-22\n\n"
                "## 15:00 某次实测\n\n"
                "- 实测结论：journal 187 条里 done 是 0 条，这个数字是数出来的不是猜的\n"
                "- 短\n"
                "| 表格 | 行 |\n"
                "```\n"
                "python -c 'x=1'\n"
                "```\n"
                "- 带密钥的行 --password hunter2 不该落明文\n")
    return MI, d, J.Journal(path, cap=500), path


def t6_parse_rules():
    MI, d, j, path = _mem_setup()
    try:
        st = MI.import_dir(d, j)
        # 样本里 3 个列表项：长条目 / "- 短"（太短）/ 带密钥条目
        # 另有 1 行表格 + 1 个 3 行代码块 ⇒ 有效条目 2 条
        assert st["written"] == 2, st
        rows = j.recent(hours=10 ** 7)
        assert len(rows) == 2, rows
        return True, "6 行样本里只抽到 2 条（短条目/表格行/代码块都被跳过）"
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def t7_idempotent():
    MI, d, j, path = _mem_setup()
    try:
        st1 = MI.import_dir(d, j)
        st2 = MI.import_dir(d, j)
        assert st1["written"] == 2, st1
        assert st2["written"] == 0 and st2["skipped_dup"] == 2, st2
        assert j.count() == 2, j.count()
        return True, "重复导入新增 0 条（按 trace 去重）"
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def t8_trace_points_to_line():
    MI, d, j, path = _mem_setup()
    try:
        MI.import_dir(d, j)
        tr = {r["trace"] for r in j.recent(hours=10 ** 7)}
        # 原文件行号：条目在第 5 行与第 11 行（第 8-10 行是代码块，被跳过）
        assert "mem:2026-10-22.md#L5" in tr, tr
        assert "mem:2026-10-22.md#L11" in tr, tr
        return True, "trace 指到原文件行号（L5 / L11，代码块内的行号没有前移）"
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def t9_no_secret_plaintext():
    MI, d, j, path = _mem_setup()
    try:
        MI.import_dir(d, j)
        body = " ".join(r["body"] for r in j.recent(hours=10 ** 7))
        assert "hunter2" not in body, body
        assert "已打码" in body, body
        return True, "密码已打码（journal.redact 生效）"
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- #10

def t10_evidence_levels():
    X = _exec_mod()
    assert X.is_exec_trace("exec:abc") and not X.is_mem_trace("exec:abc")
    assert X.is_mem_trace("mem:2026-10-22.md#L1-5") and not X.is_exec_trace("mem:x")
    assert not X.is_exec_trace("") and not X.is_exec_trace(None)
    return True, "exec: 与 mem: 前缀判定互斥，混用会被识别出来"


# ---------------------------------------------------------------- #11

def t11_separate_evidence_db():
    """证据库与对话流水库分离：记忆证据不许挤掉真实对话流水。"""
    ev = os.path.join(ROOT, "fleet_evidence.db")
    jr = os.path.join(ROOT, "fleet_journal.db")
    if not os.path.isfile(ev):
        return False, "证据库不存在：%s（先跑 python -m fleet.memory_import）" % ev
    if not os.path.isfile(jr):
        return False, "对话流水库不存在：%s" % jr
    con_e = sqlite3.connect(ev)
    con_j = sqlite3.connect(jr)
    try:
        n_ev = con_e.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        n_jr = con_j.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        mem_in_jr = con_j.execute(
            "SELECT COUNT(*) FROM journal WHERE trace LIKE 'mem:%'").fetchone()[0]
        exec_in_ev = con_e.execute(
            "SELECT COUNT(*) FROM journal WHERE trace LIKE 'exec:%'").fetchone()[0]
        assert n_ev > 0, "证据库是空的"
        assert mem_in_jr == 0, "对话流水库里混进了 %d 条记忆证据" % mem_in_jr
        return True, ("证据库 %d 条（exec %d 条）；对话流水库 %d 条，记忆证据 0 条混入"
                      % (n_ev, exec_in_ev, n_jr))
    finally:
        con_e.close()
        con_j.close()


# ---------------------------------------------------------------- main

CHECKS = [
    (1, "journal 有 trace 列，老库能补列（历史行 NULL）", t1_trace_column),
    (2, "by_trace 按时间正序取回一次执行，不串组", t2_by_trace_order),
    (3, "执行成功 ⇒ 同 trace 一条 open + 一条 done", t3_success_open_done),
    (4, "执行失败 ⇒ 只有 open，没有 done", t4_failure_no_done),
    (5, "执行不把参数值写进盘", t5_no_arg_values),
    (6, "记忆解析：只取列表项，跳表格/代码块/短条目", t6_parse_rules),
    (7, "记忆导入幂等：重复导入不新增", t7_idempotent),
    (8, "记忆溯源：trace 指到原文件行号", t8_trace_points_to_line),
    (9, "记忆里的密码不落明文", t9_no_secret_plaintext),
    (10, "证据分级 exec: / mem: 不混", t10_evidence_levels),
    (11, "证据库与对话流水库分离", t11_separate_evidence_db),
]


def main():
    print("== P0 门禁：证据层 ==")
    for no, what, fn in CHECKS:
        check(no, what, fn)
    bad = [r for r in RESULTS if not r["ok"]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("不过的：%s" % ", ".join("#%s %s" % (r["no"], r["what"]) for r in bad))
        sys.exit(1)
    print("P0 OK（证据层已通：技能进化有燃料了）")


if __name__ == "__main__":
    main()
