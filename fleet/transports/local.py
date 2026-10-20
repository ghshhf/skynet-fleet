"""本机：不需要连接，所以永远「通」——但要说清是哪台机器。"""
import os
import platform

from .base import Transport


class LocalTransport(Transport):
    name = "local"
    title = "本机进程"

    def available(self):
        return True, ""

    def describe(self):
        d = super().describe()
        d["host"] = platform.node()
        d["os"] = f"{platform.system()} {platform.release()}"
        d["cwd"] = os.getcwd()
        return d
