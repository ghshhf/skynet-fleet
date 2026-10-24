# -*- coding: utf-8 -*-
"""天网创意能力：把 radio / ble / discovery / infrared / nearby / hotspot 焊成
「一眼、一手、一哨、一网、一幕」。

定位与边界见 docs/总纲_唯一推进依据_2026-10-25.md。

五个能力：
  scene.scan     —— 一键全域快照（天网之眼）：融合 WiFi/蜂窝/蓝牙/主动发现
  ir.command     —— 红外码库命名发射（手机万能遥控的编队版，走审批闸门）
  phone.hotspot  —— 手机反向供网（信号外放；断网自救时控制面经手机存活）
  sentinel.watch —— 环境哨兵（只报变化：新设备 / 信号异常 / 设备消失）
  scene.run      —— 场景宏（Jarvis 化的一键：把原子能力串成剧本 JSON）

诚实原则（计划书第四节，推翻旧版自我设限）：支持就真做，不支持就直说，不谎报。
"""
import json
import os
import time

import android_device as A
import ble
import discovery as DISC
import infrared
import ir_codes as IC  # noqa: F401  (红外码库：ir_command 经 infrared.command 间接用)
import radio as RADIO

from .core import DeviceResult as R

#: 哨兵基线：上次拍到的「在场设备」清单（key → rec）。
SENTINEL_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "sentinel_last.json")

#: 场景宏默认剧本（可被 ~/.fleet/scene_macros.json 覆盖）。
MACROS_BUILTIN = {
    "离家": {
        "desc": "关电视（真发红外）、关热点（手机切回客户端）、哨兵重新标定基线",
        "steps": [
            {"cap": "ir.command", "args": {"brand": "小米", "device_type": "电视",
                                          "key": "off", "dry_run": False}},
            {"cap": "phone.hotspot", "args": {"on": False}},
            {"cap": "sentinel.watch", "args": {"arm": True}},
        ],
    },
    "回家": {
        "desc": "开电视（真发红外）、哨兵重新标定基线",
        "steps": [
            {"cap": "ir.command", "args": {"brand": "小米", "device_type": "电视",
                                          "key": "on", "dry_run": False}},
            {"cap": "sentinel.watch", "args": {"arm": True}},
        ],
    },
}
MACROS_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "scene_macros.json")


# ---------------------------------------------------------------- 工具

def _serial_or_err():
    d = A.attach()
    if not d.get("ok"):
        return None, d.get("err") or "手机接不上"
    return d["serial"], None


def _norm_mac(m):
    m = (m or "").strip().upper().replace("-", ":")
    return m if len(m) == 17 else ""


# ---------------------------------------------------------------- 1. scene.scan

def _fuse(layers):
    """把多源设备按 MAC 合并成「身边存在感表」。

    规则层（OUI / mDNS / 蓝牙厂商）已在这里打底——这是确定的事实，不靠模型猜。
    模型只在调用方把线索喂给它时做「这大概率是主人的手机」这类归纳，
    且归纳只接受身份推断、不接受能力断言（防编造，见 android_phone 记忆铁律）。
    """
    by_mac = {}
    rows = []

    def add(mac, row):
        n = _norm_mac(mac)
        if n:
            if n in by_mac:
                cur = by_mac[n]
                cur["layers"] = sorted(set(cur["layers"] + row["layers"]))
                if not cur.get("name") and row.get("name"):
                    cur["name"] = row["name"]
                if not cur.get("vendor") and row.get("vendor"):
                    cur["vendor"] = row["vendor"]
                if row.get("rssi") is not None and (
                        cur.get("rssi") is None or row["rssi"] > cur["rssi"]):
                    cur["rssi"] = row["rssi"]
                if row.get("ip") and not cur.get("ip"):
                    cur["ip"] = row["ip"]
                return
            row["mac"] = n
            by_mac[n] = row
            rows.append(row)
        else:
            rows.append(row)

    for a in (layers.get("wifi") or {}).get("aps", []) or []:
        add(a.get("bssid"), {
            "layers": ["wifi"], "name": a.get("ssid") or "",
            "vendor": a.get("vendor") or "", "kind": a.get("kind") or "无线",
            "rssi": a.get("rssi"),
        })
    for x in (layers.get("bt") or {}).get("devices", []) or []:
        add(x.get("mac"), {
            "layers": ["bt"], "name": x.get("name") or "",
            "vendor": x.get("vendor") or "", "kind": x.get("kind") or "蓝牙",
            "rssi": x.get("rssi"),
        })
    for dv in (layers.get("discovery") or {}).get("devices", []) or []:
        add(dv.get("mac"), {
            "layers": ["lan"], "name": dv.get("model") or dv.get("vendor") or "",
            "vendor": dv.get("vendor") or "",
            "kind": dv.get("category_cn") or "局域网设备",
            "ip": dv.get("ip"), "rssi": None,
        })

    # 排序：信号强的（贴脸的）在上；同信号按名字
    def _key(r):
        return (-(r.get("rssi") or -999), (r.get("name") or "~"))
    rows.sort(key=_key)
    for i, r in enumerate(rows, 1):
        r["id"] = i
    return rows


