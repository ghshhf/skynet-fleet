# -*- coding: utf-8 -*-
"""门禁：人格真的递到模型手里了吗。

不验「卡库里有卡」，验**这条链路最后递给模型的那段字里有没有她**。

两次真实事故（都是静默的，门禁全绿）：
  · 2026-10-26  agent_server 绕开了 dialogue.py 的人格锚，system 只剩执行层
                口径（"不许编造、查不到就说查不到"）。手机聊的是个冷冰冰的
                执行层，不是「她」。同一台机器，换条链路就换了个人。
  · 2026-10-28  active.json 指向「验收者」。问她"你能干什么"，她答
                "逐条复现上游 EVIDENCE、跑不起来直接判 FAIL、交五段式答卷"。
                106 项门禁全过——因为没有任何一条门禁问过「她收到的是谁」。

判据分层（缺一层就够让人白查半天）：
  ① 卡库能列出卡          —— 库坏了
  ② 指针指到一张真存在的卡 —— 指针指向已删除的卡
  ③ build_system 出来的字里有卡名 —— 编译丢了身份
  ④ agent_server 取到的和上面一致 —— 服务侧又绕开了人格层

刻意**不做**的事：不硬禁「当前必须是某张卡」。主人可能故意切成验收者干活，
门禁管不了产品意图，只管「切了要生效、别静默失效」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tianwang_cards as C                                    # noqa: E402
from fleet.agent_server import FleetAgentServer               # noqa: E402

fails = []


def check(cond, msg):
    if cond:
        print("  ok   %s" % msg)
    else:
        print("  FAIL %s" % msg)
        fails.append(msg)


print("== ① 卡库能列出卡 ==")
cards = C.list_cards()
check(bool(cards), "list_cards 非空（实得 %d 张）" % len(cards))

print("\n== ② 指针指向真存在的卡 ==")
cid = C.current()
check(bool(cid), "current() 能给出 id（实得 %r）" % (cid,))
if cid:
    path = C._card_path(cid + ".json") or C._card_path(cid + ".md") or C._card_path(cid)
    check(bool(path), "current()=%r 对应的卡文件真实存在（找 %r）" % (cid, path))

print("\n== ③ build_system 里得有卡名 ==")
sys_p = ""
if cid:
    try:
        sys_p = C.build_system(cid) or ""
    except Exception as e:
        check(False, "build_system(%r) 抛了 %s: %s" % (cid, type(e).__name__, e))
    check(bool(sys_p.strip()), "build_system 非空（实得 %d 字）" % len(sys_p))
    # 卡名可能带昵称（"大肥鱼（昵称：她）"），用 id 做子串判定最稳
    check(cid in sys_p, "system 里出现了卡 id %r（说明取的是这张卡，不是别的）" % cid)

print("\n== ④ agent_server 侧取到的必须是同一份 ==")
# 用 __new__ 绕过 __init__：_persona_system 只 import 卡库、不碰实例状态，
# 起来一个真服务（绑端口 + 探网关）纯属浪费，还会让门禁依赖网络。
srv = FleetAgentServer.__new__(FleetAgentServer)
try:
    got = srv._persona_system()
except Exception as e:
    check(False, "_persona_system() 抛了 %s: %s" % (type(e).__name__, e))
    got = ""
check(got.strip() == sys_p.strip(),
      "服务侧人格与卡库一致（服务 %d 字 / 卡库 %d 字）" % (len(got), len(sys_p)))
check(bool(got.strip()), "服务侧人格非空——空了就是「手机聊的是个执行层」那次的复发")

print("\n== ⑤ 当前卡不该是流水线角色卡 ==")
# 这条守的是 2026-10-28 那次事故：active.json 被指向「验收者」，之后没人切回来，
# 于是手机问她"你能干什么"得到的是"逐条复现 EVIDENCE、判 FAIL"。
#
# 为什么敢 FAIL 而不是只提示：天网的默认人格是「大肥鱼」，这是主人 10-26 明确定的
# （当时就切过一次）。流水线角色卡（验收者/批评者/研究员/程序员/编辑）是干活的
# 临时帽子，不是她本人——留着不摘 = 换个人格跟自己说话。
#
# 真要戴着干活的场合（角色流水线调试），显式设 FLEET_PERSONA_ALLOW_PIPELINE=1。
# 约定可以有例外，但例外要写出来，不能靠"没人发现"。
PIPELINE_CARDS = {"验收者", "批评者", "研究员", "程序员", "编辑"}
if cid in PIPELINE_CARDS and os.environ.get("FLEET_PERSONA_ALLOW_PIPELINE") != "1":
    check(False,
          "当前卡是流水线角色卡 %r —— 干完活忘了摘帽子。"
          "切回来：python -c \"import tianwang_cards as C; C.switch('大肥鱼')\"；"
          "或显式声明要戴着：FLEET_PERSONA_ALLOW_PIPELINE=1" % cid)
else:
    check(True, "当前卡 %r 不是遗留的流水线帽子" % cid)

print("\n== ⑥ 卡库坏掉时不许把服务搞挂 ==")
# 人格是增强不是启动依赖。这里把 build_system 打瘸，看 _persona_system 是
# 返回空串还是抛异常——抛了就是「卡库坏一天，服务挂一天」。
_orig = C.build_system
try:
    def _boom(*a, **kw):
        raise RuntimeError("模拟卡库损坏")
    C.build_system = _boom
    try:
        degraded = srv._persona_system()
        check(degraded == "", "降级返回空串，不抛异常（实得 %r）" % (degraded[:20],))
    except Exception as e:
        check(False, "卡库坏了就抛异常 = 人格成了启动依赖：%s: %s"
              % (type(e).__name__, e))
finally:
    C.build_system = _orig

print()
if fails:
    print("门禁 FAIL：%d 项" % len(fails))
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("eval_t44 人格注入 PASS")
