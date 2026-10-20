"""T0 终端：人在这里看见并调用一切能力。

为什么终端要先立（而不是先做 MCP）：MCP 是给「别的机器」用的口，终端是给「你自己」
用的面。顺序反了的话，我们会先花一轮把接口做漂亮，却依然只能靠手敲 REPL 验证它。

它只做三件事，但这三件必须对：
  1. 命令与补全**全部来自注册表**——注册表里没有的能力，这里补不出来；
  2. 她/事件开口时**不打断正在输入的那一行**（patch_stdout，不是裸 print）；
  3. 底部状态栏常驻：她在不在 / 手机在不在 / 几台在线。

用 prompt_toolkit + rich，不引 Node/React（那是给一个终端装外国关节）。
"她在不在"读的是控制面通道——她住在控制面里，控制面不在，她就不在。
"""
import argparse
import difflib
import os
import re
import sys
import threading
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from .catalog import build
from .core import result as R
from .transports import get as get_transport

HISTORY_PATH = os.environ.get("FLEET_SHELL_HISTORY", os.path.join(
    os.path.expanduser("~"), ".fleet", "shell_history"))

BUILTINS = ("help", "caps", "devices", "transports", "refresh", "save", "quit")


# ---------------------------------------------------------------- 补全

class FleetCompleter(Completer):
    """候选只来自注册表：能力名、设备名、该能力的参数名。"""

    def __init__(self, reg):
        self.reg = reg

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        tokens = text.split()
        last = tokens[-1] if tokens else ""
        prev = tokens[-2] if len(tokens) >= 2 else ""
        stripped = text.endswith(" ") or not tokens

        # 第一个词：能力 + 内置命令
        if len(tokens) <= 1 and not (stripped and len(tokens) == 1):
            word = "" if stripped else last
            for name in sorted(set(self.reg.cap_names()) | set(BUILTINS)):
                if name.startswith(word):
                    meta = "命令" if name in BUILTINS else "能力"
                    yield Completion(name, start_position=-len(word), display_meta=meta)
            return

        cap = self.reg.capability(tokens[0])
        # --device 之后补设备名（"cap --device " 与 "cap --device ph" 两种都要认）
        dev_pos = prev == "--device" or (last == "--device" and stripped)
        if dev_pos:
            word = last if prev == "--device" else ""
            for d in self.reg.devices():
                if d.name.startswith(word):
                    yield Completion(d.name, start_position=-len(word), display_meta=d.kind)
            return
        if stripped or last.startswith("-"):
            word = "" if stripped else last
            opts = ["--device", "--args", "--yes"]
            if cap is not None:
                opts += [f"--{p.name}" for p in cap.params]
            for o in opts:
                if o.startswith(word):
                    yield Completion(o, start_position=-len(word), display_meta="参数")


# ---------------------------------------------------------------- 状态栏

def _status_bits(reg):
    """三个数字：她在不在（控制面）/ 手机在不在（adb）/ 几台设备在线。"""
    ctl = get_transport("control")
    her_ok, her_reason = ctl.available() if ctl else (False, "没有 control 通道")
    adb = get_transport("adb")
    phone_ok, phone_reason = adb.available() if adb else (False, "没有 adb 通道")
    st = reg.stats()
    return {"her": (her_ok, her_reason), "phone": (phone_ok, phone_reason),
            "online": st["online"], "devices": st["devices"], "caps": st["caps"]}


def _toolbar(reg):
    b = _status_bits(reg)
    her = ("她 ✓", "class:status.ok") if b["her"][0] else ("她 ✗", "class:status.bad")
    ph = ("手机 ✓", "class:status.ok") if b["phone"][0] else ("手机 ✗", "class:status.bad")
    return FormattedText([
        ("class:status", " "),
        (her[1], her[0]),
        ("class:status", "  ｜  "),
        (ph[1], ph[0]),
        ("class:status", f"  ｜  在线 {b['online']}/{b['devices']} 台"),
        ("class:status", f"  ｜  {b['caps']} 项能力  "),
        ("class:status.hint", "  Enter 执行 / Alt+Enter 换行 / Tab 补全 / help"),
    ])


# ---------------------------------------------------------------- 事件流

class Watcher(threading.Thread):
    """设备上线/掉线就开口。这是 D3 事件总线的雏形——先让它只在终端里响。"""

    def __init__(self, shell, interval=15.0):
        super().__init__(daemon=True, name="fleet-watch")
        self.shell = shell
        self.interval = interval
        self._stop = threading.Event()
        self._prev = {}

    def stop(self):
        self._stop.set()

    def snapshot(self):
        return {r["name"]: (r["ok"], r["reason"]) for r in self.shell.reg.availability(ttl=0)}

    def run(self):
        self._prev = self.snapshot()
        while not self._stop.wait(self.interval):
            cur = self.snapshot()
            for name, (ok, reason) in cur.items():
                old = self._prev.get(name)
                if old is None:
                    continue
                if old[0] != ok:
                    if ok:
                        self.shell.say(f"设备上线：{name}", level="event")
                    else:
                        self.shell.say(f"设备掉线：{name}（{reason}）", level="warn")
            self._prev = cur


