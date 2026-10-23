"""P5 门禁：技能进化层（自写技能的可验证性 + 审查红线）。

这个门禁钉的是「技能进化不许变成幻觉工厂」。

背景（2026-10-22 实测）：
    技能进化最容易出的事是——**凭空长出看起来很像回事的技能**。
    本仓用四道闸挡（见 fleet/skills_evolve.py），这个门禁是那四道闸的外部复核：
    不看实现，只看**落盘的东西对不对**。

判据：
    ① skills_evolved/ 里每张技能都在 EVOLVED.jsonl 里有登记
    ② 每条 evolved_from 的 id 在证据库存在，且 status='done'
    ③ caps 里每个能力在注册表里存在（改了能力名后会红，这是对的）
    ④ caps 在源证据里真出现过（Pass ∧ Use 的 Use）
    ⑤ 技能目录内零脚本、零二进制（自写技能只许是纯 Markdown）
    ⑥ 目录审计判 clean
    ⑦ EVOLVED.jsonl 每行是合法 JSON，op 在允许集合内
    ⑧ **红线**：恶意样本集全部判 needs_review，且正常样本判 clean
    ⑨ 证据分级不许混：exec 级与 mem 级不许被当成一回事

第 ⑧ 条是**审查规则的红线**：
    2026-10-22 把审查从「一刀切按关键词」改成「按文件角色分档」
    （教材只看代码块、代码文件照查）。分档之后必须有固定样本证明红线没破。
    只要 8 个恶意样本里漏过一个，规则就是被放松了 ⇒ 红。

全离线：只读本地文件与本地 sqlite，不联网、不调模型。
"""
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

RESULTS = []


def check(no, what, fn, timeout=90):
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


def _evidence_journal():
    import journal as J
    return J.Journal(os.path.join(ROOT, "fleet_evidence.db"), cap=200000)


def _cap_names():
    from fleet import catalog
    reg = catalog.build(with_nodes=False, ttl=0.1)
    return reg.cap_names()


# ---------------------------------------------------------------- #1

def t1_every_skill_registered():
    from fleet import skills_evolve as SE
    d = SE.Evolved_DIR
    if not os.path.isdir(d):
        return False, "skills_evolved/ 还不存在（先跑一次 propose+promote）"
    skills = [s for s in os.listdir(d)
              if os.path.isdir(os.path.join(d, s))
              and os.path.isfile(os.path.join(d, s, "SKILL.md"))]
    if not skills:
        return False, "skills_evolved/ 下没有技能"
    names = {r.get("name") for r in SE._ledger_read()}
    missing = [s for s in skills if s not in names]
    assert not missing, "这些技能没登记在 EVOLVED.jsonl：%s" % missing
    return True, "%d 张技能全部有登记（%s）" % (len(skills), "、".join(skills))


# ---------------------------------------------------------------- #2

def t2_evidence_real():
    from fleet import skills_evolve as SE
    j = _evidence_journal()
    try:
        bad = []
        total = 0
        for s in SE.list_evolved():
            for i in s.get("evolved_from") or []:
                total += 1
                rows = j.con.execute(
                    "SELECT id,status,kind FROM journal WHERE id=?", (i,)).fetchall()
                if not rows:
                    bad.append("#%d 不存在" % i)
                elif rows[0][1] != "done":
                    bad.append("#%d 状态是 %s" % (i, rows[0][1]))
        assert not bad, "证据不成立：%s" % "；".join(bad[:5])
        return True, "%d 条 evolved_from 全部在证据库里且 status=done" % total
    finally:
        j.close_connection()


# ---------------------------------------------------------------- #3

def t3_caps_exist():
    from fleet import skills_evolve as SE
    names = set(_cap_names())
    bad = []
    for s in SE.list_evolved():
        for c in s.get("caps") or []:
            if c not in names:
                bad.append("%s → %s" % (s["name"], c))
    assert not bad, "这些能力在注册表里不存在：%s" % "；".join(bad)
    total = sum(len(s.get("caps") or []) for s in SE.list_evolved())
    return True, "%d 个 caps 引用全部存在于注册表" % total


# ---------------------------------------------------------------- #4

def t4_caps_actually_used():
    from fleet import skills_evolve as SE
    j = _evidence_journal()
    try:
        bad = []
        for s in SE.list_evolved():
            used = set()
            for i in s.get("evolved_from") or []:
                rows = j.con.execute(
                    "SELECT topic FROM journal WHERE id=?", (i,)).fetchall()
                for r in rows:
                    if r[0] and r[0].startswith("cap:"):
                        used.add(r[0][4:])
            for c in s.get("caps") or []:
                if c not in used:
                    bad.append("%s → %s" % (s["name"], c))
        assert not bad, "这些能力没在源证据里出现过（Use 不成立）：%s" % "；".join(bad)
        return True, "所有 caps 都在源证据里真出现过（Pass ∧ Use 的 Use 成立）"
    finally:
        j.close_connection()


# ---------------------------------------------------------------- #5

def t5_no_scripts_no_binaries():
    """自写技能只许是纯 Markdown：零脚本、零二进制。"""
    from fleet import skills_audit as SA
    from fleet import skills_evolve as SE
    bad = []
    for s in SE.list_evolved():
        for rel in s.get("scanned") or []:
            ext = os.path.splitext(rel)[1].lower()
            if ext in SA.SCRIPT_EXTS:
                bad.append("%s → %s（可执行脚本）" % (s["name"], rel))
            elif ext not in SA.DOC_EXTS and ext not in SA.ASSET_EXTS:
                bad.append("%s → %s（非文档/非资产）" % (s["name"], rel))
    assert not bad, "自写技能里混进了不该有的东西：%s" % "；".join(bad)
    return True, "所有自写技能都是纯 Markdown（零脚本、零二进制）"


