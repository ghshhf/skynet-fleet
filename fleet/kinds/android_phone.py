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
import radio as RADIO
import thermal as THERMAL
from .. import acquire
from .. import creative
from .. import hotspot_roam
from .. import ir_trial
from .. import proximity
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
                "notify.read", "state.read", "bt.scan", "ir.send",
                # 射频层：这台手机是编队里唯一有「天线」的设备。
                # 电脑的无线网卡被自己开的移动热点占满、也不再扫周边 AP，
                # 蜂窝频段电脑连基带都没有——这两件事只能下放给手机干。
                "radio.wifi.survey", "radio.wifi.link", "radio.cell",
                # 热状态：算力节点最该被看见的东西。手机被动散热，SOC 到 85℃ 就压频，
                # 同一套参数 tg 能从 50 掉到 30。「怎么突然慢了」不查它一定答错。
                "thermal.read",
                # ---- 创意能力（天网之眼 + 信号外放；计划书 2026-10-24）----
                # scene.scan / sentinel.watch / scene.run 是「读 + 编排」，
                # 但 scene.run 会串联动手步骤（ir/hotspot），故标 side_effect。
                # ir.command / phone.hotspot 是「动手」级，标 side_effect，闸在 catalog 层。
                "scene.scan", "ir.command", "phone.hotspot",
                "sentinel.watch", "scene.run",
                # ---- P1 交互式逐个试码（万能遥控同款 UX）----
                # start/next/yes/no/stop 五动作；start/next 发射故标 side_effect，
                # 闸在 catalog 层；start 过一次闸门后会话内免重复问（注册表豁免）。
                "ir.trial",
                # ---- P2 热点叠加编队（开热点→设备自己进来→手机侧组网视角）----
                # deploy 会真开热点（side_effect，闸在 catalog 层）；
                # clients/diff 只读（ARP 表 + ping，不开热点也能看）。
                "hotspot.roam",
                # ---- 采集层（L2/L3/L5；信息获取层调研 2026-10-24）----
                # phone.sense/phone.see 是「读 + 拍」，只读不改；life.* 是生活流聚合（本地簿记）。
                # 都不标 side_effect：拍照/传感是采集不是动作；落本地日志不改设备状态。
                "phone.sense", "phone.see",
                "life.log", "life.digest", "life.sentinel",
                # ---- 距离感知（P0；手机能力升级计划书 2026-10-24）----
                # proximity.* 把「只能报方向」升级成「米数 + 靠近/远离 + 轨迹」。
                # 纯读：扫描/传感采集，落校准文件在 ~/.fleet/，不改设备状态。
                "proximity.estimate", "proximity.track", "proximity.calibrate")

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

        # ---- 射频层：只有手机有的感官 ----
        # 这三条都是「读」，但 wifi.survey 会让射频真的发一次扫描帧，
        # 所以标 side_effect=False（不改系统状态）同时在 note 里说清它会占用射频。
        if cap == "radio.wifi.survey":
            r = RADIO.wifi_survey(s, rounds=int(args.get("rounds") or 1),
                                  settle=float(args.get("settle") or 7))
            if not r.get("ok"):
                return R.unavailable(cap, r.get("err") or "没扫到任何 AP",
                                     device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via, data={
                "count": r["count"], "bands": r["bands"],
                "aps": r["aps"], "summary": r["summary"], "table": r["table"],
                "note": "扫一次会占射频约 5-8 秒；结果含频段/信道/厂商/距离量级"})

        if cap == "radio.wifi.link":
            r = RADIO.wifi_link(s)
            if not r.get("ok"):
                return R.unavailable(cap, r.get("err") or "读不到链路状态",
                                     device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via,
                             data={k: v for k, v in r.items() if k != "ok"})

        if cap == "radio.cell":
            r = RADIO.cell(s)
            if not r.get("ok"):
                return R.unavailable(cap, r.get("err") or "读不到蜂窝服务态",
                                     device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via, data={
                "subs": r["subs"], "count": r["count"],
                "note": "频段由 ARFCN 换算频率后按 3GPP 区间反推，"
                        "不是直读——这台 ROM 的 dumpsys 不给 mBands"})

        if cap == "thermal.read":
            r = THERMAL.read(s)
            if not r.get("ok"):
                return R.unavailable(cap, r.get("err") or "读不到热状态",
                                     device=self.name, via=self.via)
            return R.success(cap, self.name, via=self.via,
                             data={k: v for k, v in r.items() if k != "ok"})

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

        # ---- 创意能力（天网之眼 + 信号外放）----
        # 逻辑全在 fleet.creative，这里只做「参数透传 + 收 DeviceResult」。
        if cap == "scene.scan":
            return creative.scene_scan(
                s,
                with_discovery=bool(args.get("with_discovery", True)),
                with_cell=bool(args.get("with_cell", True)),
                with_ble=bool(args.get("with_ble", True)),
                settle=float(args.get("settle") or 6),
                bt_secs=float(args.get("bt_secs") or 6))

        if cap == "ir.command":
            r = creative.ir_command(
                s,
                (args.get("brand") or "").strip(),
                (args.get("device_type") or "").strip(),
                (args.get("key") or "").strip(),
                dry_run=bool(args.get("dry_run", True)),
                phrase=(args.get("phrase") or None))
            return r

        # ---- P1 交互式逐个试码：手机当试码器，人当 oracle ----
        if cap == "ir.trial":
            action = (args.get("action") or "").strip().lower()
            brand = (args.get("brand") or "").strip()
            device_type = (args.get("device_type") or "").strip()
            if not action and (args.get("phrase") or "").strip():
                action, brand, device_type = ir_trial.parse_phrase(args["phrase"])
            if not action:
                action = "status"
            if action == "start":
                r = ir_trial.start(s, brand, device_type,
                                   protocols=args.get("protocols"))
            elif action in ("next", "no", "没有", "下一个"):
                r = ir_trial.next_candidate(s)
            elif action in ("yes", "有", "有了", "命中"):
                r = ir_trial.yes(s, key=(args.get("key") or None))
            elif action == "stop":
                r = ir_trial.stop()
            else:
                r = ir_trial.status()
            if not r.get("ok"):
                return R.error(cap, r.get("err") or "试码失败",
                               device=self.name, via=self.via)
            return R.success(cap, self.name,
                             side_effect=action in ("start", "next", "no",
                                                    "没有", "下一个"),
                             via=self.via, data=r)

        if cap == "phone.hotspot":
            return creative.hotspot(
                s,
                bool(args.get("on")),
                ssid=(args.get("ssid") or None),
                password=(args.get("password") or None))

        # ---- P2 热点叠加编队：开热点 → 设备自己进来 → 手机侧组网视角 ----
        if cap == "hotspot.roam":
            action = (args.get("action") or "deploy").strip().lower()
            if action == "deploy":
                r = hotspot_roam.deploy(
                    s,
                    ssid=(args.get("ssid") or None),
                    password=(args.get("password") or None),
                    settle=float(args.get("settle") or 8),
                    ping=bool(args.get("ping", True)))
                side = True                     # deploy 会真开热点
            elif action == "clients":
                r = hotspot_roam.clients_only(
                    s, ping=bool(args.get("ping", False)))
                side = False
            else:
                r = hotspot_roam.diff_only(serial=s)
                side = False
            if not r.get("ok"):
                return R.error(cap, r.get("err") or "热点编排失败",
                               device=self.name, via=self.via)
            return R.success(cap, self.name, side_effect=side, via=self.via,
                             data=r)

        if cap == "sentinel.watch":
            return creative.sentinel_watch(s, arm=bool(args.get("arm")))

        if cap == "scene.run":
            return creative.scene_run(s, (args.get("name") or "").strip())

        # ---- 采集层（L2/L3/L5：让编队真的去「获取」，不只是分析已有信息）----
        if cap == "phone.sense":
            return acquire.phone_sense(s)

        if cap == "phone.see":
            return acquire.phone_see(
                s,
                prompt=(args.get("prompt") or None),
                mode=(args.get("mode") or "structured"),
                log=bool(args.get("log", True)),
                camera_id=int(args.get("camera_id") or 0))

        if cap == "life.log":
            return acquire.life_log(
                s,
                prompt=(args.get("prompt") or None),
                mode=(args.get("mode") or "structured"))

        if cap == "life.digest":
            return acquire.life_digest(
                period=(args.get("period") or "day"),
                limit=int(args.get("limit") or 400))

        if cap == "life.sentinel":
            return acquire.life_sentinel(
                baseline_frames=int(args.get("baseline_frames") or 20))

        # ---- 距离感知（P0）----
        if cap == "proximity.estimate":
            r = RADIO.wifi_survey(s, rounds=int(args.get("rounds") or 1))
            if not r.get("ok"):
                return R.unavailable(cap, r.get("err") or "WiFi 扫描失败",
                                     device=self.name, via=self.via)
            est = proximity.estimate_aps(r["aps"])
            return R.success(cap, self.name, via=self.via, data={
                "count": len(est), "targets": est,
                "calibrated": bool(proximity.load_calib()),
                "note": "距离是区间不是单点（多径 ±6dBm → 误差可达一倍，物理限制）"
                         if proximity.load_calib() else
                         "距离是区间不是单点；未校准（默认参数），拿已知距离样本跑 "
                         "proximity.calibrate 可标定",
                "text": "\n".join(
                    f"{t['ssid'] or t['bssid']}（{t['bssid']}）{t['rssi']}dBm "
                    f"→ {t['text']}" for t in est[:12])})

        if cap == "proximity.track":
            seconds = float(args.get("seconds") or 30)
            interval = float(args.get("interval") or 1.5)
            targets = args.get("targets") or None
            res = proximity.track(proximity.ap_sampler(s), seconds=seconds,
                                  interval=interval, targets=targets)
            if not res:
                return R.unavailable(cap, "跟踪期内没采到任何 AP",
                                     device=self.name, via=self.via)
            lines = proximity.track_report(res)
            return R.success(cap, self.name, via=self.via, data={
                "seconds": seconds, "target_count": len(res),
                "lines": lines,
                "note": "趋势看整段净变化；采样间隔别低于 1s（WiFi 扫描本身 4-6 秒一回合）",
                "text": "\n".join(lines)})

        if cap == "proximity.calibrate":
            mode = (args.get("mode") or "run").strip()
            if mode == "add":
                dist = args.get("dist_m")
                rssi = args.get("rssi")
                if dist is None or rssi is None:
                    return R.error(cap, "缺参数：add 模式要 dist_m（已知距离，米）",
                                   device=self.name, via=self.via)
                band = (args.get("band") or "2.4G").strip()
                proximity.add_calib_sample(float(dist), int(rssi), band=band,
                                           bssid=(args.get("bssid") or ""))
                n = len(proximity.read_calib_samples())
                return R.success(cap, self.name, via=self.via, data={
                    "added": True, "samples_total": n,
                    "note": f"已记录（{band} {rssi}dBm @ {dist}m）；"
                            f"样本共 {n} 条，够 2 个不同距离就能 calibrate"})
            # 默认 run：从样本文件跑回归
            band = (args.get("band") or "2.4G").strip()
            c = proximity.calibrate_from_file(band=band)
            if not c.get("ok"):
                return R.unavailable(cap, c.get("err") or "校准失败",
                                     device=self.name, via=self.via)
            saved = proximity.save_calib({"a_24": c["a"], "a_5": c["a"] - 7.0,
                                          "n": c["n"], "band": band,
                                          "r2": c["r2"], "samples": c["samples"]})
            return R.success(cap, self.name, via=self.via, data={
                **c, "saved": saved,
                "note": "5G 的 A 按比 2.4G 低 7dBm 折算（未分频段校准时先这么用）"
                        if c.get("r2", 0) >= 0.8 else
                        "R²<0.8：拟合差，先查样本（距离记错/中途换位置）再谈标定"})

        return super().invoke(cap, args)
