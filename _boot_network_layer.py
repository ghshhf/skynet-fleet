r"""天网网络层开机自启（无窗口）。由 HKLM Run 键在登录时调用。

组成：
  1) selfproxy  127.0.0.1:18080 —— 统一出网层（国内直连 + 国外 Worker 隧道，
     隧道由 selfproxy 进程内直接建连，不再单独监听 18083）
  2) gitport    127.0.0.1:7810  —— GitHub 系加速通道（netaccel 子系统，只管 git）
日志写脚本同目录的 _autostart.log。
全程 pythonw 运行，不弹窗口。
"""
import os
import socket
import sys
import threading
import time

SKYNET = r"E:\xmanbian\skynet-fleet"
NETFORCE = r"E:\xmanbian\netforce"
LOG = os.path.join(SKYNET, "_autostart.log")
SELFPROXY_PORT = 18080


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

    # 2) selfproxy（网络层内建出口；进程内线程，主线程必须常驻）
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
