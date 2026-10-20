"""蓝牙层：手机当蓝牙天线，看身边有哪些在广播的设备。

WiFi 层看的是「网络」，蓝牙层看的是「人和东西」：耳机、手环、车钥匙、
共享单车、家电、灯带。这些东西不连网也在广播，所以这一层经常比 WiFi
更能说明「此刻身边有谁」。只扫不连——BLUETOOTH_SCAN 带 neverForLocation。

    python ble.py                 # 扫 8 秒出表
    python ble.py --secs 15       # 扫久一点，弱信号也能收全
    python ble.py --json
    python ble.py --on / --off    # 开关手机蓝牙（关之前先看它原来是什么状态）
    python ble.py --selftest
"""
import json
import re
import sys

import android_device as A

BROADCAST = "Manuf"        # 厂商 id 在回复里的位置（第 5 字段）

# 广播里的厂商 id（Bluetooth SIG company identifier）
MFR_VENDOR = {
    "004c": "苹果",
    "0006": "微软",
    "0075": "三星",
    "0157": "小米",
    "00e0": "谷歌",
    "0087": "Garmin",
    "038f": "佳明",
    "034c": "小牛电动",
    "04b3": "摩拜",
    "06a8": "美的",
    "0171": "亚马逊",
    "000f": "博通",
    "0059": "Nordic",
}

# 名字关键字 → 类型。按顺序匹配，越具体越靠前。
_KINDS = [
    (r"airpods|buds|freebuds|wh-|wf-|headphone|耳机", "耳机"),
    (r"band|watch|手环|手表|fit", "可穿戴"),
    (r"\btv\b|bravia|mi tv|redmi tv|电视", "电视/盒子"),
    (r"mobike|hellobike|哈啰|美团|共享", "共享单车"),
    (r"byd|nio|tesla|小鹏|理想|蔚来|link|车", "车/车钥匙"),
    (r"niu|aima|yadea|ninebot|segway|雅迪|爱玛", "电动车"),
    (r"midea|gree|haier|miio|米家|美的|格力|海尔", "家电"),
    (r"light|lamp|灯|bulb|strip", "灯"),
    (r"lock|门锁|keybox", "门锁"),
    (r"printer|print|打印机", "打印"),
    (r"speaker|sound|音箱|小爱", "音箱"),
    (r"scale|体重秤|体脂", "健康秤"),
]


def parse(out):
    """把桥的一行回复拆成设备列表。格式：mac,rssi,tx,name,mfr;..."""
    s = out or ""
    if s.startswith("OK ble"):
        s = s[6:].strip()
    rows = []
    for chunk in s.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        f = chunk.split(",")
        if len(f) < 5:
            continue
        mac = f[0].strip().upper()
        if len(mac) < 11:
            continue
        try:
            rssi = int(f[1])
        except ValueError:
            continue
        try:
            tx = int(f[2])
        except ValueError:
            tx = -127
        name = f[3].strip()
        mfr = f[4].strip().lower()
        rows.append({"mac": mac, "rssi": rssi, "tx": tx,
                     "name": name, "mfr": mfr})
    rows.sort(key=lambda r: -r["rssi"])
    return rows


def classify(row):
    """这是什么。先看名字里的关键字，再看厂商 id，最后按广播类型猜。"""
    name = (row.get("name") or "").lower()
    mfr = row.get("mfr") or ""
    for pat, kind in _KINDS:
        if re.search(pat, name):
            return kind
    if mfr == "004c":
        # 苹果设备默认不广播名字，能被看到的多是 iPhone / Mac / AirPods
        return "苹果设备"
    if mfr == "04b3":
        return "共享单车"
    if mfr == "034c":
        return "电动车"
    if mfr == "06a8":
        return "家电"
    if not name and not mfr:
        return "无名字广播"
    return "其他"


def distance_hint(rssi):
    if rssi >= -60:
        return "贴脸（同一张桌子）"
    if rssi >= -75:
        return "同屋"
    if rssi >= -85:
        return "隔壁房间"
    return "远处/墙外"


def scan(serial, secs=8):
    # 桥上的 BLE 是同步阻塞的：扫 N 秒才回一行，socket 超时必须往后放
    r = A.bridge_cmd(serial, f"BLE {int(secs)}", timeout=int(secs) + 10)
    if not r.get("ok"):
        return {"ok": False, "err": (r.get("err") or r.get("out") or "").strip()[:120]}
    rows = parse(r.get("out"))
    for x in rows:
        x["kind"] = classify(x)
        x["vendor"] = MFR_VENDOR.get(x["mfr"], "")
        x["distance"] = distance_hint(x["rssi"])
    return {"ok": True, "devices": rows}


def summary(rows):
    from collections import Counter
    c = Counter(r["kind"] for r in rows)
    named = [r for r in rows if r["name"]]
    near = [r for r in rows if r["rssi"] >= -75]
    return {"total": len(rows), "kinds": dict(c), "named": len(named),
            "near": len(near),
            "near_names": [r["name"] or r["kind"] for r in near][:8]}


