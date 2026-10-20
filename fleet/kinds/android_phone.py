"""Android 手机：编队里唯一「能动手、又不在本机」的设备。

为什么 probe 要分三层（adb 装没装 → 有没有设备 → 设备真活着）：
这三层的故障对策完全不同（装 platform-tools / 重开无线调试 / 重连 adb），
糊成一句「手机不可用」等于让人从零排查。

能力名一律 domain.verb；老函数名留在实现里，上层只认新名——
这样以后换实现（比如把 adb 换成 Shizuku）不用改调用方。
"""
import android_device as A
import ble
import infrared
import phone_ops as O
import phone_state as PS
from ..core import BaseDevice, DeviceResult as R

KEYMAP = {"back": 4, "home": 3, "enter": 66, "menu": 82, "power": 26,
          "volume_up": 24, "volume_down": 25, "recent": 187, "wakeup": 224}


class AndroidPhone(BaseDevice):
    kind = "android_phone"
    via = "adb"

    def __init__(self, name="phone:main", **kw):
        super().__init__(name, via="adb", **kw)
        self.meta.setdefault("os", "android")

    # ---------------- 可达性 ----------------

    def serial(self):
        return A.pick_serial() or ""

    def probe(self):
        try:
            devs = A.devices()
        except Exception as e:
            return False, f"adb devices 出错: {e}"
        if not devs:
            return False, "adb 没有已连接的设备（无线调试掉了 / 没开调试）"
        s = self.serial()
        if not s:
            return False, "adb 里有记录但没有 state=device 的设备"
        if not A.alive(s, timeout=8):
            return False, f"{s} 在列表里但 adb shell 不通（授权弹窗没点 / 无线调试过期）"
        self.meta["addr"] = s
        return True, ""

    def caps(self):
        return ("screen.read", "tree.read", "apps.list", "app.foreground",
                "app.launch", "input.tap", "input.type", "input.key",
                "notify.read", "state.read", "bt.scan", "ir.send")

    def invoke(self, cap, args=None):
        args = args or {}
        s = self.serial()
        if not s:
            return R.unavailable(cap, "没有可用的手机序列（adb 无设备）",
                                 device=self.name, via=self.via)
        try:
            return self._do(cap, s, args)
        except Exception as e:
            return R.error(cap, f"{type(e).__name__}: {e}", device=self.name, via=self.via)

    def _do(self, cap, s, args):
        if cap == "screen.read":
            r = O.read_screen(s)
            if not r.get("ok"):
                return R.unavailable("screen.read", r.get("err") or "取屏失败",
                                     device=self.name, via=self.via)
            labels = [x for x in r.get("labels") or [] if x][:40]
            return R.success("screen.read", self.name, via=self.via, data={
                "foreground": r.get("foreground") or "",
                "labels": labels, "node_count": len(r.get("nodes") or [])})

        if cap == "tree.read":
            got = A.ui_xml(s)
            if not got.get("ok"):
                return R.unavailable("tree.read", got.get("err") or "取 UI 树失败",
                                     device=self.name, via=self.via)
            nodes = A.parse_ui(got["xml"])
            return R.success("tree.read", self.name, via=self.via, data={
                "tree": A.format_tree(nodes, limit=int(args.get("limit") or 40)),
                "node_count": len(nodes)})

        if cap == "apps.list":
            pkgs = A.packages(s, third_only=bool(args.get("third_only", True)))
            return R.success("apps.list", self.name, via=self.via,
                             data={"packages": pkgs, "count": len(pkgs)})

        if cap == "app.foreground":
            i = A.info(s)
            fg = i.get("foreground") or ""
            if not fg:
                return R.unavailable("app.foreground", "读不到前台窗口（dumpsys 无输出）",
                                     device=self.name, via=self.via)
            return R.success("app.foreground", self.name, via=self.via,
                             data={"foreground": fg, "screen": i.get("screen", "")})

        if cap == "app.launch":
            target = (args.get("target") or args.get("package") or "").strip()
            if not target:
                return R.error("app.launch", "缺 target（包名 / activity / action / URI）",
                               device=self.name, via=self.via)
            r = O.open_app(s, target, expect=args.get("expect") or "",
                           timeout=float(args.get("timeout") or 8))
            if not r.get("ok"):
                return R.error("app.launch", r.get("err") or "启动后没到前台",
                               device=self.name, via=self.via)
            return R.success("app.launch", self.name, side_effect=True, via=self.via,
                             data={"foreground": r.get("foreground", "")})

        if cap == "input.tap":
            if args.get("text"):
                r = A.tap_text(s, args["text"], dry_run=bool(args.get("dry_run")))
            elif args.get("x") is not None and args.get("y") is not None:
                r = A.tap(s, int(args["x"]), int(args["y"]))
            else:
                return R.error("input.tap", "缺参数：text 或 x/y", device=self.name,
                               via=self.via)
            if not r.get("ok"):
                return R.error("input.tap", r.get("err") or "点击失败",
                               device=self.name, via=self.via)
            return R.success("input.tap", self.name, side_effect=True, via=self.via,
                             data={k: v for k, v in r.items() if k != "ok"})

        if cap == "input.type":
            text = args.get("text") or ""
            if not text:
                return R.error("input.type", "缺 text", device=self.name, via=self.via)
            r = A.text_input(s, text)
            if not r.get("ok"):
                return R.error("input.type", r.get("err") or "输入失败",
                               device=self.name, via=self.via)
            return R.success("input.type", self.name, side_effect=True, via=self.via,
                             data={"len": len(text)})

        if cap == "input.key":
            key = str(args.get("key") or "").lower()
            code = KEYMAP.get(key) or (int(key) if key.isdigit() else None)
            if code is None:
                return R.error("input.key", f"未知按键 {key}（可用 {sorted(KEYMAP)}）",
                               device=self.name, via=self.via)
            rc, txt = A.shell(s, f"input keyevent {code}")
            if rc != 0:
                return R.error("input.key", txt.strip()[:200] or f"rc={rc}",
                               device=self.name, via=self.via)
            return R.success("input.key", self.name, side_effect=True, via=self.via,
                             data={"key": key, "code": code})

        if cap == "notify.read":
            rows = A.notifications(s, limit=int(args.get("limit") or 10))
            return R.success("notify.read", self.name, via=self.via,
                             data={"notifications": rows, "count": len(rows)})

        if cap == "state.read":
            st = PS.snapshot(s)
            if not st.get("ok"):
                return R.unavailable("state.read", st.get("err") or "采集失败",
                                     device=self.name, via=self.via)
            return R.success("state.read", self.name, via=self.via, data={
                "brief": PS.brief(st),
                "sections": sorted((st.get("sections") or st).keys()),
                "anomalies": PS.anomalies(st)})

        if cap == "bt.scan":
            rows = ble.scan(s, secs=int(args.get("secs") or 8))
            return R.success("bt.scan", self.name, via=self.via,
                             data={"devices": rows, "count": len(rows)})

        if cap == "ir.send":
            proto = (args.get("protocol") or "nec").lower()
            addr = int(args.get("addr") or 0x04)
            cmd = int(args.get("cmd") or 0x08)
            fn = {"nec": infrared.nec, "nec_ext": infrared.nec_ext,
                  "samsung": infrared.samsung, "sony": infrared.sony}.get(proto)
            if fn is None:
                return R.error("ir.send", f"未知协议 {proto}", device=self.name, via=self.via)
            freq, pattern = fn(addr, cmd)
            if args.get("dry_run", True):        # 默认只算不发射（见 infrared 的安全默认）
                return R.success("ir.send", self.name, via=self.via, data={
                    "dry_run": True, "protocol": proto, "freq": freq,
                    "pairs": len(pattern) // 2, "head": pattern[:8]})
            r = infrared.send(s, freq, pattern)
            if not r.get("ok"):
                return R.unavailable("ir.send", r.get("err") or "桥拒绝发射",
                                     device=self.name, via=self.via)
            return R.success("ir.send", self.name, side_effect=True, via=self.via,
                             data={"dry_run": False, "protocol": proto, "freq": freq})

        return super().invoke(cap, args)
