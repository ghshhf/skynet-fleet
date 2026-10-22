"""记忆闸：证据 → 溯源 → 派生目录。

它补的是三样我们自己缺的东西（借 MemFlywheel 的骨架，不借它的存储）：

1. **证据闸**：没有流水 id 支撑的话，不许进正式记忆，只进 `pending`。
   为什么必须这么严：总纲 Phase B 的验收③是「引用必须是真的（对照 journal 原文，
   不许编）」。光靠纪律守不住——`ctx.put()` 现在是想写就写。
2. **溯源**：每条记忆记住它凭哪几条流水（journal 自增 id）。流水被 `purge()`
   删掉后（用户有逐条删除权 E-7），对应记忆自动降级为 `verified=0`，
   目录里写「证据已丢」——**记忆不会因为证据没了就消失，但会明确降级**。
3. **派生目录**：给对话注入的那一小段文本。有硬上限（40 行 / 6000 字节），
   超了按「离昨天近」截断。排序沿用 `recall` 的时间观，不另造一套。

刻意**不做**的事：不自动合并、不自动删除、不自动把 pending 转正。
它的 dream 敢自动压缩，是因为它假设所有记忆都可演化；我们这里有些话是用户原话，
删掉就再也拿不回来。所以 `dupes()` 只报告，删除权留给人。
"""
import json
import sqlite3
import threading
import time

import recall

PENDING_NS = "pending"          # 待核区：有说法、没证据
DIGEST_MAX_LINES = 40
DIGEST_MAX_BYTES = 6000
PREFER_AGE = recall.DEFAULT_PREFER_AGE   # 26h，跟往事唤醒同一套时间观


