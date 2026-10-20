"""手机状态快照：一次问清「它现在怎么样」。全部只读，一条 adb 命令组。

为什么单独一层，而不是继续往 android_device 里加函数：
android_device 管的是「怎么跟设备说话」（连接/传输/红线），phone_ops 管「动一次手」，
这里管的是「它此刻是什么状态」——电量、网络、声音、屏幕、传感器、存储。三件事的
返回形状和失败语义完全不同：状态是**可部分失败**的（读不到传感器不该让电量也消失）。

三条硬规矩：
  · 只读。所有命令里没有一个 write/put/set/svc，跑一万次也不改变手机任何状态。
  · 分节失败各自降级：哪一节读不出来就进 missing，其余照常返回——一份缺两节的
    快照远好过「整份失败」，因为上层要的是「能看到的全部」。
  · 不猜。读不到就是读不到（例如 cmd clipboard 在这台机器上明确回
    "No shell command implementation."），不许编一个空值冒充「没内容」。

    python phone_state.py            # 人读报告
    python phone_state.py --json     # 原始结构
    python phone_state.py --brief    # 一句话（给她起话题用）
    python phone_state.py --selftest
"""
import json
import re
import sys
import time

import android_device as A

# 电量/充电状态码（frameworks BatteryManager，固定值，不随 ROM 变）
BAT_STATUS = {1: "未知", 2: "充电中", 3: "放电中", 4: "未充电", 5: "已充满"}
BAT_HEALTH = {1: "未知", 2: "良好", 3: "过热", 4: "损坏", 5: "过压",
              6: "故障", 7: "过冷"}
# 免打扰（NotificationManager.zen_mode）
ZEN_MODE = {0: "关", 1: "仅优先", 2: "完全静音", 3: "仅闹钟", 4: "驾驶", 5: "睡眠"}
# 铃声模式（AudioManager.ringer_mode）
RINGER = {0: "静音", 1: "震动", 2: "正常"}
# 通话状态（TelephonyManager.call_state）
CALL_STATE = {0: "空闲", 1: "响铃", 2: "通话中"}


# ---------------------------------------------------------------- 采集

# 分批的理由：每条 adb shell 在无线调试下约 200-400ms，14 节若各自往返要 4 秒以上。
# 用 @@@key@@@ 在一条命令里打分隔符，两批就能全部拿回。输出量也分批控制：
# 第二批（dumpsys audio/sensorservice）单条几 KB，混进第一批会把小命令的错位掩盖掉。
_BATCH_A = [
    ("battery", "dumpsys battery"),
    ("wifi", "cmd wifi status"),
    ("cell", "getprop gsm.sim.state; getprop gsm.operator.alpha; "
             "getprop gsm.network.type; getprop gsm.operator.numeric"),
    ("telephony", "dumpsys telephony.registry | grep -E 'mCallState=|mVoiceRegState' | head -2"),
    ("settings", "settings get global zen_mode; settings get global mode_ringer; "
                 "settings get system screen_brightness; "
                 "settings get system screen_brightness_mode; "
                 "settings get system accelerometer_rotation; "
                 "settings get system user_rotation; "
                 "settings get global airplane_mode_on"),
    ("power", "dumpsys power | grep -E 'mWakefulness=|Display Power' | head -4"),
    ("keyguard", "dumpsys window | grep -E 'mDreamingLockscreen|isKeyguardShowing' | head -2"),
]
_BATCH_B = [
    ("audio", "dumpsys audio | grep -A6 '^- STREAM_' | head -70"),
    ("sensors", "dumpsys sensorservice | grep -E '^[0-9]+\\)|type: android.sensor|^Active sensors:'"
                " | head -40"),
    ("storage", "df -k /storage/emulated | tail -2"),
    ("mem", "cat /proc/meminfo | head -3"),
    ("uptime", "cat /proc/uptime"),
    ("recents", "dumpsys activity recents | grep -E 'Recent #|intent=' | head -20"),
    ("clip", "cmd clipboard get"),
]

