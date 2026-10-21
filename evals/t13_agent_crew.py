#!/usr/bin/env python
"""门禁 t13：角色编队三件套——五段式契约 / 容量闸 / 角色边界。

**这个门禁钉的是五件真事**（都是「容易写出来，写错了没人发现」的那种）：

  ① **契约不许被当成摆设**。
     五段式输出契约的价值全在「父级能机械消费 EVIDENCE 段」。
     一旦解析器把「空段」和「合法的 None.」混为一谈，父级就会把
     「这一段什么都没说」当成「明确说没有」⇒ **静默丢证据**。
     门禁钉死：缺段要报缺哪段；空段要报空；`None.` 必须判合法。

  ② **容量闸必须 fail-fast，不许排队**。
     官方理由很硬：排队会让「等待后代的父代理」去等「自己占着的名额」，
     互相死等。门禁钉死：满了立刻抛错，且错误里带**还差几个**。

  ③ **名额必须在真正结束后才归还**。
     「等待中的 / 待处理输入的 / 正在停止的」都还占名额——
     提前归还 ⇒ 容量被虚增 ⇒ 实际并发超过上限。
     门禁钉死：stopping 状态仍占名额；重复 release 不许把池子放飞。

  ④ **深度是独立于容量的第二个闸**。
     容量管铺多宽，深度管长多高。深度 0 = 禁止委派，根编排者不占名额。
     门禁钉死：超深度要报「这是第几层」。

  ⑤ **角色卡里写错能力名必须当场红**。
     `tool_filter` 里写一个注册表里不存在的能力名，等于这个角色想去调
     一个不存在的东西——跑起来才发现就晚了。
     门禁钉死：真实角色卡里的 tool_filter 全部必须命中注册表能力。

**全离线**：不起真服务、不连网络、不调真模型。角色执行用假 exec_fn。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.agents import capacity as cap
from fleet.agents import contract
from fleet.agents import profile as prof
from fleet.agents import roster

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ================================================================ ① 契约
print("[1] 五段式契约")

good = ("SUMMARY: 修好了空指针\n"
        "CHANGES: a.py — 加了判空\n"
        "EVIDENCE: a.py:12-18 空指针已修\n"
        "RISKS: None.\n"
        "BLOCKERS: None.")
ok, why, parsed = contract.check(good)
check(ok, f"标准五段判通过（{why}）")
check(contract.evidence(parsed).startswith("a.py:12-18"), "EVIDENCE 段能机械取出")

ok, why, _ = contract.check("SUMMARY: 只有一段")
check(not ok and any("缺" in w for w in why), f"缺段要报出来（{why}）")
check(sum(1 for w in why if "缺" in w) == 4, f"缺几段就报几段，不合并（{why}）")

ok, why, _ = contract.check("SUMMARY: a\nCHANGES:\nEVIDENCE: b\nRISKS: c\nBLOCKERS: d")
check(not ok and any("CHANGES" in w and "空" in w for w in why),
      f"空段要报『空』而不是『没写』（{why}）")

ok, why, _ = contract.check("SUMMARY: a\nCHANGES: None.\nEVIDENCE: b\nRISKS: None.\nBLOCKERS: None.")
check(ok, f"None. 是合法明确回答，不是空段（{why}）")

ok, why, _ = contract.check("我给你讲个故事……")
check(not ok and any("找不到" in w for w in why), f"压根没格式要判失败（{why}）")

ok, why, _ = contract.check("")
check(not ok, "空答卷判失败")

check(contract.is_blank("   "), "纯空白是空白")
check(not contract.is_blank("None."), "None. 不是空白")
check(contract.is_none_marker("None."), "None. 是合法否定标记")
check(not contract.is_none_marker("做了三件事"), "正常内容不是否定标记")

# prompt_block 必须真的写清五段
pb = contract.prompt_block()
check(all(s in pb for s in contract.SECTIONS), "prompt 条款里五段都在")
check("不许" in pb or "必须" in pb, "prompt 条款写成硬要求而不是建议")

# ================================================================ ②③④ 容量闸
print("[2] 容量闸")

g = cap.CapacityGate(capacity=2, max_depth=1)
check(g.in_use == 0, "根编排者不占名额（初始 0）")

l1 = g.reserve("研究员", depth=1)
check(g.in_use == 1 and g.free == 1, "预留即占名额")

try:
    g.reserve("太深", depth=2)
    check(False, "超深度要拒绝")
except cap.CapacityError as e:
    check(e.kind == "depth" and "第 2 层" in str(e), f"超深度报『第几层』（{e}）")

l2 = g.reserve("批评者", depth=1)
try:
    g.reserve("编辑", depth=1)
    check(False, "容量满要拒绝")
except cap.CapacityError as e:
    check(e.kind == "capacity" and e.need == 1, f"容量满报『还差几个』（{e}）")
    check("排队" in str(e), "错误文案点明『不排队』的理由")

l1.release()
check(g.in_use == 1, "释放一个才归还名额")

l2.release()
l2.release()
check(g.in_use == 0, "重复释放不许把池子放飞")

g.set_capacity(1)
check(g.capacity == 1, "调低容量立即生效")
g.set_capacity(4)
l3 = g.reserve("编辑", depth=1)
check(g.in_use == 1, "调高后可以再预留")

# stopping 仍占名额
l3.start()
check(l3.state == "running" and g.in_use == 1, "running 仍占名额")
l3.stop()
check(l3.state == "stopping" and g.in_use == 1, "正在停止仍占名额（没真正结束）")
l3.release()
check(g.in_use == 0, "真正结束后才归还")

g2 = cap.CapacityGate(capacity=1, max_depth=0)
try:
    g2.reserve("子", depth=1)
    check(False, "max_depth=0 时不许派生")
except cap.CapacityError as e:
    check(e.kind == "depth", "深度 0 = 禁止委派")

# ================================================================ ⑤ 角色边界 + 名册
print("[3] 角色边界与名册")

import fleet.catalog as catalog

reg = catalog.build()
known = set(reg.cap_names())
check(len(known) > 0, f"注册表有 {len(known)} 项能力可校验")

try:
    profiles = roster.load_all(known_caps=known)
    check(True, f"名册加载成功：{len(profiles)} 个角色")
except roster.RosterError as e:
    check(False, f"名册加载失败：{e}")
    profiles = {}

check("大肥鱼" in profiles, "人设卡「大肥鱼」在名册里")
for nm in ("研究员", "批评者", "编辑", "程序员", "验收者"):
    check(nm in profiles, f"执行角色「{nm}」在名册里")

for nm, p in profiles.items():
    bad = [c for c in p.tool_filter if c not in known]
    check(not bad, f"「{nm}」的 tool_filter 全部命中注册表能力" + (f"（多余：{bad[:3]}）" if bad else ""))

# tool_filter 是收窄，不是放宽
p = profiles.get("研究员")
if p:
    check(p.permits("web.corpus"), "白名单内的能力放行")
    check(not p.permits("pcctl.proc.kill"), "白名单外的能力挡住")
p2 = prof.RoleProfile(name="x", tool_filter=())
check(p2.permits("anything"), "tool_filter 为空 = 不额外收窄（但仍受注册表审批）")

# 角色卡写错能力名必须当场红
try:
    bad_card = {"spec": "chara_card_v3", "data": {"name": "坏卡", "extensions": {
        "agent": {"tool_filter": ["no.such.cap"], "max_depth": 0}}}}
    prof.from_card(bad_card, known_caps=known)
    check(False, "写错能力名要报错")
except prof.ProfileError as e:
    check("no.such.cap" in str(e), f"写错能力名当场报错（{e}）")

# 字段类型错误也要红
for bad, why in (({"agent": {"max_depth": "1"}}, "max_depth 非整数"),
                 ({"agent": {"tool_filter": "web.corpus"}}, "tool_filter 非列表"),
                 ({"agent": {"max_depth": -1}}, "max_depth 负数")):
    try:
        prof.from_card({"data": {"name": "x", "extensions": bad}})
        check(False, f"{why} 要报错")
    except prof.ProfileError:
        check(True, f"{why} 当场报错")

# ================================================================ ⑥ 编排器接入
print("[4] 编排器接入容量闸")

import orchestrator

gate = cap.CapacityGate(capacity=1, max_depth=1)
plan = [
    {"id": "s1", "desc": "角色一步", "cap": "exec", "node": "n1",
     "args": {"cmd": "echo a"}, "role": "研究员", "depth": 1},
    {"id": "s2", "desc": "再一步", "cap": "exec", "node": "n1",
     "args": {"cmd": "echo b"}, "role": "批评者", "depth": 1},
]
seen = []


def fake_exec(node, cmd):
    seen.append((node, cmd))
    return {"ok": True, "rc": 0, "out": cmd}


# 单步跑完就释放 ⇒ 第二步能拿到名额
r = orchestrator.run_plan(plan, fake_exec, gate=gate)
check(r["ok"], f"顺序跑完两步都成功（{r}）")
check(len(seen) == 2, "两步都执行了")

# 容量被占满时：fail-fast，且**不执行**后面的步
gate2 = cap.CapacityGate(capacity=1, max_depth=1)
hold = gate2.reserve("外部占位", depth=1)
seen2 = []


def fake_exec2(node, cmd):
    seen2.append(cmd)
    return {"ok": True, "rc": 0, "out": cmd}


r2 = orchestrator.run_plan(plan, fake_exec2, gate=gate2)
check(not r2["ok"], "容量满时计划失败（不排队）")
check(r2.get("rejected_by") == "capacity", f"标明是被容量闸拒绝（{r2}）")
check(not seen2, "被拒的计划一步都不许执行")
check("容量已满" in (r2.get("err") or ""), f"错误带原因（{r2.get('err')}）")
hold.release()

# 声明了 role 却没给 gate ⇒ 报错，不许静默放过
r3 = orchestrator.run_plan(plan, fake_exec2)
check(not r3["ok"] and "容量闸" in (r3.get("err") or ""),
      f"有 role 没 gate 要报错（{r3.get('err')}）")

# 普通步（无 role）不占名额，也不该因没 gate 而失败
plain = [{"id": "p1", "desc": "普通步", "cap": "exec", "node": "n1", "args": {"cmd": "echo x"}}]
r4 = orchestrator.run_plan(plain, fake_exec2)
check(r4["ok"], "无 role 的步不受容量闸约束")

# 深度的步在 max_depth=1 下要拒绝
deep_plan = [{"id": "d1", "desc": "第二层", "cap": "exec", "node": "n1",
              "args": {"cmd": "echo y"}, "role": "孙角色", "depth": 2}]
r5 = orchestrator.run_plan(deep_plan, fake_exec2, gate=cap.CapacityGate(capacity=4, max_depth=1))
check(not r5["ok"] and "第 2 层" in (r5.get("err") or ""),
      f"深度超限在编排层也要拒（{r5.get('err')}）")

print()
if FAIL:
    print(f"=== t13 失败 {len(FAIL)} 项 ===")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("=== t13 全通过 ===")
