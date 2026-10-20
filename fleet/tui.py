"""全屏终端界面（Textual）：仪表盘形态。

人在这一屏上看见并调用全部设备能力——包括刚接进来的联网层。

跟命令行版（`shell.py`）不是两套命令，是同一套的两个显影面：
  命令行版 —— 单行 REPL，给它 `emit` 就是它；
  全屏版   —— 给它 `emit` 换成写进「输出」页。

加一个能力仍只改注册表一处，两边同时出现。

线程分工（这一条不能让步）：
  耗时的事（探测设备、跑联网作业）一律丢进 worker 线程；
  主线程只负责把结果写进界面。
  worker 里拿到的东西不许直接碰 widget，一律经消息队列中转
  （worker 只攒进队列 + `call_from_thread`，主线程负责写进界面）。
  联网作业是唯一会跑几分钟的活，它的每一行都经 `_on_job_line` 实时回流，
  界面上才有真进度条和收获流，而不是一句干巴巴的「在跑」。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (DataTable, Footer, Header, Input,
                             ProgressBar, RichLog, Static, TabbedContent, TabPane)

from .shell import BUILTINS, FleetShell, _status_bits

TITLE_LINE = "设备编队 · 天网"

# 长任务进度契约：认不出格式就退化成"只知道还活着"，不硬猜
RE_PROG = re.compile(r"\((\d+)\s*/\s*(\d+)\)")
# 论文爬虫不走 (n/total)，它每行报一个源：12 个 arXiv 类 + HF + 2 个 OpenAlex + 收尾三行
RE_STEP = re.compile(r"^\[[\w\-:.]+\]")
STEPS_TOTAL = {"web.papers": 17}

CSS = """
Screen {
    layout: vertical;
    background: #0d1117;
    color: #c9d1d9;
}
#status {
    height: 1;
    width: 100%;
    padding: 0 1;
    background: #161b22;
    color: #c9d1d9;
}
#progwrap {
    height: 3;
    width: 100%;
    padding: 0 1;
    border: round #30363d;
    background: #0d1117;
}
#jobline { height: 1; }
ProgressBar { height: 1; }
#body { height: 1fr; }
TabbedContent { height: 100%; }
TabPane { padding: 0; }
#pane_dev, #pane_web { layout: horizontal; }
#devices { width: 34; }
#caps { width: 1fr; }
#sites { width: 1fr; }
#papers { width: 1fr; }
DataTable { height: 100%; background: #0d1117; }
RichLog { height: 100%; background: #0d1117; }
#cmd { border: round #58a6ff; background: #161b22; }
Footer { background: #161b22; }
Header { background: #161b22; }
"""


class FleetTUI(App):
    TITLE = TITLE_LINE
    SUB_TITLE = "人在这一屏上看见并调用全部设备"

    CSS = CSS

    BINDINGS = [
        ("ctrl+e", "focus_cmd", "命令行"),
        ("ctrl+d", "focus_devices", "设备"),
        ("ctrl+w", "focus_web", "联网"),
        ("ctrl+r", "refresh", "重新探测"),
        ("ctrl+c", "stop_job", "叫停"),
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
        self._stream = []           # 联网作业吐出的行（待主线程取走）
        self._hist = []
        self._hi = 0
        self._danger = None         # 待确认的破坏性命令（ctrl+y 才放行）
        self._dev_rows = []
        self._cap_rows = []
        self._rendered = 0          # 真正写进界面的块数（门禁里可断言）
        # 当前在跑的联网作业：进度靠它算，叫停也靠它
        self._job_cap = ""
        self._job_done = 0
        self._job_total = 0
        self._job_steps = 0
        self._job_t0 = 0.0
        self._job_minutes = 0.0
        self._job_tick = None
        self._results = []          # 「结果」页的历史

    # ---------------- 组装 ----------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("正在探测设备…", id="status")
        with Vertical(id="progwrap"):
            yield Static("", id="jobline")
            yield ProgressBar(id="prog", total=100, show_eta=False)
        with TabbedContent(initial="pane_dev"):
            with TabPane("设备", id="pane_dev"):
                with Horizontal():
                    yield DataTable(id="devices", zebra_stripes=True)
                    yield DataTable(id="caps", zebra_stripes=True)
            with TabPane("联网", id="pane_web"):
                with Horizontal():
                    yield DataTable(id="sites", zebra_stripes=True)
                    yield DataTable(id="papers", zebra_stripes=True)
            with TabPane("输出", id="pane_log"):
                yield RichLog(id="log", wrap=True, highlight=False, markup=False)
            with TabPane("结果", id="pane_res"):
                yield DataTable(id="results", zebra_stripes=True)
        yield Input(placeholder="能力名 [--device 设备] [--参数 值]　（ctrl+e 回到这里，右表选中回车即提到这里）",
                    id="cmd")
        yield Footer()

    def on_mount(self):
        self.query_one("#progwrap", Vertical).display = False
        self._boot()
        self.set_interval(5.0, self._tick_status)

    # ---------------- 启动：探测在线程里，UI 不卡 ----------------

    # group 必须分开写：Textual 的 exclusive 是「取消同组的其它活」，
    # 而 group 默认是 "default"——不写就是全挤在一组里，后起的会把先起的杀掉。
    # 实测后果：设备表整个是空的，且没有任何报错。
    @work(thread=True, exclusive=True, group="boot")
    def _boot(self):
        self.shell = FleetShell(interval=self.interval, watch=True, emit=self._emit)
        self.shell.blocking_confirm = False        # TUI 不许在事件循环里 input()
        self.shell.line_hook = self._on_job_line    # 长任务逐行回流
        self.shell.result_hook = self._on_result    # 结果攒进「结果」页
        self.call_from_thread(self._after_boot)

    def _after_boot(self):
        self._paint_caps()
        self._refresh()
        self.query_one("#cmd", Input).focus()

    # ---------------- 输出：一切渲染物都经此口 ----------------

    def _emit(self, renderable):
        self._outbox.append(renderable)
        self.call_from_thread(self._drain_outbox)

    def _drain_outbox(self):
        """主线程：把攒下的渲染物写进输出页。"""
        if not self._outbox:
            return
        log = self.query_one("#log", RichLog)
        for r in self._outbox:
            log.write(r)
        self._rendered += len(self._outbox)
        self._outbox.clear()

    # ---- 长任务的两条回流 ----
    # 这两条都在 worker 线程里被调用。从别的线程往 Textual 里递东西，
    # 官方给的是 call_from_thread，不是 post_message：实测在这个界面上，
    # 只要长任务还在跑，worker 线程里每递一次 post_message，主线程的事件循环
    # 就会整个卡住（定时器不响、界面冻住，且没有任何报错）。
    # 用 call_from_thread 就没有这问题——它是事件循环自己的跨线程入口。

    def _on_job_line(self, line):
        self._stream.append(str(line))
        self.call_from_thread(self._drain_stream)

    def _on_result(self, res):
        self._results.append((time.time(), res))
        self._results = self._results[-60:]
        self.call_from_thread(self._paint_results)

    def _drain_stream(self):
        """主线程：把联网作业吐出的行写进输出页，并据此推进度。"""
        if not self._stream:
            return
        log = self.query_one("#log", RichLog)
        lines = self._stream
        self._stream = []
        for ln in lines:
            log.write(Text(ln, style="dim"))
            self._absorb(ln)
        self._rendered += len(lines)
        self._show_job()

    def _absorb(self, line):
        """从一行里认领进度。认不出就只记步数，绝不假装有精确进度。"""
        m = RE_PROG.search(line)
        if m:
            self._job_done, self._job_total = int(m.group(1)), int(m.group(2))
            return
        if RE_STEP.match(line):
            self._job_steps += 1

    def _progress(self):
        """0~1 或 None（None = 只知道在跑，画成来回动的条）。"""
        if self._job_total > 0:
            return min(1.0, self._job_done / float(self._job_total))
        if self._job_minutes > 0:
            # 没有 (n/total) 的活（资源发现）用时间当进度：它本来就没有终点
            return min(1.0, (time.time() - self._job_t0) / (self._job_minutes * 60))
        tot = STEPS_TOTAL.get(self._job_cap)
        if tot:
            return min(1.0, self._job_steps / float(tot))
        return None

    def _show_job(self):
        """进度条：有精确进度就画实的，没有就画来回动的，不装懂。"""
        wrap = self.query_one("#progwrap", Vertical)
        if not self._job_cap:
            wrap.display = False
            return
        wrap.display = True
        p = self._progress()
        bar = self.query_one("#prog", ProgressBar)
        if p is None:
            bar.update(total=None)
        else:
            bar.update(total=100, progress=int(p * 100))
        txt = self._job_cap
        if self._job_total:
            txt += f"  {self._job_done}/{self._job_total}"
        elif self._job_steps:
            txt += f"  第 {self._job_steps} 步"
        if self._job_cap == "web.discover" and self._job_minutes:
            left = max(0, self._job_minutes * 60 - (time.time() - self._job_t0))
            txt += f"  还剩 {int(left)}s"
        self.query_one("#jobline", Static).update(Text(txt, style="bold #58a6ff"))

    def _job_tick_update(self):
        """限时作业没有别的进度可看，就让时间推着它走。"""
        if self._job_cap == "web.discover":
            self._show_job()

    # ---------------- 四块面板 ----------------

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh(self):
        """一次探测，画三张表（设备 + 站群 + 论文）。

        为什么不拆成「刷设备」和「刷联网」两个活：拆了它们会抢同一个默认组，
        后起的把先起的杀掉，结果是设备表永远是空的。它们本来就是同一件事——
        「把这一屏的数据重新取一遍」，那就该是一个活。
        """
        self.shell.reg.invalidate()
        rows = self.shell.reg.availability()
        gate = lambda *a: (True, "")     # 这两个是只读能力，不该过闸门
        sites = self.shell.reg.call("web.sites", args={}, gate=gate)
        papers = self.shell.reg.call("web.frontier", args={}, gate=gate)
        self.call_from_thread(self._paint_all, rows, sites, papers)
        self._tick_status()

    def _paint_all(self, rows, sites, papers):
        self._paint_devices(rows)
        self._paint_web(sites, papers)

    def _paint_devices(self, rows):
        t = self.query_one("#devices", DataTable)
        t.clear(columns=True)
        t.add_columns("设备", "类型", "状态")
        self._dev_rows = []
        for r in rows:
            self._dev_rows.append(r["name"])
            t.add_row(r["name"], r.get("kind", ""),
                      "● 在线" if r.get("ok") else "○ 离线")
        self._tick_status()

    def _paint_caps(self):
        t = self.query_one("#caps", DataTable)
        t.clear(columns=True)
        t.add_columns("能力", "说明", "性质")
        self._cap_rows = []
        for c in self.shell.reg.capabilities():
            self._cap_rows.append(c.name)
            kind = "破坏" if c.destructive else ("动手" if c.side_effect else "只读")
            t.add_row(c.name, c.title, kind)

    def _paint_web(self, sites, papers):
        t = self.query_one("#sites", DataTable)
        t.clear(columns=True)
        t.add_columns("站群", "站名", "大小", "目录页")
        if sites and sites.ok:
            for s in (sites.data.get("sites") or [])[:40]:
                t.add_row(s.get("code", ""), s.get("name", "")[:22],
                          f"{int(s.get('size', 0) // 1024)}KB", str(s.get("pages", 0)))
        else:
            t.add_row("—", (sites.err if sites else "联网层没装")[:22], "", "")

        p = self.query_one("#papers", DataTable)
        p.clear(columns=True)
        p.add_columns("来源", "分类", "标题")
        if papers and papers.ok:
            for i in (papers.data.get("items") or []):
                p.add_row(i.get("src", ""), i.get("cat", ""),
                          (i.get("title") or "")[:52])
        else:
            p.add_row("—", "", (papers.err if papers else "联网层没装")[:40])

    def _paint_results(self):
        t = self.query_one("#results", DataTable)
        t.clear(columns=True)
        t.add_columns("时间", "能力", "结果", "摘要")
        for ts, res in reversed(self._results[-40:]):
            flag = "✓" if res.ok else "✗"
            t.add_row(time.strftime("%H:%M:%S", time.localtime(ts)),
                      res.cap, flag, res.brief()[:60])

    def _tick_status(self):
        st = self.query_one("#status", Static)
        if not self.shell:
            return
        b = _status_bits(self.shell.reg)
        lamps = (("她", b["her"][0]), ("本机", True), ("手机", b["phone"][0]),
                 ("节点", any(b.get("online", 0) > 1 for _ in [0])),
                 ("网络", b["web"][0]))
        txt = Text()
        for i, (label, on) in enumerate(lamps):
            if i:
                txt.append("　")
            txt.append(f"{label} ", style="bold")
            txt.append("●" if on else "○",
                       style="bold #3fb950" if on else "#f85149")
        txt.append(f"　在线 {b['online']}/{b['devices']} 台　{b['caps']} 项能力",
                   style="#8b949e")
        if self._danger:
            txt.append("　⚠ 有待确认的破坏性操作（ctrl+y）", style="bold #f85149")
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
        # 联网作业才亮进度条：别的活都是一瞬间，画条出来反而晃眼
        head = (line.split() or [""])[0]
        self._job_cap = head if head.startswith("web.") else ""
        self._job_done = self._job_total = self._job_steps = 0
        self._job_t0 = time.time()
        self._job_minutes = 0.0
        if self._job_cap == "web.discover":
            m = re.search(r"--minutes\s+(\d+)", line)
            self._job_minutes = float(m.group(1)) if m else 3.0
            # 它自己不报进度，只能让时间推着条走
            self._job_tick = self.set_interval(0.5, self._job_tick_update)
        self.shell._confirm = confirm
        try:
            self.shell.dispatch(line)
        finally:
            self.shell._confirm = False
            self.call_from_thread(self._job_end)

    def _job_end(self):
        if self._job_tick:
            self._job_tick.stop()
            self._job_tick = None
        self._job_cap = ""
        self._job_done = self._job_total = self._job_steps = 0
        self._show_job()
        self._refresh()          # 收工后把新收获刷进那两张表
        self._tick_status()

    # ---------------- 选中即填 ----------------

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
        self._stop_job_now()
        self.exit()

    def _stop_job_now(self):
        for d in self.shell.reg.devices() if self.shell else ():
            stop = getattr(d, "stop", None)
            if callable(stop):
                stop()

    def action_stop_job(self):
        if self._stop_job_now():
            self.shell.say("已叫停联网作业", level="warn")
        else:
            self.shell.say("没有在跑的作业", level="info")

    def action_help(self):
        self.shell.dispatch("help")
        self.shell.say("右表选中回车即把能力名提到命令行；联网作业按 ctrl+c 叫停",
                       level="info")

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
        self.query_one("TabbedContent", TabbedContent).active = "pane_dev"
        self.query_one("#devices", DataTable).focus()

    def action_focus_web(self):
        self.query_one("TabbedContent", TabbedContent).active = "pane_web"
        self.query_one("#sites", DataTable).focus()

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

SMOKE_CMDS = ("devices", "caps", "pc.state", "web.status", "web.sites", "transports")


async def _headless(run_cmds=(), wait=2.5):
    """起一个不占真终端的界面，可选跑几条命令。"""
    app = FleetTUI()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(wait)
        for c in run_cmds:
            app._submit(c)
            await pilot.pause(1.2)
        return app


def _shot(path, size=(150, 40), web=False):
    """headless 起界面、跑几条命令、把这一屏导出成 SVG（给人看它长什么样）。"""

    async def go():
        app = FleetTUI()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(3.0)
            for c in ("devices", "pc.state", "web.status"):
                app._submit(c)
                await pilot.pause(1.2)
            # 停在哪一页就截哪一页：默认截设备页，指定 --shot-web 截联网页
            if web:
                app.query_one("TabbedContent", TabbedContent).active = "pane_web"
            await pilot.pause(0.8)
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
                    app.query_one("#devices", DataTable).row_count,
                    app.query_one("#sites", DataTable).row_count,
                    app.query_one("#papers", DataTable).row_count)

    ok, rendered, caps, devs, sites, papers = asyncio.run(go())
    fails = []
    if not ok:
        fails.append("shell 没起来")
    if rendered < len(SMOKE_CMDS):
        fails.append(f"{len(SMOKE_CMDS)} 条命令只渲染出 {rendered} 块")
    if caps == 0:
        fails.append("能力表是空的")
    if devs == 0:
        fails.append("设备表是空的")
    if sites == 0:
        fails.append("站群表是空的")
    if papers == 0:
        fails.append("论文表是空的")
    print(f"smoke: 渲染 {rendered} 块 / 能力 {caps} 项 / 设备 {devs} 台"
          f" / 站群 {sites} 行 / 论文 {papers} 行")
    for f in fails:
        print(f"  x {f}")
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
    ap.add_argument("--shot-web", action="store_true",
                    help="配合 --shot：截联网那一页（默认是设备页）")
    ap.add_argument("--smoke", action="store_true", help="不进交互：headless 跑一遍自检")
    a = ap.parse_args(argv)
    if a.shot:
        return _shot(a.shot, web=a.shot_web)
    if a.smoke:
        return _smoke()
    FleetTUI(interval=a.interval).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
