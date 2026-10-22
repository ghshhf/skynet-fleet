"""记忆闸门禁：证据 / 溯源 / 目录上限 / 幂等 / 待核不当事实。

判据来源：docs/记忆_融合落地_计划书_2026-10-22.md §三。
跟 P1/P3 门禁一个规矩：每条都能指到具体证据，不写「看起来没问题」。

跑法：python tools/mem_gate.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULTS = []


def check(no, what, fn):
    try:
        ok, evidence = fn()
    except Exception as e:
        ok, evidence = False, "%s: %s" % (type(e).__name__, e)
    RESULTS.append({"no": no, "what": what, "ok": bool(ok), "evidence": evidence})
    print(("PASS " if ok else "FAIL ") + "#%-2s %s" % (no, what))
    for line in str(evidence).splitlines():
        print("        " + line)
    return ok


class Sandbox:
    """一套临时 L3 + 流水 + 闸，测完即删（不碰任何真实库）。"""

    def __init__(self, now=1_800_000_000.0):
        import journal
        import memguard
        import memory as mem_mod
        self.now = now
        self.dir = tempfile.mkdtemp(prefix="memgate-")
        self.ctx = mem_mod.SharedContext(os.path.join(self.dir, "ctx.db"))
        self.j = journal.Journal(os.path.join(self.dir, "journal.db"))
        self.mg = memguard.MemGuard(self.ctx, self.j)

    def close(self):
        import shutil
        self.mg.close()
        self.j.close_connection()
        self.ctx.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def t1_no_evidence():
    s = Sandbox()
    try:
        r = s.mg.remember("her", "deco", "听说楼下在装修", ts=s.now - 3600)
        ok = (r.get("pending") is True
              and not s.ctx.get("her", "deco")["ok"]
              and s.ctx.get("pending", "deco")["ok"])
        return ok, "pending=%s；正式 ns 有？%s；待核区有？%s" % (
            r.get("pending"), s.ctx.get("her", "deco")["ok"],
            s.ctx.get("pending", "deco")["ok"])
    finally:
        s.close()


def t2_with_evidence():
    s = Sandbox()
    try:
        a = s.j.log("her", "her", "昨晚那局说到第3关", topic="game", ts=s.now - 26 * 3600)
        b = s.j.log("task", "PC-A", "存档读到了第3关", ts=s.now - 25 * 3600)
        r = s.mg.remember("her", "game", "昨晚那局还没玩完，说到第3关",
                          evidence=[a, b], ts=s.now - 26 * 3600)
        d = s.mg.digest(now=s.now)
        ok = (not r.get("pending")) and ("#%d" % a in d) and ("#%d" % b in d)
        return ok, "jids=%s\n目录行：%s" % (r.get("jids"), d.splitlines()[0] if d else "(空)")
    finally:
        s.close()


def t3_fake_evidence():
    s = Sandbox()
    try:
        try:
            s.mg.remember("her", "fake", "编的", evidence=[99999])
            return False, "没有报错——假证据被放行了"
        except ValueError as e:
            left = s.ctx.get("her", "fake")["ok"]
            return (not left), "拒绝原因：%s\n库里留痕？%s" % (str(e).splitlines()[0], left)
    finally:
        s.close()


def t4_evidence_lost():
    s = Sandbox()
    try:
        a = s.j.log("her", "her", "昨晚那局说到第3关", ts=s.now - 26 * 3600)
        b = s.j.log("task", "PC-A", "存档读到了第3关", ts=s.now - 25 * 3600)
        s.mg.remember("her", "game", "昨晚那局还没玩完，说到第3关",
                      evidence=[a, b], ts=s.now - 26 * 3600)
        s.j.purge(b)                       # 用户行使逐条删除权
        res = s.mg.verify(now=s.now)
        d = s.mg.digest(now=s.now)
        ok = (any(x["key"] == "game" for x in res["lost"])
              and "证据已丢" in d and "昨晚那局还没玩完" in d)
        return ok, "lost=%s\n目录行：%s" % (res["lost"], d.splitlines()[0] if d else "(空)")
    finally:
        s.close()


def t5_caps():
    s = Sandbox()
    try:
        for i in range(100):
            s.ctx.put("her", "bulk%03d" % i, "第%d条记忆内容稍微长一点点凑字节" % i,
                      ts=s.now - 30 * 3600 - i)
        d = s.mg.digest(now=s.now)
        n, b = len(d.splitlines()), len(d.encode("utf-8"))
        import memguard
        ok = n <= memguard.DIGEST_MAX_LINES + 1 and b <= memguard.DIGEST_MAX_BYTES + 80
        return ok, "100 条 → %d 行 / %d 字节（上限 %d 行 / %d 字节，末行是截断标记）" % (
            n, b, memguard.DIGEST_MAX_LINES, memguard.DIGEST_MAX_BYTES)
    finally:
        s.close()


def t6_idempotent():
    s = Sandbox()
    try:
        a = s.j.log("her", "her", "昨晚那局说到第3关", ts=s.now - 26 * 3600)
        s.mg.remember("her", "game", "昨晚那局还没玩完", evidence=[a], ts=s.now - 26 * 3600)
        for i in range(20):
            s.ctx.put("user", "u%02d" % i, "第%d件事" % i, ts=s.now - i * 3600)
        d1, d2 = s.mg.digest(now=s.now), s.mg.digest(now=s.now)
        return d1 == d2, "两次长度 %d / %d，完全一致？%s" % (
            len(d1), len(d2), d1 == d2)
    finally:
        s.close()


def t7_dupes():
    s = Sandbox()
    try:
        s.ctx.put("user", "dup_a", "同一句话", ts=s.now - 3600)
        s.ctx.put("user", "dup_b", "同一句话", ts=s.now - 3600)
        s.ctx.put("user", "uniq", "不一样的话", ts=s.now - 3600)
        ds = s.mg.dupes()
        hit = [x for x in ds if set(x["keys"]) == {"user/dup_a", "user/dup_b"}]
        still = s.ctx.get("user", "dup_b")["ok"]
        return (bool(hit) and still), "报告 %d 组：%s\n重复条目还在（只报不删）？%s" % (
            len(ds), hit[0]["keys"] if hit else "(没找到)", still)
    finally:
        s.close()


def t8_pending_not_recalled():
    """待核项不许被往事唤醒当成事实说出来。"""
    s = Sandbox()
    try:
        import recall
        s.mg.remember("her", "deco", "听说楼下在装修", ts=s.now - 26 * 3600)
        cands = recall.candidates(s.ctx, now=s.now)
        keys = [c["key"] for c in cands]
        d = s.mg.digest(now=s.now)
        ok = ("deco" not in keys) and ("待核：听说楼下在装修" in d)
        return ok, "往事候选=%s（不许含 deco）\n目录里仍有待核段？%s" % (
            keys or "(空)", "待核：听说楼下在装修" in d)
    finally:
        s.close()


def t9_extract():
    """同 topic 的多条事件 → 1 条记忆，证据是全部 id。"""
    s = Sandbox()
    try:
        a = s.j.log("task", "PC-A", "备份失败于 step-2", topic="intent:备份", ts=s.now - 3 * 3600)
        b = s.j.log("task", "PC-A", "重试仍然失败：md5 不一致", topic="intent:备份", ts=s.now - 2 * 3600)
        s.j.log("user", "user", "随口一句没主题的话", ts=s.now - 3600)   # 没 topic，不该沉淀
        made = s.mg.extract(now=s.now)
        hit = [m for m in made if m["key"] == "intent:备份"]
        ok = (len(made) == 1 and hit and hit[0]["ns"] == "workflow"
              and hit[0]["jids"] == [a, b]
              and "md5 不一致" in hit[0]["val"])
        return ok, "沉淀 %d 条：%s\n无 topic 的那条被正确跳过" % (
            len(made), [(m["ns"], m["key"], m["jids"]) for m in made])
    finally:
        s.close()


def t10_extract_idempotent():
    s = Sandbox()
    try:
        for i in range(3):
            s.j.log("user", "user", "第%d次说：想买个新键盘" % i,
                    topic="want:keyboard", ts=s.now - (3 - i) * 3600)
        m1 = s.mg.extract(now=s.now)
        d1 = s.mg.digest(now=s.now)
        m2 = s.mg.extract(now=s.now)
        d2 = s.mg.digest(now=s.now)
        ok = (len(m1) == len(m2) == 1) and (d1 == d2) and (m1[0]["jids"] == m2[0]["jids"])
        return ok, "两次各沉淀 %d / %d 条，证据 %s / %s，目录一致？%s" % (
            len(m1), len(m2), m1[0]["jids"], m2[0]["jids"], d1 == d2)
    finally:
        s.close()


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    check(1, "无证据 → 只进待核区", t1_no_evidence)
    check(2, "有证据 → 正式写入 + 目录带证据 id", t2_with_evidence)
    check(3, "编造证据 id → 拒绝且不留痕", t3_fake_evidence)
    check(4, "证据被 purge → 标「证据已丢」但记忆保留", t4_evidence_lost)
    check(5, "目录硬上限（40 行 / 6000 字节）", t5_caps)
    check(6, "目录幂等", t6_idempotent)
    check(7, "重复条目只报告不删", t7_dupes)
    check(8, "待核项不许被往事唤醒当事实说", t8_pending_not_recalled)
    check(9, "流水按 topic 沉淀成记忆（证据齐全）", t9_extract)
    check(10, "抽取幂等（重跑不膨胀）", t10_extract_idempotent)

    bad = [r for r in RESULTS if not r["ok"]]
    print("\n合计 %d 项，通过 %d，失败 %d"
          % (len(RESULTS), len(RESULTS) - len(bad), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
