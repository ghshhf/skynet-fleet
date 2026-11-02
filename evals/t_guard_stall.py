"""T_护栏：卡死守卫五阶梯 + 「界面指纹一变全部计数作废」。

全离线：不碰设备、不调模型，直接驱动 StallGuard.observe() 喂构造信号。
跑法：python evals/t_guard_stall.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.guards import StallGuard, screen_fingerprint


def _run(plan):
    """plan: list of (fp, act, ok, changed)。返回 [Decision.status]。"""
    g = StallGuard()
    out = []
    for fp, act, ok, changed in plan:
        out.append(g.observe(fp, act, ok, changed).status)
    return out


def main():
    fails = []

    # ① 无效果阶梯：界面不变、动作不变、changed 假 → 第2步 warn、第3步 stop
    tap = {"action": "tap", "label": "7"}
    st = _run([("X", tap, True, False)] * 3)
    if st[0] != "continue" or st[1] != "warn" or st[2] != "stop":
        fails.append(f"无效果阶梯不符预期: {st}")
    if "无效果" not in StallGuard().observe("X", tap, True, False).reason:  # 占位避免误用
        pass

    # ② 指纹一变：计数立即作废（不误杀真进展）
    g = StallGuard()
    assert g.observe("X", tap, True, False).status == "continue"
    # 换屏 Y：之前的 no_effect 计数应作废，从头计
    s1 = g.observe("Y", tap, True, False).status
    s2 = g.observe("Y", tap, True, False).status
    s3 = g.observe("Y", tap, True, False).status
    if not (s1 == "continue" and s2 == "warn" and s3 == "stop"):
        fails.append(f"指纹作废后应重新走 1→warn→stop，实际: {[s1, s2, s3]}")

    # ③ 重复动作阶梯：界面指纹不变但 changed 真（隔离 no_effect），repeat 累到 3 stop
    g = StallGuard()
    plan = [("X", tap, True, True)] * 5
    st = _run(plan)
    if "stop" not in st:
        fails.append(f"重复动作未触发 stop: {st}")
    # stop 应来自「重复」而非「无效果」
    g2 = StallGuard()
    last = None
    for _ in plan:
        last = g2.observe("X", tap, True, True)
    if "重复" not in last.reason:
        fails.append(f"重复动作 stop 理由应为重复，实际: {last.reason}")

    # ④ 兜圈：X,Y 交替（界面在 2 个状态间来回）→ warn 后 stop
    g = StallGuard()
    plan = [("X", {"action": "scroll"}, True, True),
            ("Y", {"action": "scroll"}, True, True)] * 5
    st = _run(plan)
    if "warn" not in st or "stop" not in st:
        fails.append(f"兜圈未触发 warn+stop: {st}")

    # ⑤ 连续失败阶梯：恒定指纹（不触发「指纹一变作废」）、changed 真（隔离 no_effect）、
    #    ok 假、动作各异（隔离 repeat）→ fail 累到 8 stop；2/4 为 warn
    g = StallGuard()
    plan = [("X", {"action": "tap", "label": str(i)}, False, True)
            for i in range(1, 10)]
    out = []
    for fp, act, ok, ch in plan:
        out.append(g.observe(fp, act, ok, ch))
    st = [d.status for d in out]
    if st[1] != "warn" or st[3] != "warn" or st[7] != "stop":
        fails.append(f"连续失败阶梯不符: {st}")
    if "连续 8 次" not in out[7].reason:
        fails.append(f"失败 stop 理由应为连续8次，实际: {out[7].reason}")

    # ⑥ 屏幕指纹稳定性（guard 之外也顺手验）
    if screen_fingerprint("com.x", ["a", "b"]) != screen_fingerprint("com.x", ["a", "b"]):
        fails.append("同屏指纹不稳定")
    if screen_fingerprint("com.x", ["a", "b"]) == screen_fingerprint("com.x", ["a", "c"]):
        fails.append("不同屏指纹撞了")

    if fails:
        print("FAIL:")
        for f in fails:
            print("  ·", f)
        return 1
    print("t_guard_stall：6 组断言全过"
          "（无效果阶梯 / 指纹作废 / 重复阶梯 / 兜圈 / 连续失败阶梯 / 指纹稳定）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