_MARK = "@@@fleet@@@"


def _script(probes):
    """把若干条命令拼成一次 shell 调用，每条前打一个标记。

    标记串刻意取得不含 shell 元字符：SSID 里出现 `$`、`&` 时不会把命令拼坏。
    """
    out = []
    for key, cmd in probes:
        out.append(f"echo {_MARK}{key}")
        out.append(cmd)
    return "; ".join(out)


_KEYS = {k for k, _ in _BATCH_A + _BATCH_B}


def _split(text):
    """按标记切回 {key: 文本}。缺的节就是空串——上层据此判「没读到」。

    只认自己声明的 key：手机输出里出现巧合的 @@@ 也不会把结构带偏。
    """
    got, cur = {}, None
    for line in text.splitlines():
        line = line.rstrip("\r")
        s = line.strip()
        if s.startswith(_MARK):
            key = s[len(_MARK):].strip()
            if key in _KEYS:
                cur = key
                got[cur] = []
                continue
        if cur:
            got[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in got.items()}


def collect(serial):
    """真机采集：两批命令，返回 {key: 原始文本}。设备不可达时抛 RuntimeError。"""
    res = {}
    for probes in (_BATCH_A, _BATCH_B):
        rc, txt = A.shell(serial, _script(probes), timeout=45)
        if rc != 0 and not txt.strip():
            raise RuntimeError(txt.strip()[:200] or f"adb shell 返回码 {rc}")
        res.update(_split(txt))
    return res


# ---------------------------------------------------------------- 分节解析
# 每个 parser 只认自己那段文本，不做跨节推断；读不出来就抛 ValueError（进 missing）。

def parse_battery(raw):
    if not raw:
        raise ValueError("没有 dumpsys battery 输出")
    num = lambda k: (re.search(rf"^\s*{k}:\s*(-?\d+)", raw, re.M) or [None, None])[1]
    plug = [n for n, pat in (("AC", r"AC powered:\s*true"), ("USB", r"USB powered:\s*true"),
                             ("无线", r"Wireless powered:\s*true"),
                             ("底座", r"Dock powered:\s*true")) if re.search(pat, raw)]
    st = num("status")
    st = int(st) if st is not None else 0
    lv = num("level")
    out = {
        "level": int(lv) if lv is not None else None,
        "status_code": st,
        "status": BAT_STATUS.get(st, f"未知({st})"),
        "plugged": plug,
        "charging": st in (2, 5) or (bool(plug) and st == 4),
        "temperature": (int(num("temperature")) if num("temperature") is not None else None),
        "health": BAT_HEALTH.get(int(num("health") or 0), "未知"),
        "technology": (re.search(r"technology:\s*(\S+)", raw) or [None, ""])[1],
    }
    # 「插着电却在放电」是真实存在的一类（充电上限/慢充/PD 协商失败），
    # 不能靠 charging 字段掩盖——它恰恰是最该被说出来的那一种。
    out["anomaly"] = ""
    if plug and st == 3:
        out["anomaly"] = "插着电但报放电（充电上限或充电器功率不足）"
    if out["temperature"] is not None and out["temperature"] >= 450:
        out["anomaly"] = (out["anomaly"] + "；" if out["anomaly"] else "") + "温度偏高"
    return out


def _rssi_quality(rssi):
    if rssi is None:
        return ""
    if rssi >= -50:
        return "很强"
    if rssi >= -60:
        return "好"
    if rssi >= -70:
        return "一般"
    if rssi >= -80:
        return "弱"
    return "很差"


