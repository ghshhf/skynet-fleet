"""热点叠加编队（P2）——开热点 → 设备自己进来 → 手机侧组网视角。

计划书原话：「现在所有能力都是『设备在我家 LAN 上』的视角；开了热点，
任何我带到现场的设备一连进来就进编队。」

本模块把「开热点」从孤零零一个开关变成一条编队流水线：

  deploy  开热点 → 等客户端 → 读 AP 侧 ARP 表 →  ping 探活 → 与基线比對
           → 新入网/掉线各归各（哨兵同款语义：只报变化）
  clients 只读：当前连了哪些设备（IP/MAC/厂商/存活）
  diff    只读：当前 vs 基线（不开热点也能看）

数据源诚实说明：
  · 客户端 IP/MAC = 手机 AP 侧的 ARP 表（ip neigh show），这是「谁连进来了」
    的唯一真源；dumpsys 只给计数不给名单，计数只作兜底对照
  · 存活 = toybox ping（Android 全系有），一轮 -W1 -c1，RTT 即距离的近亲
    （粗估，正经估距走 proximity）
  · 端口级枚举不在这里——那是 P3 net.wallprobe 的活（同一份探测逻辑）

叠加 L6 的野外自治：手机开热点 + phone_reverse_client 反连家里电脑 =
现场设备连手机、手机把能力回路传回家里——人在现场，脑在家里。
"""
import json
import os
import re
import time

import android_device as A
from discovery import OUI_VENDOR, is_randomized_mac

BASELINE_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "hotspot_clients.json")


# ------------------------------------------------------------------ 客户端读取

_NEIGH = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+)\s+lladdr\s+([0-9a-fA-F:]{17})"
                    r"(?:\s+(\S+))?", re.M)


def _parse_neigh(txt):
    """ip neigh show → [(ip, mac, iface, state)]。

    state（REACHABLE/STALE/DELAY/FAILED...）本身就是存活信息：
    FAILED = 连过现在不通了；REACHABLE = 刚通过话。
    """
    out = []
    for m in _NEIGH.finditer(txt or ""):
        ip, iface, mac, state = m.groups()
        out.append({"ip": ip, "mac": mac.upper(), "iface": iface,
                    "state": (state or "").upper()})
    return out


def ap_clients(serial):
    """读当前热点客户端。返回 {"ok", "clients": [...], "source": str}。

    客户端 IP/MAC 只有 ARP 表一个真源；解析不出就如实说「只读到计数」，
    不编名单。
    """
    rc, txt = A.shell(serial, "ip neigh show")
    clients = _parse_neigh(txt) if rc == 0 else []
    source = "ip-neigh"
    if not clients:
        # 兜底：/proc/net/arp（列序：IP / HW type / Flags / HW address / Mask / Device）
        rc2, txt2 = A.shell(serial, "cat /proc/net/arp")
        if rc2 == 0:
            for ln in (txt2 or "").splitlines()[1:]:
                parts = ln.split()
                if len(parts) >= 6 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]) \
                        and parts[3] != "00:00:00:00:00:00":
                    clients.append({"ip": parts[0], "mac": parts[3].upper(),
                                    "iface": parts[5], "state": "ARP"})
            source = "proc-net-arp"
    # dumpsys 计数只作对照：名单有了就不看它，名单没有才拿计数兜底说话
    count = None
    rc3, txt3 = A.shell(serial, "dumpsys wifi")
    if rc3 == 0:
        cm = re.search(r'(?:getNumConnectedDevices|numConnectedDevices|mNumClients)\D*?(\d+)',
                       txt3)
        if cm:
            count = int(cm.group(1))
    for c in clients:
        c["vendor"] = OUI_VENDOR.get(c["mac"][:8].upper(), "")
        c["random_mac"] = is_randomized_mac(c["mac"])
    return {"ok": True, "clients": clients, "dumpsys_count": count,
            "source": source,
            "note": ("ARP 表读到 %d 个；dumpsys 计数 %s"
                     % (len(clients), count if count is not None else "读不到"))
            if clients else
            ("ARP 表没有邻居记录（刚开热点还没设备连，或 ROM 不吐）；"
             "dumpsys 计数 %s" % (count if count is not None else "也读不到"))}


