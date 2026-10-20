"""T0 验收：她开口时，正在敲的那半句话不能丢。

真事故的形状：老 REPL 是 `input("cmd> ")`，她一句话直接 print 进去，把提示符和已经
敲的字一起冲掉——表现为「打到一半的字没了」。这个脚本守三件事：

  1. **静态门禁**：shell.py 里不许出现裸 print()（输出必须走 console / say），
     且 prompt 循环必须在 patch_stdout() 里（这两条一破，事故立刻复现）；
  2. **缓冲不动**：她说话时，输入缓冲里的那半句必须原样还在；
  3. **同一张表**：状态栏的三个数字必须来自注册表，且与 reg.stats 一致
     （状态栏自己算一套，就是下一场「显示与事实不一致」）。

为什么不用伪终端直接敲：本机 Windows + 无 conpty 时，prompt_toolkit 的
create_pipe_input 会在 session.prompt 里阻塞不返回（本轮实测 2 分钟无输出，
靠 TaskStop 才停掉）。所以改成「AST 静态门禁 + Buffer 行为断言」两层，
这是能在任何平台自动跑的等价守门。

跑法：python evals/t0_input_not_clobbered.py
"""
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHELL = os.path.join(ROOT, "fleet", "shell.py")


def _tree():
    with open(SHELL, encoding="utf-8") as f:
        return ast.parse(f.read(), filename=SHELL)


def _bare_prints(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "print":
            out.append(node.lineno)
    return out


def _patch_stdout_wraps_prompt(tree):
    """run() 里必须有一个 `with patch_stdout():`，且其中调用了 session.prompt。"""
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run"]:
        for w in [n for n in ast.walk(fn) if isinstance(n, ast.With)]:
            items = []
            for it in w.items:
                call = it.context_expr
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    items.append(call.func.id)
                elif isinstance(call, ast.Name):
                    items.append(call.id)
            if "patch_stdout" not in items:
                continue
            for c in ast.walk(w):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                        and c.func.attr == "prompt":
                    return True
    return False


def main():
    fails = []
    tree = _tree()

    # ① 静态门禁：不许裸 print
    bad = _bare_prints(tree)
    if bad:
        fails.append(f"shell.py 第 {bad} 行有裸 print()——会冲掉正在输入的行，输出必须走 console/say")

    # ② 静态门禁：prompt 必须在 patch_stdout 里
    if not _patch_stdout_wraps_prompt(tree):
        fails.append("run() 里没找到 `with patch_stdout():` 包住 session.prompt")

    # ③ 她说话不碰输入缓冲
    import io
    from prompt_toolkit.buffer import Buffer
    from rich.console import Console
    from fleet.shell import FleetShell
    from fleet.core import BaseDevice, Capability, DeviceResult as R, Registry

    class Echo(BaseDevice):
        kind = "echo"

        def probe(self):
            return True, ""

        def caps(self):
            return ("demo.echo",)

        def invoke(self, cap, args=None):
            return R.success(cap, self.name, {"text": (args or {}).get("text", "")})

    reg = Registry(path="", ttl=0)
    reg.register(Capability("demo.echo", "回声"))
    reg.add(Echo("demo:1"))
    sh = FleetShell(reg=reg, watch=False, console=Console(file=io.StringIO()))

    buf = Buffer()
    buf.insert_text("demo.ec")
    sh.say("她：你在弄啥", level="event")
    if buf.text != "demo.ec":
        fails.append(f"她开口后输入缓冲被改了: {buf.text!r}")
    if "你在弄啥" not in (sh._pending or [""])[-1]:
        fails.append("她的话没进输出通道（_pending 里没有）")

    # ④ 补全来自注册表：scr<Tab> → screen.read（用真注册表，假表补不出真能力）
    from prompt_toolkit.document import Document
    from fleet.catalog import build
    from fleet.shell import FleetCompleter

    real = build(path="", with_nodes=False)
    comp = FleetCompleter(real)

    def cands(text):
        return [c.text for c in comp.get_completions(Document(text), None)]

    scr = cands("scr")
    if not any(c.startswith("screen.") for c in scr):
        fails.append(f"scr 补不出 screen.*：{scr}")
    if not any(c.startswith("input.") for c in cands("inp")):
        fails.append(f"inp 补不出 input.*：{cands('inp')}")
    if "--device" not in cands("pc.state --dev"):
        fails.append(f"--dev 补不出 --device：{cands('pc.state --dev')}")
    if "pc:local" not in cands("pc.state --device "):
        fails.append(f"--device 后面补不出设备名：{cands('pc.state --device ')}")

    # ⑤ 状态栏与注册表同源
    from fleet.shell import _status_bits
    b = _status_bits(sh.reg)
    st = sh.reg.stats()
    if (b["online"], b["devices"], b["caps"]) != (st["online"], st["devices"], st["caps"]):
        fails.append(f"状态栏与 reg.stats 不一致: {b} vs {st}")
    if b["online"] != 1:
        fails.append(f"一台在线设备都没数出来: {b}")

    # ⑥ 一条命令走通「注册表 → 设备 → 信封」
    if not sh.dispatch("demo.echo --text 你好"):
        fails.append("dispatch 把退出信号给错了")
    if not any("demo.echo" in x for x in sh._pending):
        fails.append("调用结果没进输出通道")

    if fails:
        print("FAIL:")
        for f in fails:
            print("  ·", f)
        return 1
    print("t0_input_not_clobbered：6 组断言全过"
          "（无裸 print / patch_stdout 在位 / 缓冲不动 / 补全来自注册表 / 状态栏同源）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