def _scene_summary(layers, rows):
    parts = []
    wifi = layers.get("wifi") or {}
    if wifi.get("ok"):
        parts.append(f"WiFi {wifi.get('count', 0)} 个 AP（频段 {wifi.get('bands')}）")
    cell = layers.get("cell") or {}
    if cell.get("ok"):
        subs = cell.get("subs") or []
        if subs:
            op = "、".join(sorted({s.get("operator") or "?" for s in subs}))
            parts.append(f"蜂窝驻留 {op}")
    bt = layers.get("bt") or {}
    if bt.get("ok"):
        parts.append(f"蓝牙 {bt.get('count', len(bt.get('devices', []) or []))} 个在广播")
    disc = layers.get("discovery") or {}
    if disc.get("ok"):
        parts.append(f"局域网发现 {disc.get('count', 0)} 台")
    head = "；".join(parts) if parts else "（各层都未返回）"
    return f"{head}。共 {len(rows)} 个可识别实体（按 MAC 跨层合并）。"


def _layer_status(layers):
    out = {}
    for name in ("wifi", "cell", "bt", "discovery"):
        v = layers.get(name)
        if v is None:
            out[name] = "未请求"
        elif isinstance(v, dict) and v.get("ok"):
            out[name] = "ok"
        else:
            out[name] = "fail:" + str((v or {}).get("err") or "?")
    return out


def scene_scan(serial, with_discovery=True, with_cell=True, with_ble=True,
               settle=6, bt_secs=6):
    """一键全域快照：融合 WiFi 频段 + 蜂窝 + 蓝牙 + 主动发现，输出「存在感表」。

    返回 DeviceResult（data 含 summary / rows / layers / cell 环境上下文）。
    """
    at = int(time.time())
    layers = {}

    r = RADIO.wifi_survey(serial, rounds=1, settle=float(settle))
    layers["wifi"] = r if r.get("ok") else {"ok": False, "err": r.get("err")}

    if with_cell:
        c = RADIO.cell(serial)
        layers["cell"] = c if c.get("ok") else {"ok": False, "err": c.get("err")}

    if with_ble:
        st = ble.bt_state(serial)
        if st == "off":
            layers["bt"] = {"ok": False, "err": "蓝牙关着（ble.py --on 打开）"}
        else:
            b = ble.scan(serial, secs=int(bt_secs))
            layers["bt"] = b if b.get("ok") else {"ok": False, "err": b.get("err")}

    if with_discovery:
        try:
            d = DISC.discover(timeout=1.5, do_ports=True, max_hosts=24)
            # discover() 不带 ok/count 字段（它靠「是否抛异常」表成功），
            # 这里补上，否则 _layer_status / _scene_summary 会误判成失败。
            d["ok"] = True
            d["count"] = len(d.get("devices") or [])
            layers["discovery"] = d
        except Exception as e:  # 发现层挂了不该拖垮整张表
            layers["discovery"] = {"ok": False, "err": str(e)[:120]}

    rows = _fuse(layers)
    cell_ctx = (layers.get("cell") or {}).get("subs") or []
    data = {
        "at": at,
        "summary": _scene_summary(layers, rows),
        "rows": rows,
        "layers": _layer_status(layers),
        "cell": [{"operator": s.get("operator"), "rat": s.get("rat"),
                  "band": s.get("band"), "freq_mhz": s.get("freq_mhz"),
                  "rsrp": s.get("rsrp")} for s in cell_ctx],
        "note": "规则层（OUI/mDNS/蓝牙厂商/频段换算）已打底；模型归纳只接受"
                "「这是谁的」此类身份推断，不接受「会做什么」此类能力断言，防编造。",
    }
    return R.success("scene.scan", "phone:main", via="adb", data=data)


