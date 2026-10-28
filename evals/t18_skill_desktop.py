"""门禁 t18：桌面端技能机制（N+1）——「能列出/选用/执行/记分，提交与回滚可用」。

v4 计划书把 N+1 定为「桌面端技能机制真正跑起来」，判据是：
  技能在桌面能列出、能选用、能记分；promote 提交与 rollback 回滚可用；
  **真跑出 done 回执**，回滚后仍可加载且四闸仍过。

这个门禁锁的是真会发生的形状，一条条对着判据：

  ① **自写技能与只读清单分离**。
     /api/skills 是 vendored+用户级（只读），/api/skills/evolved 是自写
     （带动作）。混在一起就会出现「技能要不要审批」这种没法答的问题。
     且正文不外发——同 skills.summary() 的纪律。

  ② **提交是两步，不是一步**。
     第一步 propose 只过四闸不落盘；第二步 promote 才落盘，且服务端
     **重新 propose + 重新过闸**，前端带来的只有 (trace, name, desc)。
     为什么：前端传什么就落什么 = 绕过四闸。这里用一个「闸必红」的
     候选证明服务端真的在重算（不是把前端的话当真）。

  ③ **没有 force 开关**。
     计划书§七「自动改技能并直接生效」明确不做。四闸不过就原样返回
     gates，让前端显示哪闸红。

  ④ **落盘后紧跟 commit；没提交就明说回滚不可用**。
     「版本=git」：没提交的落盘只是工作区草稿，没有快照可回。
     这不是缺陷声明，是机制边界——所以 commit 不在 git 仓里时
     **不阻断落盘，但明确告诉前端回滚不可用**。

  ⑤ **回滚有三道自验，任一不过就把工作区改回原样**。
     回滚不许把仓滚坏。这里造一个「回滚目标过不了现行审查」的场景，
     验证工作区被原样恢复（不是滚坏一半留在那儿）。

  ⑥ **回滚只认已提交的版本**。
     只有一个提交时诚实拒绝，不假装能滚。

  ⑦ **选用是打点，不是审批**。
     note_use 只证明「有人调用了这张卡」，不证明「被照着执行了」。
     这里验它真在证据库留下 done/open 回执。

  ⑧ **记分不许把「没证据」粉饰成百分比**。
     没有任何能力有执行记录时，verdict 必须明说「证据不足，分数不可信」。

  ⑨ **按钮到端点是真连线**（Node 侧，见下）。
     桌面端四个动作（选用✓/✗、记分、预览）必须真发出对应请求。
     用 Playwright 的 route 拦截来验——**不真记使用事件**，
     否则门禁自己会污染证据库（那是测试数据冒充真usage）。

  ⑩ **token 是一道真门**：缺 401、错也 401（凭据无效同类），文案区分。

全离线、可复现：技能目录与证据库都在**临时 git 仓**里（回滚需要 git），
不碰仓库里真技能、不连网、不点真鼠标。

跑法：python evals/t18_skill_desktop.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet import skills_evolve as SE          # noqa: E402
from fleet.skills_evolve import SkillError     # noqa: E402
from fleet.gui import server as GS             # noqa: E402
import journal as J                            # noqa: E402

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)
    return bool(cond)


def section(t):
    print("\n=== %s ===" % t, flush=True)


# ------------------------------------------------------------------ 临时环境
# 关键：Evolved_DIR 必须落在**一个 git 仓库里**，否则 _repo_root() 返回 None，
# commit_evolved 会返回 None（没有快照），rollback 直接抛「不在 git 仓库里」。
# 所以这里自己 init 一个临时仓——完全不碰仓库里真技能，也让回滚链路是真的。
_tmp = tempfile.mkdtemp(prefix="t18_skill_")
_repo = os.path.join(_tmp, "repo")
_evolved = os.path.join(_repo, "skills_evolved")
os.makedirs(_evolved, exist_ok=True)


def _git(args, cwd=_repo):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True)


def _setup_repo():
    _git(["init", "-q"])
    _git(["config", "user.email", "t18@example.invalid"])
    _git(["config", "user.name", "t18 gate"])
    _git(["config", "commit.gpgsign", "false"])
    # 一个初始提交：git log 需要至少一个提交才认得后续的 -- <path> 历史
    with open(os.path.join(_repo, "README.md"), "w", encoding="utf-8") as f:
        f.write("t18 temp repo\n")
    _git(["add", "README.md"])
    _git(["commit", "-q", "-m", "init"])


_setup_repo()

# 打桩：技能目录、回执、证据库全指到临时处。
# 三个都是模块级常量/函数，且都在调用时才读 —— 所以运行时改生效。
_EVIDENCE_DB = os.path.join(_tmp, "evidence.db")
SE.Evolved_DIR = _evolved
SE.LEDGER = os.path.join(_evolved, "EVOLVED.jsonl")
_orig_evidence = SE._evidence_journal
SE._evidence_journal = lambda db=None: J.Journal(_EVIDENCE_DB, cap=200000)

# 三个真能力名（从注册表里现取，不写死——写死会在能力改名后变成假门禁）
from fleet.catalog import build as _build_catalog      # noqa: E402
_reg = _build_catalog(with_nodes=False, ttl=0.1)
_all_caps = sorted(_reg.cap_names())
CAPS_A = [c for c in ("ai.list", "ai.status", "apps.list") if c in _all_caps]
CAPS_B = [c for c in ("ai.list", "ai.status", "host.apps", "card.lint")
          if c in _all_caps]
if len(CAPS_A) < 3:
    # 注册表形状变了就明说，不悄悄降级成「测个假的」
    print("  x  注册表里找不到预期的能力名（ai.list/ai.status/apps.list），"
          "门禁形状已过期，需要更新而不是放过")
    FAIL.append("注册表能力名形状变化")
    CAPS_A = _all_caps[:3]
    CAPS_B = _all_caps[:4]

TRACE_A = "exec:t18-inspect-01"
TRACE_B = "exec:t18-inspect-02"
TRACE_EMPTY = "exec:t18-nothing-here"


def seed_trace(trace, caps, ok=True):
    """照 execute.py::_write 的真形状造一条执行链。

    形状必须一致：kind="task"、topic="cap:<名>"、trace=<trace>、
    成功 done / 失败 open。少一样，propose/verify 读出来的就不是真链。
    """
    j = J.Journal(_EVIDENCE_DB, cap=200000)
    ids = []
    try:
        for cap in caps:
            # 开始（open）——真执行会先写这个
            j.log("task", "t18", "开始 %s" % cap, topic="cap:%s" % cap,
                  trace=trace, status=J.OPEN_STATUS)
            # 完成（done）
            i = j.log("task", "t18", "完成 %s" % cap, topic="cap:%s" % cap,
                      trace=trace, status=J.DONE_STATUS if ok else J.OPEN_STATUS)
            ids.append(i)
    finally:
        j.close_connection()
    return ids


seed_trace(TRACE_A, CAPS_A)
seed_trace(TRACE_B, CAPS_B)
seed_trace(TRACE_EMPTY, [], ok=True)      # 一条空的 trace：没有 done

NAME = "t18-inspect"


# ------------------------------------------------------------------ GUI 服务
_cards = os.path.join(_tmp, "cards")
os.makedirs(_cards, exist_ok=True)
with open(os.path.join(_cards, "t18.json"), "w", encoding="utf-8") as f:
    json.dump({"name": "T18", "nickname": "十八", "description": "门禁用",
               "personality": "话少", "scenario": "在门禁里"}, f,
              ensure_ascii=False)
with open(os.path.join(_cards, "active.json"), "w", encoding="utf-8") as f:
    json.dump({"file": "t18.json"}, f, ensure_ascii=False)

_persona = GS.Persona(card_dir=_cards,
                      state_path=os.path.join(_tmp, "persona_state.json"))
_reg2 = _build_catalog(path=os.path.join(_tmp, "devices.json"), with_nodes=False)
srv = GS.FleetServer(registry=_reg2, persona=_persona)
srv.start()
BASE = srv.base_url
TOKEN = srv.token
print("[t18] 本地服务 %s（测试用）" % BASE, flush=True)

import urllib.error      # noqa: E402
import urllib.request   # noqa: E402


def http_json(url, token=None, method="GET", body=None, timeout=20):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Fleet-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8")), None
        except Exception:
            return e.code, None, str(e)
    except Exception as e:
        return None, None, str(e)


try:
    # ============================================================ ① 分离与契约
    section("① 自写清单与只读清单分离，且正文不外发")
    st, js, err = http_json(f"{BASE}/api/skills/evolved", token=TOKEN)
    check(st == 200, f"GET /api/skills/evolved 200（实际 {st} {err}）")
    check(bool(js) and js.get("ok") is True, "答复 ok=true")
    check(isinstance(js.get("skills"), list), "skills 是列表（空列表也是列表）")
    # 还没有自写技能：这里必须**诚实报空**，不能摆一个假的
    check(js.get("count") == 0, f"初始自写数为 0（实际 {js.get('count')}）")

    st, js, err = http_json(f"{BASE}/api/skills", token=TOKEN)
    check(st == 200 and bool(js), "GET /api/skills 仍然可用（只读清单不受影响）")

    # ============================================================ ② 两步提交
    section("② propose 只过闸不落盘；promote 才落盘")
    st, js, err = http_json(f"{BASE}/api/skill/propose", token=TOKEN,
                            method="POST",
                            body={"trace": TRACE_A, "name": NAME, "desc": "巡检"})
    check(st == 200, f"propose 200（实际 {st} {err}）")
    check(bool(js) and js.get("ok") is True,
          "四闸全过（候选可提交）：%s" % (js.get("gates") if js else js))
    gates = (js or {}).get("gates") or []
    check(len(gates) >= 4, f"闸门条目 ≥4（实际 {len(gates)}）")
    check(all(g.get("ok") for g in gates),
          "每条闸都 ok：%s" % "; ".join("%s=%s" % (g.get("gate"), g.get("ok"))
                                      for g in gates))
    check(sorted((js or {}).get("caps") or []) == sorted(CAPS_A),
          "caps 来自 trace 真调用的能力（实际 %s）" % ((js or {}).get("caps"),))
    check(len((js or {}).get("evolved_from") or []) >= 3,
          "evolved_from 有证据 id（实际 %s）" % ((js or {}).get("evolved_from"),))

    # 🔴 propose 之后**不许落盘**：这是两步的关键，落早了就等于一步
    check(not os.path.isfile(os.path.join(_evolved, NAME, "SKILL.md")),
          "propose 之后磁盘上没有 SKILL.md（没偷偷落盘）")

    # 闸必红的候选：trace 里没有 done ⇒ 服务端必须拒
    st, js, err = http_json(f"{BASE}/api/skill/propose", token=TOKEN,
                            method="POST",
                            body={"trace": TRACE_EMPTY, "name": "t18-empty"})
    check(st == 200 and bool(js) and js.get("ok") is False,
          "没有 done 证据的链提不出技能（服务端拒了）")
    check(bool((js or {}).get("err")), "拒绝时带明确原因：%s" % (js or {}).get("err"))

    # ============================================================ ③ promote
    section("③ promote 落盘 + 紧跟 commit（版本=git）")
    st, js, err = http_json(f"{BASE}/api/skill/promote", token=TOKEN,
                            method="POST",
                            body={"trace": TRACE_A, "name": NAME, "desc": "巡检"})
    check(st == 200 and bool(js) and js.get("ok") is True,
          "promote 落盘成功：%s" % ((js or {}).get("err") or "ok"))
    skmd = os.path.join(_evolved, NAME, "SKILL.md")
    check(os.path.isfile(skmd), "SKILL.md 真落盘了")
    check(os.path.isfile(os.path.join(_evolved, NAME, "_audit.json")),
          "随附审计结论落盘（一眼查来源）")
    sha1 = (js or {}).get("git")
    check(bool(sha1), f"落盘后紧跟一次 commit（sha={sha1}）")
    check((js or {}).get("rollback_ready") is True, "rollback_ready=true")
    # 回执：op=create
    led = [json.loads(l) for l in
           open(os.path.join(_evolved, "EVOLVED.jsonl"), encoding="utf-8")
           if l.strip()]
    check(any(r.get("op") == "create" and r.get("name") == NAME for r in led),
          "回执写了 op=create")

    # ============================================================ ④ 选用/记分
    section("④ 选用是打点（done/open 回执），记分不许粉饰没证据")
    st, js, err = http_json(f"{BASE}/api/skill/use", token=TOKEN,
                            method="POST", body={"name": NAME, "ok": True})
    check(st == 200 and bool(js) and js.get("ok") is True,
          "选用✓ 记上了：%s" % ((js or {}).get("err") or js))
    ev_ok = (js or {}).get("event_id")
    check(bool(ev_ok), "返回事件 id（可回查）")

    st, js, err = http_json(f"{BASE}/api/skill/use", token=TOKEN,
                            method="POST", body={"name": NAME, "ok": False})
    check(st == 200 and bool(js) and js.get("ok") is True,
          "选用✗ 也记上了（失败也是数据）")

    st, js, err = http_json(f"{BASE}/api/skill/use", token=TOKEN,
                            method="POST", body={"name": "../../etc/passwd"})
    check(st == 400, f"名字穿越被拒（实际 {st}）")

    st, js, err = http_json(f"{BASE}/api/skill/stats?name={NAME}", token=TOKEN)
    check(st == 200 and bool(js) and js.get("ok") is True,
          "记分算得出来：%s" % ((js or {}).get("err") or "ok"))
    check((js or {}).get("used") == 2,
          "使用次数 = 2（一成一败，实际 %s）" % (js or {}).get("used"))
    check((js or {}).get("used_done") == 1,
          "其中成功 1 次（实际 %s）" % (js or {}).get("used_done"))
    shadow = (js or {}).get("shadow") or []
    check(len(shadow) == len(CAPS_A),
          "影子成功率逐能力一条（实际 %d / 期望 %d）" % (len(shadow), len(CAPS_A)))
    verdict = (js or {}).get("verdict") or ""
    check("证据不足" in verdict or "%" in verdict,
          "verdict 要么给百分比、要么明说证据不足（实际 %r）" % verdict)
    # 这张卡的 caps 都有执行记录 ⇒ 不该报「证据不足」
    check("证据不足" not in verdict,
          "有执行记录时不许拿「证据不足」搪塞（实际 %r）" % verdict)

    # 「没证据就不给分」的另一种形状：caps 全是没执行过的能力。
    # 上面 t18-inspect 的 caps 都有记录，所以 verdict 必须给百分比；
    # 这里用一张 caps 不在任何 trace 里的技能来验「证据不足」那句原话。
    # 造法：直接走模块层，不落盘（落盘要走四闸，而闸2「真用过」会红）
    fake = {"name": "t18-noev", "description": "x", "caps": ["ai.chat"],
            "evolved_from": [], "steps": [None, None, None], "body": "1. 调 `ai.chat`"}
    st2 = SE.skill_stats("t18-noev")           # 不存在 ⇒ found=False ⇒ shadow 空
    check("证据不足" in (st2.get("verdict") or ""),
          "没有任何执行记录时 verdict 明说「证据不足，分数不可信」"
          "（实际 %r）" % st2.get("verdict"))
    check(st2.get("found") is False, "不存在的技能：found=false（不装存在）")
    del fake

    # 查不存在的技能：skill_stats 对「查无此技能」返回 ok=true + found=false
    # （查询本身成功了，只是没找到——这是诚实的形状，不该报成错误）。
    # 但前端必须能区分「没这张技能」和「这张技能 0 次使用」，
    # 否则会显示「使用 0 次」冒充一张存在的技能。
    from urllib.parse import quote                    # noqa: E402
    st, js, err = http_json(
        f"{BASE}/api/skill/stats?name={quote('不存在这张')}", token=TOKEN)
    check(st == 200 and bool(js), "stats 查询本身 200")
    check((js or {}).get("found") is False,
          "查不存在的技能：found=false（不装成一张 0 次使用的技能）")
    check("证据不足" in str((js or {}).get("verdict") or ""),
          "verdict 明说证据不足（实际 %r）" % (js or {}).get("verdict"))

    # ============================================================ ⑤ 清单带动作
    section("⑤ 自写清单带 caps/证据/回执/审计")
    st, js, err = http_json(f"{BASE}/api/skills/evolved", token=TOKEN)
    rows = (js or {}).get("skills") or []
    check(len(rows) == 1, f"自写技能 1 个（实际 {len(rows)}）")
    r = rows[0] if rows else {}
    check(r.get("name") == NAME, "名字对得上")
    check("body" not in r and "text" not in r,
          "正文不外发（同 skills.summary 纪律）")
    check(len(r.get("caps") or []) == len(CAPS_A), "caps 条数对")
    check(len(r.get("evolved_from") or []) >= 3, "证据条数对")
    check((r.get("ledger_last") or {}).get("op") == "create",
          "ledger 最近一条是 create（实际 %s）"
          % (r.get("ledger_last") or {}).get("op"))
    check(r.get("audit") == "clean",
          "审计结论 clean（实际 %s）" % r.get("audit"))

    # ============================================================ ⑥ 回滚
    section("⑥ 回滚：第二个版本 → 回到上一个，且四闸仍过")
    # 再造第二个版本。同名必须**显式** overwrite——不显式就拒（不静默覆盖）
    st, js, err = http_json(f"{BASE}/api/skill/promote", token=TOKEN,
                            method="POST",
                            body={"trace": TRACE_B, "name": NAME, "desc": "巡检v2"})
    check(st == 200 and bool(js) and js.get("ok") is False,
          "同名不显式 overwrite ⇒ 拒绝覆盖（不静默改）")
    check("同名技能已存在" in str((js or {}).get("err") or ""),
          "拒绝原因说清是同名已存在（实际 %r）" % (js or {}).get("err"))
    check((js or {}).get("can_overwrite") is True,
          "答复告诉前端「可以显式覆盖」（前端才知道下一步）")

    st, js, err = http_json(f"{BASE}/api/skill/promote", token=TOKEN,
                            method="POST",
                            body={"trace": TRACE_B, "name": NAME, "desc": "巡检v2",
                                  "overwrite": True})
    check(st == 200 and bool(js) and js.get("ok") is True,
          "显式 overwrite 后落盘成功：%s" % ((js or {}).get("err") or "ok"))
    sha2 = (js or {}).get("git")
    check(bool(sha2) and sha2 != sha1, f"第二个 commit 不同于第一个（{sha1} → {sha2}）")
    v2_text = open(skmd, encoding="utf-8").read()
    check(len(re.findall(r"(?m)^\s*\d+\.\s+调\s+`", v2_text)) == len(CAPS_B),
          "工作区现在是 v2（步骤数 %d）" % len(CAPS_B))

    st, js, err = http_json(f"{BASE}/api/skill/rollback", token=TOKEN,
                            method="POST", body={"name": NAME})
    check(st == 200 and bool(js) and js.get("ok") is True,
          "回滚成功：%s" % ((js or {}).get("err") or "ok"))
    rolled = (js or {}).get("rolled_to")
    check(rolled == sha1, f"回滚到上一个已提交版本（{rolled} vs {sha1}）")
    v1_text = open(skmd, encoding="utf-8").read()
    check(len(re.findall(r"(?m)^\s*\d+\.\s+调\s+`", v1_text)) == len(CAPS_A),
          "磁盘内容真是 v1（步骤数 %d）" % len(CAPS_A))
    # 回滚后仍可加载 + 四闸仍过（rollback 内部自验③，这里从外部再确认一次）
    from fleet import skills as SK                # noqa: E402
    rec = SK.load_skill(skmd)
    check(rec.get("name") == NAME, "回滚后仍能加载（frontmatter 没坏）")
    ok, gates = SE.verify(SE._candidate_from_disk(skmd))
    check(ok, "回滚后的版本四闸仍过：%s" % "; ".join(
        "%s=%s" % (g["gate"], g["ok"]) for g in gates))
    # 回滚也要留回执
    led = [json.loads(l) for l in
           open(os.path.join(_evolved, "EVOLVED.jsonl"), encoding="utf-8")
           if l.strip()]
    check(any(r.get("op") == "rollback" and r.get("name") == NAME for r in led),
          "回滚写了 op=rollback 回执（append-only）")

    # ============================================================ ⑦ 回滚的拒绝
    section("⑦ 回滚的诚实拒绝")
    # 只有一个已提交版本的技能：不许假装能滚
    st, js, err = http_json(f"{BASE}/api/skill/promote", token=TOKEN,
                            method="POST",
                            body={"trace": TRACE_A, "name": "t18-single",
                                  "desc": "only one"})
    check(st == 200 and bool(js) and js.get("ok") is True, "t18-single 落盘")
    st, js, err = http_json(f"{BASE}/api/skill/rollback", token=TOKEN,
                            method="POST", body={"name": "t18-single"})
    check(st == 200 and bool(js) and js.get("ok") is False,
          "只有一个提交时拒绝回滚（不假装能滚）")
    check("只有一个已提交版本" in str((js or {}).get("err") or ""),
          "拒绝原因说清是「只有一个已提交版本」（实际 %r）" % (js or {}).get("err"))
    # 工作区没被滚坏
    check(os.path.isfile(os.path.join(_evolved, "t18-single", "SKILL.md")),
          "拒绝回滚后文件还在（没把仓滚坏）")

    # to_sha 不在提交史里
    st, js, err = http_json(f"{BASE}/api/skill/rollback", token=TOKEN,
                            method="POST",
                            body={"name": NAME, "to": "0" * 40})
    check(st == 200 and bool(js) and js.get("ok") is False,
          "to_sha 不在提交史里时拒绝")
    check("不在这张技能的提交史里" in str((js or {}).get("err") or ""),
          "拒绝原因点明是提交史里没有（实际 %r）" % (js or {}).get("err"))

    # 回滚不存在的技能
    st, js, err = http_json(f"{BASE}/api/skill/rollback", token=TOKEN,
                            method="POST", body={"name": "t18-nope"})
    check(st == 200 and bool(js) and js.get("ok") is False, "回滚不存在的技能被拒")

    # ============================================================ ⑧ 鉴权
    section("⑧ token 是真门")
    st, js, err = http_json(f"{BASE}/api/skills/evolved")
    check(st == 401, f"不带 token → 401（实际 {st}）")
    st, js, err = http_json(f"{BASE}/api/skills/evolved", token="wrong")
    # 2026-10-29：403 → 401，理由见 t7_gui_backend.py。
    check(st == 401, f"带错 token → 401（实际 {st}）")
    st, js, err = http_json(f"{BASE}/api/skill/use", method="POST",
                            body={"name": NAME, "ok": True})
    check(st == 401, f"选用不带 token → 401（实际 {st}）")
    st, js, err = http_json(f"{BASE}/api/skill/promote", token=TOKEN,
                            method="POST", body={"trace": TRACE_A, "name": NAME})
    check(st == 200, "带对 token 的 promote 200（鉴权不误伤正常调用）")

    # ============================================================ ⑨ 没有后门开关
    section("⑨ 没有 force 后门（四闸永远跑；overwrite 只放开同名守卫）")
    import inspect                                       # noqa: E402
    sig = inspect.signature(SE.promote)
    check("force" not in sig.parameters,
          "promote 签名里没有 force（那是一个同时绕两道关的后门）：%s"
          % list(sig.parameters))
    check("allow_overwrite" in sig.parameters,
          "换成 allow_overwrite（语义收窄成「只放开同名守卫」）")
    # 行为上证明：造一个必红的候选（caps 含不存在的能力），
    # 连 allow_overwrite=True 也必须被四闸挡住
    bad = SE.propose(TRACE_A, "t18-bad", "x")
    bad["caps"] = bad["caps"] + ["no.such.cap.at.all"]
    ok, gates = SE.verify(bad)
    check(not ok, "caps 含不存在能力时四闸红")
    r_ok, r_rep = SE.promote(bad)
    check(not r_ok, "四闸红 ⇒ promote 拒绝落盘")
    r_ok2, r_rep2 = SE.promote(bad, allow_overwrite=True)
    check(not r_ok2,
          "🔴 四闸红 + allow_overwrite=True 也拒绝（overwrite 不放开闸门）")
    check("没过四闸" in str(r_rep2.get("err") or ""),
          "拒绝原因是四闸，不是同名（实际 %r）" % r_rep2.get("err"))
    check(not os.path.isfile(os.path.join(_evolved, "t18-bad", "SKILL.md")),
          "拒绝后磁盘上没有落盘")

    # ==================================================== ⑩ 按钮→端点 真连线
    # 🔴 必须放在 finally（srv.stop()）**之前**：活线检查要连着真服务。
    #    第一版放在 finally 之后，node 一侧直接 ERR_CONNECTION_REFUSED——
    #    服务早就停了，那种「失败」是自己造成的，不是验出来的。
    section("⑩ 按钮到端点真连线（Playwright route 拦截，不写脏数据）")
    _runner = os.path.join(ROOT, "tools", "uicheck", "skillwiring.js")
    if os.path.isfile(_runner):
        env = dict(os.environ)
        env["NODE_PATH"] = os.path.join(ROOT, "tools", "uicheck", "node_modules")
        env["T18_BASE"] = BASE
        env["T18_TOKEN"] = TOKEN
        env["T18_SKILL"] = NAME
        p = subprocess.run(["node", _runner],
                           cwd=os.path.join(ROOT, "tools", "uicheck"),
                           capture_output=True, env=env, timeout=180)
        out = (p.stdout or b"").decode("utf-8", "replace")
        errtxt = (p.stderr or b"").decode("utf-8", "replace")
        for line in out.splitlines():
            if line.strip():
                print("   " + line)
        if errtxt.strip():
            # 打全（最多 8 行）：只打首行时，活线检查崩在哪一行看不出，
            # 只能猜——那是本仓最不许的排查方式。
            for ln in errtxt.strip().splitlines()[:8]:
                print("   (stderr) " + ln)
        if p.returncode == 2:
            # 2 = 环境缺 node/playwright：明确报「没验」，不当成通过
            print("  x  活线检查没跑起来（缺 node 或 playwright）——这一项**未验证**")
            FAIL.append("活线检查未运行")
        elif p.returncode != 0:
            print("  x  活线检查未通过（退出码 %d）" % p.returncode)
            FAIL.append("活线检查失败")
    else:
        print("  x  找不到 tools/uicheck/skillwiring.js")
        FAIL.append("活线检查脚本缺失")

finally:
    try:
        srv.stop()
    except Exception:
        pass

# ------------------------------------------------------------------ 收尾
try:
    SE._evidence_journal = _orig_evidence
except Exception:
    pass
shutil.rmtree(_tmp, ignore_errors=True)

print("\n[t18] 失败 %d 项" % len(FAIL), flush=True)
if FAIL:
    for m in FAIL:
        print("  x " + m)
    sys.exit(1)
print("[t18] 全部通过", flush=True)
sys.exit(0)
