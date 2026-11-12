"""节奏控制：防风控、防烧钱的两类间隔（Mika Outward/AppPacing 移植）。

  · OutwardPacing：对外动作（打到设备上的操作）之间的间隔。
      默认快档：首次随机 2~5s，每多一次 +0.3~0.8s，上限 12s；
      30min 窗口内若自然间隔（思考+执行）已够，则只补不够、不空等。
      需要更强防风控时设 `FLEET_PACING=slow`：首次 8~20s、步进 0.5~1s、上限 30s
      （2026-11-12 主人拍板：默认 8~20s 太久，快档作默认）。
  · AppPacing：屏幕停留。同屏 0.5~1.2s，新屏 0.9~2s（CAREFUL 档新屏 2.5~5s）。

设计：随机数可注入（默认 random.random），纯函数 interval()/dwell() 供离线门禁用
固定值断言边界；wait_before()/pause() 才真正 sleep。不 import 任何重依赖。
"""
from __future__ import annotations

import os
import random
import time
from typing import Callable, Optional, Tuple

#: `FLEET_PACING=slow|careful|safe` 切回防风控档；其余值（含未设）走快档。
_FAST = (2.0, 5.0)
_FAST_INC = (0.3, 0.8)
_FAST_CAP = 12.0
_SLOW = (8.0, 20.0)
_SLOW_INC = (0.5, 1.0)
_SLOW_CAP = 30.0


def _is_slow() -> bool:
    return os.environ.get("FLEET_PACING", "").strip().lower() in ("slow", "careful", "safe")


class OutwardPacing:
    """对外动作间隔。每次动作执行前调一次 wait_before()。"""

    def __init__(self, rand: Optional[Callable[[], float]] = None,
                 first: Optional[Tuple[float, float]] = None,
                 inc: Optional[Tuple[float, float]] = None,
                 cap: Optional[float] = None,
                 window: float = 1800.0):
        self._r = rand if rand is not None else random.random
        slow = _is_slow()
        self.first = first if first is not None else (_SLOW if slow else _FAST)
        self.inc = inc if inc is not None else (_SLOW_INC if slow else _FAST_INC)
        self.cap = cap if cap is not None else (_SLOW_CAP if slow else _FAST_CAP)
        self.window = window
        self.calls = 0
        self._base = 0.0
        self.last_done = 0.0

    def interval(self, now: Optional[float] = None) -> float:
        """算这次要等几秒（不睡）。窗口内自然间隔已够则补 0。"""
        now = time.time() if now is None else now
        self.calls += 1
        if self.calls == 1:
            self._base = self.first[0] + self._r() * (self.first[1] - self.first[0])
        else:
            step = self.inc[0] + self._r() * (self.inc[1] - self.inc[0])
            self._base = min(self.cap, self._base + step)
        desired = self._base
        elapsed = now - self.last_done
        wait = max(0.0, desired - elapsed)
        # 超出 30min 窗口就重置基准，避免间隔无限膨胀
        if elapsed > self.window:
            self.calls = 1
            self._base = self.first[0] + self._r() * (self.first[1] - self.first[0])
            wait = self._base
        self.last_done = now + wait
        return wait

    def wait_before(self, now: Optional[float] = None):
        """真正间隔：算完就睡。"""
        w = self.interval(now)
        if w > 0:
            time.sleep(w)


class AppPacing:
    """屏幕停留。每次动作执行后调一次 pause(fp, is_new)。"""

    def __init__(self, rand: Optional[Callable[[], float]] = None,
                 same: Tuple[float, float] = (0.5, 1.2),
                 new: Tuple[float, float] = (0.9, 2.0),
                 careful_new: Tuple[float, float] = (2.5, 5.0)):
        self._r = rand if rand is not None else random.random
        self.same = same
        self.new = new
        self.careful_new = careful_new

    def dwell(self, is_new: bool = False, careful: bool = False) -> float:
        """算这次停几秒（不睡）。"""
        if is_new:
            lo, hi = (self.careful_new if careful else self.new)
        else:
            lo, hi = self.same
        return lo + self._r() * (hi - lo)

    def pause(self, fp: str = "", is_new: bool = False, careful: bool = False):
        """真正停留：算完就睡。fp 仅作占位，便于将来接入指纹判定。"""
        time.sleep(self.dwell(is_new=is_new, careful=careful))


def _selftest():
    # ① 对外间隔下限（快档，rand=0）：首次=2，之后只增不降且封顶 12
    o = OutwardPacing(rand=lambda: 0.0)
    now = 0.0
    w = o.interval(now=now)
    now += w
    assert 2.0 <= w <= 2.0001, w
    for _ in range(2, 40):
        iv = o.interval(now=now)
        now += iv
        assert iv >= 2.0 - 1e-6, iv
        assert iv <= 12.0 + 1e-6, iv

    # ② 对外间隔上限（快档，rand=1）：首次=5，涨到 12 封顶
    o2 = OutwardPacing(rand=lambda: 1.0)
    now = 0.0
    w = o2.interval(now=now)
    now += w
    assert 4.999 <= w <= 5.001, w
    for _ in range(60):
        iv = o2.interval(now=now)
        now += iv
        assert iv <= 12.0 + 1e-6, iv

    # ③ 窗口逻辑：自然间隔已够则补 0（不空等）
    o3 = OutwardPacing(rand=lambda: 0.0)
    w0 = o3.interval(now=0.0)            # ≈2，last_done=2
    w1 = o3.interval(now=100.0)          # 过了 100s → 补 0
    assert w0 >= 2.0 and w1 == 0.0, (w0, w1)

    # ③b 防风控档可由环境变量切回 8~20 / 30
    os.environ["FLEET_PACING"] = "slow"
    try:
        oslow = OutwardPacing(rand=lambda: 0.0)
        assert 8.0 <= oslow.interval(now=0.0) <= 8.0001
        assert oslow.cap == 30.0
    finally:
        os.environ.pop("FLEET_PACING", None)

    # ④ AppPacing 三档边界（rand=0 取下限，rand=1 取上限）
    lo = AppPacing(rand=lambda: 0.0)
    assert 0.5 <= lo.dwell(False) <= 0.5001, lo.dwell(False)
    assert 0.9 <= lo.dwell(True) <= 0.9001, lo.dwell(True)
    assert 2.5 <= lo.dwell(True, careful=True) <= 2.5001, lo.dwell(True, careful=True)
    hi = AppPacing(rand=lambda: 1.0)
    assert 1.199 <= hi.dwell(False) <= 1.201, hi.dwell(False)
    assert 1.999 <= hi.dwell(True) <= 2.001, hi.dwell(True)
    assert 4.999 <= hi.dwell(True, careful=True) <= 5.001, hi.dwell(True, careful=True)

    print("fleet/pacing 自检：5 组断言全过（快档下限/上限封顶/窗口补零/防风控档 / AppPacing 三档边界）")


if __name__ == "__main__":
    _selftest()