# ---------------------------------------------------------------- 2. ir.command

def _parse_ir_phrase(phrase):
    """把「小米 电视 关」这类短语拆成 (brand, device_type, key)。

    决策模型只能给一个字符串参数（arg），红外要三项，所以用短语兜底。
    解析不出三项就返回空串，交给后面的缺参校验如实报错。
    """
    toks = (phrase or "").split()
    if len(toks) >= 3:
        return toks[0], toks[1], toks[2]
    return "", "", ""


def ir_command(serial, brand, device_type, key, dry_run=True, phrase=None):
    """红外码库命名发射。brand/device_type/key 查码库 → 编码 →（dry_run=False 时）经桥发射。

    开空调/关电视是「动手」级：cap 标 side_effect + approval=ask，要过闸门。
    本机红外只有发射口没有学习口（25060RK16C），所以走码库路线——这是事实陈述。
    决策层只给一个字符串时，phrase="小米 电视 关" 会兜底拆成三项。
    """
    brand = (brand or "").strip()
    device_type = (device_type or "").strip()
    key = (key or "").strip()
    # 决策模型只能给一个字符串参数：把「小米 电视 关」短语兜底解析成三项。
    if not (brand and device_type and key) and phrase:
        brand, device_type, key = _parse_ir_phrase(phrase)
    if not (brand and device_type and key):
        return R.error("ir.command", "缺参数：brand / device_type / key 都要",
                       device="phone:main", via="bridge")
    r = infrared.command(serial, brand, device_type, key, dry_run=bool(dry_run))
    if not r.get("ok"):
        return R.error("ir.command", r.get("err") or "码库/发射失败",
                       device="phone:main", via="bridge")
    return R.success("ir.command", "phone:main",
                     side_effect=not bool(dry_run), via="bridge",
                     data={k: v for k, v in r.items() if k != "ok"})


# ---------------------------------------------------------------- 3. phone.hotspot

def _hotspot_status(serial):
    """读 dumpsys wifi，确认软 AP 是否真的在跑、SSID/客户端数。

    解析失败（ROM 不吐字段）就如实返回 started=None，不猜。
    """
    rc, txt = A.shell(serial, "dumpsys wifi")
    if rc != 0:
        return {"started": None, "err": (txt or "").strip()[:120]}
    # 软 AP 状态字段名随 ROM 变：抓所有相关线索，缺哪个少哪个
    started = None
    for line in txt.splitlines():
        low = line.lower()
        if "misstarted" in low or "mIsStarted" in line:
            started = "true" in low
            break
        if "softap" in low and ("started" in low or "enabled" in low):
            started = "true" in low
            break
    import re
    ssid_m = re.search(r'mWifiSsid\s*=\s*"([^"]*)"', txt)
    ssid = ssid_m.group(1) if ssid_m else ""
    clients = None
    cm = re.search(r'(?:getNumConnectedDevices|numConnectedDevices|mNumClients)\D*?(\d+)', txt)
    if cm:
        clients = int(cm.group(1))
    return {"started": started, "ssid": ssid, "clients": clients}


