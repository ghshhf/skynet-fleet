"""adb 通道：手机/电视/盒子都从这里进。

为什么把「adb 装没装」「有没有设备」分成两句说：这两种失败的对策完全不同——
前者要装 platform-tools，后者要去手机上重开无线调试。合成一句「adb 不可用」
等于让人自己猜。
"""
import os

from .base import Transport


def _A():
    import android_device
    return android_device


class AdbTransport(Transport):
    name = "adb"
    title = "adb（无线/USB 调试）"

    def exe(self):
        try:
            return _A().adb_path() or ""
        except Exception:
            return ""

    def serials(self):
        try:
            return list(_A().devices())
        except Exception:
            return []

    def available(self):
        exe = self.exe()
        if not exe:
            return False, "找不到 adb（platform-tools 未装或不在 PATH）"
        if not os.path.exists(exe):
            return False, f"adb 路径不存在: {exe}"
        try:
            devs = _A().devices()
        except Exception as e:
            return False, f"adb devices 执行出错: {e}"
        if not devs:
            return False, "adb 没有已连接的设备（无线调试掉了 / 没开调试）"
        return True, ""

    def describe(self):
        d = super().describe()
        d["exe"] = self.exe()
        d["serials"] = self.serials()
        return d