def parse_wifi(raw):
    if not raw:
        raise ValueError("没有 cmd wifi status 输出")
    if "No shell command implementation" in raw:
        raise ValueError("这个 ROM 不支持 cmd wifi status")
    out = {"enabled": ("Wifi is enabled" in raw),
           "ssid": "", "bssid": "", "rssi": None, "quality": "",
           "freq_mhz": None, "band": "", "link_mbps": None, "supplicant": ""}
    m = re.search(r'Wifi is connected to "([^"]*)"', raw)
    if m:
        out["ssid"] = m.group(1)
    for key, pat, cast in (("bssid", r"BSSID:\s*([0-9a-fA-F:]{17})", str),
                           ("rssi", r"RSSI:\s*(-?\d+)", int),
                           ("freq_mhz", r"Frequency:\s*(\d+)", int),
                           ("link_mbps", r"Link speed:\s*(-?\d+)Mbps", int),
                           ("supplicant", r"Supplicant state:\s*(\w+)", str)):
        mm = re.search(pat, raw)
        if mm:
            out[key] = cast(mm.group(1))
    out["quality"] = _rssi_quality(out["rssi"])
    f = out["freq_mhz"]
    if f:
        out["band"] = "2.4GHz" if f < 2500 else ("5GHz" if f < 5925 else "6GHz")
    return out


def parse_cell(raw):
    """蜂窝：这台没 SIM 也要能说清「没 SIM」，而不是留一堆空字段让人猜。"""
    if raw is None:
        raise ValueError("没有 getprop 输出")
    lines = [x.strip() for x in raw.splitlines()]
    get = lambda i: lines[i] if len(lines) > i else ""
    sim, op, ntype = get(0), get(1), get(2)
    out = {"sim": sim or "?", "operator": op, "network_type": ntype or "Unknown",
           "present": sim.upper() == "READY"}
    return out


def parse_telephony(raw):
    if not raw:
        raise ValueError("没有 telephony.registry 输出")
    cs = re.search(r"mCallState=(\d+)", raw)
    vr = re.search(r"mVoiceRegState=(\d+)\(([^)]*)\)", raw)
    dr = re.search(r"mDataRegState=(\d+)\(([^)]*)\)", raw)
    return {"call_state": CALL_STATE.get(int(cs.group(1)) if cs else -1, "?"),
            "voice_reg": (vr.group(2) if vr else ""),
            "data_reg": (dr.group(2) if dr else "")}


def parse_settings(raw):
    if raw is None:
        raise ValueError("没有 settings 输出")
    v = [x.strip() for x in raw.splitlines()]

    def at(i, default=None):
        return v[i] if len(v) > i and v[i] else default

    def num(s, default=None):
        try:
            return int(s)
        except (TypeError, ValueError):
            return default

    zen = num(at(0), 0)
    ringer = num(at(1), 2)
    return {"zen_mode": zen, "zen": ZEN_MODE.get(zen, f"未知({zen})"),
            "ringer_mode": ringer, "ringer": RINGER.get(ringer, "?"),
            "brightness": num(at(2)),
            "auto_brightness": at(3) == "1",
            "auto_rotate": at(4) == "1",
            "user_rotation": num(at(5), 0),
            "airplane": at(6) == "1"}


def parse_audio(raw):
    """音量：这里只取「音乐/铃声/闹钟/通话」四条，其余流对日常感知没意义。

    dumpsys audio 的流块格式是 `- STREAM_X:` 后面跟若干 `key: value`，
    所以必须按块走，不能全局抓 streamVolume —— 那样四条流会互相覆盖。
    """
    if not raw:
        raise ValueError("没有 dumpsys audio 输出")
    streams, cur = {}, None
    for line in raw.splitlines():
        m = re.match(r"\s*-\s*STREAM_(\w+):", line)
        if m:
            cur = m.group(1).lower()
            streams[cur] = {}
            continue
        if cur is None:
            continue
        mm = re.match(r"\s*(Muted|Max|streamVolume):\s*(\S+)", line)
        if mm:
            k, val = mm.group(1), mm.group(2)
            streams[cur][k] = (val == "true") if k == "Muted" else int(val or 0)
    keep = {k: streams[k] for k in ("music", "ring", "alarm", "voice_call", "notification")
            if k in streams}
    if not keep:
        raise ValueError("没解析出任何音量流")
    return keep


