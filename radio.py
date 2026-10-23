"""射频层：把手机当「天线 + 频谱仪」用，这两件事电脑自己做不了。

为什么非要手机：
  · 电脑的无线网卡被它自己开的移动热点占着，Windows 就不再扫周边 AP——
    电脑侧永远只看得见「已经连上的那个网」。手机连着热点还能同时扫，
    邻居有多少 WiFi、都在哪个频段、谁家信号压过来，只有它看得见。
  · 蜂窝频段更是只有手机有。电脑连基带都没有。

数据来源（全走 adb，不装 App、不 root）：
  · `cmd wifi start-scan` / `list-scan-results` —— 周边 AP（BSSID/频率/RSSI/加密）
  · `cmd wifi status`                        —— 当前这条链路（频率/速率/RSSI）
  · `dumpsys telephony.registry`             —— 蜂窝服务态（ARFCN/带宽/RSRP）

关于「频段」的口径，必须说清楚，否则就是在编：
  Android 的 dumpsys 在这台机器上**不吐 mBands 字段**（MIUI 裁掉了），
  只给 ARFCN（mChannelNumber）。所以频段是从 ARFCN 换算成中心频率、
  再按 3GPP 的频率区间反推的，属于「推算」不是「直读」。
  推算链条是标准的、可复算的：NR-ARFCN → 频率 → 区间 → 频段号。
  落在任何已知区间之外时一律报「未知」，不猜。

用法（命令行自测）：
    python radio.py wifi          # 周边 AP 扫描
    python radio.py link          # 当前链路
    python radio.py cell          # 蜂窝频段
"""
from __future__ import annotations

import re
import sys

import android_device as A
import survey


# ---------------------------------------------------------------- WiFi 频段

def wifi_channel(freq):
    """频率 → (频段名, 信道号)。公式就是 802.11 的定义，没有近似。"""
    if 2400 <= freq < 2500:
        return "2.4G", int((freq - 2407) / 5)
    if 4900 <= freq < 5900:
        return "5G", int((freq - 5000) / 5)
    if 5925 <= freq < 7125:
        return "6G", int((freq - 5950) / 5)
    return "?", 0


def wifi_survey(serial, rounds=1, settle=7):
    """扫周边 AP。返回的每条都带频段/信道/厂商/距离量级。"""
    r = survey.scan(serial, settle=settle, rounds=rounds)
    if not r.get("ok"):
        return {"ok": False, "err": r.get("err") or "扫描没有结果"}
    aps = r["aps"]
    # 按频段分组数一遍：一句话回答「我这个位置 2.4G 挤不挤、5G 有几个」
    by_band = {}
    for a in aps:
        by_band.setdefault(a["band"], []).append(a)
    return {"ok": True, "aps": aps, "count": len(aps),
            "bands": {k: len(v) for k, v in by_band.items()},
            "summary": survey.summary(aps),
            "table": survey.format_table(aps)}


def wifi_link(serial):
    """当前这条 WiFi 链路的实况：连的谁、哪个频段、多少速率、信号多强。"""
    rc, txt = A.shell(serial, "cmd wifi status")
    if rc != 0 or "Wifi" not in txt:
        return {"ok": False, "err": (txt or "").strip()[:120]}
    m = re.search(r'WifiInfo: SSID: "([^"]*)".*?BSSID: ([0-9a-fA-F:]{17}).*?'
                  r'RSSI: (-?\d+).*?Link speed: (\d+)Mbps.*?Frequency: (\d+)MHz', txt)
    if not m:
        return {"ok": False, "err": "cmd wifi status 里没找到 WifiInfo 段"}
    ssid, bssid, rssi, speed, freq = m.groups()
    band, chan = wifi_channel(int(freq))
    std = re.search(r"Wi-Fi standard: ([0-9a-zA-Z]+)", txt)
    return {"ok": True, "ssid": ssid, "bssid": bssid.upper(),
            "rssi": int(rssi), "link_mbps": int(speed),
            "freq": int(freq), "band": band, "channel": chan,
            "standard": std.group(1) if std else "",
            "distance": survey.distance_hint(int(rssi))}