def format_table(rows):
    lines = [f"{'信号':>4} {'距离':14} {'类型':10} {'名字':26} {'MAC':18} 厂商id"]
    for r in rows:
        lines.append(f"{r['rssi']:>4} {r['distance'][:14]:14} {r['kind'][:10]:10} "
                     f"{(r['name'] or '(无名)')[:26]:26} {r['mac']:18} {r['mfr'] or '-'}")
    return "\n".join(lines)


def bt_state(serial):
    r = A.bridge_cmd(serial, "BTSTATE")
    out = (r.get("out") or "").strip()
    if out.startswith("OK bt "):
        return out[6:]
    return "unknown"


def power(serial, on):
    """开关蓝牙。这里只管适配器，不碰 WiFi/移动数据（红线只管联网，蓝牙不联网）。"""
    cmd = "cmd bluetooth_manager enable" if on else "cmd bluetooth_manager disable"
    rc, txt = A.shell(serial, cmd)
    return {"ok": rc == 0, "out": (txt or "").strip()[:120], "rc": rc}


def _selftest():
    # ① 正常一行：中英文名混着、带厂商 id
    raw = ("OK ble F1:22:01:C7:51:6E,-80,-2147483648,AIMA-C7516E,01a8;"
           "B0:96:EA:C9:28:E4,-84,-2147483648,midea,06a8;"
           "03:8F:64:17:CA:4A,-83,-2147483648,,0006")
    rows = parse(raw)
    assert len(rows) == 3 and rows[0]["name"] == "AIMA-C7516E"
    assert rows[0]["mfr"] == "01a8"
    assert rows[1]["name"] == "" and rows[2]["name"] == "midea"   # 排序后按信号强弱

    # ② 按信号从强到弱排
    assert [r["rssi"] for r in rows] == [-80, -83, -84]

    # ③ 名字里带逗号/分号会被桥提前清掉；这里只保证脏数据不炸
    assert parse("OK ble") == [] and parse("") == [] and parse("垃圾数据") == []
    assert parse("AA:BB:CC:DD:EE:FF,-70") == []        # 字段不足，丢掉

    # ④ 归类：家电、电动车、车、共享单车、耳机、苹果
    assert classify({"name": "midea", "mfr": "06a8"}) == "家电"
    assert classify({"name": "AIMA-C7516E", "mfr": ""}) == "电动车"
    assert classify({"name": "BYD BLE3", "mfr": ""}) == "车/车钥匙"
    assert classify({"name": "mobike", "mfr": "04b3"}) == "共享单车"
    assert classify({"name": "Redmi Buds 4", "mfr": ""}) == "耳机"
    assert classify({"name": "", "mfr": "004c"}) == "苹果设备"
    assert classify({"name": "", "mfr": ""}) == "无名字广播"

    # ⑤ 距离只给量级，不给米数（蓝牙 RSSI 受遮挡影响太大）
    assert distance_hint(-50) == "贴脸（同一张桌子）"
    assert distance_hint(-70) == "同屋"
    assert distance_hint(-95) == "远处/墙外"

    # ⑥ 厂商 id 认得出来，认不出来也不能报错
    assert MFR_VENDOR.get("06a8") == "美的" and MFR_VENDOR.get("zzzz") is None

    # ⑦ 汇总：按类型数，近处按 -75 划线
    s = summary([{"kind": "家电", "name": "midea", "rssi": -84},
                 {"kind": "耳机", "name": "Buds", "rssi": -60},
                 {"kind": "无名字广播", "name": "", "rssi": -90}])
    assert s["total"] == 3 and s["named"] == 2 and s["near"] == 1
    assert s["near_names"] == ["Buds"]

    print("ble 自测：7 项全过")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    d = A.attach()
    if not d["ok"]:
        print("设备接不上：", d.get("err"), file=sys.stderr)
        return 2
    s = d["serial"]

    if "--on" in argv or "--off" in argv:
        want = "--on" in argv
        r = power(s, want)
        print(("已打开" if want else "已关闭") + "蓝牙" if r["ok"] else "失败：" + r["out"])
        return 0 if r["ok"] else 1

    secs = 8
    if "--secs" in argv:
        i = argv.index("--secs")
        if i + 1 < len(argv):
            try:
                secs = max(1, min(int(argv[i + 1]), 30))
            except ValueError:
                pass

    state = bt_state(s)
    if state == "off":
        print("蓝牙关着。先 python ble.py --on，扫完再 --off 还原。", file=sys.stderr)
        return 1
    got = scan(s, secs)
    if not got["ok"]:
        print("扫描失败：", got["err"], file=sys.stderr)
        return 1
    rows = got["devices"]
    if "--json" in argv:
        print(json.dumps({"devices": rows, "summary": summary(rows)},
                         ensure_ascii=False, indent=2))
        return 0
    print(format_table(rows))
    sm = summary(rows)
    print(f"\n共 {sm['total']} 个在广播｜有名 {sm['named']} 个｜近处(-75dBm 内) {sm['near']} 个")
    print("按类型：" + "，".join(f"{k} {v}" for k, v in sorted(sm["kinds"].items(),
                                                             key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
