"""t7：大脑子持久记忆接入 llm.py（全离线，mock/placeholder 后端）。

覆盖：
  ① fleet.memory_store 写入/检索/最近/清空（复用 memory_store 自测，此处聚焦 llm 接入）
  ② llm.stream 第一轮：user 记住；第二轮同义查询能 recall 到第一轮内容（拼进 system）
  ③ llm.complete 同样走记忆（调用 stream）
  ④ FLEET_MEMORY=0 关闭：不写、不检索，链路仍正常
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["FLEET_USE_MOCK"] = "1"
os.environ["FLEET_USE_FREE"] = "0"  # 关掉免费多端，避免联网；走 mock/placeholder
os.environ["FLEET_MEMORY"] = "1"

from fleet import memory_store as MS
import llm

FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


def reset_store():
    s = MS.get_store()
    s.clear()


# ---------------------------------------------------------------- ① 记忆写入与检索
print("① llm 接入记忆：两轮对话，第二轮 recall 到第一轮")
reset_store()
t1, b1 = llm.stream("我喜欢喝普洱茶")
check(isinstance(t1, str) and t1.strip(), f"第一轮有正文：{t1[:60]!r}")
check(b1 in ("mock", "placeholder"), f"第一轮后端合法：{b1}")

# 单例里应该已经有 user+assistant 两条
mems = MS.get_store().recall("普洱茶", top_k=3)
check(len(mems) >= 1, f"recall 能检到普洱茶相关：{mems}")
check(any("普洱" in m for m in mems), "检到的里含普洱")

# 第二轮同义查询，recall 应该命中上一轮
t2, b2 = llm.stream("我喜欢喝什么茶？")
check(isinstance(t2, str) and t2.strip(), f"第二轮有正文：{t2[:60]!r}")
check(b2 in ("mock", "placeholder"), f"第二轮后端合法：{b2}")

# 最近条数
rec = MS.get_store().recent(10)
check(len(rec) >= 2, f"recent 至少 2 条：{len(rec)}")
roles = {r["role"] for r in rec}
check("user" in roles and "assistant" in roles, "既有 user 也有 assistant 条目")

# ---------------------------------------------------------------- ② complete 也走记忆
print("② llm.complete 同样写入记忆")
reset_store()
t3, b3 = llm.complete("记住我的代号是幻象")
check(isinstance(t3, str) and t3.strip(), f"complete 有正文：{t3[:60]!r}")
mems3 = MS.get_store().recall("我的代号", top_k=3)
check(any("幻象" in m for m in mems3), f"complete 写入能被 recall：{mems3}")

# ---------------------------------------------------------------- ③ FLEET_MEMORY=0 关闭
print("③ FLEET_MEMORY=0 关闭：不写、不检索，链路正常")
os.environ["FLEET_MEMORY"] = "0"
# 需要重新取单例（get_store 只初始化一次，手动重置）
import fleet.memory_store as _MSM
_MSM._store = None
s0 = _MSM.get_store()
check(not s0.enabled, "FLEET_MEMORY=0 ⇒ enabled=False")

t4, b4 = llm.stream("这个不该被记住")
check(isinstance(t4, str) and t4.strip(), f"关闭记忆后链路仍正常：{t4[:60]!r}")
mems4 = s0.recall("不该被记住", top_k=3)
check(mems4 == [], "关闭记忆后 recall 为空：%r" % mems4)

# 恢复环境（不影响其他 eval）
os.environ["FLEET_MEMORY"] = "1"
os.environ["FLEET_USE_MOCK"] = "1"
os.environ["FLEET_USE_FREE"] = "0"

# ---------------------------------------------------------------- 收尾
reset_store()
print("t7 memory:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