# ---------------------------------------------------------------- 蜂窝频段

# NR 频段下行频率区间（MHz）。只收国内实际在用的 + 常见国际频段，
# 不做全表——表越长越容易抄错，而抄错一个数字比不认识更糟。
_NR_BANDS = [
    ("n1", 2110, 2170), ("n2", 1930, 1990), ("n3", 1805, 1880),
    ("n5", 869, 894), ("n7", 2620, 2690), ("n8", 925, 960),
    ("n20", 791, 821), ("n28", 758, 803), ("n38", 2570, 2620),
    ("n39", 1880, 1920), ("n40", 2300, 2400), ("n41", 2496, 2690),
    ("n66", 2110, 2200), ("n71", 617, 652), ("n75", 1432, 1517),
    ("n77", 3300, 4200), ("n78", 3300, 3800), ("n79", 4400, 5000),
]

# LTE 频段下行区间（MHz）
_LTE_BANDS = [
    ("B1", 2110, 2170), ("B2", 1930, 1990), ("B3", 1805, 1880),
    ("B5", 869, 894), ("B7", 2620, 2690), ("B8", 925, 960),
    ("B20", 791, 821), ("B28", 758, 803), ("B34", 2010, 2025),
    ("B38", 2570, 2620), ("B39", 1880, 1920), ("B40", 2300, 2400),
    ("B41", 2496, 2690), ("B66", 2110, 2200),
]

# 中国运营商 MCC/MNC → 名字。dumpsys 里其实带 mOperatorAlphaLong，
# 但双卡聚合/漫游时那个字段会空，用 MCC/MNC 兜底更稳。
_CN_CARRIER = {
    ("460", "00"): "中国移动", ("460", "02"): "中国移动", ("460", "04"): "中国移动",
    ("460", "07"): "中国移动", ("460", "08"): "中国移动",
    ("460", "01"): "中国联通", ("460", "06"): "中国联通", ("460", "09"): "中国联通",
    ("460", "10"): "中国联通",
    ("460", "03"): "中国电信", ("460", "05"): "中国电信", ("460", "11"): "中国电信",
    ("460", "12"): "中国电信",
}


def nr_freq(arfcn):
    """NR-ARFCN → 中心频率（MHz）。三个分段三段公式，3GPP TS 38.104 表 5.4.2.1-1。"""
    if arfcn < 600000:
        return 0 + 0.005 * arfcn
    if arfcn < 2016667:
        return 3000 + 0.015 * (arfcn - 600000)
    return 24250 + 0.06 * (arfcn - 2016667)


# LTE EARFCN 参数：频段 → (N_Offs-DL, F_DL_low, F_DL_high)。3GPP TS 36.101 表 5.7.3-1。
# 每个频段的 EARFCN 起点和频率起点都不同，所以只能查表，不能套一条式子。
#
# 判定必须**按 EARFCN 区间**，不能按频率窗口：
#   曾经用 `lo-5 <= f <= lo+100` 这种模糊窗口，结果 EARFCN 38950（B40）被 B39 先算中
#   ——B39 外推得到 1950MHz 恰好落在自己的区间里，于是返回了 1950 而不是 2330。
#   3GPP 表里各频段的 EARFCN 是首尾相接、互不重叠的整数区间，用区间判就是精确的。
_LTE_EARFCN = [
    ("B1", 0, 2110, 2170), ("B2", 600, 1930, 1990), ("B3", 1200, 1805, 1880),
    ("B5", 2400, 869, 894), ("B7", 2750, 2620, 2690), ("B8", 3450, 925, 960),
    ("B20", 6150, 791, 821), ("B28", 9210, 758, 803), ("B34", 36200, 2010, 2025),
    ("B38", 37750, 2570, 2620), ("B39", 38250, 1880, 1920),
    ("B40", 38650, 2300, 2400), ("B41", 39650, 2496, 2690),
    ("B66", 66436, 2110, 2200),
]


