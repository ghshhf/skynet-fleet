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

  用户原话——"web 会耗时很长，所以如果一个屏不刷新，他就看不到收获"。
  因此回流受一个**直播开关**（默认开，ctrl+b 切换）管：开着，长任务逐行
  往外长；关着，界面不被刷屏、只在收工时报一句"共 N 行"。回流目标也分屏——
  总日志（输出页）永远收一份；论文的活额外喂给「论文」页、联网的活额外喂给
  「联网」页，这两屏才是长任务时人真正盯着的地方。

  标签页（顶部横排，仿网页）：设备 / 联网(netforce) / 论文 / AI / 主机 / 输出 / 结果。
  「论文」页上半行显示今日论文数与来源分布，下面是全屏 RichLog，web.papers
  跑起来这一屏就一行行长出来；「联网」页左侧站群表、右侧收获流；
  「AI」页顶部列可用免费后端、下方全屏对话流，提问框回车即向所有后端并发提问；
  「主机」页资源条(CPU/MEM/DISK/NET)+进程表+屏幕盲文预览（操控电脑的可视化）。
"""
from __future__ import annotations

import argparse
import asyncio
import re
import shlex
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
#pane_paper { layout: vertical; }
#pane_ai { layout: vertical; }
#ailine { height: 1; padding: 0 1; color: #8b949e; background: #161b22; }
#ailog { height: 1fr; }
#pane_host { layout: vertical; }
#hostbars { height: 1; padding: 0 1; color: #c9d1d9; background: #161b22; }
#hostprocs { height: 12; }
#hostshot { height: 1fr; }
#devices { width: 34; }
#caps { width: 1fr; }
#sites { width: 56; }
#weblog { width: 1fr; }
#paperline { height: 1; padding: 0 1; color: #8b949e; background: #161b22; }
#papers { height: 8; }
#paperlog { height: 1fr; }
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
        ("ctrl+p", "focus_papers", "论文"),
        ("ctrl+a", "focus_ai", "AI"),
        ("ctrl+h", "focus_host", "主机"),
        ("ctrl+b", "toggle_live", "直播"),
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
        self._live = True           # 直播开关：作业每吐一行就长一行（默认开）
        # 当前在跑的联网作业：进度靠它算，叫停也靠它
        self._job_cap = ""
        self._job_done = 0
        self._job_total = 0
        self._job_steps = 0
        self._job_lines = 0
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
                    yield RichLog(id="weblog", wrap=True,
                                  highlight=False, markup=False)
            with TabPane("论文", id="pane_paper"):
                with Vertical():
                    yield Static("论文页：跑 web.papers 后这一屏会逐行长出来",
                                 id="paperline")
                    yield DataTable(id="papers", zebra_stripes=True)
                    yield RichLog(id="paperlog", wrap=True,
                                  highlight=False, markup=False)
            with TabPane("AI", id="pane_ai"):
                with Vertical():
                    yield Static("AI 页：在下方输入框提问，回车即向所有免费后端并发提问",
                                 id="ailine")
                    yield RichLog(id="ailog", wrap=True,
                                  highlight=False, markup=False)
                    yield Input(placeholder="问 AI（回车即向所有免费后端并发提问；多后端按 [id] 同屏）",
                                id="aiprompt")
            with TabPane("主机", id="pane_host"):
                with Vertical():
                    yield Static("主机页：跑 host.monitor 看资源、host.screen 看屏（盲文预览）",
                                 id="hostbars")
                    yield DataTable(id="hostprocs", zebra_stripes=True)
                    yield RichLog(id="hostshot", wrap=True,
                                  highlight=False, markup=False)
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
        """worker 线程：作业吐一行。

        直播开着重在"人能看见东西长出来"——每行都往界面递一次；
        关着就一行都不递（界面不刷新），只把行数攒着，收工时一并汇报。
        """
        if not self._live:
            self._job_lines += 1
            return
        self._stream.append(str(line))
        self.call_from_thread(self._drain_stream)

    def _on_result(self, res):
        self._results.append((time.time(), res))
        self._results = self._results[-60:]
        self.call_from_thread(self._paint_results)
        if res.cap.startswith("host."):
            self.call_from_thread(self._paint_host_latest)

    def _drain_stream(self):
        """主线程：把作业吐出的行喂给该去的那一屏，并据此推进度。

        总日志（输出页）永远收一份，免得切走之后什么都看不见；
        论文的活额外喂给论文页、联网的活额外喂给联网页——
        这两屏才是长任务时人真正盯着的地方。
        """
        if not self._stream:
            return
        lines = self._stream
        self._stream = []
        log = self.query_one("#log", RichLog)
        extra = self._stream_target()
        for ln in lines:
            log.write(Text(ln, style="dim"))
            if extra is not None:
                extra.write(Text(ln, style="dim"))
            self._absorb(ln)
            self._job_lines += 1
        self._rendered += len(lines)
        self._show_job()

    def _stream_target(self):
        """这一趟作业的收获该落在哪一屏。"""
        if self._job_cap == "web.papers":
            return self.query_one("#paperlog", RichLog)
        if self._job_cap.startswith("web."):
            return self.query_one("#weblog", RichLog)
        if self._job_cap.startswith("ai."):
            return self.query_one("#ailog", RichLog)
        return None

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
        self._paint_ai()

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
        self._paint_paper_head(papers)

    def _paint_paper_head(self, papers):
        """论文页顶上那一句：今天攒了多少、从哪来的、现在有没有在抓。"""
        head = Text()
        if papers and papers.ok:
            items = papers.data.get("items") or []
            seen = []
            for i in items:
                s = i.get("src") or ""
                if s and s not in seen:
                    seen.append(s)
            head.append(f"论文 {papers.data.get('day', '')}　"
                        f"共 {papers.data.get('count', 0)} 篇",
                        style="bold #58a6ff")
            head.append(f"　来源 {'/'.join(seen) or '—'}", style="#8b949e")
        else:
            head.append("论文", style="bold #58a6ff")
            head.append(f"　{(papers.err if papers else '联网层没装')[:44]}",
                        style="#8b949e")
        if self._job_cap == "web.papers":
            head.append(f"　抓取中：已出 {self._job_lines} 行", style="bold #d29922")
        self.query_one("#paperline", Static).update(head)

    def _paint_ai(self):
        """AI 页顶上那一句：现在有哪些免费后端可用。"""
        t = self.query_one("#ailine", Static)
        try:
            res = self.shell.reg.call("ai.list")
        except Exception as e:
            t.update(Text(f"AI 后端读取失败：{e}", style="#f85149"))
            return
        if not res.ok:
            t.update(Text(f"AI：{res.err}", style="#f85149"))
            return
        bs = res.data.get("backends", [])
        if not bs:
            t.update(Text("AI：无可用后端（联网用 pollinations 免key，或设 FLEET_LLM_URL）",
                          style="#8b949e"))
            return
        ids = " / ".join(b["id"] for b in bs)
        t.update(Text(f"AI 后端 {len(bs)} 个：{ids}　（下方输入框提问，回车即问所有后端）",
                      style="bold #58a6ff"))

    @staticmethod
    def _bar(pct, width=14):
        pct = max(0.0, min(100.0, float(pct)))
        filled = int(round(pct / 100.0 * width))
        return "\u2588" * filled + "\u2591" * (width - filled)

    def _paint_host_latest(self):
        """结果页里最新的 host.* 结果，画进主机页。"""
        for _ts, res in reversed(self._results):
            if res.cap.startswith("host."):
                self._paint_host(res)
                return

    def _paint_host(self, res):
        """主机页可视化：资源条 + 进程表 + 屏幕盲文预览。"""
        if res.cap == "host.monitor" and res.ok:
            d = res.data
            b = Text()
            for label, key, color in (("CPU", "cpu", "#58a6ff"),
                                      ("MEM", "mem", "#3fb950"),
                                      ("DISK", "disk", "#d29922")):
                b.append(f"{label} ", style="bold")
                b.append(self._bar(d.get(key, 0)) + f" {d.get(key, 0):.0f}%  ",
                         style=color)
            b.append(f"\u2191{d.get('net_up', 0)} ", style="#8b949e")
            b.append(f"\u2193{d.get('net_down', 0)} KB/s", style="#8b949e")
            self.query_one("#hostbars", Static).update(b)
            t = self.query_one("#hostprocs", DataTable)
            t.clear(columns=True)
            t.add_columns("PID", "进程", "CPU%")
            for p in (d.get("procs") or [])[:12]:
                t.add_row(str(p.get("pid", "")), (p.get("name") or "")[:22],
                          str(p.get("cpu", "")))
        elif res.cap == "host.apps" and res.ok:
            t = self.query_one("#hostprocs", DataTable)
            t.clear(columns=True)
            t.add_columns("PID", "进程")
            for p in (res.data.get("apps") or [])[:40]:
                t.add_row(str(p.get("pid", "")), (p.get("name") or "")[:30])
            self.query_one("#hostbars", Static).update(
                Text(f"本机进程 {len(res.data.get('apps', []))} 个", style="#58a6ff"))
        elif res.cap == "host.screen" and res.ok:
            rl = self.query_one("#hostshot", RichLog)
            rl.clear()
            prev = res.data.get("preview") or ""
            if prev:
                rl.write(Text(prev, style="#c9d1d9"))
            rl.write(Text("截图：" + res.data.get("path", ""), style="#8b949e"))

    def _paint_results(self):
        t = self.query_one("#results", DataTable)
        t.clear(columns=True)
        t.add_columns("时间", "能力", "结果", "摘要")
        for ts, res in reversed(self._results[-40:]):
            flag = "✓" if res.ok else "✗"
            t.add_row(time.strftime("%H:%M:%S", time.localtime(ts)),
                      res.cap, flag, res.brief()[:60])

    def _tick_status(self):
        # 🔴 用 query（可空）而不是 query_one：worker 线程的收工回调可能早于界面
        # 挂载完成跑进来（真踩过：dispatch 抛错 → finally → _job_end → 这里
        # query_one("#status") 抛 NoMatches，把真正的错误盖掉了）。
        # 状态灯是锦上添花，界面还没就绪时该跳过，不该炸。
        # DOMQuery.first() 在无匹配时照样抛 NoMatches（实测），所以走 .nodes 列表。
        nodes = self.query("#status").nodes
        st = nodes[0] if nodes else None
        if st is None:
            return
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
        txt.append("　直播", style="bold")
        txt.append("开" if self._live else "关",
                   style="bold #3fb950" if self._live else "#8b949e")
        if self._danger:
            txt.append("　⚠ 有待确认的破坏性操作（ctrl+y）", style="bold #f85149")
        st.update(txt)

    # ---------------- 执行 ----------------

    def on_input_submitted(self, ev):
        # AI 页的提问框：把输入转成 ai.chat，复用同一套流式管道
        if ev.input.id == "aiprompt":
            prompt = (ev.value or "").strip()
            ev.input.value = ""
            if not prompt:
                return
            self._hist.append(prompt)
            self._hi = len(self._hist)
            self._submit(f"ai.chat --prompt {shlex.quote(prompt)}")
            return
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
        self._job_cap = head if (head.startswith("web.") or head.startswith("ai.")
                                 or head.startswith("host.")) else ""
        self._job_done = self._job_total = self._job_steps = 0
        self._job_lines = 0
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
        cap = self._job_cap
        self._job_cap = ""
        self._job_done = self._job_total = self._job_steps = 0
        self._show_job()
        if cap:
            self._job_tail(cap)
        self._refresh()          # 收工后把新收获刷进那两张表
        self._tick_status()

    def _job_tail(self, cap):
        """收工那一句。直播关着的时候，这是这趟活唯一让人看见的东西。"""
        txt = Text(f"── {cap} 收工：共 {self._job_lines} 行"
                   + ("" if self._live else "（直播关着，中途没逐行刷新）"),
                   style="bold #3fb950")
        self.query_one("#log", RichLog).write(txt)
        if cap == "web.papers":
            self.query_one("#paperlog", RichLog).write(txt)
        elif cap.startswith("web."):
            self.query_one("#weblog", RichLog).write(txt)
        elif cap.startswith("ai."):
            self.query_one("#ailog", RichLog).write(txt)

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
        self.shell.say("右表选中回车即把能力名提到命令行；ctrl+p 进论文页，"
                       "ctrl+w 进联网页，ctrl+b 开关直播，联网作业按 ctrl+c 叫停",
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

    def action_focus_papers(self):
        self.query_one("TabbedContent", TabbedContent).active = "pane_paper"
        self.query_one("#paperlog", RichLog).focus()

    def action_focus_ai(self):
        self.query_one("TabbedContent", TabbedContent).active = "pane_ai"
        self.query_one("#aiprompt", Input).focus()

    def action_focus_host(self):
        self.query_one("TabbedContent", TabbedContent).active = "pane_host"
        self.query_one("#hostprocs", DataTable).focus()

    def action_toggle_live(self):
        """直播开关：长任务要不要逐行往外长。

        开着（默认）—— 联网的活一跑就是几分钟到几小时，屏幕不刷新
        人就等于没看见收获，不知道它是在干活还是已经死了。
        关着 —— 界面不再被刷屏，只在收工时汇报一句（含总行数）。
        """
        self._live = not self._live
        self.shell.say(f"直播已{'开' if self._live else '关'}："
                       + ("长任务逐行长出来" if self._live
                          else "长任务只在收工时汇报一句"), level="info")
        self._tick_status()

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

SMOKE_CMDS = ("devices", "caps", "pc.state", "web.status", "web.sites",
              "ai.list", "transports")


async def _await_render(pilot, app, before, deadline=20.0):
    """等这条命令的输出**真的渲染出来**，而不是「睡够固定时长」。

    🔴 **为什么不能固定 pause**（实测踩过）：
        原来每条命令后固定 `pilot.pause(1.2)`，靠「睡够 1.2 秒这命令肯定跑完了」
        这个假设。能力一多（加了 audio 那批），有的命令就睡不够 ⇒
        那一轮少渲染一块 ⇒ 门禁**随机红**（实测 5 轮红 1 轮，
        报 "7 条命令只渲染出 6 块"）。
        这种 flaky 门禁比没有门禁更坏：它会让所有人学会忽略红灯。

    ⇒ 现在改成**轮询到 _rendered 增长为止**，并留一个上限兜底（免得真卡死时无限等）。
    返回实际等了多久（秒），调用方可查。
    """
    waited = 0.0
    step = 0.05
    while app._rendered <= before and waited < deadline:
        await pilot.pause(step)
        waited += step
    return waited


async def _headless(run_cmds=(), wait=2.5):
    """起一个不占真终端的界面，可选跑几条命令。"""
    app = FleetTUI()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(wait)
        for c in run_cmds:
            before = app._rendered
            app._submit(c)
            await _await_render(pilot, app, before)
        return app


PANES = ("pane_dev", "pane_web", "pane_paper", "pane_ai", "pane_host", "pane_log", "pane_res")


def _shot(path, size=(150, 40), pane="pane_dev"):
    """headless 起界面、跑几条命令、把这一屏导出成 SVG（给人看它长什么样）。"""

    async def go():
        app = FleetTUI()
        async with app.run_test(size=size) as pilot:
            await pilot.pause(3.0)
            # 每页都要有真东西在画面上：设备页看清单，host.* 喂「主机」页
            # （资源条/进程表/盲文预览），web.status 喂「联网」页。
            for c in ("devices", "pc.state", "web.status",
                      "host.monitor", "host.apps", "host.screen"):
                before = app._rendered
                app._submit(c)
                await _await_render(pilot, app, before)
            # 停在哪一页就截哪一页：默认截设备页
            app.query_one("TabbedContent", TabbedContent).active = pane
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
                # 🔴 **等到位，不要等固定时长**（理由见 `_await_render` 的注释）。
                before = app._rendered
                app._submit(c)
                await _await_render(pilot, app, before)
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
                    help="配合 --shot：截联网那一页")
    ap.add_argument("--shot-pane", default="pane_dev", choices=PANES,
                    help="配合 --shot：停在哪一页截图")
    ap.add_argument("--smoke", action="store_true", help="不进交互：headless 跑一遍自检")
    a = ap.parse_args(argv)
    if a.shot:
        return _shot(a.shot, pane=("pane_web" if a.shot_web else a.shot_pane))
    if a.smoke:
        return _smoke()
    FleetTUI(interval=a.interval).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
