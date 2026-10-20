"""D0 验收：设备不在线时，能力必须报 unavailable **且带原因**。

这不是「断言一个返回值对不对」，是复现那次真事故的形状：
老模块失败时有的返回 `{}`、有的返回 `{"ok": False}`（没有 err）、有的直接抛异常。
上层（规则 / 她 / 模型）只看得到「失败了」，于是写不出「重试还是换设备还是叫人」。

判据：
  1. 离线设备的调用 → code=unavailable，err 里必须有它自己的原因；
  2. 「设备在但连不上」与「根本没有这能力」必须是两个不同的 code；
  3. 真注册表里手机没连时，screen.read 的原因要能指向 adb（不是一句「不可用」）；
  4. 同一句原因在连续两次调用里都要在（缓存不许把原因吞掉）。

跑法：python evals/d0_bridge_offline.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.core import BaseDevice, Capability, DeviceResult as R, Registry  # noqa: E402


class Dead(BaseDevice):
    """一台「在账上、但连不上」的设备。"""

    kind = "dead"

    def __init__(self, name="bridge:gone", reason="编队桥不在线"):
        super().__init__(name, via="bridge")
        self.reason = reason

    def probe(self):
        return False, self.reason

    def caps(self):
        return ("screen.read",)


class Bad(BaseDevice):
    """老模块式的 adapter：失败但不给原因。"""

    kind = "bad"

    def probe(self):
        return True, ""

    def caps(self):
        return ("screen.read",)

    def invoke(self, cap, args=None):
        return {"ok": False}          # 没有 err——最坏的那一类


def main():
    fails = []

    def check(cond, why):
        if not cond:
            fails.append(why)

    # ① 离线设备：unavailable + 原因
    reg = Registry(path="", ttl=0)
    reg.register(Capability("screen.read", "看屏幕"))
    reg.add(Dead("bridge:gone", "编队桥不在线：18765 无监听"))
    r = reg.call("screen.read")
    check(r.code == R.UNAVAILABLE, f"离线设备应 unavailable，得到 {r.code}")
    check("18765" in r.err, f"原因被吞了: {r.err!r}")
    check(r.ok is False, "离线设备不许 ok=True")

    # ② 「没有这能力」≠「连不上」：两个 code 必须不同
    r2 = reg.call("input.tap")
    check(r2.code == R.UNSUPPORTED, f"未登记能力应 unsupported，得到 {r2.code}")
    check(r2.code != r.code, "unsupported 与 unavailable 混成一个了")

    # ③ adapter 失败但不给原因 → 也要有一句能看的话，且不能是 ok
    reg2 = Registry(path="", ttl=0)
    reg2.register(Capability("screen.read", "看屏幕"))
    reg2.add(Bad("b:1"))
    r3 = reg2.call("screen.read")
    check(r3.ok is False, "adapter 返 ok=False 却成了")
    check(bool((r3.err or "").strip()), "失败结果没有原因（上层无法处置）")

    # ④ 真注册表：手机没连时，原因要指向 adb
    from fleet.catalog import build
    real = build(path="", with_nodes=False)
    ok, reason, dev = real.check("screen.read", "phone:main")
    if dev is not None and not ok:
        check("adb" in reason.lower(), f"手机不可用的原因没指向 adb: {reason!r}")
    # 两次调用原因一致（不许缓存把原因吞掉）
    ok2, reason2, _ = real.check("screen.read", "phone:main")
    check((ok, reason) == (ok2, reason2), "两次结论不一致（缓存吞了原因？）")

    # ⑤ 不可能用的时候必须有原因，不可能空字符串
    for r in (real.call("screen.read"), real.call("shell.exec")):
        if not r.ok:
            check(bool(r.err.strip()), f"{r.cap} 失败但没原因")

    if fails:
        print("FAIL:")
        for f in fails:
            print("  ·", f)
        return 1
    print("d0_bridge_offline：5 组断言全过（离线带原因 / 两种失败不混淆 / 原因可指向通道）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
