"""通道清单。加一条通道 = 在这里加一行 + 建一个文件（不改任何调用方）。"""
from .base import Transport, tcp_open  # noqa: F401
from .local import LocalTransport
from .adb import AdbTransport
from .bridge import BridgeTransport
from .control import ControlTransport


def all_transports():
    return [LocalTransport(), AdbTransport(), BridgeTransport(), ControlTransport()]


def get(name):
    for t in all_transports():
        if t.name == name:
            return t
    return None


def probe_all():
    return [t.describe() for t in all_transports()]