def ping_clients(serial, clients, timeout_ms=800):
    """对每个客户端 ping 一次探活。RTT 是「活着」的直接证据。"""
    out = []
    for c in clients:
        ip = c.get("ip")
        if not ip:
            continue
        rc, txt = A.shell(serial, f"ping -c 1 -W 1 {ip}", timeout=6)
        alive = rc == 0 and "1 packets received" in (txt or "").replace("\n", " ")
        rtt = None
        if alive:
            m = re.search(r"time[=<]([\d.]+)", txt or "")
            if m:
                rtt = float(m.group(1))
        row = dict(c)
        row["ping"] = "alive" if alive else "no-reply"
        row["rtt_ms"] = rtt
        out.append(row)
    return out


# ------------------------------------------------------------------ 基线 / 差异

def load_baseline(path=None):
    path = path if path is not None else BASELINE_PATH
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            b = json.load(f)
        return b if isinstance(b, dict) else {}
    except (OSError, ValueError):
        return {}


def save_baseline(clients, path=None):
    path = path if path is not None else BASELINE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = int(time.time())
    base = {"at": now,
            "clients": {c["mac"]: {"ip": c.get("ip"), "mac": c["mac"],
                                   "vendor": c.get("vendor", ""),
                                   "last_seen": now} for c in clients}}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def diff_clients(cur, base):
    """当前 vs 基线 → {"new": [...], "gone": [...], "still": [...]}。

    MAC 为主键（IP 会变，MAC 不会——随机 MAC 的诚实标注见 random_mac）。
    没有基线 = 全算新（并提示先建基线）。
    """
    base_macs = set((base.get("clients") or {}).keys())
    cur_macs = {c["mac"] for c in cur}
    new = [c for c in cur if c["mac"] not in base_macs]
    gone = [v for mac, v in (base.get("clients") or {}).items() if mac not in cur_macs]
    still = [c for c in cur if c["mac"] in base_macs]
    return {"new": new, "gone": gone, "still": still,
            "baseline_at": base.get("at")}


# ------------------------------------------------------------------ 编排

def deploy(serial, ssid=None, password=None, settle=8, ping=True):
    """开热点 → 等客户端 → 读名单 → 探活 → 与基线比对 → 更新基线。

    这是「把现场设备带进编队」的一键动作。开热点本身是 side_effect
    （过闸门），读/比/记是这条流水线的后半截。
    """
    from fleet import creative                    # 复用已验证的开热点实现
    r = creative.hotspot(serial, True, ssid=ssid, password=password)
    if not r.get("ok"):
        return {"ok": False, "err": r.get("err") or "开热点失败"}
    time.sleep(max(2, settle))
    got = ap_clients(serial)
    clients = got["clients"]
    if ping and clients:
        clients = ping_clients(serial, clients)
    base = load_baseline()
    d = diff_clients(clients, base)
    saved = save_baseline(clients)
    lines = []
    if d["new"]:
        lines.append("新入网 %d 台：" % len(d["new"]))
        for c in d["new"]:
            who = c.get("vendor") or ("随机 MAC" if c.get("random_mac") else "未知厂商")
            lines.append("  + %s %s（%s）%s" % (
                c["ip"], c["mac"], who,
                " [ping %s]" % c.get("ping") if ping else ""))
    if d["gone"]:
        lines.append("掉线 %d 台：%s" % (
            len(d["gone"]),
            "、".join("%s %s" % (g.get("ip", "?"), g.get("mac", "")) for g in d["gone"])))
    if not lines:
        lines.append("没有新入网/掉线（当前 %d 台在线）" % len(clients))
    return {"ok": True, "deployed": True, "ssid": r["data"].get("ssid"),
            "clients": clients, "new": d["new"], "gone": d["gone"],
            "still": d["still"], "baseline_saved": saved,
            "source": got.get("source"),
            "text": "\n".join(lines),
            "note": ("新入网的设备现在就在手机子网里——下一步让它们进编队："
                     "手机侧扫描/操控走 L6 反连回传，或现场直连（见计划书 P2）")}


def clients_only(serial, ping=False):
    """只读客户端清单（不开热点）。ping=True 时逐个探活。"""
    got = ap_clients(serial)
    clients = got["clients"]
    if ping and clients:
        clients = ping_clients(serial, clients)
    return {"ok": True, "clients": clients, "source": got.get("source"),
            "dumpsys_count": got.get("dumpsys_count"),
            "text": "\n".join(
                "  %s %s %s%s" % (c["ip"], c["mac"],
                                  c.get("vendor") or ("随机MAC" if c.get("random_mac") else ""),
                                  " [%s]" % c.get("ping") if c.get("ping") else "")
                for c in clients) or "（ARP 表为空：没有设备连进来）"}