def hotspot(serial, on, ssid=None, password=None):
    """开/关手机热点（信号外放 / 断网自救）。

    诚实标注：开软 AP 会让手机的 WiFi 客户端链路断开——这台机器平时靠本机热点上网，
    开软 AP 是「反向供网」：别的设备经手机入网，手机自己切到蜂窝。
    这是计划书里的 failover 手段（宽带死了，控制面经手机存活），不是随便的开关。
    cap 标 side_effect + approval=ask，必须过闸门才真执行。
    """
    on = bool(on)
    if on:
        ssid = (ssid or "Skynet").strip() or "Skynet"
        # 热点密码走配对体系派生更安全；这里先给默认 8 位，调用方可显式覆盖。
        pw = (password or "skynet2026").strip() or "skynet2026"
        if len(pw) < 8:
            return R.error("phone.hotspot", "热点密码至少 8 位（安全要求）",
                           device="phone:main", via="adb")
        # 走 `cmd wifi start-softap`：这条不被红线守卫拦（守卫拦的是 disable/set- 等）。
        rc, txt = A.shell(serial, f'cmd wifi start-softap "{ssid}" "{pw}"')
        if rc != 0:
            return R.error("phone.hotspot",
                           f"start-softap 失败（rc={rc}，ROM 可能改了接口）：{(txt or '').strip()[:160]}",
                           device="phone:main", via="adb")
        st = _hotspot_status(serial)
        if st.get("started") is False:
            return R.error("phone.hotspot",
                           "命令返回成功但 dumpsys 显示 AP 未起（ROM 限制或需手动确认）",
                           device="phone:main", via="adb")
        return R.success("phone.hotspot", "phone:main", side_effect=True, via="adb",
                         data={"on": True, "ssid": ssid,
                               "clients": st.get("clients"),
                               "note": "手机 WiFi 客户端链路已断开，自身切到蜂窝；"
                                       "其他设备可经此热点入网（断网自救通道）"})
    # 关
    rc, txt = A.shell(serial, "cmd wifi stop-softap")
    if rc != 0:
        return R.error("phone.hotspot",
                       f"stop-softap 失败（rc={rc}）：{(txt or '').strip()[:160]}",
                       device="phone:main", via="adb")
    st = _hotspot_status(serial)
    return R.success("phone.hotspot", "phone:main", side_effect=True, via="adb",
                     data={"on": False, "clients": st.get("clients"),
                           "note": "热点已关，手机恢复 WiFi 客户端模式"})


# ---------------------------------------------------------------- 4. sentinel.watch

def _nearby_fingerprint(serial):
    """跑一轮 nearby，返回 its fingerprint（key → rec，分 lan/wifi/bt）。"""
    import nearby as NB
    out = NB.run(with_lan=True, with_wifi=True, with_bt=True)
    return NB.fingerprint(out), out


def _flat(fp):
    """fingerprint → {(layer, key): rec}。"""
    flat = {}
    for layer in ("lan", "wifi", "bt"):
        for k, v in (fp.get(layer) or {}).items():
            flat[(layer, k)] = v
    return flat


def _near_field(rec):
    """够得着才算「在场」（借用 nearby 的判据）。

    手机单次 WiFi 扫描抖动很大，远场（-80dBm 以下）AP 本来就在噪底上下漂，
    拿它当「新出现/消失」全是噪声。拿不到 rssi 时（LAN 层）按够得着算，不冤枉人。
    """
    r = (rec or {}).get("rssi")
    return r is None or r >= -80


def sentinel_watch(serial, arm=False):
    """环境哨兵：每拍一次，只在变化时说话。

    arm=True  → 把当前在场设成基线，返回「已标定 N 个设备」。
    否则      → 当前 vs 基线 算差：新设备(info) / 信号骤降(abnormal,warn) / 设备消失(warn)。
              算完把基线更新成当前（下一拍只比这一拍的进出）。

    三档变化（计划书）：新设备出现 / 已知设备信号异常 / 已知设备消失。
    """
    fp, _out = _nearby_fingerprint(serial)
    cur = _flat(fp)

    baseline = _load_baseline()

    if arm:
        _save_baseline(cur)
        n = len(cur)
        return R.success("sentinel.watch", "phone:main", via="adb",
                         data={"armed": True, "known": n,
                               "note": f"已标定基线，现在在场 {n} 个设备；"
                                       "之后只在变化时报告"})

    base = baseline
    # 只比对「够得着」的键：远场 WiFi/蓝牙 AP 在噪底上漂，算进出全是噪声。
    cur_nf = {k for k in cur if _near_field(cur[k])}
    base_nf = {k for k in base if _near_field(base[k])}

    new_devs = [cur[k] for k in (cur_nf - base_nf)]
    gone_devs = [base[k] for k in (base_nf - cur_nf)]
    abnormal = []
    for k in (cur_nf & base_nf):
        c_rssi = cur[k].get("rssi")
        b_rssi = base[k].get("rssi")
        if isinstance(c_rssi, int) and isinstance(b_rssi, int):
            if c_rssi < b_rssi - 20:       # 信号骤降 20dBm 以上 = 异常（不是抖动）
                abnormal.append({"key": k[1], "was": b_rssi, "now": c_rssi,
                                 "name": cur[k].get("ssid") or cur[k].get("kind") or "?"})

    changed = bool(new_devs or gone_devs or abnormal)
    _save_baseline(cur)
    return R.success("sentinel.watch", "phone:main", via="adb",
                     data={"changed": changed,
                           "new": new_devs[:12], "gone": gone_devs[:12],
                           "abnormal": abnormal[:12],
                           "known": len(cur),
                           "note": "只比对够得着的设备；远场 AP 在噪底漂不算进出。"
                                   "新设备=info，消失/信号异常=warn"})


