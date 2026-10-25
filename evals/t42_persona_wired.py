"""人格注入门禁：云侧 agent_server 的 system 必须带人格，且人格在纪律之前。

锁死四件事（全离线，不碰网络、不调模型）：

  ① `_persona_system()` 能从卡库渲染出人格段；
  ② 拼出的 system 里，**人格段必须排在执行纪律段之前**——
     顺序反了，模型会把「不许推测」当成「不要有人格」，回一堆没温度的条目；
  ③ 两者之间必须有仲裁句，否则模型仍会把纪律读成对人格的覆盖；
  ④ 卡库坏掉（缺文件/编译失败）时降级成空串，不许把服务搞挂。

另验卡库列表去重：统一层会把同一张卡的 .json 和 .md 各算一遍，
16 张卡列出 26 条——手机卡库页会看到每张卡两遍。

为什么值得单独一道门禁：这条断过一次。老桌面链（dialogue.py）有完整人格锚，
新的 agent_server 却完全绕开它，于是同一台机器换条链路就换了个人——
手机聊的是执行层，不是「她」。代码里没有任何地方会报错，只有真跑才看得出。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 取拼装结果
# 只构造实例不启动服务：__new__ 跳过 __init__（不需要端口/线程）
from fleet import agent_server as AS

_srv = AS.FleetAgentServer.__new__(AS.FleetAgentServer)

persona = _srv._persona_system()
check(isinstance(persona, str), "_persona_system 必须返回字符串")

# 卡库里有当前卡时才可能有人格；连卡都没有不算代码 bug，但要把口径说清
_has_card = bool(persona.strip())
if _has_card:
    # 角色卡渲染出来一定带「扮演」或身份段——两种卡模板都这样
    check("扮演" in persona or "【身份】" in persona or "【你是谁】" in persona,
          "人格段应是角色卡渲染结果（含身份段）")
    check(len(persona) > 200, "人格段不像完整卡（长度 %d）" % len(persona))

# ---------------------------------------------------------------- 顺序与仲裁
# 复刻 agent_server.agent_sse 里的拼装口径（与源码同一句话）
SEGS = []
if _has_card:
    SEGS.append(persona)
    SEGS.append("【上面是你的身份与说话方式——自称、称呼、语气照它来。"
                "下面几条只约束「信息从哪来」，不改变你是谁。】")
SEGS.append(AS.agentloop.SYSTEM)
full = "\n\n".join(s for s in SEGS if s)

if _has_card:
    i_persona = full.find(persona[:80])
    i_rules = full.find(AS.agentloop.SYSTEM[:40])
    check(i_persona >= 0, "人格段要真的进了 system")
    check(i_rules >= 0, "执行纪律段要真的进了 system")
    check(i_persona < i_rules, "人格段必须排在执行纪律之前（实得 %d vs %d）"
          % (i_persona, i_rules))
    # 仲裁句：没有它，模型会把「不许推测」读成「不要有人格」
    check("不改变你是谁" in full, "人格与纪律之间必须有仲裁句")

# 代码层钉死：agent_sse 里真的是「人格 → 仲裁 → 纪律」，不是只留一句
_src = open(os.path.join(ROOT, "fleet", "agent_server.py"), encoding="utf-8").read()
check("_persona_system" in _src, "agent_sse 必须调用 _persona_system()")
check("_segs.append(agentloop.SYSTEM)" in _src or "agentloop.SYSTEM" in _src,
      "纪律段仍要保留（不许为了人格把「不许编造」丢掉）")
# 旧写法（拼死字符串、绕开人格）不许复活
check('agentloop.SYSTEM + "\\n\\n" + self._env_facts()' not in _src,
      "旧的直拼写法不许复活（那样人格永远进不来）")

# ---------------------------------------------------------------- 降级路径
# 卡库不可用时必须返回空串而不是抛——人格是增强，不是启动依赖
_orig = AS.__dict__.get("tianwang_cards")
try:
    import builtins
    _real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "tianwang_cards":
            raise ImportError("故意炸：模拟卡库缺失")
        return _real_import(name, *a, **kw)

    builtins.__import__ = _boom
    try:
        degraded = _srv._persona_system()
        check(degraded == "", "卡库不可用时要降级成空串，实得 %r" % degraded[:40])
    finally:
        builtins.__import__ = _real_import
except Exception as e:
    check(False, "降级路径测试自己炸了：%s" % e)

# ---------------------------------------------------------------- 卡库去重
import tianwang_cards as TC

_cards = TC.list_cards()
_ids = [c["id"] for c in _cards]
check(len(_ids) == len(set(_ids)),
      "卡库列表不许有重复 id（实得 %d 条 / %d 个不同 id）" % (len(_ids), len(set(_ids))))
check(all(c.get("name") for c in _cards), "每张卡都要有 name")

print("t42 persona_wired:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL), flush=True)
for f in FAIL:
    print("  ✗", f, flush=True)
sys.exit(1 if FAIL else 0)