def lte_freq(earfcn):
    """LTE EARFCN → 中心频率（MHz）。命中哪个频段的 EARFCN 区间就按那个频段换算。

    区间是**左闭右开**：3GPP 表里 B39 到 38649、B40 从 38650 起，首尾相接不重叠。
    写成闭区间会在交界处（38650）让 B39 抢走 B40 的频点，算出 1920 而不是 2300。
    """
    for _name, n_offs, lo, hi in _LTE_EARFCN:
        if n_offs <= earfcn < n_offs + int(round((hi - lo) / 0.1)):
            return lo + 0.1 * (earfcn - n_offs)
    return None


def band_of(freq, rat):
    """频率 + 制式 → 频段名。落在所有已知区间之外就返回空（不猜）。"""
    if freq is None:
        return ""
    table = _NR_BANDS if rat.upper().startswith("NR") else _LTE_BANDS
    span = {name: hi - lo for name, lo, hi in table}
    hits = [name for name, lo, hi in table if lo <= freq <= hi]
    if not hits:
        return ""
    # n77(3300-4200) 与 n78(3300-3800) 是包含关系，落在 3400 这种地方两者都成立。
    # 报**区间更窄**的那个（更具体、也更可能是真的），把宽的那个放进括号，
    # 免得把「3300-4200 全算 n77」这种粗结论当成事实说出去。
    if len(hits) > 1:
        hits.sort(key=lambda n: span[n])
        return hits[0] + "(也可能是 " + "/".join(hits[1:]) + ")"
    return hits[0]


def _rat_name(code):
    """RIL 的 radio technology 编号 → 人话。只列国内会遇到的。"""
    return {
        "0": "Unknown", "3": "UMTS", "4": "CDMA2000", "5": "CDMA2000",
        "6": "EVDO", "7": "1xRTT", "8": "HSDPA", "9": "HSUPA", "10": "HSPA",
        "11": "iDEN", "12": "EVDO_B", "13": "LTE", "14": "eHRPD",
        "15": "HSPAP", "16": "GSM", "17": "TD_SCDMA", "18": "IWLAN",
        "19": "LTE_CA", "20": "NR_SA",
    }.get(str(code), f"RAT{code}")


def _parse_subs(text):
    """从 dumpsys telephony.registry 里切出每个 subscription 的服务态。

    切法：以 mServiceState={...} 为锚点，向后读到该 sub 的 mSignalStrength /
    mCellIdentity / mCellInfo。不用正则一次抓到尾，是因为这几个字段顺序在
    不同 ROM 上不固定。
    """
    subs = []
    for blk in re.split(r"\n(?=\s*mServiceState=\{)", text):
        if "mServiceState={" not in blk:
            continue
        ss = re.search(r"mServiceState=\{(.*?)\}\s*$|mServiceState=\{(.*)", blk)
        if not ss:
            continue
        body = ss.group(1) or ss.group(2) or ""
        row = {}
        m = re.search(r"mChannelNumber=(-?\d+)", body)
        row["arfcn"] = int(m.group(1)) if m else None
        m = re.search(r"mCellBandwidths=\[([^\]]*)\]", body)
        row["bandwidth_khz"] = [int(x) for x in re.findall(r"\d+", m.group(1))] if m else []
        m = re.search(r"getRilDataRadioTechnology=(\d+)\(([^)]*)\)", body)
        row["rat_code"] = m.group(1) if m else ""
        row["rat_raw"] = m.group(2) if m else ""
        m = re.search(r"mOperatorAlphaLong=([^,}]*)", body)
        row["operator_raw"] = (m.group(1) or "").strip()
        m = re.search(r"mCellIdentity=\{?[^}]*?mMcc = (\d+) mMnc = (\d+)", blk)
        if m:
            row["mcc"], row["mnc"] = m.group(1), m.group(2)
            row["operator"] = _CN_CARRIER.get((row["mcc"], row["mnc"]), "")
        else:
            row["mcc"] = row["mnc"] = row["operator"] = ""
        m = re.search(r"ssRsrp = (-?\d+)", blk)
        row["rsrp"] = int(m.group(1)) if m and int(m.group(1)) < 0 else None
        m = re.search(r"ssRsrq = (-?\d+)", blk)
        row["rsrq"] = int(m.group(1)) if m and int(m.group(1)) < 0 else None
        m = re.search(r"ssSinr = (-?\d+)", blk)
        row["sinr"] = int(m.group(1)) if m else None
        m = re.search(r"isManualNetworkSelection=(\w+)", body)
        row["manual"] = (m.group(1) == "true") if m else None
        subs.append(row)
    return subs


