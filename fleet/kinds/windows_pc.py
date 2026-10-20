"""Windows 本机：这台机器既是控制面，也是设备。

它是唯一「不需要任何通道就能验证」的设备——所以它排第一：注册表、结果信封、
闸门这条链路要是有问题，在这里一眼就能看出来，不用先怀疑手机/路由/网络。
"""
import subprocess
import time

import common
import pc_state
import pc_usage
from ..core import BaseDevice, DeviceResult as R

DEFAULT_TIMEOUT = 25


class WindowsPC(BaseDevice):
    kind = "windows_pc"
    via = "local"

    def __init__(self, name="pc:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("os", "windows")

    def probe(self):
        if not getattr(pc_state, "IS_WIN", False):
            return False, "本机不是 Windows（pc.state 只实现了 Windows）"
        return True, ""

    def caps(self):
        return ("pc.state", "pc.usage", "pc.shell")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "pc.state":
            return self._state(args)
        if cap == "pc.usage":
            return self._usage(args)
        if cap == "pc.shell":
            return self._shell(args)
        return super().invoke(cap, args)

    # ---------------- 只读 ----------------

    def _state(self, args):
        st = pc_state.snapshot(want_clip=bool(args.get("clip")))
        if not st.get("ok"):
            return R.unavailable("pc.state", st.get("err") or "采集失败",
                                 device=self.name, via=self.via)
        return R.success("pc.state", self.name, via=self.via, data={
            "brief": pc_state.brief(st),
            "sections": sorted((st.get("sections") or {}).keys()),
            "anomalies": st.get("anomalies") or [],
            "presence": pc_state.presence(st),
            "missing": st.get("missing") or [],
        })

    def _usage(self, args):
        day = args.get("day") or None
        rows = pc_usage.load(day)
        if not rows:
            return R.unavailable("pc.usage", f"{day or '今天'}没有采样（常驻 --usage 没开）",
                                 device=self.name, via=self.via)
        s = pc_usage.summarize(rows)
        return R.success("pc.usage", self.name, via=self.via, data={
            "brief": pc_usage.brief(s), "day": day or "today", "samples": len(rows)})

    # ---------------- 动手 ----------------

    def _shell(self, args):
        cmd = (args.get("cmd") or "").strip()
        why = common.cmd_reject_reason(cmd)
        if why:
            return R.denied("pc.shell", why, device=self.name, via=self.via)
        timeout = float(args.get("timeout") or DEFAULT_TIMEOUT)
        t0 = time.time()
        try:
            p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return R.timeout("pc.shell", f"{timeout:.0f}s 内没跑完：{cmd}",
                             device=self.name, ms=(time.time() - t0) * 1000, via=self.via)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        ms = (time.time() - t0) * 1000
        if p.returncode != 0:
            return R.error("pc.shell", (err or out).strip()[:300] or f"rc={p.returncode}",
                           device=self.name, ms=ms, via=self.via)
        return R.success("pc.shell", self.name, ms=ms, side_effect=True, via=self.via,
                         data={"rc": p.returncode, "out": out.strip()[:4000]})
