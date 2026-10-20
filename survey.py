"""把手机变成天线：WiFi 无线侧扫描。

为什么非要手机来扫：这台电脑的射频被它自己开的移动热点占满了（单射频），
Windows 就不再扫周边 AP，所以电脑侧永远只能看见「已经连上的那个网」。
手机不一样——它连着热点，同时还能扫，周边有多少 WiFi 只有它看得见。

产出是「谁在你附近」：邻居的路由器、运营商光猫、机顶盒、智能家电、投屏盒子。
和 sweep.py 的「谁跟你在同一个网段」互补，一个是无线层的邻居，一个是 IP 层的邻居。
"""
import json
import re
import sys
import time

import android_device as A
from discovery import OUI_VENDOR, is_randomized_mac

DEFAULT_SETTLE = 7          # 触发扫描后等多久收结果
_BSSID = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")

# SSID 里的品牌线索。命中就说明不是普通路由器，是某家的设备。
_VENDOR_HINTS = [
    (r"^midea", "美的", "家电"),
    (r"^gree", "格力", "家电"),
    (r"^haier", "海尔", "家电"),
    (r"^hisense", "海信", "家电"),
    (r"^aux", "奥克斯", "家电"),
    (r"^tcl", "TCL", "家电"),
    (r"^robam|^fotile|^vatti", "厨电", "家电"),
    (r"^(mi-|xiaomi|redmi)", "小米", "智能设备"),
    (r"^(huawei|honor)", "华为", "路由器/手机"),
    (r"^(tp-?link|mercury|tenda|h3c|ruijie|netcore)", "网络设备", "路由器"),
    (r"^stb[_ ]?", "机顶盒", "运营商"),
    (r"^(cmcc|chinanet|chinaunicom|cu_|ctc)", "运营商", "光猫"),
    (r"^direct-", "WiFi Direct", "投屏/打印"),
    (r"^(hp-|epson|canon|brother)", "打印机", "打印机"),
    (r"^(ipcam|ipc|hikvision|dahua|ezviz|yi-|mijia)", "摄像头", "安防"),
]


def parse_line(line):
    """解析 cmd wifi list-scan-results 的一行。

    格式（SSID 可能为空、可能带空格、可能是中文）：
      BSSID  freq  RSSI(0:x/1:y)  age  SSID  [加密标志...]
    """
    parts = line.split(None, 4)
    if len(parts) < 5:
        return None
    bssid, freq, rssi, age, rest = parts
    if not _BSSID.match(bssid):
        return None
    try:
        freq = int(freq)
        rssi = int(re.match(r"-?\d+", rssi).group(0))
        age = float(age)
    except (AttributeError, ValueError):
        return None

    i = rest.find("[")
    if i >= 0:
        ssid, flags = rest[:i].strip(), rest[i:]
    else:
        ssid, flags = rest.strip(), ""

    if 2400 <= freq < 2500:
        band, chan = "2.4G", int((freq - 2407) / 5)
    elif 5000 <= freq < 5900:
        band, chan = "5G", int((freq - 5000) / 5)
    elif 5925 <= freq < 7125:
        band, chan = "6G", int((freq - 5950) / 5)
    else:
        band, chan = "?", 0

    return {"bssid": bssid.upper(), "ssid": ssid, "freq": freq, "band": band,
            "channel": chan, "rssi": rssi, "age": age,
            "secure": bool(flags), "flags": flags}


def parse(text):
    out = []
    for line in text.splitlines():
        row = parse_line(line)
        if row:
            out.append(row)
    return out


def classify(row):
    """给一条 AP 打标签：这是什么。判断顺序：SSID 品牌 > 开放网络 > 隐藏 > 泛化。"""
    ssid, low = row["ssid"], row["ssid"].lower()
    for pat, vendor, kind in _VENDOR_HINTS:
        if re.search(pat, low):
            return {"vendor": vendor, "kind": kind}
    if not ssid:
        return {"vendor": "", "kind": "隐藏网络"}
    if not row["secure"]:
        # 不加密的开放网络，多半是智能家电的配网热点或公共热点
        return {"vendor": "", "kind": "开放网络（多为家电配网）"}
    if re.search(r"5g|fast", low):
        return {"vendor": "", "kind": "路由器 5G"}
    return {"vendor": "", "kind": "家用路由器"}


def distance_hint(rssi):
    """RSSI 粗估距离。只说量级，别当测距。"""
    if rssi >= -45:
        return "同一房间"
    if rssi >= -60:
        return "几米内"
    if rssi >= -70:
        return "同屋偏远处"
    if rssi >= -80:
        return "隔墙/隔壁"
    return "很远"


def enrich(rows):
    for r in rows:
        r.update(classify(r))
        if not r["vendor"]:
            r["vendor"] = OUI_VENDOR.get(r["bssid"][:8].upper(), "")
        r["random_mac"] = is_randomized_mac(r["bssid"])
        r["distance"] = distance_hint(r["rssi"])
    return rows


