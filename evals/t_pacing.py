"""T_节奏：OutwardPacing / AppPacing 的间隔边界。

全离线：注入固定随机数（rand=0→下限，rand=1→上限），断言区间落在计划书给定范围。
now 按真实节奏推进（每次 += 实际等待），避免负 elapsed 把 wait 撑爆。
跑法：python evals/t_pacing.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.pacing import OutwardPacing, AppPacing


def main():
    fails = []

    # ① 对外间隔下限（rand=0）：首次=8，之后只增不降且封顶 30
    o = OutwardPacing(rand=lambda: 0.0)
    now = 0.0
    w = o.interval(now=now)
    now += w
    if not (8.0 <= w <= 8.0001):
        fails.append(f"对外首次下限应为 8，实际 {w}")
    for k in range(2, 40):
        iv = o.interval(now=now)
        now += iv
        if iv < 8.0 - 1e-6:
            fails.append(f"对外第{k}次低于下限 8: {iv}")
            break
        if iv > 30.0 + 1e-6:
            fails.append(f"对外第{k}次突破上限 30: {iv}")
            break

    # ② 对外间隔上限（rand=1）：首次=20，涨到 30 封顶
    o2 = OutwardPacing(rand=lambda: 1.0)
    now = 0.0
    w = o2.interval(now=now)
    now += w
    if not (19.999 <= w <= 20.001):
        fails.append(f"对外首次上限应为 20，实际 {w}")
    for _ in range(60):
        iv = o2.interval(now=now)
        now += iv
        if iv > 30.0 + 1e-6:
            fails.append(f"对外突破上限 30: {iv}")
            break

    # ③ 窗口逻辑：自然间隔已够则补 0（不空等）
    o3 = OutwardPacing(rand=lambda: 0.0)
    w0 = o3.interval(now=0.0)            # ≈8，last_done=8
    w1 = o3.interval(now=100.0)         # 过了 100s → 补 0
    if not (w0 >= 8.0 and w1 == 0.0):
        fails.append(f"窗口补零不符：w0={w0}, w1={w1}")

    # ④ AppPacing 三档边界（rand=0 取下限，rand=1 取上限）
    lo = AppPacing(rand=lambda: 0.0)
    if not (0.5 <= lo.dwell(False) <= 0.5001):
        fails.append(f"同屏下限应为 0.5，实际 {lo.dwell(False)}")
    if not (0.9 <= lo.dwell(True) <= 0.9001):
        fails.append(f"新屏下限应为 0.9，实际 {lo.dwell(True)}")
    if not (2.5 <= lo.dwell(True, careful=True) <= 2.5001):
        fails.append(f"CAREFUL 新屏下限应为 2.5，实际 {lo.dwell(True, careful=True)}")
    hi = AppPacing(rand=lambda: 1.0)
    if not (1.199 <= hi.dwell(False) <= 1.201):
        fails.append(f"同屏上限应为 1.2，实际 {hi.dwell(False)}")
    if not (1.999 <= hi.dwell(True) <= 2.001):
        fails.append(f"新屏上限应为 2.0，实际 {hi.dwell(True)}")
    if not (4.999 <= hi.dwell(True, careful=True) <= 5.001):
        fails.append(f"CAREFUL 新屏上限应为 5.0，实际 {hi.dwell(True, careful=True)}")

    if fails:
        print("FAIL:")
        for f in fails:
            print("  ·", f)
        return 1
    print("t_pacing：4 组断言全过"
          "（对外下限/上限封顶/窗口补零 / AppPacing 三档边界）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
