"""热状态：手机作为算力节点，最该被看见的是「它现在烫不烫、有没有在降频」。

为什么这台设备比电脑更该有这个：
  · 电脑有风扇，跑满也就 80℃ 且不掉频；这台手机是被动散热，
    SOC 一到 85℃ 就开始压频，实测连续跑分时大核能从 3.3GHz 掉到 1.6GHz，
    同一个模型同一套参数，tg 能从 50 t/s 掉到 30 出头。
  · 所以「刚才还快，现在怎么慢了」这个问题，**不查热状态就一定会答错**——
    容易去怀疑量化、怀疑线程数，实际是机身烫了。
  · 温度、频率、电池这几样，电脑侧拿不到（也不是同一个散热域），只能问手机。

数据来源（全走 adb，不装 App、不 root）：
  · `dumpsys thermalservice` —— framework 层的温度汇总 + 各档节流阈值
    （`/sys/class/thermal/thermal_zone*` 连 shell 用户都读不了，Permission denied，
     所以必须走 framework 这条；这是实测出来的，不是选择偏好）
  · `/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq` —— 各簇实时频率（这个能读）
  · `dumpsys battery` —— 电量/电池温度/是否在充电（充电时温度会额外 +5~8℃）
  · `/proc/loadavg`、`/proc/meminfo` —— 系统压力和内存余量

关于「有没有在降频」的判定，必须说清楚口径：
  频率低本身不说明降频（空闲时也低）。这里用的是「负载高 + 大核频率明显低于上限」
  这个组合去推断，属于**启发式，不是直读节流标志位**。
  真正的节流标志位在 thermal HAL 内部，dumpsys 不吐。所以结论里会标注判断依据，
  拿不准时只报数据、不下断言。

用法（命令行）：
    python thermal.py             # 一屏体检
    python thermal.py --selftest  # 离线自测（解析/判定逻辑）
"""
from __future__ import annotations

import re
import sys

import android_device as A

# Android PowerManager 的热状态等级
_STATUS_NAME = {
    0: "NONE", 1: "LIGHT", 2: "MODERATE", 3: "SEVERE",
    4: "CRITICAL", 5: "EMERGENCY", 6: "SHUTDOWN",
}
_STATUS_CN = {
    "NONE": "正常", "LIGHT": "轻微", "MODERATE": "中等",
    "SEVERE": "重度节流", "CRITICAL": "严重", "EMERGENCY": "紧急", "SHUTDOWN": "关机保护",
}


def _parse_temps(text, section):
    """从 `dumpsys thermalservice` 的一段里切出 {名字: 温度℃}。

    section 用来定位表头（"Current temperatures from HAL:" 比 "Cached temperatures:"
    新，优先用前者）。
    """
    m = re.search(re.escape(section) + r"(.*?)(?=\n\S|\Z)", text, re.S)
    if not m:
        return {}
    out = {}
    for mm in re.finditer(r"Temperature\{mValue=([\d.]+),\s*mType=\d+,\s*mName=(\w+)", m.group(1)):
        out[mm.group(2)] = round(float(mm.group(1)), 1)
    return out


def _parse_thresholds(text):
    """各传感器的第一档节流阈值（第一个非 NaN 的那个数）。

    mHotThrottlingThresholds=[NaN, NaN, NaN, 85.0, 90.0, 100.0, 117.0]
    下标 3 起才有值，对应 SEVERE/CRITICAL/EMERGENCY/SHUTDOWN。
    第一档就是「开始压频」的温度，是最该盯的数。
    """
    out = {}
    for m in re.finditer(
            r"mName=(\w+),\s*mHotThrottlingThresholds=\[([^\]]*)\]", text):
        vals = [float(x) for x in m.group(2).split(",")
                if x.strip() and x.strip().lower() != "nan"]
        if vals:
            out[m.group(1)] = min(vals)
    return out


def _cpu_util(serial, span=0.3):
    """实测 CPU 忙碌率（%）。`/proc/stat` 采两次求差。

    为什么不看 loadavg：这台 ROM 上**有 19 个内核线程常驻 D 状态**
    （hang_detect / wdtk-0..7 / cmdq_buffer_usage / VIS_CHECK_HEARTBEAT / EXTMV_TASK…），
    它们是 watchdog 类常驻任务，天生就是 D。Linux 的 loadavg 把 D 状态算作负载，
    所以这台机器的 loadavg 基线就顶在 19~20，**跟算力负载完全无关**——
    实测 CPU 只有 20% 利用率时 loadavg 也是 20。
    拿 loadavg 当「负载高」去推断降频会稳定误判，必须看真利用率。
    （`running` 数只能反映瞬时，也不够稳。）
    """
    rc, txt = A.shell(
        serial, "head -1 /proc/stat; sleep %s; head -1 /proc/stat" % span, timeout=8)
    if rc != 0 or not txt:
        return None
    lines = [ln for ln in txt.strip().splitlines() if ln.startswith("cpu ")]
    if len(lines) < 2:
        return None
    rows = []
    for ln in lines[:2]:
        vals = [int(x) for x in ln.split()[1:]]
        rows.append(vals)
    # user nice system idle iowait irq softirq steal
    def busy(r):
        return sum(r) - (r[3] if len(r) > 3 else 0) - (r[4] if len(r) > 4 else 0)

    d_total = sum(rows[1]) - sum(rows[0])
    d_busy = busy(rows[1]) - busy(rows[0])
    if d_total <= 0:
        return None
    return round(100.0 * d_busy / d_total)


