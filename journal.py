"""对话流水（她与你聊天的连续记忆底座）。

需求来自用户原话：「相当于从过去 24 小时的对话中提取出来没有完成的任务……
然后和你继续聊，起个话题」。但摸清现状后发现：**对话流根本没有落盘**——
REPL 里用户输入了什么、她说了什么、intent 成没成，全是 print 完就蒸发，
进程一重启什么都不剩。没有流水，「提取未完成任务」是无米之炊。

本模块只做一件事：把对话与任务事件**追加式**落盘（SQLite 单表），供上层
（`followups.py`）抽取未闭环项。设计约束：

1. **append-only**：只增不改，历史不被悄悄改写；
2. **敏感信息打码**：入网载荷 / `--password` / `--token` / `k=v` 形式的密码令牌一律不落明文
   ——流水会持久化，不能变成第二个泄密面（`_redact`）。
   **诚实边界**：打码只覆盖已知危险形状，`exec PC-A echo hunter2` 这类把秘密当普通
   参数传的命令**挡不住**。流水只写本地磁盘、不出网、不进任何模型提示词之外的地方；
   真要绝对干净，把 `FLEET_JOURNAL` 指到内存盘，或别在命令行里带秘密。
3. **E-7 逐条可删**：`purge(id)`；
4. **有上限**：超 `cap` 删最旧，防止无限增长（对话不是档案柜）；
5. 纯 stdlib、`ts` 可注入，自测不吃真实时钟。

一条记录 = 一次「事件」，不是一份「结论」。判断与话题生成在上层。
"""
import re
import sqlite3
import threading
import time

# 流水里允许的事件类型。task = 有明确成败的事；her = 她的话；user = 你的话；
# see = 感知到的事（身边设备增删、手机异常）。see 单独一类是因为它既不是任务也
# 不是谁说的一句话——但正是「她」起话题时最常引用的一类事实（「回来时楼下多了三辆车」）。
KINDS = ("task", "her", "user", "see")
OPEN_STATUS = "open"
DONE_STATUS = "done"
DEFAULT_CAP = 5000

# 一条记录对外暴露的字段顺序（读方法共用，避免各处 zip 各写一份而漏列）
FIELDS = ("id", "ts", "kind", "actor", "topic", "body", "ref", "status", "trace")

# 不该以明文进流水的东西（打码而非丢弃：保留「这里本来有个秘密」的痕迹）
_REDACT_PATTERNS = [
    (re.compile(r"fleet://enroll\?p=\S+"), "fleet://enroll?p=<已打码>"),
    (re.compile(r"(--password[= ]+)\S+"), r"\1<已打码>"),
    (re.compile(r"(--token[= ]+)\S+"), r"\1<已打码>"),
    (re.compile(r"\b(password|passwd|pwd|secret|token)([=:]\s*)\S+", re.I), r"\1\2<已打码>"),
]


def redact(body):
    """把敏感值打码。宁可错杀（多打一个码），不可漏放（泄一个密）。"""
    text = str(body)
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text[:2000]          # 单条上限：流水的价值在索引，不在全文