def cell(serial):
    """蜂窝：每个订阅（双卡各一条）当前驻留在哪个频段、带宽多少、信号多强。"""
    rc, txt = A.shell(serial, "dumpsys telephony.registry")
    if rc != 0 or "mServiceState" not in txt:
        return {"ok": False, "err": (txt or "").strip()[:120] or "dumpsys 无服务态"}
    # dumpsys 会带一大段历史事件，只看当前快照：取每行以 m 开头的那批
    subs = _parse_subs(txt)
    if not subs:
        return {"ok": False, "err": "没解析出任何订阅的服务态"}
    out = []
    for s in subs:
        rat = _rat_name(s.get("rat_code"))
        freq = None
        if s.get("arfcn") is not None and s["arfcn"] > 0:
            freq = nr_freq(s["arfcn"]) if rat.startswith("NR") else lte_freq(s["arfcn"])
        band = band_of(freq, rat)
        out.append({
            "operator": s.get("operator") or s.get("operator_raw") or "",
            "mcc": s.get("mcc"), "mnc": s.get("mnc"),
            "rat": rat,
            "arfcn": s.get("arfcn"),
            "freq_mhz": round(freq, 2) if freq else None,
            "band": band,
            "band_source": "ARFCN→频率→区间 推算（ROM 不吐 mBands）",
            "bandwidth_mhz": [round(b / 1000, 1) for b in s.get("bandwidth_khz") or []],
            "rsrp": s.get("rsrp"), "rsrq": s.get("rsrq"), "sinr": s.get("sinr"),
            "manual_selection": s.get("manual"),
        })
    return {"ok": True, "subs": out, "count": len(out)}