# ---------------------------------------------------------------- 终端

class FleetShell:
    def __init__(self, reg=None, interval=15.0, watch=True, console=None):
        self.reg = reg if reg is not None else build(gate=self.gate)
        if reg is None:
            self.reg.gate = self.gate
        self.console = console or Console()
        self.interval = interval
        self.watch = watch
        self.watcher = None
        self.session = None
        self._pending = []          # 输入行之前的「她的话」缓冲（测试里可断言）
        self.lines = []             # 实际提交的每一行（回放与验收用）

    # ---- 闸门：只读直接过，动手告知，破坏性必须手打 yes ----

    def gate(self, cap, dev, args, level):
        if level >= 2:
            ans = input(f"  ⚠ {cap.name} @ {dev.name} 不可逆，输入 yes 继续: ").strip().lower()
            return (ans == "yes", "主人没确认")
        self.say(f"动手：{cap.name} @ {dev.name}", level="action")
        return True, ""

    # ---- 安全输出：她开口 / 事件 / 结果都走这里，绝不裸 print ----

    def say(self, text, level="info", title=""):
        style = {"info": "white", "event": "cyan", "warn": "yellow",
                 "error": "red", "action": "magenta"}.get(level, "white")
        if title:
            self.console.print(Panel(Text(text, style=style), title=title,
                                     border_style=style, expand=False))
        else:
            self.console.print(Text(text, style=style))
        self._pending.append(text)

    # ---- 渲染 ----

    def show_devices(self):
        rows = self.reg.availability()
        t = Table(title=f"设备 {len(rows)} 台（在线 {sum(1 for r in rows if r['ok'])}）",
                  show_lines=False, header_style="bold")
        t.add_column("设备", style="bold")
        t.add_column("种类")
        t.add_column("通道")
        t.add_column("状态")
        t.add_column("能力", justify="right")
        t.add_column("原因 / 备注", overflow="fold", min_width=28)
        for r in rows:
            t.add_row(r["name"], r["kind"], r["via"],
                      Text("在线", style="green") if r["ok"] else Text("不可用", style="red"),
                      str(len(r["caps"])), "" if r["ok"] else r["reason"])
        self.console.print(t)

    def show_caps(self, word=""):
        caps = self._pick_caps(word)
        title = f"能力 {len(caps)} 项" + (f"（{word}）" if word else "")
        t = Table(title=title, header_style="bold")
        t.add_column("能力", style="bold")
        t.add_column("说明")
        t.add_column("性质")
        t.add_column("状态")
        t.add_column("设备 / 原因", overflow="fold", min_width=24)
        for c in caps:
            ok, reason, dev = self.reg.check(c.name)
            kind = "破坏" if c.destructive else ("动手" if c.side_effect else "只读")
            t.add_row(c.name, c.title, kind,
                      Text("可用", style="green") if ok else Text("不可用", style="red"),
                      dev.name if ok and dev else ("" if ok else _short_reason(reason)))
        self.console.print(t)
        if word and not caps:
            self.console.print(Text(f"没有匹配「{word}」的设备或能力族。"
                                    f"可用族：{', '.join(sorted({c.name.split('.')[0] for c in self.reg.capabilities()}))}",
                                    style="yellow"))

    def _pick_caps(self, word):
        """筛选词既可以是设备（phone / pc:local），也可以是能力族（input / net）。"""
        caps = self.reg.capabilities()
        if not word:
            return caps
        devs = [d for d in self.reg.devices()
                if word.lower() in d.name.lower() or word.lower() in d.kind.lower()]
        if devs:
            names = set()
            for d in devs:
                names |= set(d.caps())
            return [c for c in caps if c.name in names]
        return [c for c in caps if c.name.split(".", 1)[0] == word.lower()]

    def show_transports(self):
        from .transports import probe_all
        t = Table(title="通道", header_style="bold")
        for col in ("通道", "状态", "说明", "原因"):
            t.add_column(col, overflow="fold")
        for r in probe_all():
            t.add_row(r["name"],
                      Text("通", style="green") if r["ok"] else Text("不通", style="red"),
                      r["title"], "" if r["ok"] else r["reason"])
        self.console.print(t)

    def show_result(self, res):
        if res.ok:
            body = Text()
            for k, v in (res.data or {}).items():
                body.append(f"{k}: ", style="bold")
                body.append(f"{_oneline(v)}\n")
            self.console.print(Panel(body, title=f"✓ {res.cap} @ {res.device} "
                                                f"（{res.ms:.0f}ms / {res.via}）",
                                     border_style="green", expand=False))
        else:
            self.console.print(Panel(Text(f"{res.code}: {res.err}"),
                                     title=f"✗ {res.cap}", border_style="red", expand=False))
        self._pending.append(res.brief())

    # ---- 分发 ----

    def parse_args(self, cap, tokens):
        """--k v 转成 args。类型按注册表 schema 转——终端不该猜 int 还是 str。"""
        schema = {p.name: p for p in (cap.params if cap else ())}
        args, i = {}, 0
        while i < len(tokens):
            tok = tokens[i]
            if not tok.startswith("--"):
                i += 1
                continue
            key = tok[2:]
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            p = schema.get(key)
            if key in ("device", "args", "yes"):
                args[key] = val if key != "yes" else True
                i += 2
                continue
            if p is not None:
                if p.type == "int":
                    val = int(val) if str(val).lstrip("-").isdigit() else 0
                elif p.type == "bool":
                    val = str(val).lower() not in ("0", "false", "no", "")
            if val == "" and tok.startswith("--"):
                val = True
            args[key] = val
            i += 2
        return args

    def dispatch(self, line):
        line = (line or "").strip()
        if not line:
            return True
        tokens = line.split()
        head = tokens[0]

        if head in ("quit", "exit", "q"):
            return False
        if head in ("help", "?", "h"):
            self.console.print(Text(__doc__ or "", style="dim"))
            self.console.print(Text(
                "内置：" + " / ".join(BUILTINS) + "\n"
                "任意能力：<cap> [--device 设备] [--参数 值 ...]\n"
                "例：pc.state / screen.read --device phone:main / "
                "input.tap --text 确定 / caps phone", style="cyan"))
            return True
        if head == "devices":
            self.show_devices()
            return True
        if head == "caps":
            self.show_caps(tokens[1] if len(tokens) > 1 else "")
            return True
        if head == "transports":
            self.show_transports()
            return True
        if head == "refresh":
            self.reg.invalidate()
            self.say("已重新探测（缓存清空）", level="info")
            return True
        if head == "save":
            self.say(f"已落盘：{self.reg.save() or '（不可写）'}", level="info")
            return True

        cap = self.reg.capability(head)
        if cap is None:
            near = difflib.get_close_matches(head, self.reg.cap_names() + list(BUILTINS), 3)
            self.say(f"没有这个能力：{head}" + (f"（相近：{', '.join(near)}）" if near else
                                              "（敲 help 看怎么用）"), level="error")
            return True

        kw = self.parse_args(cap, tokens[1:])
        device = kw.pop("device", "") or ""
        kw.pop("yes", None)
        res = self.reg.call(head, device=device, args=kw, gate=self.gate)
        self.show_result(res)
        return True

    # ---- 主循环 ----

    def _make_session(self, inp=None, out=None):
        kb = KeyBindings()

        @kb.add("enter")
        def _(event):                       # 回车执行（多行模式下手动接管）
            event.current_buffer.validate_and_handle()

        @kb.add("escape", "enter")
        def _(event):                       # Alt+Enter 换行
            event.current_buffer.insert_text("\n")

        hist = FileHistory(HISTORY_PATH) if out is None else None
        return PromptSession(history=hist, completer=FleetCompleter(self.reg),
                             complete_while_typing=True, key_bindings=kb,
                             bottom_toolbar=lambda: _toolbar(self.reg),
                             refresh_interval=1.0, input=inp, output=out)

    def run(self, inp=None, out=None):
        self.session = self._make_session(inp, out)
        if self.watch:
            self.watcher = Watcher(self, self.interval)
            self.watcher.start()
        self.console.print(Text("设备编队终端（T0）—— help 看用法，quit 退出",
                                style="bold cyan"))
        self.show_devices()
        try:
            with patch_stdout():
                while True:
                    try:
                        line = self.session.prompt("fleet> ", multiline=True,
                                                   prompt_continuation="  ... ")
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not line:
                        continue
                    self.lines.append(line)
                    if not self.dispatch(line):
                        break
        finally:
            if self.watcher:
                self.watcher.stop()
            self.reg.save()
        return 0


def _oneline(v, limit=160):
    s = str(v).replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


_PREFIX = re.compile(r"^[A-Za-z0-9_.\-]+:[A-Za-z0-9_.\-]+:\s*")


def _short_reason(r, limit=90):
    """表格里只放第一段原因，并去掉「设备名: 」前缀（谁的设备这一列已经说了）。"""
    s = _PREFIX.sub("", str(r or "").split("；")[0]).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m fleet.shell")
    ap.add_argument("--no-watch", action="store_true", help="关掉设备上下线事件流")
    ap.add_argument("--interval", type=float, default=15.0, help="事件流探测间隔秒")
    ap.add_argument("-c", "--command", default="", help="不进入交互，只跑一条命令")
    a = ap.parse_args(argv)
    sh = FleetShell(interval=a.interval, watch=not a.no_watch)
    if a.command:
        sh.dispatch(a.command)
        return 0
    return sh.run()


if __name__ == "__main__":
    sys.exit(main())