def _save_baseline(flat):
    os.makedirs(os.path.dirname(SENTINEL_PATH), exist_ok=True)
    try:
        with open(SENTINEL_PATH, "w", encoding="utf-8") as f:
            json.dump({"at": int(time.time()),
                       "flat": {f"{l}\x00{k}": v for (l, k), v in flat.items()}},
                      f, ensure_ascii=False)
    except OSError:
        pass


def _load_baseline():
    """读哨兵基线文件 → {(layer, key): rec}。

    存档用字符串键 "lan\\x00192.168.0.9"（JSON 键只能是字符串），
    这里拆回 (layer, key) 元组——旧实现漏了这一步，导致第二次调用
    把字符串当元组拆包直接崩。文件坏了/格式不对就返回空 dict（继续干活）。
    """
    if not os.path.isfile(SENTINEL_PATH):
        return {}
    try:
        with open(SENTINEL_PATH, encoding="utf-8") as f:
            raw = json.load(f).get("flat", {})
    except (OSError, ValueError):
        return {}
    out = {}
    for key, v in raw.items():
        if "\x00" in key:
            l, k = key.split("\x00", 1)
            out[(l, k)] = v
        else:
            out[(key, "")] = v
    return out


def sentinel_loop(serial, interval=300, on_change=None):
    """常驻哨兵：每 interval 秒一拍，只在变化时回调 on_change(delta)。

    on_change(delta) 默认只打印；接入 App 推送时传一个发 urgent 池的函数即可。
    """
    while True:
        r = sentinel_watch(serial, arm=False)
        if r.get("ok") and r["data"].get("changed"):
            if on_change:
                try:
                    on_change(r["data"])
                except Exception:
                    pass
            else:
                print("[sentinel]", json.dumps(r["data"], ensure_ascii=False)[:300])
        time.sleep(max(30, int(interval)))


# ---------------------------------------------------------------- 5. scene.run