class MemGuard:
    """包在现有 SharedContext 外面的一层闸。不改它的表结构，只加一张 evidence 表。"""

    def __init__(self, ctx, journal, path=None):
        self.ctx = ctx
        self.journal = journal
        self.path = path or getattr(ctx, "path", "fleet_context.db")
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self._lk = threading.Lock()
        with self._lk:
            self.con.execute("""CREATE TABLE IF NOT EXISTS evidence(
                ns TEXT, key TEXT, jids TEXT, verified INTEGER, ts REAL,
                PRIMARY KEY(ns,key))""")
            self.con.commit()

    # ---------------- 写 ----------------

    def remember(self, ns, key, val, evidence=None, ts=None):
        """写一条记忆。

        有证据 → 正式写入；没证据 → 落 pending（不报错、也不装正式）。
        证据 id 编造（流水里查不到）→ 抛错，一个字节都不写。
        """
        val = "" if val is None else str(val).strip()
        if not val:
            raise ValueError("内容为空，不写")
        if not ns or ns == PENDING_NS:
            raise ValueError("正式记忆要指定类型 ns（不是 %r）" % ns)

        ids = []
        for i in (evidence or []):
            try:
                ids.append(int(i))
            except (TypeError, ValueError):
                raise ValueError("证据 id 必须是整数：%r" % (i,))

        if ids:
            missing = set(ids) - self.journal.exists(ids)
            if missing:
                raise ValueError(
                    "证据 id 在流水里不存在：%s —— 不许拿假证据充数"
                    % ", ".join("#%d" % i for i in sorted(missing)))
            self.ctx.put(ns, key, val, ts=ts)
            self._set_ev(ns, key, sorted(set(ids)), 1, ts)
            # 待核区里有同名条目就顺手撤掉：它已经转正了，留着就是重复
            if self.ctx.get(PENDING_NS, key).get("ok"):
                self.ctx.delete(PENDING_NS, key)
                self._drop_ev(PENDING_NS, key)
            return {"ok": True, "pending": False, "jids": sorted(set(ids))}

        self.ctx.put(PENDING_NS, key, val, ts=ts)
        self._set_ev(PENDING_NS, key, [], 0, ts)
        return {"ok": True, "pending": True,
                "why": "没有流水证据，先进待核区；找到证据再 remember 一次就转正"}

    # ---------------- 从流水沉淀成记忆 ----------------

    def extract(self, now=None, hours=72, kinds=("task", "user", "her", "see"), limit=2000):
        """把流水按 topic 聚成记忆条目——**确定性、无 LLM、每条必带证据**。

        为什么需要这一步：真实库里流已有 187 条事件，L3 只有 1 条元状态。
        缺的从来不是存储，而是「事件 → 记忆」这一跳。MemFlywheel 用子 Agent 干这活；
        我们没有模型可依赖，就用 topic 聚合这条可解释的规则顶上。

        规则（说得清、可复算）：
          · 按 `topic` 分组，**没有 topic 的事件不沉淀**（随口一句话不指向任何事）；
          · 每组取最后一条正文当摘要，挂上这一组的全部流水 id 当证据；
          · ns 按事件类型定：task→workflow，user/her→context，see→see；
          · 同一 topic 重复抽取只会覆盖同一个 key（幂等），不会越抽越多。
        """
        now = time.time() if now is None else now
        rows = self.journal.recent(hours=hours, now=now, kinds=tuple(kinds), limit=limit)
        groups = {}
        for r in rows:
            t = (r.get("topic") or "").strip()
            if not t:
                continue
            groups.setdefault(t, []).append(r)

        made = []
        for topic, items in sorted(groups.items()):
            items.sort(key=lambda r: r["ts"])
            last = items[-1]
            ns = {"task": "workflow", "user": "context",
                  "her": "context", "see": "see"}.get(last["kind"], "context")
            body = (last.get("body") or "").strip()
            if not body:
                continue
            val = body if len(body) <= 120 else body[:117] + "…"
            r = self.remember(ns, topic, val, evidence=[i["id"] for i in items],
                              ts=last["ts"])
            made.append({"ns": ns, "key": topic, "jids": r["jids"], "val": val})
        return made

    # ---------------- 核 ----------------

    def verify(self, now=None):
        """回头核对每条记忆的证据还在不在。返回 {"checked","lost":[...]}。"""
        rows = []
        with self._lk:
            for ns, key, jids, verified, ts in self.con.execute(
                    "SELECT ns,key,jids,verified,ts FROM evidence").fetchall():
                rows.append((ns, key, json.loads(jids or "[]"), verified))
        lost = []
        for ns, key, jids, verified in rows:
            if not jids:
                continue                      # 本来就没证据（pending），不算丢
            alive = self.journal.exists(jids)
            if len(alive) != len(set(jids)) and verified:
                self._set_ev(ns, key, sorted(alive), 0, None, keep_ts=True)
                lost.append({"ns": ns, "key": key,
                             "lost": sorted(set(jids) - alive)})
        return {"checked": len(rows), "lost": lost}

    def evidence_of(self, ns, key):
        with self._lk:
            row = self.con.execute(
                "SELECT jids,verified FROM evidence WHERE ns=? AND key=?",
                (ns, key)).fetchone()
        if not row:
            return {"ok": False, "err": "没有登记证据"}
        return {"ok": True, "jids": json.loads(row[0] or "[]"), "verified": bool(row[1])}

    # ---------------- 目录 ----------------

    def digest(self, now=None, max_lines=DIGEST_MAX_LINES, max_bytes=DIGEST_MAX_BYTES):
        """派生目录：给对话注入的那一小段。确定性（同一状态两次生成完全一致）。"""
        now = time.time() if now is None else now
        ev = {}
        with self._lk:
            for ns, key, jids, verified in self.con.execute(
                    "SELECT ns,key,jids,verified FROM evidence").fetchall():
                ev[(ns, key)] = (json.loads(jids or "[]"), bool(verified))

        normal, pend = [], []
        for it in self.ctx.items(500):
            ns, key, val, ts = it["ns"], it["key"], it["val"], it["ts"]
            if (ns, key) in recall.RESERVED:
                continue
            # 注意：pending 在 recall.SKIP_NS 里（它不该被当往事说出来），
            # 但目录里**必须**留着——不然人就看不到「有东西在待核」。
            if ns in recall.SKIP_NS and ns != PENDING_NS:
                continue
            if not (val or "").strip():
                continue
            jids, verified = ev.get((ns, key), ([], True))
            row = {
                "ns": ns, "key": key, "val": val, "ts": ts,
                "when": recall.human_age(ts, now),
                "score": -abs((now - ts) - PREFER_AGE),
                "jids": jids, "verified": verified,
            }
            (pend if ns == PENDING_NS else normal).append(row)

        normal.sort(key=lambda r: (r["score"], r["ts"], r["key"]), reverse=True)
        pend.sort(key=lambda r: (r["key"],))

        lines = []
        for r in normal + pend:
            if r["jids"]:
                proof = "证据 %s" % ",".join("#%d" % i for i in r["jids"])
                if not r["verified"]:
                    proof += "（证据已丢）"
            else:
                proof = "无证据"
            head = "待核：" if r["ns"] == PENDING_NS else ""
            lines.append("- %s%s（%s｜%s/%s｜%s）"
                         % (head, r["val"], r["when"], r["ns"], r["key"], proof))

        total = len(lines)
        out, used = [], 0
        for ln in lines:
            b = len(ln.encode("utf-8")) + 1
            if len(out) >= max_lines or used + b > max_bytes:
                break
            out.append(ln)
            used += b
        if len(out) < total:
            out.append("…（另有 %d 条没列出）" % (total - len(out)))
        return "\n".join(out)

    # ---------------- 整理：只报告，不删 ----------------

    def dupes(self):
        """内容完全相同的条目（多半是同一件事换个 key 写了两次）。只报告。"""
        seen = {}
        for it in self.ctx.items(500):
            if it["ns"] in recall.SKIP_NS:
                continue
            v = (it["val"] or "").strip()
            if v:
                seen.setdefault(v, []).append("%s/%s" % (it["ns"], it["key"]))
        return [{"val": v, "keys": ks} for v, ks in seen.items() if len(ks) > 1]

    # ---------------- 内部 ----------------

    def _set_ev(self, ns, key, jids, verified, ts=None, keep_ts=False):
        ts = time.time() if ts is None else ts
        with self._lk:
            if keep_ts:
                self.con.execute(
                    "UPDATE evidence SET jids=?, verified=? WHERE ns=? AND key=?",
                    (json.dumps(jids), int(verified), ns, key))
            else:
                self.con.execute(
                    "INSERT INTO evidence(ns,key,jids,verified,ts) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(ns,key) DO UPDATE SET jids=excluded.jids, "
                    "verified=excluded.verified, ts=excluded.ts",
                    (ns, key, json.dumps(jids), int(verified), ts))
            self.con.commit()

    def _drop_ev(self, ns, key):
        with self._lk:
            self.con.execute("DELETE FROM evidence WHERE ns=? AND key=?", (ns, key))
            self.con.commit()

    def close(self):
        with self._lk:
            self.con.close()


