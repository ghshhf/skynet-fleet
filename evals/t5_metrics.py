"""t5：可观测性埋点 + image.gen 在 AI 设备的接线（全离线）。

覆盖：
  ① fleet.metrics 渲染格式（counter / histogram / label 转义）
  ② ai_backends.chat_fallback 的埋点：请求数 / 失败数 / 降级切换 / 延迟 / 冷却计数
  ③ AI 设备 image.gen 接线：prompt 空校验、未接 android_bridge 时如实报「未配置」
"""
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet import metrics as M
import fleet.ai_backends as AB
from fleet.kinds.ai import AI
from fleet.core import DeviceResult as R


FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


def reset(m):
    m._series.clear()
    m._order.clear()
    if hasattr(m, "_buckets"):
        m._buckets.clear()
    if hasattr(m, "_agg"):
        m._agg.clear()


# ---------------------------------------------------------------- ① 指标渲染
print("① metrics 渲染")
M._selftest()  # 自带断言，失败会抛
txt = M.render()
for name in ("fleet_ai_requests_total", "fleet_ai_failures_total",
             "fleet_ai_cooldown_total", "fleet_ai_fallback_switches_total",
             "fleet_ai_latency_seconds", "fleet_ai_healthy_backends",
             "fleet_ai_cooldown_active"):
    check(name in txt, "渲染含指标 %s" % name)
check("HELP" in txt and "TYPE" in txt, "渲染含 HELP/TYPE 头")


# ---------------------------------------------------------------- ② chat_fallback 埋点
print("② chat_fallback 埋点")
orig = AB._chat_stream
for m in (M.ai_request_total, M.ai_failure_total, M.ai_cooldown_total, M.ai_fallback_total):
    reset(m)
reset(M.ai_latency)
AB._COOLDOWN.clear()

bs = [{"id": "pollinations", "protocol": "openai", "base_url": "x", "model": "m"}]


def fake_ok(b, prompt, system, on_token, max_tokens, timeout):
    return "PONG"


AB._chat_stream = fake_ok
text, via = AB.chat_fallback("hi", candidates=bs, timeout=5)
check(text == "PONG" and via == "pollinations", "首条成功：text=PONG via=pollinations")
check(M.ai_request_total._series.get(("pollinations", "ok"), 0) == 1, "requests_total[pollinations/ok]=1")
check(("pollinations",) in M.ai_latency._agg, "latency 记录了 pollinations")
check(M.ai_fallback_total._series == {}, "首条即成功 ⇒ 无降级计数")


def fake_empty_then_ok(b, prompt, system, on_token, max_tokens, timeout):
    if b["id"] == "pollinations":
        return ""            # 空回
    return "OK2"


AB._chat_stream = fake_empty_then_ok
for m in (M.ai_request_total, M.ai_failure_total, M.ai_cooldown_total, M.ai_fallback_total):
    reset(m)
bs2 = [{"id": "pollinations", "protocol": "openai", "base_url": "x", "model": "m"},
       {"id": "ovh", "protocol": "openai", "base_url": "y", "model": "m"}]
text, via = AB.chat_fallback("hi", candidates=bs2, timeout=5)
check(text == "OK2" and via == "ovh", "空回后切 ovh 成功：text=OK2 via=ovh")
check(M.ai_fallback_total._series.get((), 0) == 1, "fallback 切换计数=1")
check(M.ai_failure_total._series.get(("pollinations", "empty"), 0) == 1, "pollinations/empty 失败=1")
check(M.ai_request_total._series.get(("pollinations", "empty"), 0) == 1, "pollinations/empty 请求=1")
check(M.ai_request_total._series.get(("ovh", "ok"), 0) == 1, "ovh/ok 请求=1")


def fake_429(b, prompt, system, on_token, max_tokens, timeout):
    raise urllib.error.HTTPError("u", 429, "rate", {}, None)


AB._chat_stream = fake_429
for m in (M.ai_failure_total, M.ai_cooldown_total):
    reset(m)
AB._COOLDOWN.clear()
try:
    AB.chat_fallback("hi", candidates=[{"id": "llm7", "protocol": "openai",
                                        "base_url": "z", "model": "m"}], timeout=5)
except RuntimeError:
    pass
check(M.ai_failure_total._series.get(("llm7", "rate_limit"), 0) == 1, "llm7/rate_limit 失败=1")
check(M.ai_cooldown_total._series.get(("llm7", "rate_limit"), 0) == 1, "llm7 进入冷却计数=1")
check(AB.cooldown_remaining("llm7") > 0, "llm7 确实进入冷却")


# 健康/冷却瞬时量：手动冷却 pollinations，healthy_backends 应刷新 gauge
AB._COOLDOWN.clear()
AB.mark_failure("pollinations", {"kind": "transient", "bench": True, "cooldown": 30})
hb = AB.healthy_backends()
check(M.ai_cooldown_active._series.get((), 0) == 1, "cooldown_active 瞬时量=1")
check(M.ai_healthy_backends._series.get((), 0) == len(hb), "healthy_backends 瞬时量对齐")
AB.mark_success("pollinations")
AB.healthy_backends()
check(M.ai_cooldown_active._series.get((), 0) == 0, "mark_success 后冷却清零")
AB._chat_stream = orig


# ---------------------------------------------------------------- ③ AI 设备 image.gen 接线
print("③ AI 设备 image.gen 接线")
ai = AI()
check("image.gen" in ai.caps(), "image.gen 已进入 AI 设备能力表")

r = ai.invoke("image.gen", {"prompt": ""})
check((not r.ok) and "prompt" in (r.err or ""), "空 prompt 被拒并给原因")

r2 = ai.invoke("image.gen", {"prompt": "一只猫"})
check((not r2.ok), "未接 android_bridge 时 image.gen 失败（不静默交空）")
check("未配置" in (r2.err or ""), "失败原因明确指向「后端未配置」：%s" % r2.err)


# ---------------------------------------------------------------- 收尾
print("t5 metrics:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
