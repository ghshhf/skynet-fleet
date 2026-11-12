# -*- coding: utf-8 -*-
"""t_solve_offline：给 agent_phone.solve 补的端到端离线门禁。

此前唯一像「solve」的 t67 其实只跑滑块页（slider.solve_page），
solve 本体的守卫接线没有回归保护。这里不碰真机、不起窗口：
假 serial + 假读屏 + 假模型，只验两件事——

  ① 界面不变时，「无效果」阶梯必须在第 3 步真的触发
     （changed 必须是「界面是否变化」，不是「adb 命令是否成功」）；
  ② 界面在变时，solve 不许误杀，能正常走到 done。

跑法：python evals/t_solve_offline.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent_phone as ap  # noqa: E402
from fleet.guards import StallGuard, screen_fingerprint  # noqa: E402


class NoPacing:
    """节奏层置空：门禁只验逻辑，不 sleep。"""

    def __init__(self, *a, **k):
        pass

    def wait_before(self, *a, **k):
        pass

    def pause(self, *a, **k):
        pass


def _install(state):
    """把真机读屏/动作换成可控假实现；真实 do() 包装保持不变。"""

    def fake_brief(serial, limit=40):
        return "com.fake", list(state["items"]), "", ""

    def fake_do_raw(serial, act):
        # 模拟 adb 命令成功；界面是否变化由 state["mutate"] 决定
        if state["mutate"]:
            state["items"].append("- 新按钮%d  [可点]" % len(state["items"]))
        return {"ok": True, "changed": True, "err": ""}

    ap.screen_brief = fake_brief
    ap._do_raw = fake_do_raw
    ap.OutwardPacing = NoPacing
    ap.AppPacing = NoPacing
    ap.usable_models = lambda: ["fake/model"]


def _run(mutate, answers):
    state = {"items": ["- 固定屏幕  [可点]"], "mutate": mutate}
    _install(state)
    it = iter(answers)

    def fake_call(model, messages, *a, **k):
        return next(it)

    ap.call = fake_call
    return ap.solve("FAKE-SERIAL", "离线任务", max_steps=6, verbose=False)


def main():
    fails = []

    # 0) 旧口径（changed=命令成功）本身触发不了「无效果」——证明第 ① 条有牙
    g = StallGuard()
    fp = screen_fingerprint("com.fake", ["- 固定屏幕  [可点]"])
    old = [g.observe(fp, {"action": "tap", "label": "b%d" % i}, True, True).status
           for i in range(1, 6)]
    if old != ["continue"] * 5:
        fails.append(f"旧口径不该触发守卫，实际 {old}")

    # ① 固定屏：动作不同但界面永远不变 -> 第 3 步「无效果」
    taps = ['{"action":"tap","label":"btn%d"}' % i for i in range(1, 7)]
    log = _run(mutate=False, answers=taps)
    last = log[-1]
    if "无效果" not in str(last.get("guard", "")):
        fails.append(f"固定屏未触发「无效果」：{last}")
    elif last.get("step") != 3:
        fails.append(f"「无效果」应在第 3 步，实际第 {last.get('step')} 步")

    # ② 界面在变：不许误杀，模型给 done 就正常收工
    log2 = _run(mutate=True, answers=[
        '{"action":"tap","label":"a"}',
        '{"action":"tap","label":"b"}',
        '{"action":"done","answer":"完成"}',
    ])
    last2 = log2[-1]
    if last2.get("done") != "完成":
        fails.append(f"界面在变时没走到 done：{last2}")
    if any("guard" in row for row in log2):
        fails.append(f"界面在变时被守卫误杀：{log2}")

    if fails:
        print("t_solve_offline：FAIL")
        for f in fails:
            print("  ✗", f)
        return 1
    print("t_solve_offline：2 组断言全过"
          "（固定屏第 3 步无效果 / 变化屏不误杀且能 done）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
