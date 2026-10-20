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
import json
import os
import socket
import sys
import urllib.request

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
check(any(b["id"] == "ovh" and not b.get("need_key")
          for b in r.data["backends"]),
      "OVHcloud 免 key 兜底也在（第二个真·零配置端点）")
# 🔴 硬编码的 model 必须还是上游真有的 model（2026-10-21 实测踩的坑）：
#   llm7 原来写死 "gpt-4o-mini"，该模型已不在 llm7 当前 70 个模型里 → 调必 404。
#   「配了个看着很合理的模型名」是这类 bug 的伪装，离线门禁锁死它。
_llm7 = [b for b in r.data["backends"] if b["id"] == "llm7"]
if _llm7:
    check(_llm7[0].get("model") != "gpt-4o-mini",
          "llm7 不再用已下线的 gpt-4o-mini")
else:
    print("  - llm7 无 token 未装配（跳过模型名断言）")


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

# ⑤b3 默认 max_tokens 必须够推理模型用（防止将来被改回 512 重新踩坑）
from fleet.kinds.ai import DEFAULT_MAX_TOKENS
check(DEFAULT_MAX_TOKENS >= 1024,
      f"默认 max_tokens={DEFAULT_MAX_TOKENS} 够推理模型思考（<1024 会空回）")
_seen_mt = {}


def _capture_mt(b, prompt, system="", on_token=None, max_tokens=512, timeout=60):
    _seen_mt["v"] = max_tokens
    return "好"


AB.chat = _capture_mt
reg.call("ai.chat", args={"prompt": "你好"})
AB.chat = fake_chat
check(_seen_mt.get("v") == DEFAULT_MAX_TOKENS,
      f"ai.chat 不传 max_tokens 时用默认值（实得 {_seen_mt.get('v')}）")


# ⑤ 流中途卡死：已收到的内容不许丢；一个字都没收到必须报原因
#    （真踩出来的：上游偶发卡死时异常直接冲出 _chat_stream，半截答案全作废；
#      用户只看到「出错」，其实模型已经写了一半。真跑 8 次撞到 1 次 timeout）
class _FakeResp:
    """假 HTTP 响应：迭代到第 n 块后按剧本抛异常。"""

    def __init__(self, chunks, exc=None):
        self._chunks = list(chunks)
        self._exc = exc
        self.closed = False

    def __iter__(self):
        for c in self._chunks:
            yield c
        if self._exc:
            raise self._exc

    def close(self):
        self.closed = True


def _sse(text):
    return ("data: " + json.dumps(
        {"choices": [{"delta": {"content": text}}]}) + "\n\n").encode("utf-8")


_orig_urlopen = urllib.request.urlopen
_orig_resolve = AB._resolve_model
AB._resolve_model = lambda b: "fake-model"

# ⑤a 收到一半卡死 -> 半截内容必须给出去，且标明被中断
_captured = []
try:
    urllib.request.urlopen = lambda *a, **k: _FakeResp(
        [_sse("前半句"), _sse("后半句")], exc=socket.timeout("timed out"))
    t = AB._chat_stream({"id": "x", "base_url": "http://127.0.0.1:1/v1",
                         "need_key": False}, "问", "", _captured.append,
                        32, 5)
    check("前半句" in t and "后半句" in t,
          "流中途卡死：已收到的内容没被丢掉")
    check("[流被中断]" in t, "卡死时答案里标明了「流被中断」")
    check("前半句" in "".join(_captured),
          "中断前的内容已经实时回流过（不是最后才吐）")
except Exception as e:
    check(False, f"流中途卡死路径不该抛异常（抛了 {type(e).__name__}: {e}）")

# ⑤b 一个字都没收到就卡死 -> 必须报原因，不许交空串
try:
    urllib.request.urlopen = lambda *a, **k: _FakeResp([], exc=socket.timeout("timed out"))
    AB._chat_stream({"id": "x", "base_url": "http://127.0.0.1:1/v1",
                     "need_key": False}, "问", "", None, 32, 5)
    check(False, "零内容超时应该抛错（不该静默返回空串）")
except RuntimeError as e:
    check("超时" in str(e) or "中断" in str(e),
          f"零内容超时带原因（{e}）")
except Exception as e:
    check(False, f"零内容超时应抛 RuntimeError（抛了 {type(e).__name__}）")

# ⑤b2 推理模型把预算烧光（真因锁死）：finish_reason=length 且正文 0 字符
#      ⇒ 必须报「预算不够」这个**具体原因**，不许退化成笼统的「上游抽风」
#      （真跑抓到的：max_tokens=300 时 minimax-m3.1-flash 思考烧光预算、正文 0 字符，
#        界面看着像「模型没话说」，人被引到错误方向去——重试没用，加预算才有用）
def _sse_fin(text, fin):
    return ("data: " + json.dumps(
        {"choices": [{"delta": {"content": text} if text else {},
                      "finish_reason": fin}]}) + "\n\n").encode("utf-8")


try:
    urllib.request.urlopen = lambda *a, **k: _FakeResp(
        [_sse_fin("", None), _sse_fin("", "length")])
    AB._chat_stream({"id": "x", "base_url": "http://127.0.0.1:1/v1",
                     "need_key": False}, "问", "", None, 300, 5)
    check(False, "预算烧光时应该抛错（不该静默返回空串）")
except RuntimeError as e:
    check("预算" in str(e) and "max_tokens" in str(e),
          f"预算烧光时给出的是具体原因（{e}）")
    check("抽风" not in str(e),
          "不许把「预算不够」说成「上游临时抽风」（那会把人引向错误排查方向）")
except Exception as e:
    check(False, f"预算烧光应抛 RuntimeError（抛了 {type(e).__name__}: {e}）")

urllib.request.urlopen = _orig_urlopen
AB._resolve_model = _orig_resolve


# ⑥ 审批档 approval：deny 永远拒绝；ask 即便非副作用也要闸门
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
