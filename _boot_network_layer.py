r"""天网网络层开机自启（无窗口）。由 HKLM Run 键在登录时调用。

组成：
  1) selfproxy  127.0.0.1:18080 —— 统一出网层（国内直连 + 国外 Worker 隧道，
     隧道由 selfproxy 进程内直接建连，不再单独监听 18083）
  2) gitport    127.0.0.1:7810  —— GitHub 系加速通道（netaccel 子系统，只管 git）
  3) lan-mcp    0.0.0.0:18765   —— 手机直连电脑的控制面（手机 LanClient 协议，
     token 即授权）。它必须**绑在对外地址上**，所以单独攒一个子进程，日志单独落盘。
日志写脚本同目录的 _autostart.log；lan-mcp 自己的日志在 _lan_mcp.log。
全程 pythonw 运行，不弹窗口。
"""
import os
import socket
import subprocess
import sys
import threading
import time

# 位置从脚本自身推导，不写死盘符：换机器/换盘只改目录摆法，不改代码。
SKYNET = os.path.dirname(os.path.abspath(__file__))
NETFORCE = os.environ.get("NETFORCE_HOME") or os.path.join(
    os.path.dirname(SKYNET), "netforce")
LOG = os.path.join(SKYNET, "_autostart.log")
SELFPROXY_PORT = 18080
LAN_MCP_PORT = 18765


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _alive(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _start_lan_mcp():
    """手机直连电脑的控制面。已在监听就跳过，避免两个实例抢同一个端口。"""
    if _alive(LAN_MCP_PORT):
        log("lan-mcp: already running on %d" % LAN_MCP_PORT)
        return
    script = os.path.join(SKYNET, "lan_mcp_server.py")
    if not os.path.isfile(script):
        log("lan-mcp: 脚本缺失，跳过（%s）" % script)
        return
    try:
        out = open(os.path.join(SKYNET, "_lan_mcp.log"), "a", encoding="utf-8")
        flags = 0
        # 无窗口 + 与父进程脱钩：父进程退出后子进程照样活着
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags |= subprocess.DETACHED_PROCESS
        p = subprocess.Popen(
            [sys.executable, script],
            cwd=SKYNET, stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT,
            creationflags=flags, close_fds=True,
        )
        log("lan-mcp: 已启动 pid=%s 端口 %d" % (p.pid, LAN_MCP_PORT))
    except Exception as e:  # noqa: BLE001
        log("lan-mcp ERROR %s: %s" % (type(e).__name__, e))


def main():
    # 1) gitport（GitHub 系加速子系统；detached，独立存活）
    try:
        sys.path.insert(0, SKYNET)
        from fleet import netaccel
        ok, msg = netaccel.start()
        log("gitport start: %s %s" % (ok, msg))
        if ok:
            ok2, msg2 = netaccel.git_proxy(True)
            log("git_proxy: %s %s" % (ok2, msg2))
    except Exception as e:  # noqa: BLE001
        log("gitport ERROR %s: %s" % (type(e).__name__, e))

    # 2) lan-mcp（手机直连电脑；独立子进程，自带日志）
    _start_lan_mcp()

    # 3) selfproxy（网络层内建出口；进程内线程，主线程必须常驻）
    if _alive(SELFPROXY_PORT):
        log("selfproxy: already running on %d" % SELFPROXY_PORT)
        return  # 已有实例常驻，本进程退出即可，避免堆积
    try:
        sys.path.insert(0, NETFORCE)
        import selfproxy
        addr = selfproxy.start()
        log("selfproxy: %s" % addr)
    except Exception as e:  # noqa: BLE001
        log("selfproxy ERROR %s: %s" % (type(e).__name__, e))
        return

    # 常驻，别让 selfproxy 随主线程退出
    threading.Event().wait()


if __name__ == "__main__":
    main()
