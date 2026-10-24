"""OSINT 设备：把外部情报变成一种「能力」。

它属于设备层的理由：osint.* 是「这张网之外的世界有什么」的唯一来源，和
LanScanner（这张网里有什么）是镜像的两极——一个向内、一个向外。它本身不发任何
包到目标（只读公开/被动源），所以 probe 只问一句「requests 装了没」。
"""
import osint as O
from ..core import BaseDevice, DeviceResult as R


class Osint(BaseDevice):
    kind = "osint"
    via = "internet"

    def __init__(self, name="osint:web", **kw):
        super().__init__(name, via="internet", **kw)
        self.meta.setdefault("note", "只查公开/被动源，仅限你拥有或授权的资产")

    def probe(self):
        try:
            import requests  # noqa: F401
        except Exception as e:
            return False, f"未装 requests（pip install requests）：{e}"
        return True, ""

    def caps(self):
        return ("osint.crt", "osint.dns", "osint.rdap", "osint.shodan")

    def invoke(self, cap, args=None):
        args = args or {}
        op = cap.split(".", 1)[1]
        m = O.run(op, **{k: v for k, v in args.items() if v is not None})
        if not m.get("ok"):
            return R.unavailable(cap, m.get("err") or "查询失败",
                                 device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data=m)
