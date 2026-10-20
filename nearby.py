"""身边全景：IP 层（电脑看）+ 无线层（手机当天线）。

为什么要两个一起：电脑的射频被它自己开的移动热点占满，扫不到周边 AP；
手机的射频没被占，能看见二十几个邻居网络。反过来，手机只看得到无线层的
AP 名，看不到谁在哪台 IP 上、开了什么端口。两边拼起来才是「身边有什么」。

每次跑完留一份指纹在 ~/.fleet/nearby_last.json，下次自动比对，谁进来了、
谁走了，一眼能看见——扫描的价值不在「这一刻有什么」，在「变化」。

    python nearby.py            # 两层都跑，并与上次比对
    python nearby.py --lan      # 只 IP 层
    python nearby.py --wifi     # 只无线层
    python nearby.py --json     # 机器可读
    python nearby.py --no-save  # 不留指纹（只读探测）
"""
import json
import os
import sys
import time
from collections import Counter

import android_device as A
import ble as B
import survey as V
import sweep as S

HIST = os.path.join(os.path.expanduser("~"), ".fleet", "nearby_hist.json")
BT_SECS = 8         # 蓝牙扫一轮的时长；BLE 广播是事件式的，扫久点才收得全
LAYERS = ("lan", "wifi", "bt")
LAYER_CN = {"lan": "IP 层", "wifi": "无线层", "bt": "蓝牙层"}
LAYER_UNIT = {"lan": "台", "wifi": "个", "bt": "个"}


def run(with_lan=True, with_wifi=True, with_bt=True):
    out = {"at": int(time.time()), "lan": None, "wifi": None, "bt": None, "notes": []}
    if with_lan:
        r = S.sweep(do_wifi=False)   # 电脑侧扫 WiFi 是浪费：射频被热点占了
        devs = r.get("devices") or []
        kinds = Counter(d.get("category") or d.get("kind") or "未知" for d in devs)
        out["lan"] = {
            "ok": bool(devs), "device_count": len(devs),
            "subnets": r.get("subnets") or [],
            "by_kind": dict(kinds),
            "cameras": sum(v for k, v in kinds.items() if "摄像" in str(k)),
            "profile": r.get("profile") or [],
            "elapsed": r.get("elapsed"),
            "devices": [{"ip": d.get("ip"), "mac": d.get("mac") or "",
                         "category": d.get("category") or "",
                         "category_cn": d.get("category_cn") or "",
                         "vendor": d.get("vendor") or ""} for d in devs],
        }
        if not devs:
            out["notes"].append("IP 层没扫到设备（网卡断了？）")
    dev = None
    if with_wifi or with_bt:
        dev = A.attach()          # 手机只接一次，两层共用
    if with_wifi:
        d = dev
        if not d or not d.get("ok"):
            out["wifi"] = {"ok": False, "err": d.get("err")}
            out["notes"].append("手机接不上，无线层缺失")
        else:
            g = V.scan(d["serial"], rounds=3)   # 一轮会漏 AP，多轮取并集才稳
            if g.get("ok"):
                out["wifi"] = {"ok": True, "aps": g["aps"], "summary": V.summary(g["aps"])}
            else:
                out["wifi"] = {"ok": False, "err": g.get("err")}
                out["notes"].append("无线扫描失败：" + str(g.get("err"))[:80])
    if with_bt:
        d = dev
        if not d or not d.get("ok"):
            out["bt"] = {"ok": False, "err": d.get("err")}
            out["notes"].append("蓝牙层缺失：手机接不上")
        else:
            st = B.bt_state(d["serial"])
            if st == "off":
                out["bt"] = {"ok": False, "err": "蓝牙关着"}
                out["notes"].append("蓝牙层跳过：手机蓝牙关着（ble.py --on 打开）")
            else:
                g = B.scan(d["serial"], secs=BT_SECS)
                if g.get("ok"):
                    out["bt"] = {"ok": True, "devices": g["devices"],
                                 "summary": B.summary(g["devices"])}
                else:
                    out["bt"] = {"ok": False, "err": g.get("err")}
                    out["notes"].append("蓝牙扫描失败：" + str(g.get("err"))[:80])
    return out