def _cpu_freq(serial):
    """各簇实时频率。只取每簇第一个核作代表（同簇频率一致）。"""
    rc, txt = A.shell(serial, "cat /sys/devices/system/cpu/online", timeout=6)
    online = (txt or "").strip()
    cpus = []
    if "-" in online or "," in online:
        for part in online.split(","):
            if "-" in part:
                lo, hi = part.split("-")
                cpus.extend(range(int(lo), int(hi) + 1))
            elif part.strip():
                cpus.append(int(part))
    if not cpus:
        cpus = list(range(8))
    # 每簇取代表：实测这台是 0-3 小核 / 4-6 中核 / 7 超大核
    reps = sorted({cpus[0], cpus[len(cpus) // 2], cpus[-1]})
    clusters = []
    for c in reps:
        rc1, cur = A.shell(
            serial, f"cat /sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq", timeout=6)
        rc2, mx = A.shell(
            serial, f"cat /sys/devices/system/cpu/cpu{c}/cpufreq/cpuinfo_max_freq", timeout=6)
        try:
            cur_i, max_i = int(cur.strip()), int(mx.strip())
        except (ValueError, AttributeError):
            continue
        clusters.append({
            "cpu": c,
            "cur_mhz": round(cur_i / 1000),
            "max_mhz": round(max_i / 1000),
            "pct": round(100.0 * cur_i / max_i) if max_i else None,
        })
    return {"online": online, "clusters": clusters}


def _parse_battery(bt):
    """`dumpsys battery` → 电量/温度/电压/是否充电。

    正则必须锚行首：`Max charging voltage: 0` 会抢先匹配非锚定的 `voltage:`，
    结果电压永远报 0mV（这个 bug 真出现过）。
    """
    b = {}
    if not bt:
        return b
    for key, pat, scale in (
            ("level", r"^\s*level:\s*(\d+)", 1),
            ("temp_c", r"^\s*temperature:\s*(\d+)", 0.1),
            ("voltage_mv", r"^\s*voltage:\s*(\d+)", 1)):
        mm = re.search(pat, bt, re.M)
        if mm:
            v = float(mm.group(1)) * scale
            b[key] = round(v, 1) if scale != 1 else int(v)
    b["charging"] = bool(re.search(r"\b(AC|USB|Wireless|Dock) powered:\s*true", bt))
    return b


def read(serial):
    """一屏体检：温度 + 频率 + 电池 + 内存 + 负载，并给一句人话结论。"""
    rc, txt = A.shell(serial, "dumpsys thermalservice", timeout=10)
    if rc != 0 or "Thermal Status" not in (txt or ""):
        return {"ok": False, "err": (txt or "").strip()[:160] or "读不到 thermalservice"}

    m = re.search(r"Thermal Status:\s*(\d+)", txt)
    status = int(m.group(1)) if m else -1
    sname = _STATUS_NAME.get(status, f"UNKNOWN({status})")

    temps = _parse_temps(txt, "Current temperatures from HAL:")
    if not temps:
        temps = _parse_temps(txt, "Cached temperatures:")
    thresholds = _parse_thresholds(txt)

    # 最烫的那个（排除 SKIN，皮肤温度天然低、不说明问题）
    core_temps = {k: v for k, v in temps.items() if k in ("CPU", "GPU", "SOC", "NPU")}
    hottest = max(core_temps.items(), key=lambda kv: kv[1]) if core_temps else (None, None)

    cpu_thr = thresholds.get("CPU")
    headroom = round(cpu_thr - temps["CPU"], 1) if (cpu_thr and "CPU" in temps) else None

    freq = _cpu_freq(serial)

    rc2, bt = A.shell(serial, "dumpsys battery", timeout=8)
    b = _parse_battery(bt) if rc2 == 0 else {}

    rc3, mi = A.shell(serial, "cat /proc/meminfo", timeout=8)
    mem = {}
    if rc3 == 0 and mi:
        def kb(key):
            mm = re.search(key + r":\s*(\d+)\s*kB", mi)
            return int(mm.group(1)) if mm else None
        total, avail = kb("MemTotal"), kb("MemAvailable")
        swap_t, swap_f = kb("SwapTotal"), kb("SwapFree")
        if total:
            mem["total_mb"] = round(total / 1024)
            mem["avail_mb"] = round(avail / 1024) if avail else None
        if swap_t and swap_f is not None:
            mem["swap_used_mb"] = round((swap_t - swap_f) / 1024)

    rc4, la = A.shell(serial, "cat /proc/loadavg", timeout=6)
    load = []
    if rc4 == 0 and la:
        load = [float(x) for x in la.split()[:3]]
    util = _cpu_util(serial)

    # ---- 结论：先看热状态等级，再看「CPU 真忙 + 大核频率低」这个组合
    notes = []
    if sname != "NONE":
        notes.append(f"系统热状态已进入 {sname}（{_STATUS_CN.get(sname, sname)}），"
                     f"正在主动压频，此刻的跑分不可用")
    if headroom is not None:
        if headroom <= 0:
            notes.append(f"CPU {temps['CPU']}℃ 已越过首档节流线 {cpu_thr}℃，"
                         f"性能已被系统限住")
        elif headroom <= 8:
            notes.append(f"CPU {temps['CPU']}℃ 距首档节流线 {cpu_thr}℃ 只剩 {headroom}℃，"
                         f"再跑重负载会开始掉频")
    # CPU 真忙 + 大核没跑满 = 可疑降频（启发式）。前提是**真利用率**高，
    # 不能拿 loadavg 判——D 状态进程（等 ZRAM 换页）能把 loadavg 顶到 20 而 CPU 空闲。
    clusters = freq.get("clusters") or []
    big = max(clusters, key=lambda c: c["max_mhz"]) if clusters else None
    if big and util is not None and util >= 60 and (big["pct"] or 100) < 70:
        notes.append(f"CPU 利用率 {util}% 已吃满，但超大核只跑到 {big['pct']}%"
                     f"（{big['cur_mhz']}/{big['max_mhz']}MHz）—— 疑似降频"
                     f"（启发式判断，非直读标志位）")
    if not notes:
        if util is not None:
            notes.append(f"温度与频率都正常（CPU 利用率 {util}%），"
                         f"当前性能可代表真实水平")
        else:
            notes.append("温度与频率都正常，当前性能可代表真实水平")
    if mem.get("avail_mb") is not None and mem["avail_mb"] < 600:
        notes.append(f"可用内存只剩 {mem['avail_mb']}MB，跑大模型前应释放")
    if mem.get("swap_used_mb") and mem["swap_used_mb"] > 3000:
        notes.append(f"swap 已用 {mem['swap_used_mb']}MB（总 {mem['total_mb']}MB 物理内存），"
                     f"频繁换页会拖慢推理")
    if load and load[0] > 15 and (util or 0) < 50:
        notes.append(f"loadavg {load[0]:.1f} 但 CPU 只用了 {util}% —— 这台 ROM 有 19 个"
                     f"常驻 D 状态内核线程（watchdog 类），loadavg 基线就在 20 上下，"
                     f"不代表负载，别用它判断忙闲")

    return {
        "ok": True,
        "thermal_status": status,
        "thermal_status_name": sname,
        "thermal_status_cn": _STATUS_CN.get(sname, sname),
        "temps": temps,
        "hottest": {"name": hottest[0], "c": hottest[1]} if hottest[0] else None,
        "throttle_start_c": thresholds,
        "cpu_headroom_c": headroom,
        "cpu": freq,
        "battery": b,
        "mem": mem,
        "load": load,
        "cpu_util_pct": util,
        "verdict": "；".join(notes),
        "note": "温度来自 framework thermal HAL（sysfs 不对 shell 开放）；"
                "降频判定是「负载高+频率低」的启发式推断，不是直读标志位",
    }


# ---------------------------------------------------------------- 自测

_FIXTURE = """IsStatusOverride: false
Thermal Status: 3
Cached temperatures:
	Temperature{mValue=56.616, mType=0, mName=CPU, mStatus=0}
	Temperature{mValue=33.7, mType=2, mName=BATTERY, mStatus=0}
HAL Ready: true
Current temperatures from HAL:
	Temperature{mValue=42.161, mType=0, mName=CPU, mStatus=0}
	Temperature{mValue=42.161, mType=1, mName=GPU, mStatus=0}
	Temperature{mValue=32.9, mType=2, mName=BATTERY, mStatus=0}
	Temperature{mValue=86.2, mType=13, mName=SOC, mStatus=0}
Temperature static thresholds from HAL:
	TemperatureThreshold{mType=0, mName=CPU, mHotThrottlingThresholds=[NaN, NaN, NaN, 85.0, 90.0, 100.0, 117.0], mColdThrottlingThresholds=[NaN, NaN, NaN, NaN, NaN, NaN, NaN]}
	TemperatureThreshold{mType=1, mName=GPU, mHotThrottlingThresholds=[NaN, NaN, NaN, 85.0, 90.0, 100.0, 117.0], mColdThrottlingThresholds=[NaN, NaN, NaN, NaN, NaN, NaN, NaN]}
"""


def _selftest():
    bad = []

    def eq(got, want, tag):
        if got != want:
            bad.append(f"{tag}: got={got!r} want={want!r}")

    # 温度：必须优先取 HAL 段（42.161），不能取到 Cached（56.616）
    t = _parse_temps(_FIXTURE, "Current temperatures from HAL:")
    eq(t.get("CPU"), 42.2, "temps 取 HAL 段")
    eq(t.get("SOC"), 86.2, "temps SOC")
    eq(len(t), 4, "temps 条数")
    # Cached 段单独取
    tc = _parse_temps(_FIXTURE, "Cached temperatures:")
    eq(tc.get("CPU"), 56.6, "temps Cached 段")
    eq(_parse_temps("没有这一段", "Current temperatures from HAL:"), {}, "temps 缺段返回空")

    # 阈值：第一个非 NaN，NaN 必须被丢掉
    th = _parse_thresholds(_FIXTURE)
    eq(th.get("CPU"), 85.0, "threshold CPU")
    eq(th.get("GPU"), 85.0, "threshold GPU")

    # 热状态等级映射
    eq(_STATUS_NAME[0], "NONE", "status 0")
    eq(_STATUS_NAME[3], "SEVERE", "status 3")
    eq(_STATUS_CN["SEVERE"], "重度节流", "status 中文")

    # 余量计算：85 - 42.2 = 42.8
    eq(round(85.0 - t["CPU"], 1), 42.8, "headroom")

    # 电池：电压必须取真 `voltage:` 而不是 `Max charging voltage: 0`
    batt = _parse_battery(
        "Current Battery Service state:\n"
        "  AC powered: false\n"
        "  Max charging voltage: 0\n"
        "  level: 71\n"
        "  scale: 100\n"
        "  voltage: 4090\n"
        "  temperature: 334\n")
    eq(batt.get("level"), 71, "battery level")
    eq(batt.get("voltage_mv"), 4090, "battery 电压不取 Max charging")
    eq(batt.get("temp_c"), 33.4, "battery 温度 /10")
    eq(batt.get("charging"), False, "battery 放电中")
    eq(_parse_battery("  USB powered: true\n  voltage: 4200\n").get("charging"), True,
       "battery 充电中")
    eq(_parse_battery(""), {}, "battery 空输入")

    if bad:
        print("THERMAL SELFTEST FAIL")
        for b in bad:
            print("  -", b)
        return 1
    print("THERMAL SELFTEST OK  (温度解析/阈值/状态映射/余量/电池 共 %d 项)" % 17)
    return 0


def main(argv=None):
    argv = argv or sys.argv[1:]
    if argv and argv[0] == "--selftest":
        return _selftest()
    s = A.pick_serial()
    if not s:
        print("没有可用设备")
        return 2
    r = read(s)
    if not r.get("ok"):
        print("读不到热状态:", r.get("err"))
        return 3
    print(f"热状态 {r['thermal_status_name']}（{r['thermal_status_cn']}）  "
          f"最烫 {r['hottest']['name']} {r['hottest']['c']}℃" if r["hottest"] else "")
    print("温度: " + "  ".join(f"{k}={v}℃" for k, v in r["temps"].items()))
    print("节流线: " + "  ".join(f"{k}≥{v}℃" for k, v in r["throttle_start_c"].items())
          + (f"   CPU 余量 {r['cpu_headroom_c']}℃" if r["cpu_headroom_c"] is not None else ""))
    cl = r["cpu"]["clusters"]
    print("频率: " + "  ".join(f"cpu{c['cpu']} {c['cur_mhz']}/{c['max_mhz']}MHz({c['pct']}%)"
                              for c in cl))
    b = r["battery"]
    print(f"电池: {b.get('level')}%  {b.get('temp_c')}℃  "
          f"{'充电中' if b.get('charging') else '放电中'}  {b.get('voltage_mv')}mV")
    print(f"内存: 可用 {r['mem'].get('avail_mb')}MB / {r['mem'].get('total_mb')}MB  "
          f"swap 用 {r['mem'].get('swap_used_mb')}MB   "
          f"CPU 利用率 {r['cpu_util_pct']}%   loadavg {r['load']}")
    print(f"\n结论：{r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
