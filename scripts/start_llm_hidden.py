# -*- coding: utf-8 -*-
"""无窗启动 llama-server（天网的本地脑）。

路径不写死盘符（2026-10-29 改）
--------------------------------
原来这里写死 `E:\\_tools\\llama.cpp-b11483\\bin` 和 `E:\\_train_qwen06b\\v13-Q8_0.gguf`，
而 `start-decider.bat` 写死 `G:\\...`，`tianwang.bat` 又把同一套抄了一遍——
**同一份目录结构在三个盘上就有三份硬编码**，换到只有 C/D 盘的机器全废，
而且废得无声无息（文件不在，脚本只是「起不来」）。

现在只写两件事：东西**叫什么**、它**相对怎么摆**。
盘符由 `fleet/locate.py` 运行时枚举本机根目录后与 hints 做全组合——
代码里一个盘符都没有。想强指定就设环境变量（永远最高优先）：

    FLEET_LLAMA_SERVER   llama-server 的完整路径（或含它的目录）
    FLEET_LLM_MODEL      模型 .gguf 的完整路径（或含它的目录）

为什么不用 bat 里的 `start ... /min cmd /c`：那会留一个最小化黑窗在任务栏；
`start /b` 不建新窗口但不解析 `>` 重定向（`1>file` 会被原样当成参数传给
llama-server，它收到 `1>C:\\...` 直接报错退出）。

无窗做法来自 netforce 爬虫内核（skynet/net.py `_hidden_startupinfo()`，
README「后台跑不许弹窗」条）：三件套缺一不可——
STARTUPINFO + STARTF_USESHOWWINDOW + SW_HIDE、creationflags=CREATE_NO_WINDOW、
stdin=DEVNULL；STARTUPINFO 实例每次新建（复用第二次失效）。

日志不走 `>` 重定向，用 llama-server 自带的 --log-file。
2026-10-22：去掉 -dev Vulkan1——本机纯核显（Intel UHD）没有 Vulkan 设备，
带了 llama-server 直接起不来；CPU 模式 llama.cpp 自己会选。

用法
----
    python scripts/start_llm_hidden.py                  # 对话：8080 / ctx 4096
    python scripts/start_llm_hidden.py --port 18080 --tag decider
    python scripts/start_llm_hidden.py --model /path/to/x.gguf --no-wait
"""
import argparse
import glob
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fleet import locate  # noqa: E402  （要在 sys.path 补完之后导入）

#: llama-server 相对怎么摆。换机器只要目录结构还长得像就能找到，盘符不重要。
LLAMA_HINTS = (
    "_tools/llama.cpp/bin",
    "_tools/llama.cpp-b11483/bin",
    "_downloads/llamacpp/bin",
    "_downloads/llamacpp-b11840",
    "tools/llamacpp",
    "tools/llama.cpp/build/bin",
    "llama.cpp/build/bin",
)

#: 模型相对怎么摆。`tmp` 进来是因为这台开发机的 1.7B 就落在 D:/tmp，
#: 不属于任何「正规」目录——但它是真在用的那份，不找就白找。
MODEL_HINTS = (
    "_train_qwen06b",
    "_models/llm/gguf",
    "_models/llm",
    "_models",
    "models",
    "tmp",
    "work/_local_ai",
)

#: 模型残片下限。今天下 llama.cpp 到 96% 被超时截断，残片「存在」但喂给
#: llama-server 会崩得莫名其妙——所以低于这个大小的 gguf 不算数。
MODEL_MIN_BYTES = 50 * 1024 * 1024


