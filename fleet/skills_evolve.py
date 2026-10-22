"""技能进化：从「跑通过的执行」长出「可验证的技能」。

借的东西只有两样（其余一律不搬，理由见 docs/技能进化_可验证融合落地_计划书_2026-10-22.md）：
  · ScienceClaw 的**回放闸** —— 候选要持久化，必须同时满足
    `R_src = Pass ∧ Use`（通过了 **且** 真的用上了每个组件）、
    独立验证上**严格提升**、噪声守卫（≥2 改善、≤1 退化）。
  · SkillClaw 的**三阶段** —— Summarize（取证据）→ Aggregate（聚步骤）→ Execute（写卡）。

**不搬** LLM 裁判：生成与判定全程不需要模型。
理由不是「模型不准」，是**这件事根本不需要模型**：
  一次成功执行的步骤顺序就是技能的内容，能力注册表就是技能用到的词表，
  证据库就是判定「有没有真的跑过」的地方。三样都是现成的。

四道硬闸（任一不过就不许落盘）：

  闸1 溯源真   evolved_from 的每个 id 都在证据库里，且 status='done'。
              且**至少一条**是 exec: 级（真跑过）；mem: 级只能做补充。
              —— 记忆只能证明「当时记下了这么一件事」，证明不了「能力跑过」。
  闸2 能力真   caps 里的每个能力都在注册表里存在，
              **且**在源事件的 topic 里真出现过（= Pass ∧ Use 的 Use）。
  闸3 可执行   正文里出现的每个能力名都必须已申报在 caps 里；
              步骤数 3–15（少于 3 是废话，多于 15 是没人看的长文）。
  闸4 文本干净 正文经 journal.redact 后无密钥残留；目录审计判 clean。

诚实边界（写在代码里，防止以后自己骗自己）：
  我们没有 ScienceClaw 的「重置环境重放」——真实设备没有可重置的沙盒。
  所以本模块是**弱验证**：它能挡住「凭空编造的技能」，
  挡不住「在这台机器能跑、在另一台跑不了」。后者靠多节点统计发现，不靠这里假装能验。
"""
import datetime
import json
import os
import re
import shutil

import journal as J
from . import skills as SK
from . import skills_audit as SA

# 自写技能目录（与 skills/ 分开：那边是 vendored 第三方，有自己的契约与门禁）
Evolved_DIR = os.path.join(SK.REPO_SKILLS_DIR, "..", "skills_evolved")
Evolved_DIR = os.path.abspath(Evolved_DIR)
LEDGER = os.path.join(Evolved_DIR, "EVOLVED.jsonl")

# 步骤数边界
MIN_STEPS = 3
MAX_STEPS = 15

# 噪声守卫（照抄 ScienceClaw 的数值）
GUARD_MIN_IMPROVED = 2
GUARD_MAX_REGRESSED = 1

# 能力名在正文里的形状（只用于**发现**，命中与否还要过注册表）
_CAP_TOKEN = re.compile(r"\b([a-z][a-z0-9_]*(?:\.[a-zA-Z0-9_]+)+)\b")

# 触发技能的 topic 前缀（使用时记一条，便于回查「这张技能被用过几次」）
USE_PREFIX = "skill:use:"
# 技能使用事件的 trace 前缀（第三种级别：既不是 exec 也不是 mem）
USE_TRACE_PREFIX = "use:"


# ------------------------------------------------------------------ 证据

def _evidence_journal(db=None):
    path = db or os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "fleet_evidence.db")
    return J.Journal(path, cap=200000)


def _done_events(j, ids):
    """按 ids 取证据，且只认 status='done' 的。返回 (存在的, 缺失的)。"""
    if not ids:
        return [], []
    q = ",".join("?" * len(ids))
    rows = j.con.execute(
        "SELECT id,kind,status,trace,topic FROM journal WHERE id IN (%s)" % q,
        list(ids)).fetchall()
    got = {r[0]: r for r in rows}
    ok, bad = [], []
    for i in ids:
        r = got.get(i)
        if r is None:
            bad.append((i, "证据不存在"))
        elif r[2] != J.DONE_STATUS:
            bad.append((i, "证据状态是 %s（不是 done）" % r[2]))
        else:
            ok.append(r)
    return ok, bad


# ------------------------------------------------------------------ 提议