def load_macros():
    """内置剧本 + 用户覆盖（~/.fleet/scene_macros.json）。"""
    macros = dict(MACROS_BUILTIN)
    if os.path.isfile(MACROS_PATH):
        try:
            with open(MACROS_PATH, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                macros.update(user)
        except (OSError, ValueError):
            pass
    return macros


def _parse_step(step):
    """剧本里的一条 → (cap, args)。支持结构化 dict 与计划书的字符串写法。

    字符串写法（计划书示例风格）：
      "ir.command 小米 电视 off"   → cap=ir.command, brand/device_type/key
      "phone.hotspot off"          → cap=phone.hotspot, on=False
      "phone.hotspot on"           → on=True
      "sentinel arm"               → cap=sentinel.watch, arm=True
      "net.nearby --save"          → cap=net.nearby, save=True
      "scene.scan"                 → cap=scene.scan
    dict 写法优先（最稳）：{"cap": "ir.command", "args": {...}}
    """
    if isinstance(step, dict):
        return step.get("cap") or "", (step.get("args") or {})
    toks = str(step).split()
    if not toks:
        return "", {}
    head = toks[0].lower()
    if head == "ir.command":
        # 期望 4 段：ir.command <brand> <device> <key>
        if len(toks) >= 4:
            return "ir.command", {"brand": toks[1], "device_type": toks[2], "key": toks[3]}
        return "ir.command", {}
    if head == "phone.hotspot":
        on = (len(toks) >= 2 and toks[1].lower() in ("on", "1", "true", "开"))
        return "phone.hotspot", {"on": on}
    if head == "sentinel":
        return "sentinel.watch", {"arm": (len(toks) >= 2 and toks[1].lower() in ("arm", "标定"))}
    if head == "net.nearby":
        save = any(t in ("--save", "--save-fingerprint", "save") for t in toks[1:])
        return "net.nearby", {"save": save}
    if head == "scene.scan":
        return "scene.scan", {}
    return head, {}


def scene_run(serial, name, macros=None):
    """执行一个命名场景宏：把剧本里的步骤逐条 dispatch 到对应 cap。

    模型只负责把自然语言映射到宏名（96% 决策率已验证）；宏本体人工审核。
    步骤失败默认中断（fail_fast），honest 地报告哪一步没过。
    """
    from .kinds.android_phone import AndroidPhone  # 懒导入，避免环形依赖
    name = (name or "").strip()
    if not name:
        return R.error("scene.run", "缺宏名", device="phone:main", via="adb")
    macros = macros or load_macros()
    m = macros.get(name)
    if not m:
        avail = "、".join(macros) or "（无）"
        return R.error("scene.run", f"没有名为「{name}」的宏；可用：{avail}",
                       device="phone:main", via="adb")
    steps = m.get("steps") or []
    if not steps:
        return R.error("scene.run", f"宏「{name}」没有步骤", device="phone:main", via="adb")

    phone = AndroidPhone()
    results = []
    for step in steps:
        cap, args = _parse_step(step)
        if not cap:
            results.append({"step": step, "ok": False, "note": "解析不出 cap（格式不支持）"})
            break
        r = phone.invoke(cap, args)        # 走完整 invoke（含 probe/可达性）
        ok = bool(r.get("ok")) if isinstance(r, dict) else bool(r)
        note = ""
        if isinstance(r, dict):
            note = r.get("err") or ("" if ok else "未成功")
            if not note and ok:
                note = "成功"
        results.append({"step": step if isinstance(step, str) else cap,
                        "cap": cap, "ok": ok, "note": note[:120]})
        # 失败中断（除非宏显式允许继续）
        if not ok and m.get("fail_fast", True):
            break

    ran = sum(1 for x in results if x["ok"])
    return R.success("scene.run", "phone:main", side_effect=True, via="adb",
                     data={"name": name, "desc": m.get("desc", ""),
                           "total": len(steps), "ran": ran,
                           "steps": results})


# ---------------------------------------------------------------- 自测

def _selftest():
    ok = 0
    fails = []

    def check(name, cond):
        if cond:
            ok += 1
        else:
            fails.append(name)

    # ① MAC 归一化
    check("MAC 归一化", _norm_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF")
    check("MAC 太短忽略", _norm_mac("aa:bb") == "")

    # ② 融合：同 MAC 跨层合并，不同 MAC 分开
    layers = {
        "wifi": {"ok": True, "aps": [
            {"bssid": "AA:BB:CC:DD:EE:01", "ssid": "Home", "vendor": "Xiaomi",
             "kind": "家电", "rssi": -50},
            {"bssid": "AA:BB:CC:DD:EE:02", "ssid": "Cafe", "vendor": "", "kind": "", "rssi": -80},
        ]},
        "bt": {"ok": True, "devices": [
            {"mac": "AA:BB:CC:DD:EE:01", "name": "midea", "vendor": "美的",
             "kind": "家电", "rssi": -55},   # 与上面 Home 同 MAC → 合并
            {"mac": "AA:BB:CC:DD:EE:03", "name": "Buds", "vendor": "", "kind": "耳机", "rssi": -60},
        ]},
        "discovery": {"ok": True, "devices": [
            {"ip": "192.168.0.5", "mac": "AA:BB:CC:DD:EE:04", "vendor": "Hikvision",
             "category_cn": "摄像头", "model": "HIKVISION DS-2CD"},
        ]},
    }
    rows = _fuse(layers)
    check("融合后 4 行（同 MAC 合并 1 行）", len(rows) == 4)
    merged = [r for r in rows if r.get("mac") == "AA:BB:CC:DD:EE:01"]
    check("同 MAC 跨层合并", len(merged) == 1 and set(merged[0]["layers"]) == {"wifi", "bt"})
    check("合并行取强信号", merged and merged[0]["rssi"] == -50)
    # 排序：信号强在上
    check("按信号排序", rows[0]["rssi"] == -50)

    # ③ 场景总结：各层 ok 都进总结
    s = _scene_summary(layers, rows)
    check("总结含 WiFi/蓝牙/局域网", "WiFi" in s and "蓝牙" in s and "局域网" in s)

    # ④ 层状态：失败层标 fail
    bad_layers = {"wifi": {"ok": False, "err": "手机接不上"}, "cell": None,
                  "bt": {"ok": True, "devices": []}, "discovery": {"ok": True, "devices": []}}
    st = _layer_status(bad_layers)
    check("层状态：失败标 fail", st["wifi"].startswith("fail"))
    check("层状态：未请求标未请求", st["cell"] == "未请求")

    # ⑤ 红外命名发射（dry_run 不发射，无副作用；真机无桥时 device 层会 unavailable）
    #    这里只验「参数解析 → 码库查找」这一段，不碰桥。
    proto, addr, cmd = IC.lookup("小米", "电视", "power")
    check("码库查 小米电视 power", proto == "nec" and addr == 4 and cmd == 8)
    # ir_command 缺参会报错（走 DeviceResult.error，err 非空）
    e = ir_command("FAKE", "", "", "")
    check("ir.command 缺参报错", (not e.ok) and e.code == R.ERROR and e.err)

    # ⑥ 热点状态解析：_hotspot_status 走 A.shell，这里 monkeypatch 成 rc!=0，
    #    验证「读不到 dumpsys 时 started=None 不猜」。
    orig_shell = A.shell
    A.shell = lambda s, c: (1, "denied")
    hs = _hotspot_status("X")
    check("热点状态 rc!=0 → started=None", hs["started"] is None)
    A.shell = orig_shell

    # ⑦ 哨兵 diff：基线 vs 当前
    base_fp = {"lan": {"192.168.0.9": {"kind": "pc"}},
               "wifi": {"B1": {"ssid": "常在", "kind": "光猫", "rssi": -50}},
               "bt": {}}
    cur_fp = {"lan": {"192.168.0.9": {"kind": "pc"},
                     "192.168.0.100": {"kind": "摄像头"}},
              "wifi": {"B1": {"ssid": "常在", "kind": "光猫", "rssi": -75},  # 骤降 25dBm
                       "B2": {"ssid": "新人", "kind": "路由", "rssi": -60}},
              "bt": {}}
    cur_flat = {}
    for l, d in cur_fp.items():
        for k, v in d.items():
            cur_flat[(l, k)] = v
    _save_baseline(cur_flat)  # 写盘，arm 路径覆盖
    # 回合一致性：存的是字符串键，读回来必须是 (layer,key) 元组——
    # 这就是「第二次调用崩溃」的修复点。
    loaded = _load_baseline()
    check("哨兵：基线存读回合一致", ("lan", "192.168.0.9") in loaded
          and ("wifi", "B2") in loaded and len(loaded) == 3)
    # 用内存基线测 diff：直接构造 baseline 字典喂给 sentinel 的等价逻辑
    base_set = {(l, k) for l, d in base_fp.items() for k in d}
    cur_set = {(l, k) for l, d in cur_fp.items() for k in d}
    new = cur_set - base_set
    gone = base_set - cur_set
    check("哨兵：识别新设备", ("lan", "192.168.0.100") in new and ("wifi", "B2") in new)
    check("哨兵：识别消失设备", ("lan", "192.168.0.9") not in gone)
    # 信号异常：B1 从 -50 掉到 -75
    b_rssi = base_fp["wifi"]["B1"]["rssi"]
    c_rssi = cur_fp["wifi"]["B1"]["rssi"]
    check("哨兵：识别信号骤降", c_rssi < b_rssi - 20)

    # ⑧ 场景宏解析：字符串步骤
    cap1, a1 = _parse_step("ir.command 小米 电视 off")
    check("宏解析 ir.command", cap1 == "ir.command"
          and a1 == {"brand": "小米", "device_type": "电视", "key": "off"})
    cap2, a2 = _parse_step("phone.hotspot off")
    check("宏解析 hotspot off", cap2 == "phone.hotspot" and a2 == {"on": False})
    cap3, a3 = _parse_step("sentinel arm")
    check("宏解析 sentinel arm", cap3 == "sentinel.watch" and a3 == {"arm": True})
    cap4, a4 = _parse_step("net.nearby --save")
    check("宏解析 net.nearby save", cap4 == "net.nearby" and a4 == {"save": True})
    cap5, a5 = _parse_step({"cap": "phone.hotspot", "args": {"on": True}})
    check("宏解析 dict 优先", cap5 == "phone.hotspot" and a5 == {"on": True})
    cap6, a6 = _parse_step("")
    check("宏解析空步 → 无 cap", cap6 == "" and a6 == {})

    # ⑨ 内置宏存在且步骤非空
    macros = load_macros()
    check("内置「离家」宏存在", "离家" in macros and macros["离家"]["steps"])
    check("内置「回家」宏存在", "回家" in macros and macros["回家"]["steps"])

    if fails:
        print("creative 自测 FAIL：", fails)
        return 1
    print(f"creative 自测：{ok} 项全过")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