def parse_power(raw):
    if not raw:
        raise ValueError("没有 dumpsys power 输出")
    w = re.search(r"mWakefulness=(\w+)", raw)
    d = re.search(r"Display Power:\s*state=(\w+)", raw)
    return {"wakefulness": w.group(1) if w else "?",
            "screen_on": bool(d and d.group(1) == "ON")}


def parse_keyguard(raw):
    if not raw:
        raise ValueError("没有 window 输出")
    kg = re.search(r"isKeyguardShowing=(\w+)", raw)
    dream = re.search(r"mDreamingLockscreen=(\w+)", raw)
    locked = (kg and kg.group(1) == "true") or (dream and dream.group(1) == "true")
    return {"locked": bool(locked)}


def parse_sensors(raw):
    """传感器：只报「有哪些 + 哪些正在跑」。

    型号/厂商/minRate 这类东西对编队的判断没有用；有用的是「有没有光感/距离/加速度」
    ——决定上层能不能问「现在房间亮不亮」「手机在不在兜里」。
    """
    if not raw:
        raise ValueError("没有 sensorservice 输出")
    found = {}
    for line in raw.splitlines():
        m = re.match(r"\s*\d+\)\s*(.+?)\s*\|.*type:\s*android\.sensor\.(\w+)", line)
        if m:
            found.setdefault(m.group(2), m.group(1))
    active_block, act = [], False
    for line in raw.splitlines():
        if "Active sensors:" in line:
            act = True
            continue
        if act and line.strip():
            active_block.append(line.strip())
    if not found:
        raise ValueError("传感器列表空（可能是权限或 ROM 限制）")
    return {"count": len(found), "types": sorted(found),
            "names": {k: found[k] for k in sorted(found)},
            "active": len(active_block)}


def parse_storage(raw):
    if not raw:
        raise ValueError("没有 df 输出")
    line = [x for x in raw.strip().splitlines() if re.search(r"\d+%\s+/", x)]
    if not line:
        raise ValueError("df 输出里没有可用行")
    parts = line[-1].split()
    if len(parts) < 6:
        raise ValueError("df 行字段不够")
    total_kb, used_kb, avail_kb = int(parts[1]), int(parts[2]), int(parts[3])
    return {"total_gb": round(total_kb / 1048576, 1),
            "used_gb": round(used_kb / 1048576, 1),
            "free_gb": round(avail_kb / 1048576, 1),
            "used_pct": int(parts[4].rstrip("%"))}