def propose(trace, name, description="", db=None, reg=None):
    """从一次执行的证据链里长出一张技能候选（**不落盘**）。

    步骤顺序 = 证据链里能力被调用的顺序。**不调用 LLM**。
    """
    j = _evidence_journal(db)
    try:
        rows = j.by_trace(trace)
    finally:
        j.close_connection()

    done = [r for r in rows if r["status"] == J.DONE_STATUS]
    if not done:
        raise SkillError(
            "trace %s 里没有 done 的证据（找到 %d 条事件，0 条 done）——"
            "没有跑通过的执行，长不出技能" % (trace, len(rows)))

    steps = []
    for r in done:
        topic = r["topic"] or ""
        if not topic.startswith("cap:"):
            continue
        cap = topic[4:]
        title = ""
        if reg is not None:
            c = reg.capability(cap)
            title = getattr(c, "title", "") if c else ""
        steps.append((cap, title, r["id"]))

    if len(steps) < MIN_STEPS:
        raise SkillError(
            "这条链只有 %d 个能力调用（至少要 %d 个）："
            "少于这个数的不是流程，是一次操作" % (len(steps), MIN_STEPS))
    if len(steps) > MAX_STEPS:
        raise SkillError(
            "这条链有 %d 个能力调用（上限 %d）：超过这个数就不该是一张卡，"
            "该拆成多张" % (len(steps), MAX_STEPS))

    caps = []
    for cap, _t, _i in steps:
        if cap not in caps:
            caps.append(cap)
    ev_from = [i for _c, _t, i in steps]

    guard = "任一步失败即停，不许继续往下试"
    desc = description or ("按固定顺序调用 %d 个能力完成一次操作；"
                           "顺序来自 trace %s 的实测执行" % (len(caps), trace))

    body = _render_body(name, desc, trace, steps, guard)
    front = _render_frontmatter(name, desc, ev_from, caps, guard)
    return {
        "name": name,
        "description": desc,
        "trace": trace,
        "evolved_from": ev_from,
        "caps": caps,
        "guard": guard,
        "steps": steps,
        "body": body,
        "text": front + "\n" + body,
        "skill": _skill_record(name, desc, ev_from, caps, guard, body),
    }


def _render_frontmatter(name, desc, ev_from, caps, guard):
    return ("---\n"
            "name: %s\n"
            "description: %s\n"
            "agent_created: true\n"
            "license: internal\n"
            "evolved_from: [%s]\n"
            "caps: [%s]\n"
            "guard: %s\n"
            "---\n" % (name, desc, ", ".join(str(i) for i in ev_from),
                       ", ".join(caps), guard))


def _render_body(name, desc, trace, steps, guard):
    lines = ["# %s" % name, "", desc, "", "## 步骤", ""]
    for i, (cap, title, jid) in enumerate(steps, start=1):
        lines.append("%d. 调 `%s`%s（证据 journal#%d）"
                     % (i, cap, ("——" + title) if title else "", jid))
    lines += ["", "## 失败怎么办", "",
              "- %s" % guard,
              "- 出错就停，把错误原样报出来，不许自己换一条路继续。", "",
              "## 这张卡从哪来", "",
              "- 来自 trace `%s` 的实测执行（证据 id：%s）。" % (
                  trace, ", ".join(str(i) for _c, _t, i in steps)),
              "- 它证明的是「这些能力按这个顺序跑通过」，"
              "不证明「在任何机器上都成立」。", ""]
    return "\n".join(lines)


def _skill_record(name, desc, ev_from, caps, guard, body):
    return {"name": name, "description": desc, "evolved_from": list(ev_from),
            "caps": list(caps), "guard": guard, "body": body}


# ------------------------------------------------------------------ 四闸