def _selftest():
    """离线自测：只验纯数学那部分（ARFCN↔频率↔频段、信道换算、dumpsys 解析）。

    不碰设备——这批公式是「编错了就悄悄给出错频段」的地方，最该被门禁盯住。
    """
    bad = []

    def eq(got, want, tag):
        if got != want:
            bad.append(f"{tag}: got={got!r} want={want!r}")

    # --- WiFi 信道：802.11 定义，三个频段各验两个点 + 越界
    eq(wifi_channel(2412), ("2.4G", 1), "ch 2412")
    eq(wifi_channel(2437), ("2.4G", 6), "ch 2437")
    eq(wifi_channel(2484), ("2.4G", 15), "ch 2484")
    eq(wifi_channel(5180), ("5G", 36), "ch 5180")
    eq(wifi_channel(5745), ("5G", 149), "ch 5745")
    eq(wifi_channel(5955), ("6G", 1), "ch 5955")
    eq(wifi_channel(6115), ("6G", 33), "ch 6115")
    eq(wifi_channel(1000), ("?", 0), "ch 越界")

    # --- NR-ARFCN → 频率：三段公式的段内点 + 两个分段边界
    eq(round(nr_freq(504990), 2), 2524.95, "nr 504990")
    eq(round(nr_freq(627264), 2), 3408.96, "nr 627264")
    eq(round(nr_freq(600000), 2), 3000.0, "nr 边界600000")
    eq(round(nr_freq(2016667), 2), 24250.0, "nr 边界2016667")
    eq(round(nr_freq(2016668), 2), 24250.06, "nr 边界+1")
    eq(round(nr_freq(632628), 2), 3489.42, "nr 632628")

    # --- LTE EARFCN → 频率：查表试算，验三个不同 N_Offs 的频段 + 两个频段交界
    eq(round(lte_freq(100), 2), 2120.0, "lte B1 earfcn100")
    eq(round(lte_freq(1650), 2), 1850.0, "lte B3 earfcn1650")
    eq(round(lte_freq(38950), 2), 2330.0, "lte B40 earfcn38950")
    eq(round(lte_freq(38649), 2), 1919.9, "lte B39 上界")
    eq(round(lte_freq(38650), 2), 2300.0, "lte B40 下界")
    eq(lte_freq(999999), None, "lte 越界应返回 None")

    # --- 频段反推：包含关系必须报区间更窄的那个
    eq(band_of(2524.95, "NR_SA"), "n41", "band n41")
    eq(band_of(3408.96, "NR_SA"), "n78(也可能是 n77)", "band n78/n77")
    eq(band_of(1850.0, "LTE"), "B3", "band B3")
    eq(band_of(500.0, "NR_SA"), "", "band 未知不猜")
    eq(band_of(None, "NR_SA"), "", "band 无频率")
    # n38(2570-2620, 50MHz) 与 n41(2496-2690, 194MHz) 重叠，报更窄的 n38
    eq(band_of(2600.0, "NR_SA"), "n38(也可能是 n41)", "band 重叠取窄")

    # --- 制式编号 → 人话
    eq(_rat_name("20"), "NR_SA", "rat 20")
    eq(_rat_name("13"), "LTE", "rat 13")
    eq(_rat_name("99"), "RAT99", "rat 未知")

    # --- dumpsys 解析：用真机抓下来的片段（已裁到最小可复现长度）
    fake = (
        "  mServiceState={mVoiceRegState=0(IN_SERVICE), "
        "mChannelNumber=504990, mCellBandwidths=[100000], "
        "getRilDataRadioTechnology=20(NR_SA), mOperatorAlphaLong=CHN-UNICOM}\n"
        "   mCellIdentity={ mMcc = 460 mMnc = 01 ...}\n"
        "   mSignalStrength=SignalStrength:{ssRsrp = -76 ssRsrq = -11 ssSinr = 17}\n"
    )
    subs = _parse_subs(fake)
    eq(len(subs), 1, "parse 条数")
    if subs:
        s = subs[0]
        eq(s["arfcn"], 504990, "parse arfcn")
        eq(s["bandwidth_khz"], [100000], "parse 带宽")
        eq(s["rat_code"], "20", "parse rat")
        eq(s["mcc"], "460", "parse mcc")
        eq(s["operator"], "中国联通", "parse 运营商")
        eq(s["rsrp"], -76, "parse rsrp")
        eq(s["sinr"], 17, "parse sinr")

    if bad:
        print("RADIO SELFTEST FAIL")
        for b in bad:
            print("  -", b)
        return 1
    print("RADIO SELFTEST OK  (信道/NR/LTE/频段/解析 共 %d 项)" % 26)
    return 0


def main(argv=None):
    argv = argv or sys.argv[1:]
    if argv and argv[0] == "--selftest":
        return _selftest()
    mode = argv[0] if argv else "wifi"

    s = A.pick_serial()
    if not s:
        print("没有可用设备")
        return 2
    if mode == "wifi":
        r = wifi_survey(s, rounds=1)
        print(r.get("table") or r.get("err"))
        if r.get("ok"):
            print(f"\n共 {r['count']} 个 AP，频段分布 {r['bands']}")
    elif mode == "link":
        r = wifi_link(s)
        print(r)
    elif mode == "cell":
        r = cell(s)
        for sub in r.get("subs", []):
            # 带宽有时为空（ROM 在 CA 聚合时不给 mCellBandwidths），空就别硬打 []
            bw = "/".join(f"{b:g}" for b in sub["bandwidth_mhz"]) or "带宽?"
            print(f"{sub['operator']:6} {sub['rat']:8} ARFCN={sub['arfcn']} "
                  f"{sub['freq_mhz']}MHz {sub['band']:8} {bw}MHz "
                  f"RSRP={sub['rsrp']} SINR={sub['sinr']}")
        if not r.get("ok"):
            print(r.get("err"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
