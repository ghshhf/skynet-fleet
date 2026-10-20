"""编队桥：手机上那个常驻 app（监听 8765），电脑侧经 adb forward 映射到 18765。

为什么要单独一条通道：adb 能进不代表桥在——桥被 MIUI 杀掉是**常态**（内存紧张时
系统直接清进程，无障碍开关还会被一起清空）。这时候 `adb shell input tap` 也可能
因为「USB 调试（安全设置）」没开而失败，于是「手机在不在」其实有三个层次：
  adb 通 / 桥通 / 能注入
本文件只回答第二层，第三层由 kinds/android_phone 的 check 去问。
"""
import os

from .base import Transport, tcp_open

LOCAL_PORT = int(os.environ.get("FLEET_BRIDGE_LOCAL_PORT", "18765"))


class BridgeTransport(Transport):
    name = "bridge"
    title = f"编队桥（127.0.0.1:{LOCAL_PORT} ← 手机 8765）"

    def available(self):
        if tcp_open("127.0.0.1", LOCAL_PORT, timeout=0.25):
            return True, ""
        return False, (f"编队桥不在线（本机 {LOCAL_PORT} 无监听）："
                       "桥被系统杀了，或 adb forward 没建")

    def describe(self):
        d = super().describe()
        d["local_port"] = LOCAL_PORT
        return d