def fingerprint(out):
    """只抽会变的部分：谁在（IP / BSSID）以及它是什么。"""
    fp = {"at": out.get("at") or 0, "lan": {}, "wifi": {}, "bt": {}}
    lan = out.get("lan") or {}
    for d in lan.get("devices") or []:
        if d.get("ip"):
            fp["lan"][d["ip"]] = {"mac": d.get("mac") or "",
                                  "kind": d.get("category_cn") or d.get("category") or ""}
    wifi = out.get("wifi") or {}
    for a in wifi.get("aps") or []:
        if a.get("bssid"):
            fp["wifi"][a["bssid"]] = {"ssid": a.get("ssid") or "",
                                      "kind": a.get("kind") or "",
                                      "rssi": a.get("rssi")}
    for x in (out.get("bt") or {}).get("devices") or []:
        if x.get("mac"):
            fp["bt"][x["mac"]] = {"ssid": x.get("name") or "",
                                  "kind": x.get("kind") or "",
                                  "rssi": x.get("rssi")}
    return fp


WINDOW = 5          # 看最近几次：手机单次扫描抖动很大，只比上一份会把抖动当进出
FRINGE = -80        # 弱于这个值的 AP 在墙外/几十米外，信号本身就在噪底上下浮


def _near(rec):
    """信号够得着才算「在场」。拿不到 rssi 时按够得着算，不冤枉人。"""
    r = (rec or {}).get("rssi")
    return r is None or r >= FRINGE


def trend(hist, cur, window=WINDOW):
    """按「最近 window 次里出现过几次」判存续，而不是只比上一份。

    手机的 WiFi 扫描一轮只听得见一部分 AP（实测连扫三次分别 17/21/19 个，
    两次相邻完整扫描只有 20 个 BSSID 重合），拿它和上一份硬要比，得出的
    「新出现/消失」全是噪声。两处降噪：
      ① 计数：只有最近 window 次每次都在的才算「一直在」，只有这次才见的才算「新的」；
      ② 卡信号：-80dBm 以下的远场 AP 本来就在噪底上漂，进出无意义，只报个数。
    """
    hist = [h for h in (hist or []) if isinstance(h, dict)][-window:]
    t = {"runs": len(hist), "window": window,
         "age": (cur.get("at") or 0) - (hist[-1].get("at") or 0) if hist else 0}
    for layer in LAYERS:
        t[layer] = {"solid": [], "new": [], "lost": [], "fringe": 0}
    if not hist:
        return t
    n = len(hist)
    for layer in LAYERS:
        cur_map = cur.get(layer) or {}
        trackable = {k for k, v in cur_map.items() if _near(v)}
        if layer in ("wifi", "bt"):
            t[layer]["fringe"] = len(cur_map) - len(trackable)
        seen = {k: 0 for k in trackable}
        for h in hist:
            hm = h.get(layer) or {}
            for k in trackable:
                if k in hm and _near(hm[k]):
                    seen[k] += 1
        for k, c in seen.items():
            if c == 0:
                t[layer]["new"].append({"key": k, **cur_map[k]})
            elif c == n:
                t[layer]["solid"].append({"key": k, **cur_map[k]})
        for k in set().union(*[set((h.get(layer) or {})) for h in hist]):
            if k in trackable:
                continue
            if k in cur_map:                      # 还在，只是退到远场 → 不算走
                continue
            # 只有「最近 window 次里每次都在、且每次都够得着」的才算真消失
            if all(k in (h.get(layer) or {}) and _near(h[layer][k]) for h in hist):
                t[layer]["lost"].append({"key": k, **hist[-1][layer][k]})
        if layer in ("wifi", "bt"):     # 同一 BSSID 可能被多次统计，去重后按名字排
            for kind in ("new", "lost", "solid"):
                uniq = {x["key"]: x for x in t[layer][kind]}
                t[layer][kind] = sorted(uniq.values(),
                                        key=lambda x: x.get("ssid") or "~")
    t["changed"] = any(t[l]["new"] or t[l]["lost"] for l in LAYERS)
    return t


