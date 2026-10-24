"""距离感知引擎 —— P0 第一枪：把「只能报方向」升级成「米数 + 靠近/远离 + 轨迹」。

主人痛点原话：「探测距离相关的，你现在就不知道距离范围是多远，只能你给我报一个
方向，我大概去猜。」本模块四件武器：

  a. RSSI → 米        对数距离路径损耗模型 d = 10^((A-rssi)/(10n))
  b. 区间不报单点     同一距离 RSSI 抖动 ±5~10dBm（多径衰落）→ 单点误差可达一倍
  c. 热冷导航         RSSI 时间序列线性回归 → 正在靠近 / 正在远离 / 原地
  d. PDR + 三角定位   步数计×步长+磁力计航向 与 3+ AP 加权最小二乘，互为校验

诚实边界（物理限制，不是没优化，写进每个出口）：
  · 室内多径让单次 RSSI 不可靠 → 一切距离输出都是区间
  · 航向依赖手持姿态，姿态不稳时 PDR 只当粗参照
  · 目标设备（摄像头/AP）自身没有位置——本模块是「无位置设备」的补位方案，
    手机自己的 GPS 是参照系，不替代对目标的估距

数据来源全部已有：radio.wifi.survey / nearby.bt.scan 的 RSSI，
termux-sensor 的 step_counter / accelerometer / magnetic_field。零新增依赖。
"""
import json
import math
import os
import time

# ------------------------------------------------------------------ 模型参数
#
# 默认校准值（用户没校准时用）：2.4GHz 室内经验值。
# A = 1 米处参考接收功率（dBm）；n = 路径损耗指数（2.0 自由空间，2.7~4.0 室内）。
DEFAULT_A_24 = -45.0
DEFAULT_A_5 = -52.0        # 5GHz 同距离衰减快 6~8dBm
DEFAULT_N = 3.0
JITTER_DB = 6.0            # 单次采样不确定度：区间宽度 = ±6dBm
MAX_DIST = 120.0           # 再远就是「信号若有若无」，不硬报数字

CALIB_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "proximity_calib.json")
SAMPLES_PATH = os.path.join(os.path.expanduser("~"), ".fleet", "proximity_samples.jsonl")


def _band_a(band, a=None):
    if a is not None:
        return a
    return DEFAULT_A_5 if band == "5G" else DEFAULT_A_24


def rssi_to_distance(rssi, a=DEFAULT_A_24, n=DEFAULT_N):
    """对数距离路径损耗模型：d = 10^((A - rssi)/(10n))，单位米。

    rssi >= A 时设备就在 1 米内（模型下限 0.1m，别报 0）。
    """
    d = 10.0 ** ((a - float(rssi)) / (10.0 * n))
    return max(0.1, min(MAX_DIST, d))


def distance_interval(rssi, band="2.4G", a=None, n=None, jitter=JITTER_DB):
    """(lo, hi) 米——一切距离输出的唯一内部形态。

    区间宽度来自 ±jitter dBm 的采样不确定度：信号强 jitter 的那一端更近，
    弱的那端更远。**报区间不报单点，这是物理限制不是保守。**
    """
    aa = _band_a(band, a)
    nn = n if n is not None else DEFAULT_N
    d_near = rssi_to_distance(rssi + jitter, aa, nn)   # 信号更强 → 更近
    d_far = rssi_to_distance(rssi - jitter, aa, nn)
    return (round(d_near, 1), round(d_far, 1))


def fmt_dist(lo, hi):
    """对用户的距离格式。1 米内单独说，不报「0.3~0.9 米」那种伪精确。"""
    if hi <= 1.5:
        return "1 米内"
    if lo >= MAX_DIST:
        return "很远（%d 米以上，信号若有若无）" % int(MAX_DIST)
    return "%g~%g 米" % (lo, hi)


# ------------------------------------------------------------------ 校准

