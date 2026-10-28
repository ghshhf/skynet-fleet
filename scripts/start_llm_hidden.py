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
    """找模型。explicit 可以是文件全路径，也可以是目录（里面挑最新的 gguf）。"""
    if explicit:
        if os.path.isdir(explicit):
            hits = [f for f in glob.glob(os.path.join(explicit, "*.gguf"))
                    if os.path.getsize(f) >= MODEL_MIN_BYTES]
            if hits:
                return max(hits, key=os.path.getmtime)
        elif os.path.isfile(explicit):
            return explicit
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

    model = find_model(a.model)
    if not model:
        _complain("模型（.gguf，且 >%dMB）" % (MODEL_MIN_BYTES // 1048576),
                  locate.explain("*.gguf", env=("FLEET_LLM_MODEL", "FLEET_MODEL"),
                                 hints=MODEL_HINTS))
        return 1

    template = os.path.join(_ROOT, "templates", "qwen3_thinkblock.jinja")
    use_template = (not a.no_template) and os.path.isfile(template)

    tag = ("_" + a.tag) if a.tag else ""
    log = os.path.join(os.environ.get("TEMP") or ".", "tianwang_llama%s.log" % tag)

    cmd = [llama, "-m", model, "--port", str(a.port), "--host", "127.0.0.1",
           "-c", str(a.ctx), "-ngl", str(a.ngl), "-t", str(a.threads),
           "--jinja", "--reasoning-budget", "0", "--no-webui",
           "--log-file", log]
    if use_template:
        cmd += ["--chat-template", template]

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
