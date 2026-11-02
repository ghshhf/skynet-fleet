"""T_压缩：确定性上下文压缩门禁。

全离线：不碰设备、不调模型，直接驱动 Compressor。
跑法：python evals/t_compress.py

验收口径（计划书第五節）：
- 10 轮往返历史压缩后 token 降幅 ≥ 设定阈值；
- 任务原话与确认往返不丢字节（逐字节出现在压缩串里）；
- 压缩是确定性的（同输入同输出）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet.compress import Compressor, estimate_tokens

REDUCTION_FLOOR = 0.30  # 压缩后 token 必须 ≤ naive 的 (1 - 0.30)


def _naive_history(task, user_msg, confirm_q, confirm_a, steps):
    """模拟"不懂得折叠"的朴素历史：任务/用户/确认逐字 + 每步一长句。"""
    lines = ["任务：" + task, "用户：" + user_msg,
             "确认：" + confirm_q + " → " + confirm_a]
    for n, action, target, ok, changed in steps:
        if ok and changed:
            res = "成功且界面已变化"
        elif ok:
            res = "成功但界面无变化"
        else:
            res = "失败"
        lines.append(
            f"第 {n} 步：模型决定执行 {action} 动作，目标为「{target}」，"
            f"执行完毕后返回结果——{res}。"
        )
    return "\n".join(lines)


def main():
    # 任务原话含特殊字符，确保"逐字节不丢"是真实校验而非巧合
    task = "打开计算器算 12345×6789 的结果（保留到整数）"
    user_msg = "用手机自带计算器，别用网页"
    confirm_q = "要清屏从头开始吗？"
    confirm_a = "确认清除"

    steps = []
    comp = Compressor()
    comp.set_task(task)
    comp.add_user(user_msg)
    comp.add_confirm(confirm_q, confirm_a)
    for i in range(1, 11):
        action = "tap" if i % 2 else "set_text"
        target = f"btn_{i}" if i % 2 else f"text={i}"
        steps.append((i, action, target, True, i != 5))  # 第5步界面没变，制造 no_change
        comp.add_step(i, action, target, ok=True, changed=(i != 5))

    compressed = comp.render()
    naive = _naive_history(task, user_msg, confirm_q, confirm_a, steps)

    # ① 步数守恒：10 轮往返没有丢步
    assert comp.step_count() == 10, comp.step_count()
    assert "Step 10 " in compressed, compressed

    # ② 确定性：同输入同输出
    assert compressed == comp.render(), "render 非确定性"

    # ③ 不折：任务原话逐字节在压缩串里
    assert task in compressed, "任务原话丢失"
    # ④ 不折：确认往返逐字节在压缩串里
    assert confirm_q in compressed and confirm_a in compressed, "确认往返丢失"
    # ⑤ 不折：用户消息逐字节在压缩串里
    assert user_msg in compressed, "用户消息丢失"

    # ⑥ 降幅：压缩后 token ≤ naive 的 (1 - 0.30)
    ct = estimate_tokens(compressed)
    nt = estimate_tokens(naive)
    assert ct < nt, f"压缩后反而更长：{ct} vs {nt}"
    assert ct <= nt * (1 - REDUCTION_FLOOR), \
        f"降幅不足：压缩 {ct} > naive*0.7={nt * (1 - REDUCTION_FLOOR):.0f}"

    # ⑦ no_change 状态正确表达（第5步界面没变）
    assert "Step 5 tap(btn_5) → no_change" in compressed, compressed

    print(f"t_compress：7 组断言全过（步数守恒 / 确定性 / 不折×3 / "
          f"降幅 {nt}→{ct} ≤{nt * (1 - REDUCTION_FLOOR):.0f} / no_change 正确）")


if __name__ == "__main__":
    main()
