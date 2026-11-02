"""卡死守卫：识别手机 agent 在界面上「空转烧 token」的五种形态。

设计要点（对齐 Mika OperationGuards，参数按天网现状定）：
  · 界面指纹一变，失速计数立即作废——这是「不误杀真进展」的底线。
  · 但兜圈检测用的「指纹序列」不随界面变化清空：界面在几个状态间跳变，
    恰恰是兜圈要抓的对象。
  · 五阶梯：重复动作 / 无效果 / 兜圈 / 连续失败 / 重读。
  · 判定只依赖 (指纹, 动作, 是否成功, 界面是否变化) 四个信号，
    不碰设备、不 import 任何重依赖，方便离线门禁直接断言。

关于「复用 perceive.fingerprint()」的更正（2026-11-02 核代码）：
  计划书原写复用 perceive/nearby 的 fingerprint()，但那是近场网络设备的指纹
  （IP/MAC/BSSID/蓝牙），不是屏幕 UI 指纹。本模块不 import 它，屏幕指纹由
  调用方用 screen_fingerprint() 从读屏结果现算——仓库里本就没有 UI 指纹函数。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


def screen_fingerprint(pkg: str, items: List[str]) -> str:
    """从读屏结果算一个稳定指纹：同一屏→同一串，界面一变→立刻变。

    `pkg` 是前台包名，`items` 是 screen_brief() 压出的可读控件清单。
    两者拼一起做 SHA-1，取前 16 位十六进制。
    """
    h = hashlib.sha1()
    h.update((pkg or "").encode("utf-8", "replace"))
    h.update(b"\x1f")  # 分隔符，避免 "ab"+"c" 与 "a"+"bc" 撞
    h.update("\n".join(items or []).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


@dataclass
class Decision:
    status: str        # "continue" | "warn" | "stop"
    reason: str        # 人类可读：卡在哪
    hint: str = ""     # warn 时塞进下一轮 prompt 的三条出路


def _act_key(act: Dict[str, Any]) -> str:
    """动作指纹：用来判「重复动作」。带关键参数，避免「tap 7」和「tap 8」算同一个。"""
    a = str(act.get("action", "")).lower()
    parts = [a]
    for k in ("label", "id", "target", "text", "percent", "direction", "package"):
        if act.get(k) is not None:
            parts.append(f"{k}={act[k]}")
    return " ".join(parts)


class StallGuard:
    """五阶梯卡死守卫。每步调用一次 observe()，拿回一个 Decision。"""

    def __init__(self):
        self.fp_history: List[str] = []   # 兜圈检测用，跨界面变化保留
        self._reset()

    def _reset(self):
        # 「界面指纹一变计数作废」只清失速计数，不清 fp_history。
        self.last_fp: Optional[str] = None
        self.prev_key: Optional[str] = None
        self.repeat = 0
        self.no_effect = 0
        self.fail = 0
        self.reread = 0

    @staticmethod
    def _hint(what: str) -> str:
        return (f"出路：① 换一个控件或在清单里找更准的名字 ② 先 back/scroll 找新入口 "
                f"③ 实在卡住就 done 报告现状——别硬点（{what}）")

    def observe(self, fp: str, act: Dict[str, Any], ok: bool, changed: bool) -> Decision:
        # 界面一变：失速计数作废（不误杀真进展）；指纹序列不清。
        if self.last_fp is not None and fp != self.last_fp:
            self._reset()
        self.last_fp = fp

        key = _act_key(act)
        a = str(act.get("action", "")).lower()

        # 重复动作：同一动作连着来
        self.repeat = self.repeat + 1 if key == self.prev_key else 0
        self.prev_key = key

        # 无效果：界面没变（changed 假），但动作还在做
        self.no_effect = self.no_effect + 1 if not changed else 0

        # 连续失败
        self.fail = self.fail + 1 if not ok else 0

        # 重读：在看/等但界面没变（scroll/wait 且未变化）
        self.reread = self.reread + 1 if (a in ("scroll", "wait") and not changed) else 0

        # 兜圈：2~3 个指纹循环
        self.fp_history.append(fp)
        if len(self.fp_history) > 14:
            self.fp_history.pop(0)
        loop_state, loop_n = self._loop_state()

        # —— stop 优先于 warn ——
        if loop_state == "twice":
            return Decision("stop", f"界面在 {loop_n} 个状态里兜圈（≥2 个完整循环）",
                            self._hint("兜圈空转"))
        if self.repeat >= 3:
            return Decision("stop", "同一动作重复 3 次（卡死）", self._hint("重复动作"))
        if self.no_effect >= 3:
            return Decision("stop", "动作连续 3 次界面无变化（无效果）", self._hint("点在空处"))
        if self.fail >= 8:
            return Decision("stop", "连续 8 次动作失败", self._hint("控件找不对"))
        if self.reread >= 12:
            return Decision("stop", "连续 12 步只在看/等、界面没变化（重读空转）",
                            self._hint("反复刷新"))

        # —— warn ——
        if loop_state == "once":
            return Decision("warn", f"界面疑似在 {loop_n} 个状态里兜圈", self._hint("疑似兜圈"))
        if self.repeat == 2:
            return Decision("warn", "同一动作已重复 2 次", self._hint("重复动作"))
        if self.no_effect == 2:
            return Decision("warn", "动作连续 2 次界面无变化（无效果）", self._hint("点在空处"))
        if self.fail in (2, 4):
            return Decision("warn", f"已连续 {self.fail} 次动作失败", self._hint("控件找不对"))
        if self.reread in (8, 5):
            return Decision("warn", f"连续 {self.reread} 步只在看/等、界面没变",
                            self._hint("反复刷新"))

        return Decision("continue", "")

    def _loop_state(self):
        """返回 ("none"|"once"|"twice", n)。n∈{2,3}。

        循环里必须至少含 2 个不同指纹——全相同（屏幕没动）不算兜圈，
        那种情况归 no_effect / repeat 管。
        """
        h = self.fp_history
        for n in (2, 3):
            if len(h) >= 2 * n and h[-n:] == h[-2 * n:-n]:
                if len(set(h[-n:])) < 2:
                    continue
                if len(h) >= 4 * n and h[-n:] == h[-4 * n:-3 * n]:
                    return "twice", n
                return "once", n
        return "none", 0


def _selftest():
    tap = {"action": "tap", "label": "7"}

    # ① 无效果阶梯：界面不变、动作不变、changed 假 → 第2步 warn、第3步 stop
    g = StallGuard()
    out = [g.observe("X", tap, True, False) for _ in range(3)]
    st = [d.status for d in out]
    assert st == ["continue", "warn", "stop"], st
    assert "无效果" in out[1].reason and "无效果" in out[2].reason, [d.reason for d in out]

    # ② 指纹一变：失速计数立即作废（不误杀真进展），从头走 1→warn→stop
    g = StallGuard()
    assert g.observe("X", tap, True, False).status == "continue"
    s = [g.observe("Y", tap, True, False).status for _ in range(3)]
    assert s == ["continue", "warn", "stop"], s

    # ③ 重复动作阶梯：界面指纹不变但 changed 真（隔离 no_effect），repeat 累到 3 stop
    g = StallGuard()
    out = [g.observe("X", tap, True, True) for _ in range(5)]
    st = [d.status for d in out]
    assert "stop" in st, st
    assert "重复" in out[-1].reason, out[-1].reason

    # ④ 兜圈：X,Y 交替（界面在 2 个状态间来回）→ warn 后 stop
    g = StallGuard()
    plan = [("X", {"action": "scroll"}, True, True),
            ("Y", {"action": "scroll"}, True, True)] * 5
    out = [g.observe(fp, a, ok, ch) for fp, a, ok, ch in plan]
    st = [d.status for d in out]
    assert "warn" in st and "stop" in st, st

    # ⑤ 连续失败阶梯：同屏、changed 真、ok 假、动作各异 → fail 累到 8 stop
    g = StallGuard()
    plan = [("X", {"action": "tap", "label": str(i)}, False, True) for i in range(1, 10)]
    out = [g.observe(fp, a, ok, ch) for fp, a, ok, ch in plan]
    st = [d.status for d in out]
    assert st[1] == "warn" and st[3] == "warn" and st[7] == "stop", st
    assert "连续 8 次" in out[7].reason, out[7].reason

    # ⑥ 屏幕指纹稳定性
    assert screen_fingerprint("com.x", ["a", "b"]) == screen_fingerprint("com.x", ["a", "b"])
    assert screen_fingerprint("com.x", ["a", "b"]) != screen_fingerprint("com.x", ["a", "c"])

    print("fleet/guards 自检：6 组断言全过"
          "（无效果阶梯 / 指纹作废 / 重复阶梯 / 兜圈 / 连续失败阶梯 / 指纹稳定）")


if __name__ == "__main__":
    _selftest()