def calibrate(samples, band="2.4G"):
    """[(d_m, rssi), ...] → {"ok":True,"a":..,"n":..,"r2":..} 或 {"ok":False,"err":..}

    模型 log10(d) = (A - rssi)/(10n) → rssi = A - 10n·log10(d)：
    对 (x=log10 d, y=rssi) 做一元线性回归，斜率 k = -10n，截距 b = A。
    少于 2 个**不同**距离就是欠定，如实报错不硬算。
    """
    pts = [(float(d), float(r)) for d, r in samples if d and d > 0]
    if len(pts) < 2:
        return {"ok": False, "err": "校准样本至少 2 条（建议 4 个不同距离）"}
    xs = [math.log10(d) for d, _ in pts]
    ys = [r for _, r in pts]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-9:
        return {"ok": False, "err": "所有样本同一个距离，无法标定（要不同距离）"}
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    k = sxy / sxx                     # 斜率 = -10n
    b = my - k * mx                   # 截距 = A
    n = -k / 10.0
    # R²：拟合质量。室内实测 R² < 0.8 时结果只能当粗估。
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (b + k * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-9 else 0.0
    if not (1.0 <= n <= 6.0):
        return {"ok": False, "err": "回归出的 n=%.2f 不在合理区间（1~6），"
                                    "样本可能有问题" % n}
    return {"ok": True, "a": round(b, 1), "n": round(n, 2), "r2": round(r2, 3),
            "band": band, "samples": len(pts)}


def load_calib(path=CALIB_PATH):
    """读校准参数。没有文件/文件坏了 → 返回空 dict（调用方用默认值，不崩）。"""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def save_calib(cal, path=CALIB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cal = dict(cal)
    cal["at"] = int(time.time())
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cal, f, ensure_ascii=False, indent=1)
        return True
    except OSError:
        return False


def add_calib_sample(dist_m, rssi, band="2.4G", bssid="", path=SAMPLES_PATH):
    """追加一条校准样本（已知距离测 RSSI）。攒够 4 个不同距离跑 calibrate()。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"at": int(time.time()), "dist_m": float(dist_m),
           "rssi": int(rssi), "band": band, "bssid": bssid}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_calib_samples(path=SAMPLES_PATH):
    out = []
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except ValueError:
                        continue
    except OSError:
        pass
    return out


def calibrate_from_file(band="2.4G", path=SAMPLES_PATH):
    """从样本文件跑回归。样本 <2 个不同距离 → 如实报差几个。"""
    rows = read_calib_samples(path)
    pts = [(r["dist_m"], r["rssi"]) for r in rows if r.get("band") == band]
    dists = sorted({round(d, 1) for d, _ in pts})
    if len(dists) < 2:
        return {"ok": False, "err": "校准样本只有 %d 个距离（%s），至少 2 个不同距离"
                % (len(dists), dists)}
    return calibrate(pts, band=band)


# ------------------------------------------------------------------ 热冷导航

def hot_cold(series, min_db=2.0):
    """RSSI 时间序列 → 趋势。series = [(t, rssi), ...]（t 单调，秒）。

    对序列做最小二乘直线回归得斜率（dBm/s），净变化 = 斜率 × 时长。
    判定：净增强 ≥ min_db → approaching（正在靠近）
          净减弱 ≥ min_db → receding（正在远离）
          否则 still（原地/噪声里打转）
    返回 dict 带 trend / delta_db / slope / samples。
    """
    pts = [(float(t), float(r)) for t, r in series if t is not None]
    if len(pts) < 3:
        return {"ok": False, "err": "序列至少 3 个采样点（现在 %d）" % len(pts),
                "trend": "unknown"}
    t0 = pts[0][0]
    xs = [t - t0 for t, _ in pts]
    ys = [r for _, r in pts]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx < 1e-9:
        return {"ok": True, "trend": "still", "delta_db": 0.0,
                "slope_db_per_s": 0.0, "samples": len(pts),
                "note": "所有采样同一时刻，判不了趋势"}
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx                       # dBm/s
    span = xs[-1] - xs[0]
    delta = slope * span                    # 整段净变化 dBm
    if delta >= min_db:
        trend = "approaching"
    elif delta <= -min_db:
        trend = "receding"
    else:
        trend = "still"
    return {"ok": True, "trend": trend, "delta_db": round(delta, 1),
            "slope_db_per_s": round(slope, 3), "samples": len(pts),
            "span_s": round(span, 1)}


_TREND_CN = {"approaching": "正在靠近", "receding": "正在远离",
             "still": "基本原地（或在噪声里打转）", "unknown": "判不了"}


def trend_cn(t):
    return _TREND_CN.get(t, t)


# ------------------------------------------------------------------ 三角定位

def trilaterate(meas, iters=60):
    """[(x, y, d), ...] → {"ok":True,"x":..,"y":..,"rmse":..}；<3 点欠定。

    加权最小二乘（权重 1/d：近的测得准，说话大声）。高斯-牛顿迭代，
    初始解 = 加权质心。rmse 是残差均方根——它就是「这次定位多不自信」。
    """
    pts = [(float(x), float(y), float(d)) for x, y, d in meas if d > 0]
    if len(pts) < 3:
        return {"ok": False, "err": "三角定位至少 3 个已知位置 AP（现在 %d）" % len(pts)}
    w = [1.0 / d for _, _, d in pts]
    sw = sum(w)
    x = sum(wi * px for wi, (px, _, _) in zip(w, pts)) / sw
    y = sum(wi * py for wi, (_, py, _) in zip(w, pts)) / sw
    for _ in range(iters):
        jx = jy = 0.0
        h00 = h01 = h11 = 0.0
        for (px, py, d), wi in zip(pts, w):
            dx, dy = x - px, y - py
            r = math.hypot(dx, dy)
            if r < 0.5:
                r = 0.5
            # 残差 f = r - d；雅可比 row = (dx/r, dy/r)
            fx, fy = dx / r, dy / r
            res = r - d
            jx += wi * fx * res
            jy += wi * fy * res
            h00 += wi * fx * fx
            h01 += wi * fx * fy
            h11 += wi * fy * fy
        det = h00 * h11 - h01 * h01
        if abs(det) < 1e-9:
            break
        # 解 2x2：δ = -H⁻¹g（H=[h00,h01;h01,h11]）
        step_x = (-jx * h11 + jy * h01) / det
        step_y = (jx * h01 - jy * h00) / det
        step = math.hypot(step_x, step_y)
        if step > 50:
            step_x, step_y = step_x * 50 / step, step_y * 50 / step
        x += step_x
        y += step_y
        if math.hypot(step_x, step_y) < 1e-4:
            break
    rmse = math.sqrt(sum(wi * ((math.hypot(x - px, y - py) - d) ** 2)
                         for (px, py, d), wi in zip(pts, w)) / sw)
    return {"ok": True, "x": round(x, 1), "y": round(y, 1),
            "rmse": round(rmse, 1), "anchors": len(pts),
            "note": "rmse 即本次定位的残差——它大就说明这次别信"}


# ------------------------------------------------------------------ 航向 + PDR

def heading_deg(accel, mag, axis="y"):
    """加速度+磁力计 → 设备某轴的航向角（度，0=正北，顺时针）。

    做法（自带倾斜补偿，平放/斜举都算）：
      g     = 归一化加速度      —— 设备坐标里的「天」方向
      m_h   = mag - (mag·g)g    —— 磁场水平分量（设备坐标里）
      north = m_h 归一化        —— 设备坐标里的「北」（磁场水平分量指向磁北）
      east  = north × g         —— 设备坐标里的「东」（右手定则）
      heading = atan2(v·east, v·north)，v = 指定轴指向的单位向量

    axis 选哪个轴 = 手持姿态（这是本函数的诚实边界）：
      "y" 平放/口袋/车载支架：手机顶端即行进方向
      "z" 竖屏握持屏幕朝脸：摄像头所指（-z）即行进方向
    竖屏时 y 轴指向天空，它的方位角没有意义——所以不是猜一个，是让调用方按姿态选。

    任一传感器近零（没出数）/ 磁场与重力共线（姿态退化）→ 返 None，调用方降级。
    """
    try:
        ax, ay, az = (float(v) for v in accel)
        mx, my, mz = (float(v) for v in mag)
    except (TypeError, ValueError):
        return None
    g = math.sqrt(ax * ax + ay * ay + az * az)
    m = math.sqrt(mx * mx + my * my + mz * mz)
    if g < 1e-6 or m < 1e-6:
        return None
    ax, ay, az = ax / g, ay / g, az / g
    # 磁场水平分量：减去沿重力方向的投影 → 设备坐标里的「北」
    dot = mx * ax + my * ay + mz * az
    hx, hy, hz = mx - dot * ax, my - dot * ay, mz - dot * az
    hn = math.sqrt(hx * hx + hy * hy + hz * hz)
    if hn < 1e-6:
        return None                      # 磁场与重力共线，姿态退化了
    nx, ny, nz = hx / hn, hy / hn, hz / hn
    # east = north × g
    ex, ey, ez = (ny * az - nz * ay, nz * ax - nx * az, nx * ay - ny * ax)
    v = {"y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, -1.0)}.get(axis, (0.0, 1.0, 0.0))
    return math.degrees(math.atan2(v[0] * ex + v[1] * ey + v[2] * ez,
                                   v[0] * nx + v[1] * ny + v[2] * nz)) % 360.0


class Pdr:
    """步程推算（Pedestrian Dead Reckoning）：步数增量 × 步长 + 航向 → 轨迹。

    用法：
        p = Pdr(stride=0.72)            # 步长个人标定，0.6~0.85 常见
        p.update(step_counter_读数, heading_deg(...))   # 每次传感更新调一次
    坐标：起点 (0,0)，x 东 y 北，单位米。
    诚实边界：步长是估值、航向随姿态抖——PDR 是「我这段时间大概走了多远」
    的粗参照，与 RSSI 估距互校用，不当真值。
    """

    def __init__(self, stride=0.72, x=0.0, y=0.0):
        self.stride = float(stride)
        self.x, self.y = float(x), float(y)
        self._last_steps = None
        self.steps_total = 0

    def update(self, steps, hdg):
        """steps = step_counter 绝对值（单调递增）；hdg = 航向角或 None。
        返回本次位移 (dx, dy)。"""
        if steps is None:
            return (0.0, 0.0)
        steps = int(steps)
        if self._last_steps is None:
            self._last_steps = steps
            return (0.0, 0.0)
        d_steps = steps - self._last_steps
        self._last_steps = steps
        if d_steps <= 0:
            return (0.0, 0.0)            # 计数器回退/没走：不动
        self.steps_total += d_steps
        if hdg is None:
            return (0.0, 0.0)            # 没航向只记步不走位
        dist = d_steps * self.stride
        dx = dist * math.sin(math.radians(hdg))
        dy = dist * math.cos(math.radians(hdg))
        self.x += dx
        self.y += dy
        return (round(dx, 2), round(dy, 2))

    def moved(self):
        """起点到现在的直线距离（米）。"""
        return round(math.hypot(self.x, self.y), 1)


# ------------------------------------------------------------------ 采样与出口

def ap_sampler(serial):
    """造一个 sampler：每调一次扫一轮 WiFi，返回 [(bssid, rssi), ...]。

    供 track() 用；设备无关，测试时换假 sampler 即可。
    """
    def _s():
        import survey                     # 延迟导入：survey 不依赖本模块
        r = survey.scan(serial, settle=3, rounds=1)
        if not r.get("ok"):
            return []
        return [(a["bssid"], a["rssi"]) for a in r["aps"]]
    return _s


def track(sampler, seconds=30, interval=1.5, targets=None):
    """热冷跟踪主循环：每 interval 秒采一次，共 seconds 秒。

    sampler() → [(bssid, rssi), ...]；targets = 只跟这几个 BSSID（默认全跟）。
    返回 {bssid: {"series":[(t,rssi)], "verdict": hot_cold(...)}}。
    每轮采样间隔别低于 1s——WiFi 扫描本身要 4-6 秒一回合，采再密也是重复值。
    """
    if targets:
        want = {t.upper() for t in targets}
    else:
        want = None
    series = {}
    t_start = time.time()
    t_end = t_start + max(3, seconds)
    # 防飞圈：正常情况下每 interval 秒一轮；万一 sleep 失灵（测试/异常环境），
    # 采样数到顶也停，不然能把 CPU 转到冒烟。
    max_rounds = int(max(3, seconds) / max(0.5, interval) * 3) + 100
    rounds = 0
    while time.time() < t_end and rounds < max_rounds:
        rounds += 1
        # 时间戳取「调度时刻」与「实际墙钟」的较大值：真机上每轮扫描要几秒，
        # 墙钟主导（斜率反映真实耗时）；测试把 sleep 打空时墙钟不走，
        # 调度时刻主导（时间轴确定，不受 Windows ~15.6ms 时钟分辨率影响）。
        ts = max(time.time(), t_start + (rounds - 1) * max(0.5, interval))
        for bssid, rssi in sampler():
            if want and bssid.upper() not in want:
                continue
            series.setdefault(bssid, []).append((ts, rssi))
        time.sleep(max(0.5, interval))
    out = {}
    for bssid, s in series.items():
        s.sort()
        out[bssid] = {"series": [(round(t - s[0][0], 1), r) for t, r in s],
                      "verdict": hot_cold(s)}
    return out


def estimate_aps(rows, calib=None):
    """survey rows → 每个 AP 的距离区间（近的在前）。"""
    cal = calib if calib is not None else load_calib()
    n = cal.get("n", DEFAULT_N)
    a24 = cal.get("a_24", DEFAULT_A_24)
    a5 = cal.get("a_5", DEFAULT_A_5)
    out = []
    for r in rows:
        rssi = r.get("rssi")
        if rssi is None:
            continue
        band = r.get("band") or "2.4G"
        aa = a5 if band == "5G" else a24
        lo, hi = distance_interval(rssi, band=band, a=aa, n=n)
        out.append({"bssid": r.get("bssid", ""), "ssid": r.get("ssid", ""),
                    "band": band, "rssi": rssi,
                    "dist_lo": lo, "dist_hi": hi, "text": fmt_dist(lo, hi),
                    "kind": r.get("kind", ""), "vendor": r.get("vendor", "")})
    out.sort(key=lambda x: (x["dist_lo"], -x["rssi"]))
    return out


def track_report(res, calib=None, top=8):
    """track() 结果 → 人话报告。每个目标一行：趋势 + 当前区间。"""
    cal = calib if calib is not None else load_calib()
    n = cal.get("n", DEFAULT_N)
    a24 = cal.get("a_24", DEFAULT_A_24)
    a5 = cal.get("a_5", DEFAULT_A_5)
    lines = []
    items = sorted(res.items(),
                   key=lambda kv: -abs(kv[1]["verdict"].get("delta_db", 0)))
    for bssid, d in items[:top]:
        v = d["verdict"]
        if not v.get("ok"):
            continue
        last = d["series"][-1][1] if d["series"] else None
        band = "2.4G"                     # track 不带频段，按 2.4G 保守估
        lo, hi = distance_interval(last, band=band, a=a24, n=n) if last is not None else (0, 0)
        lines.append(f"{bssid}：{trend_cn(v['trend'])}"
                     f"（净变化 {v['delta_db']:+g}dBm / {v['span_s']:g}s，"
                     f"{v['samples']} 次采样）→ 当前 {fmt_dist(lo, hi)}")
    return lines


# ------------------------------------------------------------------ 自测

def _selftest():
    def check(name, cond, extra=""):
        tag = "PASS" if cond else "FAIL"
        print(f"  [{tag}] {name} {extra}")
        if not cond:
            raise SystemExit(f"proximity selftest failed: {name}")

    print("proximity selftest —— 距离感知引擎")

    # ① 模型单调性：信号越强越近；A 处就是 1 米
    check("A 处 = 1 米", abs(rssi_to_distance(-45, a=-45, n=3) - 1.0) < 1e-6)
    check("信号强→更近", rssi_to_distance(-40, a=-45, n=3)
          < rssi_to_distance(-60, a=-45, n=3))
    d1 = rssi_to_distance(-55, a=-45, n=3)
    check("n=3 时 -55dBm ≈ 2.15m", 2.0 < d1 < 2.3, f"got {d1:.2f}")
    check("下限 0.1m", rssi_to_distance(-10, a=-45, n=3) == 0.1)
    check("上限 120m", rssi_to_distance(-200, a=-45, n=3) == 120.0)

    # ② 区间：永远 lo<=hi，且包含点估计
    for rssi in (-95, -70, -55, -45, -30):
        lo, hi = distance_interval(rssi)
        mid = rssi_to_distance(rssi)
        check(f"区间有序且含点估 rssi={rssi}",
              lo <= hi and lo <= mid <= hi, f"[{lo},{hi}] mid={mid:.1f}")
    check("1 米内合并说法", fmt_dist(0.5, 1.2) == "1 米内")
    check("区间格式", fmt_dist(3.1, 8.4) == "3.1~8.4 米", fmt_dist(3.1, 8.4))

    # ③ 校准回归：合成数据（真值 a=-46, n=2.8）应能回归出来
    synth = []
    for d in (1, 2, 3, 5, 8, 12):
        r = -46 - 10 * 2.8 * math.log10(d)
        synth.append((d, r))
    c = calibrate(synth)
    check("校准收敛", c.get("ok"), str(c))
    check("回归 A ≈ -46", abs(c["a"] + 46) < 0.5, f"got {c['a']}")
    check("回归 n ≈ 2.8", abs(c["n"] - 2.8) < 0.05, f"got {c['n']}")
    check("R² ≈ 1", c["r2"] > 0.999, f"got {c['r2']}")
    check("同距离样本拒算", not calibrate([(5, -60), (5, -62)]).get("ok"))
    check("单条样本拒算", not calibrate([(5, -60)]).get("ok"))

    # ④ 热冷：合成「走近」序列（-70 线性爬到 -50）应判 approaching
    approaching = [(i * 1.5, -70 + i * 1.0) for i in range(21)]
    v = hot_cold(approaching)
    check("走近判定", v["trend"] == "approaching", str(v))
    receding = [(i * 1.5, -50 - i * 1.0) for i in range(21)]
    check("远离判定", hot_cold(receding)["trend"] == "receding")
    still = [(i * 1.5, -60 + (1 if i % 2 else -1)) for i in range(21)]
    check("原地判定", hot_cold(still)["trend"] == "still")
    check("三点以下拒判", not hot_cold([(0, -60), (1, -59)]).get("ok"))
    check("中文映射", trend_cn("approaching") == "正在靠近")

    # ⑤ 三角定位：三锚环绕的真点 (3,4) 应被找回
    meas = [(0, 0, 5.0), (10, 0, math.hypot(3 - 10, 4)), (5, 10, math.hypot(3 - 5, 4 - 10))]
    for ax_, ay_, d in meas:
        check(f"锚({ax_},{ay_})距离自洽",
              abs(math.hypot(3 - ax_, 4 - ay_) - d) < 0.5)
    t = trilaterate(meas)
    check("三角定位收敛", t.get("ok"), str(t))
    check("定位找回 (3,4)", abs(t["x"] - 3) < 0.5 and abs(t["y"] - 4) < 0.5,
          f"got ({t['x']},{t['y']})")
    check("rmse 小", t["rmse"] < 0.5, f"got {t['rmse']}")
    check("两点欠定拒算", not trilaterate([(0, 0, 5), (10, 0, 7)]).get("ok"))

    # ⑥ 航向：测的是「设备轴指向」，姿态不同选不同轴
    # 平放机顶朝北：磁场北在设备 +y → y 轴方位 0°
    h = heading_deg((0, 0, 9.8), (0, 50, 0))
    check("平放顶朝北 = 0°", h is not None and abs(h) < 1, f"got {h}")
    # 平放机顶朝东：磁场北在设备 -x → y 轴方位 90°
    h = heading_deg((0, 0, 9.8), (-50, 0, 0))
    check("平放顶朝东 = 90°", h is not None and abs(h - 90) < 1, f"got {h}")
    # 平放机顶朝南：磁场北在设备 -y → y 轴方位 180°
    h = heading_deg((0, 0, 9.8), (0, -50, 0))
    check("平放顶朝南 = 180°", h is not None and abs(h - 180) < 1, f"got {h}")
    # 竖屏握持面朝北：磁场北在设备 -z → -z（摄像头向）方位 0°
    h = heading_deg((0, 9.8, 0), (0, 0, -50), axis="z")
    check("竖屏面朝北 = 0°", h is not None and abs(h) < 1, f"got {h}")
    # 竖屏握持面朝东：用户面东时北在左手侧，设备 x 指右（南）→ 磁北 = (-50,0,0)
    # -z（摄像头向）应指向东 = 90°
    h = heading_deg((0, 9.8, 0), (-50, 0, 0), axis="z")
    check("竖屏面朝东 = 90°", h is not None and abs(h - 90) < 1, f"got {h}")
    check("零加速度返 None", heading_deg((0, 0, 0), (1, 2, 3)) is None)
    # 磁场与重力共线（姿态退化）→ None，不猜
    check("磁与重力共线返 None", heading_deg((0, 0, 9.8), (0, 0, 50)) is None)

    # ⑦ PDR：50 步 × 0.8m 步长，一路向北 → 走了 40m 到 (0,40)
    p = Pdr(stride=0.8)
    p.update(1000, 0.0)                    # 首拍只记基数
    dx, dy = p.update(1050, 0.0)
    check("PDR 北移 40m", abs(dy - 40) < 1e-6 and abs(dx) < 1e-6, f"got ({dx},{dy})")
    check("PDR 直线距离", p.moved() == 40.0)
    p2 = Pdr(stride=0.8)
    p2.update(0, None); p2.update(100, 90.0)     # 一路向东
    check("PDR 东移 80m", abs(p2.x - 80) < 1e-6 and abs(p2.y) < 1e-6,
          f"got ({p2.x},{p2.y})")
    p3 = Pdr()
    p3.update(0, 0.0); p3.update(100, 0.0); p3.update(100, 0.0)   # 计数器不动
    check("计数器不动不加位移", p3.moved() == 72.0 and p3.x == 0.0,
          f"got ({p3.x},{p3.y})")
    check("步数只计增量", p3.steps_total == 100, f"got {p3.steps_total}")

    # ⑧ track：假 sampler 合成「一个 BSSID 信号渐强」
    calls = {"n": 0}
    def fake():
        calls["n"] += 1
        return [("AA:BB:CC:DD:EE:01", -70 + calls["n"] * 2),
                ("AA:BB:CC:DD:EE:02", -60)]
    orig_sleep = time.sleep
    time.sleep = lambda s: None           # 自测不等真钟
    try:
        res = track(fake, seconds=4, interval=0.01)
    finally:
        time.sleep = orig_sleep
    v = res["AA:BB:CC:DD:EE:01"]["verdict"]
    check("track 走近判定", v["trend"] == "approaching", str(v))
    check("track 没采样到不出现", "AA:BB:CC:DD:EE:02" in res)
    rep = track_report(res)
    check("track 报告有字", rep and "正在靠近" in rep[0], rep[:1])

    # ⑨ estimate_aps：区间 + 排序（近的在前）
    rows = [{"bssid": "A", "ssid": "near", "band": "2.4G", "rssi": -40},
            {"bssid": "B", "ssid": "far", "band": "5G", "rssi": -80}]
    est = estimate_aps(rows, calib={})
    check("估计排序近在前", est[0]["ssid"] == "near" and est[1]["ssid"] == "far")
    check("5G 用 5G 的 A", est[1]["dist_lo"] > est[0]["dist_hi"])

    print("proximity selftest: ALL PASS")
    return True


if __name__ == "__main__":
    _selftest()
