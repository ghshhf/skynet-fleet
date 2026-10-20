"""全屏终端界面（Textual）：人在这一屏上看见并调用全部设备能力。

为什么要有它：命令行能跑命令，但**看不见**——键位看不见、能力清单看不见、
设备在不在线要敲一条命令才知道。这一屏把三样同时摊在眼前：左边设备、
右边全部能力、中间是发生过的一切（她的话、事件、每次调用的结果）。

它与 `shell.py` 不是两套命令，是同一套的两个显影面：
  命令行版（shell.py）—— 单行 REPL，给它 `emit` 就是它；
  全屏版（本文件）  —— 给它 `emit` 换成写进中间那扇窗。
二者共用 `FleetShell.dispatch`，加一个能力只改注册表一处，两边同时出现。

线程分工：耗时的事（探测设备、跑能力）一律丢进 worker 线程，
主线程只负责把渲染结果写进窗口（经 `_emit` → 消息队列 → `on_flush`）。
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, Input, RichLog, Static

from .shell import BUILTINS, FleetShell, _status_bits

HELP_LINE = ("例：pc.state / screen.read --device phone:main / input.tap --text 确定"
             "　右表选中回车即可把能力名提到命令行")


class Flush(Message):
    """worker 线程产出的渲染物，请主线程写进日志窗。"""


class FleetTUI(App):
    TITLE = "设备编队"
    SUB_TITLE = "人在这一屏上看见并调用全部设备"

    CSS = """
    Screen { layout: vertical; }
    #status {
        height: 1; width: 100%; padding: 0 1;
        background: $boost; color: $text;
    }
    #body { height: 1fr; }
    #devices { width: 30; }
    #logpane { width: 1fr; border: round $accent; }
    #caps { width: 62; }
    DataTable { height: 100%; }
    RichLog { height: 1fr; }
    #cmd { border: round $primary; }
    """

    BINDINGS = [
        ("ctrl+e", "focus_cmd", "命令行"),
        ("ctrl+d", "focus_devices", "设备"),
        ("ctrl+k", "focus_caps", "能力"),
        ("ctrl+r", "refresh", "重新探测"),
        ("ctrl+s", "save", "落盘"),
        ("ctrl+l", "clear_log", "清屏"),
        ("ctrl+y", "confirm_danger", "确认破坏性"),
        ("f1", "help", "用法"),
        ("ctrl+q", "quit", "退出"),
        ("up", "history_prev", "上一条"),
        ("down", "history_next", "下一条"),
    ]

    def __init__(self, interval=20.0):
        super().__init__()
        self.shell = None
        self.interval = interval
        self._outbox = []
        self._hist = []
        self._hi = 0
        self._danger = None        # 待确认的破坏性命令（ctrl+y 才放行）
        self._dev_rows = []
        self._cap_rows = []
        self._rendered = 0         # 真正写进窗口的块数（门禁里可断言）

    # ---------------- 组装 ----------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("正在探测设备…", id="status")
        with Horizontal(id="body"):
            yield DataTable(id="devices", zebra_stripes=True)
            with Vertical(id="logpane"):
                yield RichLog(id="log", wrap=True, highlight=True, markup=False)
            yield DataTable(id="caps", zebra_stripes=True)
        yield Input(placeholder="能力名 [--device 设备] [--参数 值]　（ctrl+e 回到这里）",
                    id="cmd")
        yield Footer()

    def on_mount(self):
        self._boot()
        self.set_interval(5.0, self._tick_status)

    # ---------------- 启动：探测在线程里，UI 不卡 ----------------

    @work(thread=True, exclusive=True)
    def _boot(self):
        self.shell = FleetShell(interval=self.interval, watch=True, emit=self._emit)
        self.shell.blocking_confirm = False        # TUI 不许在事件循环里 input()
        self.call_from_thread(self._after_boot)

    def _after_boot(self):
        self._paint_caps()
        self._refresh()
        self.query_one("#cmd", Input).focus()

    # ---------------- 输出：一切渲染物都经此口 ----------------

    def _emit(self, renderable):
        self._outbox.append(renderable)
        self.post_message(Flush())                  # post_message 线程安全

    def on_flush(self, _ev):
        if not self._outbox:
            return
        log = self.query_one("#log", RichLog)
        for r in self._outbox:
            log.write(r)
        self._rendered += len(self._outbox)
        self._outbox.clear()

    # ---------------- 三块面板 ----------------

    @work(thread=True, exclusive=True)
    def _refresh(self):
        """重新探测（重活）+ 把设备表画出来（轻活交还主线程）。"""
        self.shell.reg.invalidate()
        rows = self.shell.reg.availability()
        self.call_from_thread(self._paint_devices, rows)
        self._tick_status()

    def _paint_devices(self, rows):
        t = self.query_one("#devices", DataTable)
        t.clear(columns=True)
        t.add_columns("设备", "状态")
        self._dev_rows = []
        for r in rows:
            self._dev_rows.append(r["name"])
            t.add_row(r["name"], "● 在线" if r["ok"] else "○ 离线")

    def _paint_caps(self):
        t = self.query_one("#caps", DataTable)
        t.clear(columns=True)
        t.add_columns("能力", "说明", "性质")
        self._cap_rows = []
        for c in self.shell.reg.capabilities():
            self._cap_rows.append(c.name)
            kind = "破坏" if c.destructive else ("动手" if c.side_effect else "只读")
            t.add_row(c.name, c.title, kind)

    def _tick_status(self):
        st = self.query_one("#status", Static)
        if not self.shell:
            return
        b = _status_bits(self.shell.reg)
        parts = [("她 ", "bold"),
                 ("✓" if b["her"][0] else "✗", "green" if b["her"][0] else "red"),
                 ("　手机 ", "bold"),
                 ("✓" if b["phone"][0] else "✗", "green" if b["phone"][0] else "red"),
                 (f"　在线 {b['online']}/{b['devices']} 台　{b['caps']} 项能力", "")]
        txt = Text()
        for seg, style in parts:
            txt.append(seg, style=style or None)
        if self._danger:
            txt.append("　⚠ 有待确认的破坏性操作（ctrl+y）", style="bold red")
        st.update(txt)

    # ---------------- 执行 ----------------

    def on_input_submitted(self, ev):
        line = (ev.value or "").strip()
        ev.input.value = ""
        if not line:
            return
        self._hist.append(line)
        self._hi = len(self._hist)
        self._submit(line)

    def _submit(self, line):
        head = (line.split() or [""])[0]
        if head in ("quit", "exit", "q"):
            self.exit()
            return
        if head not in BUILTINS:
            cap = self.shell.reg.capability(head)
            if cap is not None and cap.destructive and "--yes" not in line:
                self._danger = line
                self.shell.say(f"破坏性操作待确认：{line}", level="warn")
                self.shell.say("按 ctrl+y 放行，ctrl+l 撤销这条", level="info")
                self._tick_status()
                return
        self._run(line)

    @work(thread=True)
    def _run(self, line, confirm=False):
        self.shell._confirm = confirm
        try:
            self.shell.dispatch(line)
        finally:
            self.shell._confirm = False

    # ---------------- 右表/左表：选了就把能力名提到命令行 ----------------

    def on_data_table_row_selected(self, ev):
        t = ev.data_table
        if t.id == "caps":
            i = t.cursor_row
            if 0 <= i < len(self._cap_rows):
                inp = self.query_one("#cmd", Input)
                inp.value = self._cap_rows[i] + " "
                inp.focus()
        elif t.id == "devices":
            i = t.cursor_row
            if 0 <= i < len(self._dev_rows):
                self.shell.say(f"设备：{self._dev_rows[i]}", level="info")

    # ---------------- 动作 ----------------

    def action_quit(self):
        self.exit()

    def action_help(self):
        self.shell.dispatch("help")
        self.shell.say(HELP_LINE, level="info")

    def action_clear_log(self):
        self._danger = None
        self.query_one("#log", RichLog).clear()
        self._tick_status()

    def action_refresh(self):
        self._refresh()

    def action_save(self):
        self.shell.say(f"已落盘：{self.shell.reg.save() or '（不可写）'}", level="info")

    def action_confirm_danger(self):
        if not self._danger:
            self.shell.say("没有待确认的破坏性操作", level="info")
            return
        line, self._danger = self._danger, None
        self._tick_status()
        self._run(line, confirm=True)

    def action_focus_cmd(self):
        self.query_one("#cmd", Input).focus()

    def action_focus_devices(self):
        self.query_one("#devices", DataTable).focus()

    def action_focus_caps(self):
        self.query_one("#caps", DataTable).focus()

    def action_history_prev(self):
        if not isinstance(self.focused, Input) or not self._hist:
            return
        self._hi = max(0, self._hi - 1)
        self.query_one("#cmd", Input).value = self._hist[self._hi]

    def action_history_next(self):
        if not isinstance(self.focused, Input) or not self._hist:
            return
        self._hi = min(len(self._hist), self._hi + 1)
        inp = self.query_one("#cmd", Input)
        inp.value = "" if self._hi >= len(self._hist) else self._hist[self._hi]


# ---------------------------------------------------------------- 门禁 / 出图

SMOKE_CMDS = ("devices", "caps", "pc.state", "transports")


async def _headless(run_cmds=(), wait=2.5):
    """起一个不占真终端的界面，可选跑几条命令。返回 (app, pilot)。"""
    app = FleetTUI()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(wait)
        for c in run_cmds:
            app._submit(c)
            await pilot.pause(1.2)
        return app


def _shot(path, size=(150, 40)):
    """headless 起界面、跑几条命令、把这一屏导出成 SVG（给人看它长什么样）。"""

    async def go():
        app = FleetTUI()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(3.0)
            for c in ("devices", "pc.state"):
                app._submit(c)
                await pilot.pause(1.2)
            await pilot.pause(0.5)
            return app.export_screenshot()

    svg = asyncio.run(go())
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return 0


def _smoke():
    async def go():
        app = FleetTUI()
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause(3.0)
            for c in SMOKE_CMDS:
                app._submit(c)
                await pilot.pause(1.2)
            return (app.shell is not None, app._rendered,
                    app.query_one("#caps", DataTable).row_count,
                    app.query_one("#devices", DataTable).row_count)

    ok, rendered, caps, devs = asyncio.run(go())
    fails = []
    if not ok:
        fails.append("shell 没起来")
    if rendered < len(SMOKE_CMDS):
        fails.append(f"{len(SMOKE_CMDS)} 条命令只渲染出 {rendered} 块")
    if caps == 0:
        fails.append("能力表是空的")
    if devs == 0:
        fails.append("设备表是空的")
    print(f"smoke: 渲染 {rendered} 块 / 能力 {caps} 项 / 设备 {devs} 台")
    for f in fails:
        print(f"  ✗ {f}")
    return 1 if fails else 0


def _safe_stdout():
    """让中文输出在任何 Windows 控制台上都不会炸。

    CI 的 windows runner 控制台是 cp1252，print 一句「渲染 4 块」会当场抛
    UnicodeEncodeError（charmap cannot encode），门禁就红了——而本机是 UTF-8，
    本地跑永远绿，只有推上去才暴露。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:          # 不是 TextIOWrapper（比如被 Textual 接管了）
            pass


def main(argv=None):
    _safe_stdout()
    ap = argparse.ArgumentParser(prog="fleet-tui")
    ap.add_argument("--interval", type=float, default=20.0, help="设备事件探测间隔秒")
    ap.add_argument("--shot", default="", help="不进交互：把界面导出成 SVG（给人看长什么样）")
    ap.add_argument("--smoke", action="store_true", help="不进交互：headless 跑一遍自检")
    a = ap.parse_args(argv)
    if a.shot:
        return _shot(a.shot)
    if a.smoke:
        return _smoke()
    FleetTUI(interval=a.interval).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
