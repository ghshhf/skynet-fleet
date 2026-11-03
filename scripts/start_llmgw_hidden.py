# -*- coding: utf-8 -*-
"""无窗启动天网 llmgw 网关（8790）：一个入口、后台聚合切换。

为什么单独一个脚本：llmgw_server.serve() 是阻塞的（serve_forever），
直接 `%PY% -m fleet.llmgw_server` 会让调用它的 bat 卡住不返回。
这里用 Popen + 隐藏三件套（照搬 start_llm_hidden.py 的 _hidden_startupinfo），
让它无窗常驻、bat 立即返回。

关键教训（2026-11-02）：在 AI 对话里用子进程拉起网关，会话切换后进程
会被系统回收（天网纪律「AI 任务后台进程会被终止」），导致 WorkBuddy 连
8790 报「连接网络错误」。所以网关必须走**用户侧启动**（双击 bat / 开机
自启），不能靠对话里的临时 Popen。

用法：
    python scripts/start_llmgw_hidden.py              # 默认 8790
    python scripts/start_llmgw_hidden.py --port 18790
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _hidden_startupinfo():
    """Windows 隐藏控制台三件套之一。每次必须新建实例（复用会失效）。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def main():
    ap = argparse.ArgumentParser(description="无窗启动天网 llmgw 网关")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()

    logpath = os.path.join(_ROOT, "llmgw_server.log")
    cmd = [sys.executable, "-m", "fleet.llmgw_server", "--port", str(a.port)]
    try:
        logf = open(logpath, "ab")
        subprocess.Popen(
            cmd,
            cwd=_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=logf,        # llmgw_server 用 sys.stderr 写日志，这里一并落盘
            stderr=logf,
            startupinfo=_hidden_startupinfo(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logf.close()
    except Exception as e:
        print("[start_llmgw] 启动失败：%s" % e)
        return 1
    print("[start_llmgw] 已无窗启动 llmgw 网关（端口 %d），日志 %s" % (a.port, logpath))
    return 0


if __name__ == "__main__":
    sys.exit(main())
