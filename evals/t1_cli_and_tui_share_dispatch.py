"""T1 事故型验收：同一个 dispatch，两个显影面（命令行 + 全屏）。

这是复现一次真事故的形状，不是「断言返回值对不对」：
给 shell 加 emit 注入口时，批量替换把 `_print` 方法体里那行
`self.console.print(...)` 也改成了 `self._print(...)` → 命令行版一调就无限递归。
而全屏版的 smoke 全绿——它走 emit 分支，压根没碰到那条路。

教训：**两条路必须一起验**。只验一条，另一条会在你以为没事的时候烂掉。

判据：
  1. 命令行那条路（emit=None）真能打印出内容，且不许自噬（RecursionError）；
  2. 全屏那条路（emit=收集）拿到的是同一批渲染物，类型得是 Table；
  3. 同一条命令在两边必须「都有输出或都没有」——不许一边响一边哑。

跑法：python evals/t1_cli_and_tui_share_dispatch.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from fleet.catalog import build  # noqa: E402
from fleet.shell import FleetShell  # noqa: E402


def _cli_shell(reg):
    """命令行那一面：输出进缓冲区，emit 保持 None（走 console.print）。"""
    return FleetShell(reg=reg, watch=False, console=Console(file=io.StringIO(), width=110))


def main():
    fails = []
    reg = build(gate=lambda *a: (True, ""))

    # ① 命令行那条路：能打印、不自噬
    sh = _cli_shell(reg)
    try:
        for cmd in ("devices", "caps", "transports"):
            sh.dispatch(cmd)
    except RecursionError as e:
        fails.append(f"命令行版 dispatch 无限递归：{e}")
    out = sh.console.file.getvalue()
    if not out.strip():
        fails.append("命令行版一条都没打印出来")
    if "设备" not in out:
        fails.append(f"命令行版输出里没有设备表：{out[:100]!r}")

    # ② 全屏那条路：拿到渲染物，且类型正确
    got = []
    sh2 = FleetShell(reg=reg, watch=False, emit=got.append)
    sh2.dispatch("devices")
    if len(got) != 1:
        fails.append(f"全屏版拿到 {len(got)} 个渲染物，期望 1 个")
    elif not isinstance(got[0], Table):
        fails.append(f"全屏版拿到的不是 Table：{type(got[0]).__name__}")

    # ③ 同一条命令，两边不许一边响一边哑
    for cmd in ("devices", "caps"):
        buf = io.StringIO()
        a = FleetShell(reg=reg, watch=False, console=Console(file=buf, width=110))
        b_got = []
        b = FleetShell(reg=reg, watch=False, emit=b_got.append)
        a.dispatch(cmd)
        b.dispatch(cmd)
        if bool(buf.getvalue().strip()) != bool(b_got):
            fails.append(f"「{cmd}」两版一个有输出一个没有")

    for f in fails:
        print("  ✗ " + f)
    print("t1 两版共用 dispatch：" + ("全过" if not fails else f"{len(fails)} 项不过"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
