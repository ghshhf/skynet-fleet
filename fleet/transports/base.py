"""通道（transports）：一类「怎么连」一个文件。

D0 只落**真实用得上的四条**：local / adb / bridge / control。
rtsp / ssh / http / mqtt / ir 留到 D2、D4 再建——空壳通道比没有更糟：它会让终端
显示「支持摄像头」，实际一调就崩，等于教人不敢相信这张表。

通道只回答一个问题：「这条道现在通不通，不通为什么」。真动作由 kinds/ 里的
adapter 调老模块完成——通道不搬业务代码。
"""
import socket


class Transport:
    name = "?"
    title = ""

    def available(self):
        """返回 (可用: bool, 原因: str)。不可用时必须说清。"""
        return True, ""

    def describe(self):
        ok, reason = self.available()
        return {"name": self.name, "title": self.title, "ok": ok, "reason": reason}


def tcp_open(host, port, timeout=0.35):
    """端口是否有人听。短超时——探测不该拖住终端。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