def scan(serial, settle=DEFAULT_SETTLE):
    """触发一次真扫描并取结果。手机侧扫描约 4-6 秒一轮。"""
    A.shell(serial, "cmd wifi start-scan")
    time.sleep(settle)
    rc, txt = A.shell(serial, "cmd wifi list-scan-results")
    if rc != 0 or "BSSID" not in txt:
        return {"ok": False, "err": txt.strip()[:120]}
    rows = enrich(parse(txt))
    rows.sort(key=lambda r: -r["rssi"])
    return {"ok": True, "aps": rows}


def format_table(rows):
    lines = [f"{'信号':>4} {'频段':4} {'类型':14} {'厂商':8} {'SSID':26} {'BSSID':18} 距离"]
    for r in rows:
        name = r["ssid"] or "(隐藏)"
        lines.append(f"{r['rssi']:>4} {r['band']:4} {r['kind'][:14]:14} "
                     f"{(r['vendor'] or '-')[:8]:8} {name[:26]:26} {r['bssid']:18} {r['distance']}")
    return "\n".join(lines)


def summary(rows):
    """按类别数一遍，给一句人话结论。"""
    from collections import Counter
    c = Counter(r["kind"] for r in rows)
    band = Counter(r["band"] for r in rows)
    near = [r for r in rows if r["rssi"] >= -65]
    return {"total": len(rows), "kinds": dict(c), "bands": dict(band),
            "near": len(near),
            "near_names": [r["ssid"] or "(隐藏)" for r in near]}


def _selftest():
    # ① 普通一行
    r = parse_line("  f0:9b:b8:0e:91:bc       2432    -82(0:-85/1:-86)     5.408    CU_u8M6   [WPA2-PSK-CCMP][ESS]")
    assert r and r["ssid"] == "CU_u8M6" and r["rssi"] == -82, r
    assert r["band"] == "2.4G" and r["channel"] == 5, r
    assert r["secure"] is True

    # ② SSID 为空（隐藏网络）时不能把加密标志当成名字
    r = parse_line("  c6:27:28:38:da:11       5200    -82(0:-86/1:-84)     6.107                    [WPA2-PSK-CCMP][ESS]")
    assert r and r["ssid"] == "" and r["band"] == "5G", r

    # ③ SSID 带空格 / 中文
    r = parse_line("  ea:8a:d8:1e:37:05       2462    -38(0:-40/1:-42)     4.416    DESKTOP-1BQS174 2486   [WPA2-PSK-CCMP][ESS]")
    assert r["ssid"] == "DESKTOP-1BQS174 2486", r["ssid"]
    r = parse_line("  62:70:8e:17:0e:d9       5220    -86(0:-89/1:-90)     6.058    大大怪没烦恼   [WPA2-PSK-CCMP][ESS]")
    assert r["ssid"] == "大大怪没烦恼", r["ssid"]

    # ④ 表头、空行、残缺行一律返 None，不能崩
    assert parse_line("    BSSID              Frequency      RSSI           Age(sec)") is None
    assert parse_line("") is None
    assert parse_line("  zz:zz") is None

    # ⑤ 归类：家电、运营商、投屏、隐藏、开放
    assert classify({"ssid": "midea_e3_0543", "secure": False})["kind"] == "家电"
    assert classify({"ssid": "CMCC-GdGe", "secure": True})["kind"] == "光猫"
    assert classify({"ssid": "DIRECT-33J0362", "secure": True})["kind"] == "投屏/打印"
    assert classify({"ssid": "", "secure": True})["kind"] == "隐藏网络"

    # ⑥ 频段换算
    assert parse_line("  00:11:22:33:44:55  2412  -50(0:-50/1:-50)  1.0  x  [ESS]")["channel"] == 1
    assert parse_line("  00:11:22:33:44:55  5745  -50(0:-50/1:-50)  1.0  x  [ESS]")["channel"] == 149

    # ⑦ 距离只是量级
    assert distance_hint(-40) == "同一房间" and distance_hint(-90) == "很远"

    print("survey \u81ea\u6d4b\uff1a7 \u9879\u5168\u8fc7")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    d = A.attach()
    if not d["ok"]:
        print("设备接不上：", d.get("err"), file=sys.stderr)
        return 2
    got = scan(d["serial"])
    if not got["ok"]:
        print("扫描失败：", got["err"], file=sys.stderr)
        return 1
    rows = got["aps"]
    if "--json" in argv:
        print(json.dumps({"aps": rows, "summary": summary(rows)},
                         ensure_ascii=False, indent=2))
        return 0
    print(format_table(rows))
    s = summary(rows)
    print(f"\n共 {s['total']} 个 AP｜近处（-65dBm 内）{s['near']} 个："
          + "、".join(s["near_names"][:8]))
    print("按类型：" + "，".join(f"{k} {v}" for k, v in sorted(s["kinds"].items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