def _selftest():
    """离线验证：闸 / 溯源 / 降级 / 目录上限 / 幂等 / 重复报告。"""
    import os
    import tempfile

    import memory as mem_mod

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)
    fd, jd = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(jd)

    ctx = mem_mod.SharedContext(db)
    from journal import Journal
    j = Journal(jd)
    mg = MemGuard(ctx, j)
    try:
        now = 1_800_000_000.0

        # ① 无证据 → 待核，正式 ns 里没有
        r = mg.remember("her", "deco", "听说楼下在装修", ts=now - 3600)
        assert r["pending"] and not ctx.get("her", "deco")["ok"], r
        assert ctx.get(PENDING_NS, "deco")["ok"]
        print("[selftest] 无证据只进待核区 OK")

        # ② 有证据 → 正式写入，目录带 #id
        a = j.log("her", "her", "昨晚那局说到第3关", topic="game", ts=now - 26 * 3600)
        b = j.log("task", "PC-A", "存档读到了第3关", ts=now - 25 * 3600)
        r = mg.remember("her", "game_lastnight", "昨晚那局还没玩完，说到第3关",
                        evidence=[a, b], ts=now - 26 * 3600)
        assert not r["pending"] and r["jids"] == [a, b], r
        d = mg.digest(now=now)
        assert "#%d" % a in d and "#%d" % b in d, d
        assert "待核：听说楼下在装修" in d, d
        print("[selftest] 有证据进正式 + 目录带证据 id OK")

        # ③ 编造证据 → 抛错且不写
        try:
            mg.remember("her", "fake", "编的", evidence=[99999])
            raise AssertionError("编造 id 应报错")
        except ValueError as e:
            assert "不存在" in str(e), e
        assert not ctx.get("her", "fake")["ok"]
        print("[selftest] 编造证据被拒（且不留痕） OK")

        # ④ 证据被删 → 降级
        j.purge(b)
        res = mg.verify(now=now)
        assert any(x["key"] == "game_lastnight" for x in res["lost"]), res
        assert not mg.evidence_of("her", "game_lastnight")["verified"]
        d = mg.digest(now=now)
        assert "证据已丢" in d, d
        assert "昨晚那局还没玩完" in d, "记忆本身不许因为证据没了就消失"
        print("[selftest] 证据被 purge → 标「证据已丢」但记忆保留 OK")

        # ⑤ 上限：100 条也压到 40 行内
        for i in range(100):
            ctx.put("her", "bulk%03d" % i, "第%d条记忆内容稍微长一点点凑字节" % i,
                    ts=now - 30 * 3600 - i)
        d = mg.digest(now=now)
        assert len(d.splitlines()) <= DIGEST_MAX_LINES + 1, len(d.splitlines())
        assert len(d.encode("utf-8")) <= DIGEST_MAX_BYTES + 80, len(d.encode("utf-8"))
        print("[selftest] 目录硬上限（≤40 行 / ≤6000 字节） OK")

        # ⑥ 幂等
        assert mg.digest(now=now) == d
        print("[selftest] 目录幂等 OK")

        # ⑦ 重复报告
        ctx.put("user", "dup_a", "同一句话", ts=now - 3600)
        ctx.put("user", "dup_b", "同一句话", ts=now - 3600)
        ds = mg.dupes()
        assert any(set(x["keys"]) == {"user/dup_a", "user/dup_b"} for x in ds), ds
        print("[selftest] 重复条目只报告不删 OK")
        print("[selftest] PASS")
    finally:
        mg.close()
        j.close_connection()
        ctx.close()
        for f in (db, jd):
            if os.path.exists(f):
                os.remove(f)


