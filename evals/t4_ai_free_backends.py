"""门禁 t4：免费 AI 接入（离线，不连真网络）。

锁死四件事，全用假后端、可复现：
  ① ai.list 离线只读，pollinations 免 key 默认在（零配置开箱即用）
  ② ai.chat 流式回流 + 多后端并发（按 [id] 同屏）
  ③ ai.status 逐后端真探测（**探活的网络 I/O 也假掉**——见下）
  ④ 审批档 approval：deny 永远拒绝（fail-closed），ask 即便非副作用也要闸门确认

不连外网——pollinations 真连通性是另一回事，那条由真跑验证，不进门禁。

**为什么连 probe 都得假掉（这是真踩出来的）**：
  ai.status 的职责是「真发一条请求验连通」——它会去敲每个端点的 /models。
  这在门禁里等于把「本机网关此刻开着没、那个 key 过期没」也一起验了：
  本地网关没启动、或某个 key 被判 invalid，门禁就红了，而红的原因跟代码
  一点关系都没有。门禁红必须意味着代码坏了，否则就是假警报（狼来了）。
  所以这里假掉网络 I/O，**契约路径仍然真跑**（AI._status 真的逐个后端调
  AB.probe、真的汇总成 backends 列表），并且借一个必挂的假后端把
  「失败必须带原因」这条锁死——真挂掉的后端不许从清单里静默消失。
  真连通性另有真跑验证，不进门禁。
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
# ③ ai.status 逐后端探测（网络 I/O 假掉，契约路径真跑）
_probed = []


def fake_probe(b, timeout=8):
    """假探活：记录探了谁，zz_dead 必挂。"""
    _probed.append(b["id"])
    if b["id"] == "zz_dead":
        return False, "假后端：连不上（探活失败必须带原因）"
    return True, ""


AB.probe = fake_probe
# 塞一个必挂的后端（走 _CUSTOM 这条正规追加口，不改 backends() 本身）
AB._CUSTOM.append({"id": "zz_dead", "title": "假挂的后端", "protocol": "openai",
                   "base_url": "http://127.0.0.1:1/v1", "model": "m",
                   "key_env": None, "need_key": False})

r = reg.call("ai.status")
check(r.ok, "ai.status 成功")
rows = r.data["backends"]
ids = [x["id"] for x in rows]
check(ids == [b["id"] for b in AB.backends()],
      "ai.status 逐个后端都探了（顺序与清单一致，不许漏）")
check(_probed == ids, "每个后端都真调了 probe（不是自己编的结论）")
check(all(x["ok"] for x in rows if x["id"] != "zz_dead"), "通的后端报 ok")
dead = [x for x in rows if x["id"] == "zz_dead"]
check(bool(dead) and dead[0]["ok"] is False, "挂的后端报 not ok")
check(bool(dead) and (dead[0]["reason"] or "").strip(),
      "挂的后端带原因（失败必须给原因，不许静默消失）")
AB._CUSTOM.pop()          # 摘掉假后端，别留给后面的断言


# ④ 空回不许静悄悄：后端流里没有正文时，必须变成一句带原因的话
#    （真踩出来的：上游偶发空流，界面上看着像「模型没话说」，人只能盯空白猜）
AB.chat = lambda b, prompt, system="", on_token=None, max_tokens=512, timeout=60: ""
r = reg.call("ai.chat", args={"prompt": "你好"})
check(r.ok, "空回时 ai.chat 仍返回信封（不是崩）")
ans = r.data.get("answers", {})
check(all((v or "").strip() for v in ans.values()),
      "空回被换成一句带原因的话（不许交空串）")
check(any("空回" in (v or "") for v in ans.values()),
      "空回的说明里点名「空回」")
AB.chat = fake_chat                       # 还原，后面的断言还要用它

r = reg.call("ai.complete", args={"prompt": "你好"})
check(r.ok and r.data.get("text"), "ai.complete 正常路径拿到正文")


# ⑤ 审批档 approval：deny 永远拒绝；ask 即便非副作用也要闸门
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