class Journal:
    """追加式对话流水。线程安全（控制面有多个周期线程同时写）。"""

    def __init__(self, path="fleet_journal.db", cap=DEFAULT_CAP):
        self.path = path
        self.cap = cap
        self.con = sqlite3.connect(path, check_same_thread=False)
        self._lk = threading.Lock()
        with self._lk:
            self.con.execute("""CREATE TABLE IF NOT EXISTS journal(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, kind TEXT, actor TEXT, topic TEXT,
                body TEXT, ref INTEGER, status TEXT, trace TEXT)""")
            self.con.execute("CREATE INDEX IF NOT EXISTS journal_ts ON journal(ts)")
            self.con.execute("CREATE INDEX IF NOT EXISTS journal_topic ON journal(topic, status)")
            self._migrate()
            self.con.commit()

    def _migrate(self):
        """老库补 `trace` 列。

        为什么加 trace 而不是把 trace 编进 topic：
          topic 是给人读的自由文本，随时会改措辞；trace 是结构，要按它聚一组调用。
          把结构塞进文本，后面每解析一次就赌一次格式没变。

        ALTER TABLE ADD COLUMN 只加不删：既有行的 trace 为 NULL，
        语义 = 「这条记录没有归到任何一次执行」，不许当成空字符串。
        """
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(journal)").fetchall()}
        if "trace" not in cols:
            self.con.execute("ALTER TABLE journal ADD COLUMN trace TEXT")
        self.con.execute("CREATE INDEX IF NOT EXISTS journal_trace ON journal(trace)")

    # ---------- 写 ----------
    def log(self, kind, actor, body, topic=None, ref=None, ts=None, status=OPEN_STATUS,
            trace=None):
        """追加一条事件，返回自增 id（供上层引用为证据）。

        `status` 可以显式指定：有些事**写下来时就已经了结了**（比如「这台临时节点
        已清理干净」）。默认 open 会让它变成一笔永远挂着的账，`followups` 和记忆
        沉淀都会把它当成待办，纯属自找噪音。

        `trace` 把「一次用户意图下的多次调用」串起来。命名约定（技能进化用）：
          · `exec:<trace_id>` —— 能力真的跑过（`fleet/execute.py` 写），证据最强；
          · `mem:<文件>#L<a>-<b>` —— 从记忆日志导入的事实陈述，可回查原文，证据次之。
        两种前缀不许混：技能验证按前缀分级，混了就分不清「真跑过」和「我写过」。
        """
        if kind not in KINDS:
            raise ValueError(f"未知事件类型: {kind}（应为 {KINDS} 之一）")
        if status not in (OPEN_STATUS, DONE_STATUS):
            raise ValueError(f"未知状态: {status}（应为 {OPEN_STATUS}/{DONE_STATUS}）")
        ts = time.time() if ts is None else ts
        with self._lk:
            cur = self.con.execute(
                "INSERT INTO journal(ts,kind,actor,topic,body,ref,status,trace) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (ts, kind, actor, topic, redact(body), ref, status, trace))
            self.con.commit()
            self._trim()
            return cur.lastrowid

    def close(self, topic, ts=None):
        """一件事闭环了：该 topic 下所有 open 记录标 done（不是删——闭环也要留痕）。"""
        ts = time.time() if ts is None else ts
        with self._lk:
            cur = self.con.execute(
                "UPDATE journal SET status=? WHERE topic=? AND status=?",
                (DONE_STATUS, topic, OPEN_STATUS))
            self.con.commit()
            return cur.rowcount

    def purge(self, entry_id):
        """E-7：逐条可删。"""
        with self._lk:
            cur = self.con.execute("DELETE FROM journal WHERE id=?", (entry_id,))
            self.con.commit()
            return cur.rowcount

    def _trim(self):
        n = self.con.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        if n > self.cap:
            # 删最旧的 (n - cap) 条：流水的价值在近处，不在全量
            self.con.execute(
                "DELETE FROM journal WHERE id IN "
                "(SELECT id FROM journal ORDER BY id ASC LIMIT ?)", (n - self.cap,))
            self.con.commit()

    # ---------- 读 ----------
    def open_topics(self, hours=24, now=None, kinds=("task", "her")):
        """未闭环的事：**有** topic、还开着、且在窗口内。按首现时间正序（先欠的先说）。

        没有 topic 的记录（随口一句话）不算未完成项——它不指向任何一件待办。
        """
        now = time.time() if now is None else now
        lo = now - hours * 3600
        with self._lk:
            rows = self.con.execute(
                "SELECT id,ts,kind,actor,topic,body,ref,status,trace FROM journal "
                "WHERE status=? AND topic IS NOT NULL AND kind IN (%s) AND ts>=? "
                "ORDER BY ts ASC" % ",".join("?" * len(kinds)),
                (OPEN_STATUS, *kinds, lo)).fetchall()
        return [dict(zip(FIELDS, r)) for r in rows]

    def recent(self, hours=24, now=None, kinds=None, limit=200):
        """窗口内的全部流水（含已闭环）——复盘用。"""
        now = time.time() if now is None else now
        lo = now - hours * 3600
        q = ("SELECT id,ts,kind,actor,topic,body,ref,status,trace FROM journal WHERE ts>=?")
        args = [lo]
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        q += " ORDER BY ts ASC LIMIT ?"
        args.append(limit)
        with self._lk:
            rows = self.con.execute(q, args).fetchall()
        return [dict(zip(FIELDS, r)) for r in rows]

    def by_trace(self, trace, only_done=False):
        """取一次执行的全部事件，按时间正序（= 步骤发生的顺序）。

        技能生成的唯一取数入口：只认 trace 精确匹配，不做模糊聚合。
        没这条纪律，就会出现「看了一堆同期事件就以为是同一次执行」。
        """
        if not trace:
            return []
        q = "SELECT id,ts,kind,actor,topic,body,ref,status,trace FROM journal WHERE trace=?"
        args = [trace]
        if only_done:
            q += " AND status=?"
            args.append(DONE_STATUS)
        q += " ORDER BY ts ASC, id ASC"
        with self._lk:
            rows = self.con.execute(q, args).fetchall()
        return [dict(zip(FIELDS, r)) for r in rows]

    def close_all(self, kinds=("task", "her")):
        """全部闭环（自测复位用；生产上不做这种批量动作）。"""
        with self._lk:
            cur = self.con.execute(
                "UPDATE journal SET status=? WHERE status=? AND kind IN (%s)"
                % ",".join("?" * len(kinds)), (DONE_STATUS, OPEN_STATUS, *kinds))
            self.con.commit()
            return cur.rowcount

    def exists(self, ids):
        """给定 id 里哪些真的存在。

        写记忆时要拿流水当证据，就必须能核验这些 id 不是编出来的。
        返回集合而不是布尔：这样调用方能指出**具体哪几个**不存在。
        """
        ints = []
        for i in ids:
            try:
                ints.append(int(i))
            except (TypeError, ValueError):
                continue
        if not ints:
            return set()
        q = ",".join("?" * len(ints))
        with self._lk:
            rows = self.con.execute(
                "SELECT id FROM journal WHERE id IN (%s)" % q, ints).fetchall()
        return {r[0] for r in rows}

    def count(self):
        with self._lk:
            return self.con.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    def close_connection(self):
        with self._lk:
            self.con.close()


def _selftest():
    """离线验证：追加 / 闭环 / 窗口 / 打码 / 逐条删 / 上限。"""
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    j = Journal(path, cap=50)
    try:
        now = 1_800_000_000.0
        # 追加：一条失败的任务 + 一条她想说没说的
        a = j.log("task", "PC-A", "意图『备份』失败于 step-2：type 不存在的文件", topic="intent:备份", ts=now - 3600)
        b = j.log("her", "her", "我还在想 phone.screen.on", topic="her:phone.screen.on", ts=now - 1800)
        j.log("her", "her", "刚说过的一句话", topic=None, ts=now - 600)
        assert a < b
        print("[selftest] 追加式写入 + 自增 id OK")

        # 闭环：只关指定 topic，别的照旧
        assert j.close("intent:备份") == 1
        rows = j.open_topics(now=now)
        assert [r["topic"] for r in rows] == ["her:phone.screen.on"], rows
        print("[selftest] 闭环只关指定 topic（闭环留痕不删） OK")

        # 窗口：24h 外的不算未完成（陈年旧事不翻）
        j.log("task", "PC-Ghost", "很久以前的排队", topic="queue:PC-Ghost", ts=now - 30 * 3600)
        rows = j.open_topics(hours=24, now=now)
        assert all(r["topic"] != "queue:PC-Ghost" for r in rows), rows
        print("[selftest] 24h 窗口过滤 OK")

        # 打码：入网载荷与密码绝不落明文
        j.log("user", "user", "enroll 一下 fleet://enroll?p=QUJDREVG. deadbeef --password hunter2",
              topic="t:enroll", ts=now - 60)
        row = [r for r in j.recent(hours=1, now=now) if r["topic"] == "t:enroll"][0]
        assert "QUJDREVG" not in row["body"] and "hunter2" not in row["body"], row["body"]
        assert "已打码" in row["body"], row["body"]
        print("[selftest] 载荷/密码打码（不落明文） OK")

        # E-7：逐条可删
        assert j.purge(row["id"]) == 1
        assert all(r["topic"] != "t:enroll" for r in j.recent(hours=1, now=now))
        print("[selftest] 逐条可删（E-7） OK")

        # trace：一次执行的多次调用能按时间正序取回；别的 trace 不许混进来
        t1 = j.log("task", "PC-A", "第1步 adb.wifi.info", topic="cap:adb.wifi.info",
                   trace="exec:T1", ts=now + 10, status=DONE_STATUS)
        t2 = j.log("task", "PC-A", "第2步 adb.wifi.connect", topic="cap:adb.wifi.connect",
                   trace="exec:T1", ts=now + 12, status=DONE_STATUS)
        j.log("task", "PC-B", "别人的调用", topic="cap:adb.wifi.info",
              trace="exec:T2", ts=now + 11, status=DONE_STATUS)
        seq = j.by_trace("exec:T1")
        assert [r["id"] for r in seq] == [t1, t2], seq
        assert all(r["trace"] == "exec:T1" for r in seq)
        assert len(j.by_trace("exec:T1", only_done=True)) == 2
        assert j.by_trace("") == [] and j.by_trace("exec:nope") == []
        print("[selftest] trace 聚组（按时间正序，不串组） OK")

        # 老库迁移：没有 trace 列的历史库打开就补列，历史行 trace 为 NULL（不是空串）
        import sqlite3
        fd2, p2 = tempfile.mkstemp(suffix=".db")
        os.close(fd2)
        os.remove(p2)
        raw = sqlite3.connect(p2)
        raw.execute("CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts REAL, kind TEXT, actor TEXT, topic TEXT, body TEXT, "
                    "ref INTEGER, status TEXT)")
        raw.execute("INSERT INTO journal(ts,kind,actor,body,status) "
                    "VALUES(1,'task','PC-X','老记录','open')")
        raw.commit()
        raw.close()
        j2 = Journal(p2)
        try:
            old = j2.recent(hours=10 ** 7, now=now)
            assert len(old) == 1 and old[0]["trace"] is None, old
            j2.log("task", "PC-A", "新记录", trace="exec:T9", ts=now)
            assert j2.by_trace("exec:T9")[0]["body"] == "新记录"
        finally:
            j2.close_connection()
            os.remove(p2)
        print("[selftest] 老库自动补 trace 列（历史行 NULL） OK")

        # 上限：超 cap 自动砍最旧
        for i in range(60):
            j.log("user", "user", f"第{i}句", ts=now - 50 + i)
        assert j.count() <= 50, j.count()
        oldest = j.recent(hours=1000, now=now, kinds=("user",), limit=1)[0]
        assert "第59句" in oldest["body"] or oldest["ts"] > now - 60, oldest
        print(f"[selftest] 上限环切（cap=50，现存 {j.count()}） OK")

        # 坏输入：未知类型直接报错，不静默吞掉
        try:
            j.log("nonsense", "x", "y")
            raise AssertionError("未知类型应报错")
        except ValueError:
            pass
        print("[selftest] 未知事件类型报错 OK")
        print("[selftest] PASS")
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