def load_hist():
    try:
        with open(HIST, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("runs") or []
    except (OSError, ValueError, AttributeError):
        return []


def save_hist(hist, fp, keep=12):
    runs = ([h for h in hist if isinstance(h, dict)] + [fp])[-keep:]
    os.makedirs(os.path.dirname(HIST), exist_ok=True)
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump({"runs": runs}, f, ensure_ascii=False)
    return HIST


def _ago(sec):
    sec = max(int(sec or 0), 0)          # 时钟回拨时不许出现「-5 分钟前」
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    if sec < 86400:
        return f"{sec // 3600} 小时"
    return f"{sec // 86400} 天"


def _trend_lines(t):
    if not t or not t.get("runs"):
        return []
    lines = ["",
             f"【存续】按最近 {t['runs']} 次扫描算（本次距上次 {_ago(t['age'])}）："]
    for layer in LAYERS:
        seg = t.get(layer) or {}
        u = LAYER_UNIT[layer]
        if not (seg.get("solid") or seg.get("new") or seg.get("lost")):
            continue
        lines.append(f"  · {LAYER_CN[layer]}：一直在 {len(seg['solid'])} {u}"
                     + (f"，这次才见 {len(seg['new'])} {u}" if seg["new"] else "")
                     + (f"，连续几次不见 {len(seg['lost'])} {u}" if seg["lost"] else ""))
        if seg.get("fringe"):
            lines.append(f"    （另有 {seg['fringe']} {u}弱于 {FRINGE}dBm 的远场，"
                         "信号在噪底上漂，不参与进出判断）")
    for layer in ("bt", "wifi", "lan"):        # 蓝牙的「新出现」最值得先看
        seg = t.get(layer) or {}
        if seg.get("new"):
            names = [(x.get("ssid") or x.get("kind") or "(无名)") for x in seg["new"][:6]]
            lines.append(f"  新见到的（{LAYER_CN[layer]}）：" + "、".join(names))
        if seg.get("lost"):
            names = [(x.get("ssid") or x.get("kind") or "(无名)") for x in seg["lost"][:6]]
            lines.append(f"  不见了的（{LAYER_CN[layer]}）：" + "、".join(names))
    if not t["changed"]:
        lines.append("  （没有真的进出，都是扫描抖动范围内）")
    return lines


def report(out, t=None):
    lines = []
    lan, wifi = out.get("lan"), out.get("wifi")

    if lan and lan["ok"]:
        lines.append(f"【IP 层】{lan['device_count']} 台设备，"
                     f"{len(lan['subnets'])} 个网段，{lan['elapsed']}s")
        for net in lan["subnets"]:
            lines.append(f"  · {net}")
        top = sorted(lan["by_kind"].items(), key=lambda x: -x[1])[:6]
        lines.append("  类型：" + "，".join(f"{k} {v}" for k, v in top))
        if lan["cameras"]:
            pct = round(lan["cameras"] * 100 / lan["device_count"])
            lines.append(f"  摄像头 {lan['cameras']} 台（{pct}%）")
        for p in lan["profile"][:4]:
            lines.append(f"  {p}")

    if wifi and wifi.get("ok"):
        s = wifi["summary"]
        lines.append(f"\n【无线层】{s['total']} 个 AP，近处（-65dBm 内）{s['near']} 个")
        lines.append("  类型：" + "，".join(f"{k} {v}" for k, v in
                                         sorted(s["kinds"].items(), key=lambda x: -x[1])))
        if s["near_names"]:
            lines.append("  最近的：" + "、".join(s["near_names"][:6]))
        smart = [a for a in wifi["aps"] if a["kind"] in ("家电", "开放网络（多为家电配网）",
                                                        "智能设备")]
        if smart:
            lines.append("  家电/智能设备："
                         + "、".join(f"{a['ssid']}（{a['vendor'] or '?'} {a['rssi']}）"
                                    for a in smart[:6]))
        hidden = [a for a in wifi["aps"] if a["kind"] == "隐藏网络"]
        if hidden:
            lines.append(f"  隐藏网络 {len(hidden)} 个（多数是路由器的 5G 伴生 SSID）")
    elif wifi and not wifi.get("ok"):
        lines.append(f"\n【无线层】不可用：{wifi.get('err')}")

    bt = out.get("bt")
    if bt and bt.get("ok"):
        s = bt["summary"]
        lines.append(f"\n【蓝牙层】{s['total']} 个在广播，有名 {s['named']} 个，"
                     f"近处（-75dBm 内）{s['near']} 个")
        lines.append("  类型：" + "，".join(f"{k} {v}" for k, v in
                                         sorted(s["kinds"].items(), key=lambda x: -x[1])))
        named = [d for d in bt["devices"] if d.get("name")][:8]
        if named:
            lines.append("  认得出的：" + "、".join(
                f"{d['name']}（{d['kind']} {d['rssi']}）" for d in named))
    elif bt and not bt.get("ok"):
        lines.append(f"\n【蓝牙层】不可用：{bt.get('err')}")

    if out.get("notes"):
        lines.append("\n注意：" + "；".join(out["notes"]))
    lines.extend(_trend_lines(t))
    return "\n".join(lines)


def _selftest():
    # ① 两层都在：标题、设备数、AP 数都要出现
    out = {
        "at": 0,
        "lan": {"ok": True, "device_count": 44, "subnets": ["192.168.0", "192.168.137"],
                "by_kind": {"camera": 20, "unknown": 18, "phone": 3},
                "cameras": 20, "profile": ["网关 192.168.0.16"], "elapsed": 15.4},
        "wifi": {"ok": True, "aps": [
            {"ssid": "midea_e3_0543", "kind": "家电", "vendor": "美的", "rssi": -77},
            {"ssid": "", "kind": "隐藏网络", "vendor": "", "rssi": -80},
        ], "summary": {"total": 2, "near": 0, "kinds": {"家电": 1, "隐藏网络": 1},
                       "near_names": []}},
        "notes": [],
        "bt": {"ok": True, "devices": [
            {"mac": "B0:96:EA:C9:28:E4", "name": "midea", "kind": "家电", "rssi": -72},
            {"mac": "E4:1F:07:33:3A:22", "name": "mobike", "kind": "共享单车", "rssi": -90},
        ], "summary": {"total": 2, "named": 2, "near": 1,
                       "kinds": {"家电": 1, "共享单车": 1}}},
    }
    t = report(out)
    assert "44 台设备" in t and "2 个网段" in t
    assert "20 台（45%）" in t            # 摄像头占比按实数算，不四舍五入到整数
    assert "2 个 AP" in t
    assert "midea_e3_0543" in t
    assert "隐藏网络 1 个" in t
    assert "【蓝牙层】2 个在广播" in t and "认得出的" in t and "共享单车" in t

    # ② 无线层缺：不能抛异常，要显式说明不可用
    out2 = {"at": 0, "lan": out["lan"], "wifi": {"ok": False, "err": "手机接不上"},
            "notes": ["手机接不上，无线层缺失"]}
    t2 = report(out2)
    assert "【无线层】不可用：手机接不上" in t2
    assert "注意：" in t2 and "无线层缺失" in t2

    # ③ 只有 IP 层：不出现无线层段落
    t3 = report({"at": 0, "lan": out["lan"], "wifi": None, "notes": []})
    assert "【IP 层】" in t3 and "【无线层】" not in t3

    # ④ 全空：不炸，notes 照常打印
    t4 = report({"at": 0, "lan": None, "wifi": None, "notes": ["IP 层没扫到设备（网卡断了？）"]})
    assert "网卡断了" in t4

    # ⑤ 指纹只留会变的东西：没 ip / 没 bssid 的条目要丢掉
    fp = fingerprint({"at": 100, "lan": {"devices": [
        {"ip": "192.168.0.5", "mac": "aa:bb", "category": "camera", "category_cn": "摄像头"},
        {"ip": "", "mac": "cc:dd"},                       # 没有 ip 的残缺条目
    ]}, "wifi": {"aps": [
        {"bssid": "aa:11:22:33:44:55", "ssid": "X", "kind": "家电"},
        {"bssid": "", "ssid": "Y", "kind": "光猫"},
    ]}})
    assert list(fp["lan"]) == ["192.168.0.5"] and fp["lan"]["192.168.0.5"]["kind"] == "摄像头"
    assert list(fp["wifi"]) == ["aa:11:22:33:44:55"]

    # ⑥ 存续：一直在的、只有这次才见的、连着几次都不在的，三类要分清
    h1 = {"at": 100, "lan": {"192.168.0.9": {"kind": "pc"}},
          "wifi": {"B1": {"ssid": "常在的", "kind": "光猫"}}}
    h2 = {"at": 130, "lan": {"192.168.0.9": {"kind": "pc"}},
          "wifi": {"B1": {"ssid": "常在的", "kind": "光猫"}}}
    cur = {"at": 160, "lan": {"192.168.0.9": {"kind": "pc"},
                              "192.168.0.100": {"kind": "摄像头"}},
           "wifi": {"B1": {"ssid": "常在的", "kind": "光猫"},
                    "B2": {"ssid": "刚冒出来", "kind": "路由器"}}}
    g = trend([h1, h2], cur)
    assert [x["key"] for x in g["lan"]["solid"]] == ["192.168.0.9"]
    assert [x["key"] for x in g["lan"]["new"]] == ["192.168.0.100"]
    assert [x["key"] for x in g["wifi"]["solid"]] == ["B1"]
    assert [x["key"] for x in g["wifi"]["new"]] == ["B2"]
    assert g["lan"]["lost"] == [] and g["wifi"]["lost"] == []
    assert g["changed"] is True and g["age"] == 30   # 距上一次（不是第一次）

    # ⑦ 抖动不算消失：历史上只缺过某一次，就不能报「走了」
    j1 = {"at": 100, "lan": {}, "wifi": {"B1": {"ssid": "老网络"}}}
    j2 = {"at": 120, "lan": {}, "wifi": {}}              # 这次抖没了
    j3 = {"at": 140, "lan": {}, "wifi": {"B1": {"ssid": "老网络"}}}
    g2 = trend([j1, j2, j3], {"at": 160, "lan": {}, "wifi": {}})
    assert g2["wifi"]["lost"] == [], g2["wifi"]["lost"]

    # ⑧ 连着几次都在、这次没有 —— 才叫真的走了
    g3 = trend([j1, {"at": 140, "lan": {}, "wifi": {"B1": {"ssid": "老网络"}}}],
               {"at": 160, "lan": {}, "wifi": {}})
    assert [x["ssid"] for x in g3["wifi"]["lost"]] == ["老网络"]

    # ⑨ 没有历史时不给结论，也不许编出时间差
    assert trend([], {"at": 9, "lan": {}, "wifi": {}})["runs"] == 0
    assert _trend_lines(None) == [] and _trend_lines({"runs": 0}) == []

    # ⑩ 「没有真的进出」要明说，不能让人以为漏打印了
    same = {"at": 160, "lan": {"192.168.0.9": {}}, "wifi": {"B1": {"ssid": "常在的"}}}
    g4 = trend([{"at": 140, "lan": {"192.168.0.9": {}}, "wifi": {"B1": {"ssid": "常在的"}}}], same)
    assert g4["changed"] is False
    assert any("抖动" in x for x in _trend_lines(g4))

    # ⑪ 远场 AP 不参与进出判断：-85dBm 的墙外网络本来就在噪底上漂
    f1 = {"at": 100, "lan": {}, "wifi": {"B9": {"ssid": "墙外的", "rssi": -88}}}
    fc = {"at": 160, "lan": {}, "wifi": {"B9": {"ssid": "墙外的", "rssi": -86},
                                         "B8": {"ssid": "刚扫到的远场", "rssi": -90}}}
    g5 = trend([f1], fc)
    assert g5["wifi"]["fringe"] == 2, g5["wifi"]["fringe"]
    assert g5["wifi"]["new"] == [] and g5["wifi"]["lost"] == []
    assert g5["changed"] is False
    # 退到远场也算还在，不许报「走了」
    g6 = trend([{"at": 100, "lan": {}, "wifi": {"B9": {"ssid": "墙外的", "rssi": -70}}}],
               {"at": 160, "lan": {}, "wifi": {"B9": {"ssid": "墙外的", "rssi": -88}}})
    assert g6["wifi"]["lost"] == []

    # ⑫ 蓝牙层也进存续统计，单位是「个」，名字取不到就按类型叫
    bh = [{"at": 100, "lan": {}, "wifi": {}, "bt": {"M1": {"ssid": "midea", "kind": "家电", "rssi": -70}}}]
    bc = {"at": 160, "lan": {}, "wifi": {},
          "bt": {"M1": {"ssid": "midea", "kind": "家电", "rssi": -75},
                 "M2": {"ssid": "", "kind": "共享单车", "rssi": -78}}}
    bd = trend(bh, bc)
    assert [x["key"] for x in bd["bt"]["solid"]] == ["M1"]
    assert [x["key"] for x in bd["bt"]["new"]] == ["M2"]
    body = _trend_lines(bd)
    assert any("蓝牙层" in x for x in body)
    assert any("共享单车" in x for x in body)      # 无名设备用类型代替名字

    print("nearby 自测：12 项全过")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = run(with_lan=("--wifi" not in argv and "--bt" not in argv),
              with_wifi=("--lan" not in argv and "--bt" not in argv),
              with_bt=("--no-bt" not in argv and "--lan" not in argv
                       and "--wifi" not in argv))
    fp = fingerprint(out)
    g = None
    if "--no-save" not in argv:
        hist = load_hist()
        g = trend(hist, fp) if hist else None   # 没有历史就不编造「没变」
        save_hist(hist, fp)
    if "--json" in argv:
        out["trend"] = g
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(report(out, g))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
