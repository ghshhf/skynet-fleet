"""身边全景：IP 层（电脑看）+ 无线层（手机当天线）。

为什么要两个一起：电脑的射频被它自己开的移动热点占满，扫不到周边 AP；
手机的射频没被占，能看见二十几个邻居网络。反过来，手机只看得到无线层的
AP 名，看不到谁在哪台 IP 上、开了什么端口。两边拼起来才是「身边有什么」。

    python nearby.py            # 两层都跑
    python nearby.py --lan      # 只 IP 层
    python nearby.py --wifi     # 只无线层
    python nearby.py --json     # 机器可读
"""
import json
import sys
import time
from collections import Counter

import android_device as A
import survey as V
import sweep as S


def run(with_lan=True, with_wifi=True):
    out = {"at": int(time.time()), "lan": None, "wifi": None, "notes": []}
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
        }
        if not devs:
            out["notes"].append("IP 层没扫到设备（网卡断了？）")
    if with_wifi:
        d = A.attach()
        if not d["ok"]:
            out["wifi"] = {"ok": False, "err": d.get("err")}
            out["notes"].append("手机接不上，无线层缺失")
        else:
            g = V.scan(d["serial"])
            if g.get("ok"):
                out["wifi"] = {"ok": True, "aps": g["aps"], "summary": V.summary(g["aps"])}
            else:
                out["wifi"] = {"ok": False, "err": g.get("err")}
                out["notes"].append("无线扫描失败：" + str(g.get("err"))[:80])
    return out


def report(out):
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

    if out.get("notes"):
        lines.append("\n注意：" + "；".join(out["notes"]))
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
    }
    t = report(out)
    assert "44 台设备" in t and "2 个网段" in t
    assert "20 台（45%）" in t            # 摄像头占比按实数算，不四舍五入到整数
    assert "2 个 AP" in t
    assert "midea_e3_0543" in t
    assert "隐藏网络 1 个" in t

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

    print("nearby 自测：4 项全过")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = run(with_lan="--wifi" not in argv, with_wifi="--lan" not in argv)
    if "--json" in argv:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(report(out))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
