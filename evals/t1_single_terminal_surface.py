"""T1 事故型验收：终端只留一个面（命令行），且 dispatch 不许自噬。

来历：早先有「命令行 + 全屏」两个显影面，共用一套 dispatch。加 emit 注入口时
批量替换把 `_print` 方法体里那行 `self.console.print(...)` 也改成了 `self._print(...)`
→ 命令行版一调就无限递归，而全屏版的 smoke 全绿（它走 emit 分支，压根没碰那条路）。
教训：**两条渲染路径必须一起验**。

2026-11-07 主人定：全屏与命令行本来就是同一套命令，不再养两个面，全屏删除。
所以本门禁验三件事：

  1. 只剩一个入口：`fleet/tui.py` / `fleet_tui.py` / `fleet-tui.bat` / `build-tui.yml`
     都不许回来，`python -m fleet.shell --tui` 必须被 argparse 拒掉（不是悄悄忽略）；
  2. 同一条 dispatch 的两条渲染路径（console.print 与 emit）一起验：都要有产物、
     都不许自噬、同一条命令不许一边响一边哑；
  3. 真实能力表仍能装配起来（拆全屏不许把注册表带塌）。

速度：用两台假设备打这两条渲染路径，不探真机——本门禁只该跑几秒。
用真注册表会把 70 台设备全 probe 一遍（实测 100 秒），那是拿门禁预算换噪声。

跑法：python evals/t1_single_terminal_surface.py
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from fleet.core import BaseDevice, Capability, DeviceResult, Registry  # noqa: E402
from fleet.shell import FleetShell, main as shell_main  # noqa: E402

GONE = ("fleet/tui.py", "fleet_tui.py", "fleet-tui.bat", ".github/workflows/build-tui.yml")


class _Fake(BaseDevice):
    kind = "fake"
    via = "local"

    def probe(self):
        return True, ""

    def caps(self):
        return ("fake.ping", "fake.echo")

    def invoke(self, cap, args=None):
        return DeviceResult.success(cap, "pong", device=self.name, via=self.via)


def _fake_reg():
    reg = Registry(gate=lambda *a: (True, ""))
    reg.register_all([Capability("fake.ping", "假探测"), Capability("fake.echo", "假回显")])
    reg.add(_Fake("fake:one"))
    reg.add(_Fake("fake:two"))
    return reg


def _cli_shell(reg):
    """命令行那一面：输出进缓冲区，emit 保持 None（走 console.print）。"""
    return FleetShell(reg=reg, watch=False, console=Console(file=io.StringIO(), width=110))


def main():
    fails = []

    # ① 全屏那套必须已经拆掉（文件名与参数口都不许留后门）
    for rel in GONE:
        if os.path.exists(os.path.join(ROOT, rel)):
            fails.append(f"全屏入口还在：{rel}（已定合并为命令行，应删除）")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            shell_main(["--tui"])
        fails.append("--tui 参数仍被接受（应已移除）")
    except SystemExit as e:
        if e.code != 2:
            fails.append(f"--tui 退出码 {e.code!r}，期望 argparse 的 2")

    # ③ 真实能力表仍能装配（不 probe，只算装配）
    from fleet.catalog import build
    real = build(gate=lambda *a: (True, ""))
    n_caps = len(real.cap_names())
    if n_caps < 400:
        fails.append(f"真实能力表只剩 {n_caps} 条，装配塌了")

    reg = _fake_reg()

    # ② 命令行那条路：能打印、不自噬
    sh = _cli_shell(reg)
    try:
        for cmd in ("devices", "caps"):
            sh.dispatch(cmd)
    except RecursionError as e:
        fails.append(f"dispatch 无限递归：{e}")
    out = sh.console.file.getvalue()
    if not out.strip():
        fails.append("命令行一条都没打印出来")
    if "设备" not in out:
        fails.append(f"输出里没有设备表：{out[:100]!r}")

    # ② 的 emit 那条路（程序化捕获，GUI/脚本在用）：拿到渲染物，类型得对
    got = []
    sh2 = FleetShell(reg=reg, watch=False, emit=got.append)
    sh2.dispatch("devices")
    if len(got) != 1:
        fails.append(f"emit 拿到 {len(got)} 个渲染物，期望 1 个")
    elif not isinstance(got[0], Table):
        fails.append(f"emit 拿到的不是 Table：{type(got[0]).__name__}")

    # ② 的两条路不许一边响一边哑
    for cmd in ("devices", "caps"):
        buf = io.StringIO()
        a = FleetShell(reg=reg, watch=False, console=Console(file=buf, width=110))
        b_got = []
        b = FleetShell(reg=reg, watch=False, emit=b_got.append)
        a.dispatch(cmd)
        b.dispatch(cmd)
        if bool(buf.getvalue().strip()) != bool(b_got):
            fails.append(f"「{cmd}」两条路径一个有输出一个没有")

    for f in fails:
        print("  ✗ " + f)
    print(f"t1 单一终端面（能力表 {n_caps} 条）：" + ("全过" if not fails else f"{len(fails)} 项不过"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
