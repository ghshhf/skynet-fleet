"""确定性上下文压缩（批次2，2026-11-02）。

长任务历史不折叠，10+ 轮往返会爆上下文、前缀缓存失效。本模块把"工具往返"
折成一行结构化日志，但**任务原话 / 确认往返 / 用户消息不折**（语义关键且短）。

设计铁律（计划书要求）：
- 压缩是**纯函数**、零随机、零 LLM——同样的输入永远产出同样的字符串，可复现、可门禁。
- 任务原话作为**前缀逐字保留**（"前缀只追加"，保护缓存）；工具往返折成 `Step N tool(target) → applied/failed`。
- 接入点：agent_phone.solve 的历史管理（替换原 done_steps[-8:] 截断）。
"""
import re
import sys

__all__ = ["Compressor", "estimate_tokens"]


def estimate_tokens(text):
    """确定性的轻量 token 估算：CJK 按字、其余按空白分词。

    只用于门禁的"降幅"判定，不追求精确；同一字符串永远得到同一个数。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = re.sub(r"[\u4e00-\u9fff]", " ", text)
    words = [w for w in re.split(r"\s+", latin) if w]
    return cjk + len(words)


class Compressor:
    """把一轮任务的完整历史收成可复现的压缩串。

    条目有序：任务原话（前缀，不折）→ user / confirm（逐字）→ step（折成一行）。
    """

    def __init__(self):
        self._task = None
        self._entries = []

    def set_task(self, text):
        """任务原话：前缀、不折、逐字保留。"""
        self._task = "" if text is None else str(text)

    def add_user(self, text):
        """用户消息：逐字保留，不折。"""
        self._entries.append(("user", str(text)))

    def add_confirm(self, question, answer):
        """确认往返：问题与答复都逐字保留，不折。"""
        self._entries.append(("confirm", str(question), str(answer)))

    def add_step(self, n, action, target="", ok=False, changed=False):
        """工具往返：折成一行。target 是动作参数（不含动作名本身）。"""
        self._entries.append(
            ("step", int(n), str(action), str(target), bool(ok), bool(changed))
        )

    def render(self):
        """产出压缩后的历史字符串（确定性）。

        结构：任务原话（前缀逐字）→ 各条目（user/confirm 逐字，step 折成一行）。
        """
        out = []
        if self._task is not None:
            out.append("任务：" + self._task)
        for e in self._entries:
            k = e[0]
            if k == "user":
                out.append("用户：" + e[1])
            elif k == "confirm":
                out.append("确认：" + e[1] + " → " + e[2])
            elif k == "step":
                _, n, action, target, ok, changed = e
                if ok and changed:
                    status = "applied"
                elif ok:
                    status = "no_change"
                else:
                    status = "failed"
                tgt = "(" + target + ")" if target else ""
                out.append(f"Step {n} {action}{tgt} → {status}")
        return "\n".join(out)

    # ---- 门禁 / 调试辅助 ----
    def step_count(self):
        return sum(1 for e in self._entries if e[0] == "step")

    def has_verbatim(self, text):
        """text 是否逐字节出现在 render 输出里（用于"不折"校验）。"""
        return text in self.render()


def _selftest():
    c = Compressor()
    c.set_task("算 7 的阶乘")          # 任务原话（不折）
    c.add_user("用手机计算器")          # 用户消息（不折）
    c.add_confirm("要清屏吗？", "是")   # 确认往返（不折）
    for i in range(1, 11):             # 10 轮工具往返（折）
        c.add_step(i, "tap", f"btn_{i}", ok=True, changed=True)
    r = c.render()
    assert "算 7 的阶乘" in r, r                          # 任务原话逐字
    assert "用手机计算器" in r, r                          # 用户消息逐字
    assert "要清屏吗？" in r and "是" in r, r              # 确认往返逐字
    assert "Step 10 tap(btn_10) → applied" in r, r        # step 折成一行
    assert r == c.render(), "render 非确定性"             # 确定性
    assert c.has_verbatim("算 7 的阶乘")                  # 不折辅助校验
    print("fleet/compress 自检：6 组断言全过（不折 / 折行 / 确定性）")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
