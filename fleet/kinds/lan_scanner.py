"""局域网扫描器：不是「一台设备」，是「一只眼睛」。

它属于设备层的理由：`net.scan` / `net.nearby` 是「这张网里有什么」的唯一来源，
而「有什么」是设备编队的前置——不认识的东西谈不上操控。它自己没有 socket，
所以 probe 只问一句「本机有没有可扫的网段」。
"""
import nearby
import sweep as S
from .. import acquire
from ..core import BaseDevice, DeviceResult as R


class LanScanner(BaseDevice):
    kind = "lan_scanner"
    via = "local"

    def __init__(self, name="lan:scanner", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("note", "只探测端口与指纹，不发凭据、不取画面")

    def probe(self):
        try:
            nets = S.local_subnets()
        except Exception as e:
            return False, f"枚举网段失败: {e}"
        if not nets:
            return False, "本机没有可扫网段（网卡全断 / 只有回环）"
        return True, ""

    def caps(self):
        return ("net.scan", "net.nearby", "net.dissect", "net.survey")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "net.scan":
            quick = bool(args.get("quick"))
            r = S.sweep(do_nbns=not quick, do_mdns=not quick,
                        timeout_ms=int(args.get("timeout_ms") or 400))
            devs = r.get("devices") or []
            return R.success("net.scan", self.name, via=self.via, data={
                "count": len(devs), "devices": devs,
                "subnets": r.get("subnets") or [],
                "elapsed": round(float(r.get("elapsed") or 0), 1),
                "notes": r.get("notes") or []})
        if cap == "net.nearby":
            out = nearby.run(with_lan=not args.get("no_lan"),
                             with_wifi=not args.get("no_wifi"),
                             with_bt=not args.get("no_bt"))
            return R.success("net.nearby", self.name, via=self.via, data={
                "report": nearby.report(out), "raw": out})

        # ---- L2 采集层：把空气里跑的流量拆开看 ----
        if cap == "net.dissect":
            r = acquire.net_dissect(
                duration=float(args.get("duration") or 5),
                iface=(args.get("iface") or None),
                bpf=(args.get("bpf") or None),
                count=int(args.get("count") or 300))
            if not r.get("ok"):
                return R.unavailable("net.dissect", r.get("err") or "抓包失败",
                                     device=self.name, via=self.via)
            return R.success("net.dissect", self.name, via=self.via, data=r)

        if cap == "net.survey":
            r = acquire.net_survey(
                duration=float(args.get("duration") or 20),
                iface=(args.get("iface") or None))
            if not r.get("ok"):
                return R.unavailable("net.survey", r.get("err") or "侦察失败",
                                     device=self.name, via=self.via)
            return R.success("net.survey", self.name, via=self.via, data=r)

        return super().invoke(cap, args)