# ---------------------------------------------------------------- #6

def t6_audit_clean():
    from fleet import skills_audit as SA
    from fleet import skills_evolve as SE
    bad = []
    for s in SE.list_evolved():
        rep = SA.audit_skill(s["dir"])
        if rep["verdict"] != "clean":
            bad.append("%s：%s" % (s["name"], "；".join(rep["findings"][:2])))
    assert not bad, "审查结论不是 clean：%s" % "；".join(bad)
    n = len(SE.list_evolved())
    return True, "%d 张自写技能审查结论全是 clean" % n


# ---------------------------------------------------------------- #7

def t7_ledger_valid():
    from fleet import skills_evolve as SE
    allowed = {"create", "refine", "quarantine", "rollback"}
    path = SE.LEDGER
    if not os.path.isfile(path):
        return False, "EVOLVED.jsonl 不存在：%s" % path
    n = 0
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as e:
                return False, "第 %d 行不是合法 JSON：%s" % (lineno, e)
            if rec.get("op") not in allowed:
                return False, "第 %d 行的 op=%r 不在允许集合 %s" % (
                    lineno, rec.get("op"), sorted(allowed))
            n += 1
    assert n > 0, "EVOLVED.jsonl 是空的"
    return True, "%d 条回执格式合法，op 都在允许集合内" % n


# ---------------------------------------------------------------- #8（红线）

def t8_evil_samples_blocked():
    """红线：8 个恶意样本必须全被拦；正常样本必须不被误杀。"""
    from fleet import skills_audit as SA
    evil_dir = os.path.join(ROOT, "evals", "fixtures", "evil_skill")
    clean_dir = os.path.join(ROOT, "evals", "fixtures", "clean_skill")
    assert os.path.isdir(evil_dir), "恶意样本集不存在：%s" % evil_dir
    rep = SA.audit_skill(evil_dir)
    assert rep["verdict"] == "needs_review", \
        "恶意样本集被判 clean ⇒ 审查规则被放松了，红线破了"
    labels = set()
    for f in rep["findings"]:
        if f.startswith("["):
            labels.add(f[1:f.index("]")])
    need = {"destructive_shell", "download_exec", "subprocess", "secret_path",
            "dynamic_exec", "dynamic_import", "powershell"}
    missing = need - labels
    assert not missing, "这些恶意形状没被抓到：%s" % "、".join(sorted(missing))
    rep2 = SA.audit_skill(clean_dir)
    assert rep2["verdict"] == "clean", \
        "正常样本被判 needs_review ⇒ 规则还是太严（误杀没除掉）：%s" % \
        "；".join(rep2["findings"][:3])
    return True, ("恶意样本抓出 %d 类（%s）；正常样本 clean"
                  % (len(labels), "、".join(sorted(labels))))


# ---------------------------------------------------------------- #9

def t9_evidence_levels_not_mixed():
    from fleet import execute as X
    from fleet import skills_evolve as SE
    j = _evidence_journal()
    try:
        exec_rows = j.con.execute(
            "SELECT COUNT(*) FROM journal WHERE trace LIKE 'exec:%'").fetchone()[0]
        mem_rows = j.con.execute(
            "SELECT COUNT(*) FROM journal WHERE trace LIKE 'mem:%'").fetchone()[0]
        assert exec_rows > 0, "证据库里一条 exec 级证据都没有（技能验证门会全红）"
        assert mem_rows > 0, "证据库里一条 mem 级证据都没有（记忆导入没跑？）"
        assert X.is_exec_trace("exec:x") and not X.is_mem_trace("exec:x")
        assert X.is_mem_trace("mem:x#L1") and not X.is_exec_trace("mem:x")
        # 每张自写技能都必须是 exec 级长出来的
        for s in SE.list_evolved():
            ids = s.get("evolved_from") or []
            rows = j.con.execute(
                "SELECT trace FROM journal WHERE id IN (%s)"
                % ",".join("?" * len(ids)), ids).fetchall() if ids else []
            assert any((r[0] or "").startswith("exec:") for r in rows), \
                "%s 不是从 exec 级证据长出来的" % s["name"]
        return True, ("exec 级 %d 条、mem 级 %d 条，分级清楚；"
                      "所有自写技能都由 exec 级证据支撑" % (exec_rows, mem_rows))
    finally:
        j.close_connection()


# ---------------------------------------------------------------- main

CHECKS = [
    (1, "每张自写技能都在 EVOLVED.jsonl 里有登记", t1_every_skill_registered),
    (2, "evolved_from 的每个 id 在证据库存在且 done", t2_evidence_real),
    (3, "caps 里每个能力在注册表里存在", t3_caps_exist),
    (4, "caps 在源证据里真出现过（Use 成立）", t4_caps_actually_used),
    (5, "自写技能零脚本、零二进制", t5_no_scripts_no_binaries),
    (6, "自写技能审查结论 clean", t6_audit_clean),
    (7, "EVOLVED.jsonl 格式合法、op 合法", t7_ledger_valid),
    (8, "红线：恶意样本全被拦，正常样本不误杀", t8_evil_samples_blocked),
    (9, "证据分级 exec/mem 不混，自写技能都由 exec 支撑", t9_evidence_levels_not_mixed),
]


def main():
    print("== P5 门禁：技能进化层 ==")
    for no, what, fn in CHECKS:
        check(no, what, fn)
    bad = [r for r in RESULTS if not r["ok"]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("不过的：%s" % ", ".join("#%s %s" % (r["no"], r["what"]) for r in bad))
        sys.exit(1)
    print("P5 OK（技能进化层站得住：没有一张是编的）")


if __name__ == "__main__":
    main()
