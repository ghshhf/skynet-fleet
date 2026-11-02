"""代码转化门禁：锁死「LLM 转译 → 去围栏 → 语法/编译校验」链路（全离线，mock LLM，不耗额度）。

对应铁律：注释不是防线，测试才是。code_convert 的价值是「机器转 + 机器验」，
这里就把这两半都钉住：去围栏要对、校验要真拦语法错、LLM 失败要如实报错。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.core import DeviceResult as R
import fleet.kinds.llmgw_device as LG
from fleet.kinds.code_convert import (
    CodeConvert, _strip_fence, _verify_python,
)

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 去围栏
check(_strip_fence("```python\nx = 1\n```") == "x = 1", "去 python 围栏失败")
check(_strip_fence("```\nx = 2\n```") == "x = 2", "去裸围栏失败")
check(_strip_fence("x = 3") == "x = 3", "裸代码被改动")

# ---------------------------------------------------------------- 语法/编译校验
good, gmsg = _verify_python("x = 1\nprint(x)\n")
check(good is True, "合法代码被误判失败: %s" % gmsg)
bad, bmsg = _verify_python("def f(:\n  pass\n")
check(bad is False, "非法语法竟然通过校验")
check(bool(bmsg), "失败未给原因")

# ---------------------------------------------------------------- mock LLM（不联网）
_FAKE = {"content": "```python\nx = 1\n```", "fail": False}


def _fake_invoke(self, cap, args=None):
    if _FAKE["fail"]:
        return R.error(cap, "模拟网关不可用", device="llmgw", via="mock")
    return R.success(cap, "llmgw", via="mock", data={
        "result": {"choices": [{"message": {"content": _FAKE["content"]}}]}})


LG.LlmGwDevice.invoke = _fake_invoke

dev = CodeConvert()

# 1) 正常转译：带围栏 → 去围栏 → 语法过 → ok
r1 = dev.invoke("code.convert", {"code": "var x = 1;", "src": "javascript", "dst": "python"})
check(getattr(r1, "ok", False), "正常转译未成功: %r" % getattr(r1, "err", None))
d1 = getattr(r1, "data", {}) or {}
check(d1.get("code") == "x = 1", "转译结果未去围栏: %r" % d1.get("code"))
check(d1.get("ok_verify") is True, "转译结果未通过语法校验")

# 2) LLM 返回非法 python：语法校验必须拦下 → ok=False
_FAKE["content"] = "def f(:\n  pass"
r2 = dev.invoke("code.convert", {"code": "x", "dst": "python"})
check(getattr(r2, "ok", False) is False, "语法错的转译竟被当成成功")

# 3) LLM 不可用：如实报错，不伪造结果
_FAKE["fail"] = True
r3 = dev.invoke("code.convert", {"code": "x", "dst": "python"})
check(getattr(r3, "ok", False) is False, "网关不可用却报成功")
check("模拟网关不可用" in (getattr(r3, "err", "") or ""), "网关失败原因未透传: %r" % getattr(r3, "err", None))

# 4) code.verify 能力
_FAKE["fail"] = False
r4 = dev.invoke("code.verify", {"code": "y = 1\n", "lang": "python"})
check(getattr(r4, "ok", False), "code.verify 合法代码被判失败")
r5 = dev.invoke("code.verify", {"code": "def (", "lang": "python"})
check(getattr(r5, "ok", False) is False, "code.verify 非法代码未拦下")

# ---------------------------------------------------------------- 汇总
if FAIL:
    print("FAIL")
    for m in FAIL:
        print("  -", m)
    sys.exit(1)
print("PASS: 代码转化链路全过（去围栏/语法校验/端到端转译/失败透传）")
sys.exit(0)