def _cli(argv):
    """直接对真实库跑：`python memguard.py --digest`（默认读仓库里的两个库）。"""
    import argparse

    import memory as mem_mod
    from journal import Journal

    ap = argparse.ArgumentParser(description="记忆闸：看目录 / 核证据 / 找重复")
    ap.add_argument("--ctx", default="fleet_context.db", help="L3 库路径")
    ap.add_argument("--journal", default="fleet_journal.db", help="流水库路径")
    ap.add_argument("--digest", action="store_true", help="打印派生目录")
    ap.add_argument("--extract", action="store_true", help="从流水沉淀成记忆（幂等）")
    ap.add_argument("--hours", type=float, default=72, help="抽取窗口（小时）")
    ap.add_argument("--verify", action="store_true", help="核对证据是否还在")
    ap.add_argument("--dupes", action="store_true", help="列出内容重复的条目")
    args = ap.parse_args(argv)

    ctx = mem_mod.SharedContext(args.ctx)
    j = Journal(args.journal)
    mg = MemGuard(ctx, j)
    try:
        if args.extract:
            made = mg.extract(hours=args.hours)
            print("从流水沉淀出 %d 条记忆（每条都带证据 id）" % len(made))
            for m in made[:15]:
                print("  %s/%s ← 证据 %s" % (m["ns"], m["key"],
                                            ",".join("#%d" % i for i in m["jids"])))
            if len(made) > 15:
                print("  …另有 %d 条" % (len(made) - 15))
        if args.digest:
            d = mg.digest()
            n = len(d.splitlines())
            print(d or "(还没有任何记忆)")
            print("---\n%d 行 / %d 字节" % (n, len(d.encode("utf-8"))))
        if args.verify:
            r = mg.verify()
            print("核对 %d 条，证据已丢 %d 条" % (r["checked"], len(r["lost"])))
            for x in r["lost"]:
                print("  %s/%s ← 丢了 %s" % (x["ns"], x["key"],
                                             ",".join("#%d" % i for i in x["lost"])))
        if args.dupes:
            ds = mg.dupes()
            print("重复 %d 组（只报告，不删）" % len(ds))
            for x in ds:
                print("  %s ← %s" % (x["val"][:40], " / ".join(x["keys"])))
        if not (args.digest or args.verify or args.dupes or args.extract):
            ap.print_help()
    finally:
        mg.close()
        j.close_connection()
        ctx.close()


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        _cli(sys.argv[1:])