def parse_mem(raw):
    if not raw:
        raise ValueError("没有 meminfo 输出")
    g = lambda k: (re.search(rf"{k}:\s*(\d+)", raw) or [None, None])[1]
    total, avail = g("MemTotal"), g("MemAvailable")
    if not total:
        raise ValueError("meminfo 里没有 MemTotal")
    return {"total_mb": int(total) // 1024,
            "avail_mb": (int(avail) // 1024) if avail else None}


def parse_uptime(raw):
    if not raw:
        raise ValueError("没有 /proc/uptime")
    try:
        sec = float(raw.split()[0])
    except (ValueError, IndexError):
        raise ValueError(f"uptime 读不懂：{raw[:40]!r}")
    return {"seconds": int(sec), "human": human_dur(sec)}


def parse_recents(raw):
    if not raw:
        raise ValueError("没有 recents 输出")
    apps = []
    for line in raw.splitlines():
        m = re.search(r"Recent #\d+.*?A=\d+:([\w.]+)", line)
        if m and m.group(1) not in apps:
            apps.append(m.group(1))
    if not apps:
        raise ValueError("最近任务里没读出包名")
    return {"apps": apps[:8]}


def parse_clip(raw):
    """剪贴板：Android 10 起 shell 无读取实现，这是系统限制，不是 bug。

    宁可明确报「这台读不到」，也不能返回空串让人以为剪贴板是空的。
    """
    if raw is None:
        raise ValueError("没有执行")
    if "No shell command implementation" in raw or not raw.strip():
        raise ValueError("系统没有 cmd clipboard 实现（Android 10+ 限制）")
    return {"text": raw.strip()[:200]}


_PARSERS = {
    "battery": parse_battery, "wifi": parse_wifi, "cell": parse_cell,
    "telephony": parse_telephony, "settings": parse_settings, "audio": parse_audio,
    "power": parse_power, "keyguard": parse_keyguard, "sensors": parse_sensors,
    "storage": parse_storage, "mem": parse_mem, "uptime": parse_uptime,
    "recents": parse_recents, "clip": parse_clip,
}


def human_dur(sec):
    sec = max(int(sec or 0), 0)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分钟"


# ---------------------------------------------------------------- 组装

def snapshot(serial=None):
    """完整状态。设备连不上 -> ok=False；连上但某节读不出 -> 该节进 missing。"""
    if not serial:
        d = A.attach()
        if not d.get("ok"):
            return {"ok": False, "at": time.time(),
                    "err": d.get("err") or "手机接不上"}
        serial = d["serial"]
    try:
        raw = collect(serial)
    except Exception as e:
        return {"ok": False, "at": time.time(), "err": f"采集失败：{e}"}

    secs, missing = {}, []
    for key, fn in _PARSERS.items():
        txt = raw.get(key)
        if txt is None:                      # 这一节整段没回来（命令被占位吞了）
            missing.append({"section": key, "err": "没取到"})
            continue
        try:
            secs[key] = fn(txt)
        except Exception as e:
            missing.append({"section": key, "err": str(e)[:120]})
    out = {"ok": True, "at": time.time(), "serial": serial,
           "sections": secs, "missing": missing}
    out["anomalies"] = anomalies(out)
    return out


def anomalies(st):
    """值得主动说出来的一件事集合。规则先写成数据，后面 policies 接进去就能用。"""
    s = st.get("sections") or {}
    out = []
    bat = s.get("battery") or {}
    if bat.get("anomaly"):
        out.append(bat["anomaly"])
    lv = bat.get("level")
    if lv is not None and lv <= 20 and not bat.get("plugged"):
        out.append(f"电量只剩 {lv}%，没在充电")
    storage = s.get("storage") or {}
    if storage.get("used_pct", 0) >= 95:
        out.append(f"存储快满了（{storage.get('used_pct')}%）")
    mem = s.get("mem") or {}
    if mem.get("avail_mb") is not None and mem["total_mb"] and \
            mem["avail_mb"] / mem["total_mb"] < 0.10:
        out.append(f"可用内存只剩 {mem['avail_mb']}MB")
    wifi = s.get("wifi") or {}
    if wifi.get("enabled") and not wifi.get("ssid"):
        out.append("WiFi 开着但没连上任何网络")
    return out


def brief(st):
    """一句话人话（她起话题用）。多个片段用「；」连，绝不输出空句。"""
    if not st.get("ok"):
        return f"手机状态没读到：{st.get('err', '')}"
    s = st.get("sections") or {}
    parts = []
    bat = s.get("battery") or {}
    if bat.get("level") is not None:
        seg = f"电 {bat['level']}%"
        if bat.get("plugged"):
            seg += f"（{'+'.join(bat['plugged'])} 供电，{bat.get('status')}）"
        else:
            seg += f"（{bat.get('status')}）"
        parts.append(seg)
    wifi = s.get("wifi") or {}
    if wifi.get("ssid"):
        q = wifi.get("quality") or ""
        band = wifi.get("band") or ""
        tail = "/".join(x for x in (band, q) if x)
        parts.append(f"连 {wifi['ssid']}" + (f"（{tail}）" if tail else ""))
    elif wifi.get("enabled") is False:
        parts.append("WiFi 关着")
    cell = s.get("cell") or {}
    if cell and not cell.get("present"):
        parts.append("没插 SIM")
    tele = s.get("telephony") or {}
    if tele.get("call_state") and tele["call_state"] != "空闲":
        parts.append(f"正在{tele['call_state']}")
    pw = s.get("power") or {}
    kg = s.get("keyguard") or {}
    if pw:
        parts.append("屏幕亮着" if pw.get("screen_on") else "屏幕灭着")
        parts.append("锁着" if kg.get("locked") else "已解锁")
    st2 = s.get("settings") or {}
    if st2:
        if st2.get("zen_mode"):
            parts.append(f"免打扰 {st2.get('zen')}")
        if st2.get("ringer") and st2["ringer"] != "正常":
            parts.append(f"铃声 {st2.get('ringer')}")
    audio = s.get("audio") or {}
    music = (audio.get("music") or {})
    if music and music.get("streamVolume") == 0:
        parts.append("媒体音量为 0")
    if not parts:
        return "手机连上了，但每一项都没读出内容"
    line = "；".join(parts) + "。"
    if st.get("anomalies"):
        line += " 注意：" + "、".join(st["anomalies"]) + "。"
    return line


def report(st):
    """人读报告：按「身体 -> 环境 -> 资源」排，异常单列在最上面。"""
    if not st.get("ok"):
        return f"手机状态没读到：{st.get('err', '')}"
    s = st.get("sections") or {}
    L = []
    if st.get("anomalies"):
        L.append("【要注意】")
        for a in st["anomalies"]:
            L.append(f"  ! {a}")
        L.append("")
    bat = s.get("battery") or {}
    if bat:
        plug = "+".join(bat.get("plugged") or []) or "未插电"
        L.append(f"【电量】{bat.get('level')}%  {bat.get('status')}  {plug}"
                 f"  {bat.get('temperature')}°C  健康={bat.get('health')}")
    pw, kg = s.get("power") or {}, s.get("keyguard") or {}
    if pw:
        L.append(f"【屏幕】{pw.get('wakefulness')}  "
                 f"{'亮' if pw.get('screen_on') else '灭'}  "
                 f"{'锁屏' if kg.get('locked') else '已解锁'}")
    wifi = s.get("wifi") or {}
    if wifi:
        if wifi.get("ssid"):
            L.append(f"【WiFi】{wifi['ssid']}  {wifi.get('band')}  "
                     f"{wifi.get('rssi')}dBm（{wifi.get('quality')}）  "
                     f"{wifi.get('link_mbps')}Mbps  {wifi.get('bssid')}")
        else:
            L.append(f"【WiFi】{'开着' if wifi.get('enabled') else '关着'}，未连接")
    cell = s.get("cell") or {}
    tele = s.get("telephony") or {}
    if cell:
        if cell.get("present"):
            L.append(f"【蜂窝】{cell.get('operator')}  {cell.get('network_type')}  "
                     f"语音={tele.get('voice_reg')}  数据={tele.get('data_reg')}")
        else:
            L.append(f"【蜂窝】没插 SIM（{cell.get('sim')}）")
    if tele:
        L.append(f"【通话】{tele.get('call_state')}")
    st2 = s.get("settings") or {}
    if st2:
        rot = "自动旋转" if st2.get("auto_rotate") else f"锁定 {st2.get('user_rotation') * 90}°"
        L.append(f"【声音】铃声={st2.get('ringer')}  免打扰={st2.get('zen')}")
        L.append(f"【显示】亮度={st2.get('brightness')}"
                 f"（{'自动' if st2.get('auto_brightness') else '手动'}）  {rot}"
                 f"  飞行模式={'开' if st2.get('airplane') else '关'}")
    audio = s.get("audio") or {}
    if audio:
        seg = []
        for k, cn in (("music", "媒体"), ("ring", "铃声"), ("alarm", "闹钟"),
                      ("voice_call", "通话"), ("notification", "通知")):
            v = audio.get(k)
            if v:
                mx = v.get("Max") or 0
                cur = v.get("streamVolume")
                seg.append(f"{cn} {cur}/{mx}" + ("（静音）" if v.get("Muted") else ""))
        if seg:
            L.append("【音量】" + "  ".join(seg))
    se = s.get("sensors") or {}
    if se:
        L.append(f"【传感器】{se.get('count')} 个在用 {se.get('active')} 个："
                 + "、".join(se.get("types") or [])[:120])
    sto = s.get("storage") or {}
    mem = s.get("mem") or {}
    up = s.get("uptime") or {}
    res = []
    if sto:
        res.append(f"存储 {sto.get('used_gb')}/{sto.get('total_gb')}GB"
                   f"（{sto.get('used_pct')}%）")
    if mem:
        res.append(f"内存 可用 {mem.get('avail_mb')}/{mem.get('total_mb')}MB")
    if up:
        res.append(f"已开机 {up.get('human')}")
    if res:
        L.append("【资源】" + "  ".join(res))
    rec = s.get("recents") or {}
    if rec:
        L.append("【最近用过】" + "、".join(rec.get("apps") or []))
    if st.get("missing"):
        L.append("")
        L.append("【读不到】")
        for m in st["missing"]:
            L.append(f"  - {m['section']}: {m['err']}")
    if not L:
        return "手机连上了，但各项都没读出内容（可能是 ROM 限制）"
    return "\n".join(L)


# ---------------------------------------------------------------- 自测

_FIX_BATTERY = """Current Battery Service state:
  AC powered: true
  USB powered: true
  Wireless powered: false
  status: 3
  health: 2
  present: true
  level: 50
  scale: 100
  voltage: 3
  temperature: 26
  technology: Li-ion"""

_FIX_WIFI = """Wifi is enabled
Wifi is connected to "TP-LINK_E858"
WifiInfo: SSID: TP-LINK_E858, BSSID: 78:60:5b:cf:e8:5a, MAC: 1c:79:2d:a6:76:59, \
Supplicant state: COMPLETED, Wi-Fi standard: 5, RSSI: -44, Link speed: 434Mbps, \
Frequency: 5745MHz, Net ID: 1"""

_FIX_AUDIO = """- STREAM_VOICE_CALL:
   Muted: false
   Min: 1
   Max: 5
   streamVolume:4
- STREAM_MUSIC:
   Muted: false
   Min: 0
   Max: 15
   streamVolume:15
- STREAM_RING:
   Muted: false
   Min: 0
   Max: 7
   streamVolume:7"""

_FIX_SENSORS = """Sensor List:
0000000000) Accelerometer sensor      | The Android Open Source Project | \
ver: 1 | type: android.sensor.accelerometer(1) | perm: n/a | flags: 0x00000000
	continuous | minRate=5.00Hz | maxRate=142.86Hz
Active sensors:"""


def _selftest():
    # ① 电量：插着电却在放电必须被说出来（真机就是这样，不能静默）
    b = parse_battery(_FIX_BATTERY)
    assert b["level"] == 50 and b["status"] == "放电中" and b["charging"] is False, b
    assert b["plugged"] == ["AC", "USB"] and "插着电但报放电" in b["anomaly"], b

    # ② WiFi：SSID/信号/频段/质量四个都要对；5GHz 不能标成 2.4
    w = parse_wifi(_FIX_WIFI)
    assert w["ssid"] == "TP-LINK_E858" and w["rssi"] == -44, w
    assert w["band"] == "5GHz" and w["quality"] == "很强", w
    assert parse_wifi("Wifi is disabled")["ssid"] == ""
    # 频段边界：2412 是 2.4G、5180 是 5G、6115 是新 6G
    for freq, band in ((2412, "2.4GHz"), (5180, "5GHz"), (6115, "6GHz")):
        assert parse_wifi(f'WifiInfo: RSSI: -60, Frequency: {freq}MHz')["band"] == band

    # ③ 音量：四条流不能互相覆盖
    a = parse_audio(_FIX_AUDIO)
    assert a["music"]["streamVolume"] == 15 and a["music"]["Max"] == 15, a
    assert a["ring"]["streamVolume"] == 7 and a["voice_call"]["streamVolume"] == 4, a

    # ④ 传感器：名字 + 类型都要取到，没有在用的时候 active=0 而不是报错
    se = parse_sensors(_FIX_SENSORS)
    assert se["count"] == 1 and "accelerometer" in se["types"], se
    assert se["active"] == 0, se

    # ⑤ 设置七项：顺序错一位就全错，所以每项单独断言
    s = parse_settings("1\n2\n153\n0\n0\n0\n0")
    assert (s["zen_mode"], s["ringer_mode"], s["brightness"]) == (1, 2, 153), s
    assert s["zen"] == "仅优先" and s["auto_brightness"] is False, s
    assert parse_settings("")["zen_mode"] == 0            # 空行=没设置，退默认值不抛

    # ⑥ 蜂窝没 SIM 也要能说清
    c = parse_cell("ABSENT\n\nUnknown\n")
    assert c["present"] is False and c["sim"] == "ABSENT", c

    # ⑦ 存储/内存/运行时长
    sto = parse_storage("Filesystem 1K-blocks Used Available Use% Mounted on\n"
                        "/dev/fuse 10682936 599736 9935744 6% /storage/emulated")
    assert sto["total_gb"] == 10.2 and sto["used_pct"] == 6, sto
    assert parse_mem("MemTotal: 2004028 kB\nMemFree: 47448 kB\nMemAvailable: 841320 kB"
                     )["avail_mb"] == 821
    assert parse_uptime("646462.51 2241839.94")["human"] == "7 天 11 小时"

    # ⑧ 剪贴板读不到必须明确报，不许返回空串冒充「剪贴板是空的」
    try:
        parse_clip("No shell command implementation.")
        raise AssertionError("剪贴板读不到却当成功了")
    except ValueError as e:
        assert "Android 10" in str(e), e

    # ⑨ 分节失败互不影响：坏一节，其余照常
    raw = {"battery": _FIX_BATTERY, "wifi": "", "audio": _FIX_AUDIO,
           "settings": "1\n2\n153\n0\n0\n0\n0", "sensors": _FIX_SENSORS}
    secs, missing = {}, []
    for key in _PARSERS:
        txt = raw.get(key, "")
        try:
            secs[key] = _PARSERS[key](txt)
        except Exception as e:
            missing.append({"section": key, "err": str(e)})
    assert "battery" in secs and "audio" in secs, missing
    assert any(m["section"] == "wifi" for m in missing), missing

    # ⑩ 一句话与报告都不许抛，也不许输出空句
    st = {"ok": True, "at": 0, "sections": secs, "missing": missing,
          "anomalies": ["电量只剩 12%，没在充电"]}
    line = brief(st)
    assert line and "电 50%" in line and "TP-LINK" not in line, line   # wifi 坏了就不提
    assert "要注意" in report(st)
    assert brief({"ok": False, "err": "没接手机"}) == "手机状态没读到：没接手机"
    assert report({"ok": True, "sections": {}, "missing": []}).strip() != ""

    # ⑪ 异常规则：低电未充电 / 存储将满 / WiFi 开着却没连
    low = {"sections": {"battery": {"level": 15, "plugged": [], "anomaly": ""}}}
    assert any("只剩 15%" in x for x in anomalies(low)), anomalies(low)
    assert any("存储快满" in x for x in anomalies(
        {"sections": {"storage": {"used_pct": 97}}}))
    assert any("没连上" in x for x in anomalies(
        {"sections": {"wifi": {"enabled": True, "ssid": ""}}}))

    # ⑫ 命令组里不许出现写操作（这条错了，跑一万次就会改一万次真机）
    script = _script(_BATCH_A + _BATCH_B).lower()
    for bad in ("settings put", "svc wifi", "svc data", " am ", " input ",
                "pm uninstall", "cmd wifi set", "cmd wifi connect"):
        assert bad not in script, f"采集命令里混进了写操作：{bad}"

    print("phone_state 自测：12 项全过")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        _selftest()
        return 0
    st = snapshot()
    if "--json" in argv:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif "--brief" in argv:
        print(brief(st))
    else:
        print(report(st))
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