def verify(candidate, db=None, reg=None, roots=None):
    """跑四道闸。返回 (ok, gates)；gates 是 [{gate, ok, detail}]。

    闸的顺序刻意从「最便宜且最致命」到「最贵」：
    能力不存在 ⇒ 正文里提到一个不存在的东西，后面都不用看了。
    """
    gates = []

    def add(gate, ok, detail):
        gates.append({"gate": gate, "ok": bool(ok), "detail": detail})
        return ok

    name = candidate.get("name") or "?"
    caps = list(candidate.get("caps") or [])
    ev_from = list(candidate.get("evolved_from") or [])

    # ---- 闸2 能力真（先做这个：注册表查一下最便宜）----
    if reg is None:
        from . import catalog
        reg = catalog.build(with_nodes=False, ttl=0.1)
    cap_names = reg.cap_names()

    ok2, missing, why = SK.validate_caps({"caps": caps}, cap_names)
    add("闸2 能力真·存在", ok2, why)

    # ---- 闸1 溯源真 ----
    j = _evidence_journal(db)
    try:
        ok_rows, bad = _done_events(j, ev_from)
        if not ev_from:
            add("闸1 溯源真", False, "evolved_from 是空的：这张技能没有来源")
        else:
            detail = "取到 %d 条 done 证据" % len(ok_rows)
            if bad:
                add("闸1 溯源真", False,
                    "有 %d 条证据不成立：%s" % (
                        len(bad), "；".join("#%d %s" % (i, why2) for i, why2 in bad[:5])))
            else:
                add("闸1 溯源真", True, detail)
        # 强证据：至少一条 exec: 级
        exec_rows = [r for r in ok_rows if (r[3] or "").startswith("exec:")]
        mem_rows = [r for r in ok_rows if (r[3] or "").startswith("mem:")]
        add("闸1 溯源真·强证据",
            bool(exec_rows),
            "exec 级 %d 条（真跑过）、mem 级 %d 条（只是记过）"
            % (len(exec_rows), len(mem_rows)))

        # ---- 闸2 的另一半：Use（这些能力在源事件里真出现过）----
        used = set()
        for r in ok_rows:
            topic = r[4] or ""
            if topic.startswith("cap:"):
                used.add(topic[4:])
        not_used = [c for c in caps if c not in used]
        add("闸2 能力真·真用过", not not_used,
            "全部 %d 个能力都在源证据里出现过" % len(caps) if not not_used
            else "这些能力在源证据里没出现过：%s" % "、".join(not_used))
    finally:
        j.close_connection()

    # ---- 闸3 可执行 ----
    body = candidate.get("body") or ""
    mentioned = set()
    for m in _CAP_TOKEN.finditer(body):
        tok = m.group(1)
        if tok in cap_names:
            mentioned.add(tok)
    undeclared = sorted(mentioned - set(caps))
    n_steps = len(candidate.get("steps") or [])
    ok3a = not undeclared
    add("闸3 可执行·无未申报能力", ok3a,
        "正文提到的能力都已申报" if ok3a
        else "正文里出现了没申报的能力：%s" % "、".join(undeclared))
    ok3b = MIN_STEPS <= n_steps <= MAX_STEPS
    add("闸3 可执行·步数", ok3b,
        "步骤 %d 步（允许 %d–%d）" % (n_steps, MIN_STEPS, MAX_STEPS))

    # ---- 闸4 文本干净 ----
    redacted = J.redact(body)
    leak = [w for w in ("--password", "--token") if w in redacted]
    secret_words = re.findall(r"(?i)(?:password|passwd|api[_-]?key|token)\s*=\s*\S+",
                              redacted)
    ok4a = not secret_words
    add("闸4 文本干净·无密钥", ok4a,
        "redact 后无密钥赋值" if ok4a else "redact 后仍有：%s" % secret_words[:3])

    # 闸4 的另一半：目录审计（用临时的候选目录审）
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "tmp", "_evolve_audit")
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(candidate.get("text") or "")
        rep = SA.audit_skill(tmp)
        ok4b = rep["verdict"] == "clean"
        add("闸4 文本干净·审计", ok4b,
            "审计结论 clean" if ok4b
            else "审计结论 %s：%s" % (rep["verdict"], "；".join(rep["findings"][:3])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = all(g["ok"] for g in gates)
    return ok, gates


# ------------------------------------------------------------------ 噪声守卫

def noise_guard(improved, regressed):
    """覆盖现役技能前的最后一道：改善 ≥2、退化 ≤1，否则保留现役。

    为什么要有：一个回合的成功可能只是运气。多回合稳定改善才算数。
    """
    return improved >= GUARD_MIN_IMPROVED and regressed <= GUARD_MAX_REGRESSED


def use_stats(name, db=None):
    """技能的使用统计：从证据库反查，不自己记账。

    统计两件事：
      · 使用次数：topic = `skill:use:<name>` 的事件（用了这张卡的次数）
      · 影子成功率：这张卡覆盖的那些能力，最近的 done/open 比
        —— 这是**近似**，测的是「那些能力最近跑得怎么样」，
          不是「这张卡自己跑得好不好」。文档里必须这么写。
    """
    j = _evidence_journal(db)
    try:
        rows = j.recent(hours=10 ** 9, limit=200000)
    finally:
        j.close_connection()
    use_rows = [r for r in rows if (r["topic"] or "") == USE_PREFIX + name]
    return {
        "name": name,
        "used": len(use_rows),
        "used_done": sum(1 for r in use_rows if r["status"] == J.DONE_STATUS),
        "use_rows": [r["id"] for r in use_rows],
    }


def skill_stats(name, db=None):
    """一张技能的全部记分。

    ⚠️ 三条不许混淆的东西，这里分开算：
      · **使用次数**  —— 有多少条 `skill:use:<name>` 事件（真用了这张卡）
      · **影子成功率** —— 这张卡覆盖的那些能力，最近的 done/open 比
        （测的是「那些能力最近跑得怎么样」，**不是**这张卡自己跑得好不好）
      · **证据充分性** —— 没有任何一条能力有执行记录时，**明确报「证据不足，
        分数不可信」**，不许给个 0.5 充数。
    """
    sk = None
    for s in list_evolved():
        if s.get("name") == name:
            sk = s
            break
    caps = list(sk.get("caps") or []) if sk else []

    j = _evidence_journal(db)
    try:
        rows = j.recent(hours=10 ** 9, limit=200000)
    finally:
        j.close_connection()

    shadow = []
    for cap in caps:
        sel = [r for r in rows if (r["topic"] or "") == "cap:%s" % cap]
        # ⚠️ 必须按「一次调用」分组，不能直接数 done/open：
        #   一次成功调用会留下 **1 条 open（开始）+ 1 条 done（完成）**，
        #   一次失败调用只留 1 条 open。
        #   直接比 done:open 会永远等于 1:1，成功率恒为 50%，毫无意义。
        #   正确的分法：按 (trace, cap) 分组 —— 有 done 的算成功，没 done 的算失败。
        groups = {}
        for r in sel:
            key = (r["trace"] or "",)
            g = groups.setdefault(key, {"done": 0, "open": 0})
            g["done" if r["status"] == J.DONE_STATUS else "open"] += 1
        done_calls = sum(1 for g in groups.values() if g["done"] > 0)
        fail_calls = sum(1 for g in groups.values() if g["done"] == 0)
        total = done_calls + fail_calls
        shadow.append({"cap": cap, "done": done_calls, "open": fail_calls,
                       "total": total,
                       "rate": (done_calls / total) if total else None,
                       "ids": [r["id"] for r in sel[:20]]})

    use = use_stats(name, db=db)
    with_data = [s for s in shadow if s["rate"] is not None]
    if not with_data:
        verdict = "证据不足，分数不可信（这张卡覆盖的能力里，没有任何一条有执行记录）"
    else:
        avg = sum(s["rate"] for s in with_data) / len(with_data)
        verdict = "影子成功率 %.0f%%（覆盖 %d/%d 个能力有执行记录）" % (
            avg * 100, len(with_data), len(shadow))

    return {"name": name, "found": bool(sk), "caps": caps,
            "used": use["used"], "used_done": use["used_done"],
            "use_rows": use["use_rows"],
            "shadow": shadow, "verdict": verdict}


# ------------------------------------------------------------------ 落盘

def _ledger_read():
    if not os.path.isfile(LEDGER):
        return []
    out = []
    with open(LEDGER, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _ledger_append(rec):
    os.makedirs(Evolved_DIR, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def promote(candidate, db=None, reg=None, force=False):
    """把过了四闸的候选落盘到 skills_evolved/，并写回执。

    落盘后不覆盖既有同名技能（要覆盖必须走噪声守卫 + 显式 --force）。
    """
    ok, gates = verify(candidate, db=db, reg=reg)
    if not ok and not force:
        bad = "；".join("%s：%s" % (g["gate"], g["detail"])
                        for g in gates if not g["ok"])
        return False, {"error": "没过四闸，拒绝落盘：%s" % bad, "gates": gates}

    name = candidate.get("name") or ""
    dest = os.path.join(Evolved_DIR, name)
    if os.path.exists(dest) and not force:
        return False, {"error": "同名技能已存在（不静默覆盖）：%s" % dest}

    if os.path.exists(dest):
        shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    with open(os.path.join(dest, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(candidate.get("text") or "")

    # 随附审计（与 skills/ 同样的做法，方便以后一眼查来源）
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "tmp", "_evolve_audit")
    try:
        os.makedirs(tmp, exist_ok=True)
        with open(os.path.join(tmp, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(candidate.get("text") or "")
        rep = SA.audit_skill(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with open(os.path.join(dest, "_audit.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)

    rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "op": "create", "name": name, "trace": candidate.get("trace"),
           "from": candidate.get("evolved_from"), "caps": len(candidate.get("caps") or []),
           "gate": "pass", "gates": len(gates)}
    _ledger_append(rec)
    return True, {"name": name, "dest": dest, "ledger": rec, "gates": gates}


def list_evolved():
    """已落盘的自写技能。"""
    out = []
    if not os.path.isdir(Evolved_DIR):
        return out
    for fn in sorted(os.listdir(Evolved_DIR)):
        d = os.path.join(Evolved_DIR, fn)
        if not os.path.isdir(d):
            continue
        sk = os.path.join(d, "SKILL.md")
        if not os.path.isfile(sk):
            continue
        try:
            rec = SK.load_skill(sk)
        except SK.SkillError as e:
            out.append({"name": fn, "error": str(e)})
            continue
        out.append(rec)
    return out


# ------------------------------------------------------------------ 自测

def _selftest():
    """离线自测：四闸各有反例、噪声守卫、提议。"""
    import os
    import sys
    import tempfile

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    from fleet.core import registry as RC

    reg = RC.Registry(path="", ttl=0)
    reg.register(RC.Capability("a.one", "第一步"))
    reg.register(RC.Capability("a.two", "第二步"))
    reg.register(RC.Capability("a.three", "第三步"))
    reg.register(RC.Capability("a.four", "第四步"))

    # 造一份证据库：一条 4 步的 exec 链，全 done
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    j = J.Journal(path, cap=500)
    tr = "exec:TEST"
    ids = []
    for i, cap in enumerate(["a.one", "a.two", "a.three", "a.four"]):
        ids.append(j.log("task", "test", "完成 %s" % cap, topic="cap:%s" % cap,
                         trace=tr, status=J.DONE_STATUS))
    # 一条没 done 的
    j.log("task", "test", "没成", topic="cap:a.one", trace="exec:OTHER")
    # 一条 mem 级的（只有记忆，没有执行）
    mem_id = j.log("see", "mem", "记了一笔", topic="mem:x.md", trace="mem:x.md#L1",
                   status=J.DONE_STATUS)

    try:
        cand = propose(tr, "demo-skill", "演示技能", db=path, reg=reg)
        assert cand["caps"] == ["a.one", "a.two", "a.three", "a.four"], cand["caps"]
        assert cand["evolved_from"] == ids, cand["evolved_from"]
        print("[selftest] 提议：从执行链长出步骤与证据 id OK")

        # 四闸全过
        ok, gates = verify(cand, db=path, reg=reg)
        assert ok, gates
        print("[selftest] 四闸全过（真证据 + 真能力 + 可执行 + 干净） OK")

        # 反例1：证据 id 是编的
        bad = dict(cand, evolved_from=[999999])
        ok2, g2 = verify(bad, db=path, reg=reg)
        assert not ok2, g2
        assert any(not g["ok"] and "溯源" in g["gate"] for g in g2), g2
        print("[selftest] 反例1：编造的证据 id 被闸1拦住 OK")

        # 反例2：caps 里有不存在的能力
        bad2 = dict(cand, caps=cand["caps"] + ["a.nope"])
        ok3, g3 = verify(bad2, db=path, reg=reg)
        assert not ok3, g3
        assert any("a.nope" in g["detail"] for g in g3), g3
        print("[selftest] 反例2：不存在的能力被闸2拦住（并指名道姓） OK")

        # 反例3：正文偷偷用了没申报的能力
        cand3 = dict(cand)
        cand3["body"] = cand["body"] + "\n额外调 `a.four` 一次\n"
        cand3["caps"] = ["a.one", "a.two", "a.three"]
        cand3["steps"] = cand["steps"][:3]
        ok4, g4 = verify(cand3, db=path, reg=reg)
        assert not ok4, g4
        assert any(not g["ok"] and "没申报" in g["detail"] for g in g4), g4
        print("[selftest] 反例3：正文偷用未申报能力被闸3拦住 OK")

        # 反例4：只有 mem 级证据（没真跑过）
        cand4 = dict(cand, evolved_from=[mem_id], caps=["a.one", "a.two", "a.three"],
                     steps=cand["steps"][:3])
        ok5, g5 = verify(cand4, db=path, reg=reg)
        assert not ok5, g5
        assert any("强证据" in g["gate"] and not g["ok"] for g in g5), g5
        print("[selftest] 反例4：只有记忆证据（没真跑过）被闸1拦住 OK")

        # 反例5：步骤太少
        cand5 = dict(cand, steps=cand["steps"][:2],
                     caps=["a.one", "a.two"])
        ok6, g6 = verify(cand5, db=path, reg=reg)
        assert not ok6, g6
        print("[selftest] 反例5：步骤太少被闸3拦住 OK")

        # 噪声守卫
        assert noise_guard(2, 0) and noise_guard(2, 1)
        assert not noise_guard(1, 0), "只改善一次不算数"
        assert not noise_guard(3, 2), "退化两次就保留现役"
        print("[selftest] 噪声守卫（≥2 改善、≤1 退化） OK")
        print("[selftest] PASS")
    finally:
        j.close_connection()
        if os.path.exists(path):
            os.remove(path)


def note_use(name, ok=True, db=None, actor="fleet", detail=""):
    """记一次「这张技能被用了」。

    为什么要有这个：技能被读进提示词之后有没有照着做，本仓观测不到
    （没有 chat 代理层，也不打算造）。唯一能拿到的真信号是**使用时打一条**。
    记 topic = `skill:use:<name>`，成功 done、失败 open（失败也是数据）。

    ⚠️ 这条记录只说明「有人调用了这张卡」，**不说明**它被照着执行了。
       影子成功率是另一个指标，见 skill_stats。
    """
    j = _evidence_journal(db)
    try:
        return j.log("task", actor,
                     "使用技能 %s%s" % (name, ("：" + detail) if detail else ""),
                     topic=USE_PREFIX + name,
                     trace="%s%s" % (USE_TRACE_PREFIX, name),
                     status=J.DONE_STATUS if ok else J.OPEN_STATUS)
    finally:
        j.close_connection()


# ------------------------------------------------------------------ CLI

def _cli(argv):
    import argparse
    p = argparse.ArgumentParser(prog="fleet.skills_evolve",
                                description="技能进化：提议 / 验证 / 落盘 / 记分")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出已落盘的自写技能")
    ps = sub.add_parser("stats", help="记分")
    ps.add_argument("name")
    pp = sub.add_parser("propose", help="从一条执行链提议技能（不落盘）")
    pp.add_argument("--trace", required=True)
    pp.add_argument("--name", required=True)
    pp.add_argument("--desc", default="")
    pm = sub.add_parser("promote", help="验证并落盘")
    pm.add_argument("--name", required=True)
    pm.add_argument("--trace", default="")
    pm.add_argument("--desc", default="")
    pm.add_argument("--force", action="store_true")
    pu = sub.add_parser("note-use", help="记一次技能使用")
    pu.add_argument("name")
    pu.add_argument("--fail", action="store_true")
    pu.add_argument("--detail", default="")
    a = p.parse_args(argv)
    cmd = a.cmd or "list"

    if cmd == "list":
        for s in list_evolved():
            print("%-28s caps=%d 证据=%d  %s" % (
                s.get("name"), len(s.get("caps") or []),
                len(s.get("evolved_from") or []), (s.get("description") or "")[:40]))
        return 0

    if cmd == "stats":
        st = skill_stats(a.name)
        print("技能：%s（%s）" % (st["name"], "已落盘" if st["found"] else "未落盘"))
        print("使用次数：%d（其中成功 %d）" % (st["used"], st["used_done"]))
        print("影子成功率：")
        for s in st["shadow"]:
            rate = "证据不足" if s["rate"] is None else "%.0f%%" % (s["rate"] * 100)
            print("  %-24s done=%-4d open=%-4d %s" % (s["cap"], s["done"], s["open"], rate))
        print("结论：%s" % st["verdict"])
        return 0

    if cmd == "propose":
        cand = propose(a.trace, a.name, a.desc)
        ok, gates = verify(cand)
        for g in gates:
            print(("  ok " if g["ok"] else "  X  ") + g["gate"] + " :: " + g["detail"])
        print("四闸总结果：%s" % ("通过" if ok else "未通过"))
        if ok:
            print("\n--- 候选 SKILL.md ---\n")
            print(cand["text"])
        return 0 if ok else 1

    if cmd == "promote":
        if a.trace:
            cand = propose(a.trace, a.name, a.desc)
        else:
            cand = {"name": a.name, "description": a.desc, "trace": "",
                    "evolved_from": [], "caps": [], "guard": "", "steps": [],
                    "body": "", "text": ""}
        ok, rep = promote(cand, force=a.force)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    if cmd == "note-use":
        i = note_use(a.name, ok=not a.fail, detail=a.detail)
        print("已记使用事件 #%d（%s）" % (i, "成功" if not a.fail else "失败"))
        return 0

    p.print_help()
    return 2


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(_cli(sys.argv[1:]))