def _hidden_startupinfo():
    """Windows 隐藏控制台三件套之一。每次必须新建实例（复用会失效）。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def find_llama(explicit=""):
    """找 llama-server。explicit 最高优先（命令行直给）。"""
    return locate.exe(
        "llama-server.exe",
        env=("FLEET_LLAMA_SERVER", "FLEET_LLAMA_BIN"),
        extra=(explicit,) if explicit else (),
        hints=LLAMA_HINTS,
    )


def find_model(explicit=""):
    """找模型。explicit 可以是目录 / 文件全路径 / 单纯的文件名，三种都认。

    **指名了就必须用那一份，不许换一个顶上。** 今天真踩过：`--model
    Qwen3-1.7B-Q8_0.gguf` 因为 cwd 下没这个文件，静默兜底成「已知位置里
    最新的 gguf」= 0.6B，两个端口起的其实是同一个模型，对照实验当场作废。
    名字是名字，位置是位置：位置可以自己找，找错就是错，找不到要报错。
    """
    if explicit:
        if os.path.isdir(explicit):
            hits = [f for f in glob.glob(os.path.join(explicit, "*.gguf"))
                    if os.path.getsize(f) >= MODEL_MIN_BYTES]
            if hits:
                return max(hits, key=os.path.getmtime)
            raise FileNotFoundError("目录里没有够大的 gguf：%s" % explicit)
        if os.path.isfile(explicit):
            return explicit
        # 给的是名字不是路径 → 按文件名去已知位置找，找到才算数
        hit = locate.file(os.path.basename(explicit), env=(),
                          hints=MODEL_HINTS, min_size=MODEL_MIN_BYTES)
        if hit:
            return hit
        raise FileNotFoundError(
            "指名要的模型找不到：%s（已知位置里没有同名的，不拿别的顶替）"
            % explicit)
    return locate.file(
        "",
        env=("FLEET_LLM_MODEL", "FLEET_MODEL"),
        hints=MODEL_HINTS,
        pattern="*.gguf",
        min_size=MODEL_MIN_BYTES,
    )


def _complain(what, info):
    """找不到时说清楚「找过哪儿 + 怎么让它找到」，不是一句「找不到」。"""
    print("[start_llm] 找不到 %s。找过这些地方：" % what)
    for t in info.get("tried", []):
        print("    %-22s %-46s %s" % (t["source"], t["path"][:46], t["why"]))
    print("  -> %s" % info.get("tip", ""))


def _log_tail(path, n=12):
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8", "replace")
        return "\n".join(text.strip().splitlines()[-n:])
    except Exception as e:
        return "（读不到日志 %s：%s）" % (path, e)


def build_cmd(llama, model, port, ctx=4096, ngl=99, threads=8,
              log="", no_template=False):
    """构造 llama-server 命令行。返回 (cmd, 是否用了自定义模板)。

    **单拎出来是为了让门禁能验产线真正用的参数。** 门禁自己再拼一份 cmd，只能
    证明「这样写是对的」，证明不了「我们是这样写的」——2026-10-29 那次模板参数
    挂十天没人发现，就是因为没有哪条门禁看过产线的命令行。
    """
    template = os.path.join(_ROOT, "templates", "qwen3_thinkblock.jinja")
    use_template = (not no_template) and os.path.isfile(template)
    cmd = [llama, "-m", model, "--port", str(port), "--host", "127.0.0.1",
           "-c", str(ctx), "-ngl", str(ngl), "-t", str(threads),
           "--jinja", "--reasoning-budget", "0", "--no-webui"]
    if log:
        cmd += ["--log-file", log]
    if use_template:
        # 必须是 --chat-template-file（传路径），不是 --chat-template（传内容）。
        # 传错这个参数名，llama-server 会把整条路径当 jinja 源码编译，报
        #   Invalid template variable name: 'qwen3_thinkblock'
        # 而且它**不返回 500**：HTTP 200 + finish_reason=stop，正文就是这段
        # 错误文字。上游看到的是「模型答了一段废话」，不是「配置错了」。
        # 从 10-22 到 10-29，所有本地模型的决策测试都在这个坑里，全部作废。
        cmd += ["--chat-template-file", template]
    return cmd, use_template


def main():
    ap = argparse.ArgumentParser(description="无窗起 llama-server（天网本地脑）")
    ap.add_argument("--port", type=int, default=8080,
                    help="8080=对话主模型 / 18080=决策调度模型")
    ap.add_argument("--ctx", type=int, default=4096, help="上下文长度")
    ap.add_argument("--model", default="", help="直接指定 gguf（文件或目录）")
    ap.add_argument("--llama", default="", help="直接指定 llama-server 路径")
    ap.add_argument("--tag", default="", help="日志文件名后缀，如 decider / chat")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--ngl", type=int, default=99, help="卸载到 GPU 的层数")
    ap.add_argument("--wait", type=float, default=120.0, help="就绪等待秒数，0=不等")
    ap.add_argument("--no-template", action="store_true",
                    help="不传自定义对话模板（模型自带时用）")
    a = ap.parse_args()

    llama = find_llama(a.llama)
    if not llama:
        _complain("llama-server.exe", locate.explain(
            "llama-server.exe", env=("FLEET_LLAMA_SERVER", "FLEET_LLAMA_BIN"),
            hints=LLAMA_HINTS))
        print("  （llama.cpp 官方发布包：github.com/ggml-org/llama.cpp/releases）")
        return 1

    try:
        model = find_model(a.model)
    except FileNotFoundError as e:
        print("[start_llm] %s" % e)
        _complain("模型（.gguf，且 >%dMB）" % (MODEL_MIN_BYTES // 1048576),
                  locate.explain("*.gguf", env=("FLEET_LLM_MODEL", "FLEET_MODEL"),
                                 hints=MODEL_HINTS))
        return 1
    if not model:
        _complain("模型（.gguf，且 >%dMB）" % (MODEL_MIN_BYTES // 1048576),
                  locate.explain("*.gguf", env=("FLEET_LLM_MODEL", "FLEET_MODEL"),
                                 hints=MODEL_HINTS))
        return 1

    tag = ("_" + a.tag) if a.tag else ""
    log = os.path.join(os.environ.get("TEMP") or ".", "tianwang_llama%s.log" % tag)

    cmd, use_template = build_cmd(
        llama, model, a.port, ctx=a.ctx, ngl=a.ngl, threads=a.threads,
        log=log, no_template=a.no_template)

    print("[start_llm] llama-server : %s" % llama)
    print("[start_llm] 模型         : %s" % model)
    print("[start_llm] 端口/上下文  : %d / %d%s" % (
        a.port, a.ctx, "" if use_template else "（无自定义模板）"))

    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,   # 日志走 --log-file；继承父 stdout 管道会炸解码
            stderr=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        print("[start_llm] 启动失败：%s" % e)
        return 1
    print("[start_llm] 已无窗启动，日志 %s" % log)

    if a.wait <= 0:
        return 0

    # 就绪探测：CPU 加载要几十秒，给到 --wait。
    deadline = time.time() + a.wait
    while time.time() < deadline:
        time.sleep(2)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/v1/models" % a.port, timeout=3) as r:
                if r.status == 200:
                    print("[start_llm] 就绪（%.0fs）" % (a.wait - (deadline - time.time())))
                    return 0
        except Exception:
            pass

    # 没就绪就把日志尾巴甩出来——不然用户只看到一句「没起来」，无从下手。
    print("[start_llm] %.0fs 未就绪。日志尾部：" % a.wait)
    print(_log_tail(log))
    return 0  # 不阻塞调用方，让它自己的循环决定降级


if __name__ == "__main__":
    sys.exit(main())
