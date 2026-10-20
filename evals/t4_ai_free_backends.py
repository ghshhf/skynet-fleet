"""门禁 t4：免费 AI 接入（离线，不连真网络）。

锁死四件事，全用假后端、可复现：
  ① ai.list 离线只读，pollinations 免 key 默认在（零配置开箱即用）
  ② ai.chat 流式回流 + 多后端并发（按 [id] 同屏）
  ③ ai.status 逐后端真探测（这里用假 chat 支撑探活，但契约路径是真跑的）
  ④ 审批档 approval：deny 永远拒绝（fail-closed），ask 即便非副作用也要闸门确认

不连外网——pollinations 真连通性是另一回事，那条由真跑验证，不进门禁。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet.ai_backends as AB
from fleet.catalog import build
from fleet.core import Capability, result as R
from fleet.core.device import BaseDevice


FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ---- 假后端：确定性 chat，验证流式与多后端并发 ----
def fake_chat(b, prompt, system="", on_token=None, max_tokens=512, timeout=60):
    text = f"[{b['id']}] 回复: {prompt}"
    if on_token:
        half = len(text) // 2
        on_token(text[:half])          # 两段回流，验证「逐行/逐块」是真的
        on_token(text[half:])
    return text


AB.chat = fake_chat

reg = build(gate=lambda cap, dev, args, level: (True, ""))


# ① ai.list 离线只读，pollinations 免 key 默认在
r = reg.call("ai.list")
check(r.ok, "ai.list 成功")
check(any(b["id"] == "pollinations" for b in r.data["backends"]),
      "pollinations 免 key 默认在（零配置开箱即用）")


# ② ai.chat 流式回流 + 多后端
lines = []
r = reg.call("ai.chat", args={"prompt": "你好", "_on_line": lines.append})
check(r.ok, "ai.chat 成功")
check("pollinations" in r.data["answers"], "ai.chat 拿到 pollinations 答案")
check(any("[pollinations]" in ln for ln in lines), "ai.chat 逐块回流带 [id] 标签")


# ③ ai.status 逐后端探测（假 chat 支撑探活，契约路径是真跑的）
r = reg.call("ai.status")
check(r.ok, "ai.status 成功")
check(all(b["ok"] for b in r.data["backends"]), "ai.status 各后端探活 ok")


# ④ 审批档 approval：deny 永远拒绝；ask 即便非副作用也要闸门
class Mini(BaseDevice):
    kind = "mini"

    def probe(self):
        return True, ""

    def caps(self):
        return ("probe.deny_demo", "probe.ask_demo")

    def invoke(self, cap, args=None):
        return R.DeviceResult.success(cap, self.name, data={"x": 1})


reg.add(Mini("mini:1"))
reg.register(Capability("probe.deny_demo", "演示拒绝", approval="deny"))
reg.register(Capability("probe.ask_demo", "演示需确认", approval="ask"))

r = reg.call("probe.deny_demo", args={})
check(r.code == "denied", f"approval=deny 永远拒绝（got {r.code}）")

gate_no = lambda c, d, a, l: (False, "未确认")
gate_yes = lambda c, d, a, l: (True, "")
r = reg.call("probe.ask_demo", args={}, gate=gate_no)
check(r.code == "denied", "approval=ask 无确认→拒绝")
r = reg.call("probe.ask_demo", args={}, gate=gate_yes)
check(r.ok, "approval=ask 有确认→放行")


print("t4 ai_free:", "PASS" if not FAIL else f"FAIL ({len(FAIL)} 项)", flush=True)
sys.exit(1 if FAIL else 0)
