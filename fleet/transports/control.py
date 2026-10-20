"""控制面通道：编队节点（node_agent）都挂在它上面。

为什么 D0 就建它：`shell.exec` / `file.push` 这类「对别的机器动手」的能力，
实现不在本进程里，而在控制面持有的节点 socket 上。通道不通时，
最诚实的回答是「控制面没跑」，而不是「能力不存在」。
"""
import os

from .base import Transport, tcp_open

HOST = os.environ.get("FLEET_HOST", "127.0.0.1")
PORT = int(os.environ.get("FLEET_PORT", "9000"))


class ControlTransport(Transport):
    name = "control"
    title = f"控制面（{HOST}:{PORT}）"

    def available(self):
        if tcp_open(HOST, PORT, timeout=0.3):
            return True, ""
        return False, (f"控制面未运行（{HOST}:{PORT} 无监听）："
                       "编队节点表无从查询，远程能力全部不可用")

    def describe(self):
        d = super().describe()
        d["host"], d["port"] = HOST, PORT
        return d
