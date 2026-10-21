# -*- coding: utf-8 -*-
"""无窗启动 llama-server（天网桌面端的本地脑）。

为什么不用 bat 里的 `start ... /min cmd /c`：那会留一个最小化黑窗在任务栏；
`start /b` 不建新窗口但不解析 `>` 重定向（`1>file` 会被原样当成参数传给
llama-server，它收到 `1>C:\\...` 直接报错退出）。

无窗做法来自 netforce 爬虫内核（skynet/net.py `_hidden_startupinfo()`，
README「后台跑不许弹窗」条）：三件套缺一不可——
STARTUPINFO + STARTF_USESHOWWINDOW + SW_HIDE、creationflags=CREATE_NO_WINDOW、
stdin=DEVNULL；STARTUPINFO 实例每次新建（复用第二次失效）。

日志不走 `>` 重定向，用 llama-server 自带的 --log-file（%TEMP%/tianwang_llama.log）。
2026-10-22：去掉 -dev Vulkan1——本机纯核显（Intel UHD）没有 Vulkan 设备，
带了 llama-server 直接起不来；CPU 模式 llama.cpp 自己会选。
"""
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LLAMA = r"E:\_tools\llama.cpp-b11483\bin\llama-server.exe"
MODEL = r"E:\_train_qwen06b\v13-Q8_0.gguf"
TEMPLATE = os.path.join(HERE, "..", "templates", "qwen3_thinkblock.jinja")
PORT = 8080
LOG = os.path.join(os.environ.get("TEMP") or ".", "tianwang_llama.log")


def _hidden_startupinfo():
    """Windows 隐藏控制台三件套之一。每次必须新建实例（复用会失效）。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def main():
    if not os.path.isfile(LLAMA):
        print("[start_llm] 找不到 llama-server：%s" % LLAMA)
        return 1
    if not os.path.isfile(MODEL):
        print("[start_llm] 找不到模型：%s" % MODEL)
        return 1
    cmd = [LLAMA, "-m", MODEL, "--port", str(PORT),
           "--host", "127.0.0.1", "-c", "4096", "-ngl", "99",
           "--jinja", "--chat-template", TEMPLATE,
           "--reasoning-budget", "0", "-t", "8", "--no-webui",
           "--log-file", LOG]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,   # 日志走 --log-file；继承父stdout管道会炸解码
            stderr=subprocess.DEVNULL,
            startupinfo=_hidden_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        print("[start_llm] 启动失败：%s" % e)
        return 1
    print("[start_llm] 已无窗启动，日志 %s" % LOG)

    # 就绪探测：CPU 加载 0.6B Q8_0 实测 30~60s，给到 120s
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(2)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/v1/models" % PORT, timeout=3) as r:
                if r.status == 200:
                    print("[start_llm] 就绪（%ds）" % int(120 - (deadline - time.time())))
                    return 0
        except Exception:
            pass
    print("[start_llm] 120s 未就绪——看日志 %s；bat 的等待循环会兜底" % LOG)
    return 0  # 不阻塞 bat，让它自己的循环决定


if __name__ == "__main__":
    sys.exit(main())