def diff_only(serial=None, ping=False):
    """只读：当前 vs 基线（ serial 给了就现读，不给就用上次清单）。"""
    if serial:
        cur = clients_only(serial, ping=ping)["clients"]
    else:
        cur = [{"ip": v.get("ip"), "mac": k, "vendor": v.get("vendor", "")}
               for k, v in (load_baseline().get("clients") or {}).items()]
    d = diff_clients(cur, load_baseline())
    lines = []
    for c in d["new"]:
        lines.append("  + %s %s" % (c.get("ip"), c.get("mac")))
    for g in d["gone"]:
        lines.append("  - %s %s" % (g.get("ip", "?"), g.get("mac", "")))
    return {"ok": True, "new": d["new"], "gone": d["gone"], "still": d["still"],
            "text": "\n".join(lines) or "（与基线无差异）",
            "note": "没有基线时全算新——先 deploy 一轮建基线" if not load_baseline()
            else "基线建于 %s" % time.strftime("%m-%d %H:%M",
                                            time.localtime(d["baseline_at"] or 0))}


# ------------------------------------------------------------------ 自测

def _selftest():
    import shutil
    import tempfile

    def check(name, cond, extra=""):
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {name} {extra}")
        if not cond:
            raise SystemExit(f"hotspot_roam selftest failed: {name}")

    tmp = tempfile.mkdtemp(prefix="hsroam_")
    bp = os.path.join(tmp, "clients.json")

    print("hotspot_roam selftest —— 热点叠加编队")

    # ① ARP 解析：真机格式（ip neigh show 的输出长相）
    sample = """192.168.43.22 dev wlan0 lladdr aa:bb:cc:dd:ee:01 REACHABLE
192.168.43.37 dev wlan0 lladdr aa:bb:cc:dd:ee:02 STALE
192.168.43.99 dev wlan0  FAILED
fe80::1 dev wlan0 lladdr aa:bb:cc:dd:ee:03 router STALE
192.168.43.22 dev wlan0 lladdr aa:bb:cc:dd:ee:01 DELAY"""
    rows = _parse_neigh(sample)
    check("IPv4 有 lladdr 的全解析（3 行）", len(rows) == 3, f"got {len(rows)}")
    check("IPv6 行被跳过", all(":" not in r["ip"] for r in rows))
    check("FAILED 无 lladdr 不收", all(r["mac"] for r in rows))
    check("REACHABLE 状态保留", rows[0]["state"] == "REACHABLE")
    check("接口名解析", rows[0]["iface"] == "wlan0")
    check("空输入不崩", _parse_neigh("") == [])
    check("垃圾输入不崩", _parse_neigh("not an arp line\n\x00\xff") == [])

    # ② 基线 / 差异：MAC 主键，新/掉/留各归各
    base = {"at": 1000, "clients": {
        "AA:BB:CC:DD:EE:01": {"ip": "192.168.43.22", "mac": "AA:BB:CC:DD:EE:01",
                              "last_seen": 1000},
        "AA:BB:CC:DD:EE:02": {"ip": "192.168.43.37", "mac": "AA:BB:CC:DD:EE:02",
                              "last_seen": 1000}}}
    cur = [{"ip": "192.168.43.22", "mac": "AA:BB:CC:DD:EE:01"},
           {"ip": "192.168.43.55", "mac": "AA:BB:CC:DD:EE:09"}]
    d = diff_clients(cur, base)
    check("新入网 1 台", len(d["new"]) == 1 and d["new"][0]["mac"].endswith("EE:09"))
    check("掉线 1 台", len(d["gone"]) == 1 and d["gone"][0]["mac"].endswith("EE:02"))
    check("仍在 1 台", len(d["still"]) == 1)
    check("IP 变了仍算仍在（MAC 主键）",
          diff_clients([{"ip": "10.0.0.9", "mac": "AA:BB:CC:DD:EE:01"}], base)["still"]
          and not diff_clients([{"ip": "10.0.0.9", "mac": "AA:BB:CC:DD:EE:01"}],
                               base)["new"])
    d2 = diff_clients(cur, {})
    check("无基线全算新", len(d2["new"]) == 2 and not d2["still"])

    # ③ 基线落盘/读回
    ok = save_baseline(cur, bp)
    check("基线落盘", ok and os.path.isfile(bp))
    back = load_baseline(bp)
    check("基线读回", set(back["clients"]) == {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:09"})
    check("基线带时间戳", back.get("at", 0) > 1_700_000_000)

    # ④ diff_only 的文本出口（基线路径指到 tmp，不碰真 HOME）
    # 注意 -m 双实例：本 selftest 跑在 __main__，要补丁的是 __main__ 的全局，
    # import fleet.hotspot_roam 拿到的是另一份实例，补它没用。
    import sys as _sys
    _main = _sys.modules["__main__"]
    orig_bp = _main.BASELINE_PATH
    _main.BASELINE_PATH = bp
    if os.path.isfile(bp):
        os.remove(bp)                  # 上一测试块落过基线，先清空测「无基线」分支
    try:
        r = diff_only(serial=None)
        check("无 serial 时用基线", r["ok"])
        check("空基线全新的提示", "先 deploy" in r["note"], r["note"])
        save_baseline(cur, bp)
        r3 = diff_only(serial=None)
        check("有基线时报基线时间", "基线建于" in r3["note"], r3["note"])
        check("有基线时无差异提示", "无差异" in r3["text"], r3["text"])
    finally:
        _main.BASELINE_PATH = orig_bp

    # ⑤ clients_only 的 ARP 打桩（不打真设备）
    orig_shell = A.shell
    A.shell = lambda serial, cmd, timeout=60: (
        (0, sample) if "ip neigh" in cmd else
        (0, "numConnectedDevices=2") if "dumpsys" in cmd else (1, ""))
    try:
        got = ap_clients("fake")
        check("打桩读到 3 个邻居", len(got["clients"]) == 3, f"got {len(got['clients'])}")
        check("dumpsys 计数作对照", got["dumpsys_count"] == 2)
        check("OUI 查厂商不崩", all("vendor" in c for c in got["clients"]))
        # /proc/net/arp 兜底路径
        A.shell = lambda serial, cmd, timeout=60: (
            (1, "") if "ip neigh" in cmd else
            (0, "IP address       HW type     Flags       HW address\n"
                "192.168.43.22    0x1         0x2         aa:bb:cc:dd:ee:01     *     wlan0\n")
            if "proc/net/arp" in cmd else (0, ""))
        got2 = ap_clients("fake")
        check("proc/net/arp 兜底读到 1 个", len(got2["clients"]) == 1
              and got2["source"] == "proc-net-arp", str(got2)[:120])
        check("MAC 列取对（不是 Flags 的 0x2）",
              got2["clients"] and got2["clients"][0]["mac"] == "AA:BB:CC:DD:EE:01",
              str(got2["clients"])[:120])
        check("全零 MAC 不收", all(c["mac"] != "00:00:00:00:00:00" for c in got2["clients"]))
    finally:
        A.shell = orig_shell

    # ⑥ deploy 的流水线：creative.hotspot 打桩，验证「开→等→读→比→记」顺序
    import fleet.creative as CR
    orig_hotspot = CR.hotspot
    calls = []
    def fake_hotspot(serial, on, ssid=None, password=None):
        calls.append(("hotspot", on))
        from fleet.core.result import DeviceResult
        return {"ok": True, "data": {"ssid": ssid or "Skynet", "clients": 0}}
    CR.hotspot = fake_hotspot
    A.shell = lambda serial, cmd, timeout=60: (
        (0, sample) if "ip neigh" in cmd else
        (0, "1 packets received, time=12.3 ms") if "ping" in cmd else
        (0, "") if "dumpsys" in cmd else (1, ""))
    _main.BASELINE_PATH = bp          # deploy 的基线也指到 tmp，不碰真 HOME
    if os.path.isfile(bp):
        os.remove(bp)                 # 清掉上一块的基线，测「首轮全新」
    try:
        r = deploy("fake", ssid="Skynet", settle=2)
        check("deploy 先开热点", calls and calls[0] == ("hotspot", True))
        check("deploy 读到客户端", r["ok"] and len(r["clients"]) == 3, str(r)[:100])
        check("deploy 探活有 RTT", any(c.get("rtt_ms") == 12.3 for c in r["clients"]))
        check("deploy 首轮全新", len(r["new"]) == 3)
        check("deploy 有文字报告", "新入网 3 台" in r["text"], r["text"][:60])
        check("deploy 写了基线", r["baseline_saved"])
        r_again = deploy("fake", ssid="Skynet", settle=2)
        check("二轮 deploy 无新入网（基线生效）", len(r_again["new"]) == 0,
              f"got {len(r_again['new'])}")
    finally:
        CR.hotspot = orig_hotspot
        A.shell = orig_shell

    shutil.rmtree(tmp, ignore_errors=True)
    print("hotspot_roam selftest: ALL PASS")
    return True


if __name__ == "__main__":
    _selftest()
