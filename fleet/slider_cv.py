# -*- coding: utf-8 -*-
"""天网 · 滑块视觉与轨迹（纯 Pillow + numpy，零新增依赖）
============================================================
为什么单独成模块，不直接写进设备里：

  1. **算法要能单测**。设备层掺了注册表、审批闸门、依赖探活，一测就得起一整套；
     而这里输入两个 PIL 对象、输出一个数字，`python -m fleet.slider_cv --selftest`
     合成数据就能跑完。门禁 t65 正是这么做的。
  2. **两条腿可以分开用**。找缺口不一定要拖动（离线分析、调试截图都要），
     造轨迹也不一定要缺口（距离已知时直接生成）。硬绑成一条链会互相拖累。
  3. **这是整个滑块方案里唯一"算法正确性可以证明"的部分**。其他部分
     （反检测底座、行为采集、厂商 JS）都依赖外部版本，而 NCC / 贝塞尔 / 缓动
     是对的就永远对。

三条判定路径，按证据强度裁决（顺序不是拍脑袋）：

    内容匹配  content  缺口保留原内容、只压暗 → 灰度掩码 NCC。命中就是硬证据
    平坦检测  flat     缺口被抹成纯色 → 局部方差骤降到全图中位数的零头
    形状匹配  shape    只能靠轮廓对 → 常对不上（缺口是白框双线，alpha 是单线），标 weak

**最重要的一句话**：`solve_gap` 会在拿不准时返回 `weak`，让调用方重试。
滑块输一次通常只是换张图，乱提交一次可能触发风控降级 —— 能说"我不确定"比给错答案值钱。
"""
import base64
import io
import math
import random
import sys

# 🔴 这两个依赖**不许顶层硬 import**（2026-11-02 补）。
# 原因不是洁癖：`catalog.py` 在模块顶层 `from .kinds.slider_device import ...`，
# 而 slider_device 又顶层 import 本模块 —— 于是「本机没装 Pillow」会一路炸到
# `import fleet.catalog`，把**全部 415 条能力**一起带走，连 `/api/caps` 都返不出东西。
# 仓库里 vision.py / motion.py / host.py 全用函数内延迟 import，就是这个原因；
# 本文件是唯一破例的，破的还是最贵的那条路。
# 现在：模块永远可 import，缺依赖只在真调用时报一句人话。
try:
    import numpy as np
    from PIL import Image
    _DEPS_ERR = ""
except Exception as _e:                                  # noqa: BLE001
    np = None
    Image = None
    _DEPS_ERR = ("缺 Pillow/numpy（%s）。装：pip install Pillow numpy"
                 % (_e,))


def deps_ok():
    """依赖齐不齐。能力声明照旧在（能看见），只是调不动 —— 与 probe 的口径一致。"""
    return not _DEPS_ERR


def _require():
    if _DEPS_ERR:
        raise RuntimeError(_DEPS_ERR)


# ---------------------------------------------------------------- 阈值（改这里，别散落在函数里）

# 内容匹配：分数够高 + 与次高峰拉得开 = 强证据
CONTENT_STRONG_SCORE = 0.55
CONTENT_STRONG_MARGIN = 0.08
# 内容匹配：分数中等 = 弱证据（能用但标 weak）
CONTENT_WEAK_SCORE = 0.40
# 平坦检测：缺口处方差要低到全图中位数的这个比例以下
FLAT_RATIO_LIMIT = 0.10
# 滑块图自身纹理强度低于这个值就别谈 NCC 了（纯色模板匹配无意义）
TILE_STD_MIN = 8.0
# 掩码内缩像素数
MASK_PAD = 4


# ---------------------------------------------------------------- 像素工具

def _gray(a):
    """RGB(A) → 灰度 float32。亮度加权，不用简单平均（平均会丢边缘）。"""
    a = np.asarray(a).astype(np.float32)
    if a.ndim == 3:
        return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]
    return a


def _sobel(img):
    """3x3 Sobel 边缘幅度。自己算，**不依赖 cv2** —— 这是 numpy 能做到的核心。"""
    p = np.pad(img, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]
          - p[:-2, :-2] - 2 * p[1:-1, :-2] - p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + 2 * p[2:, 2:]
          - p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:])
    return np.hypot(gx, gy)


def _ncc_2d(bg_g, tile_g, mask=None):
    """归一化互相关（TM_CCOEFF_NORMED 等价实现），二维搜索，支持掩码。

    三个必须做对的点，缺一条结果就是错的：

      1. **二维**：只搜水平方向会算错，因为滑块块在背景里的 y 不一定是 0。
         真机上 y 常由接口给（极验 v4 的 load 响应里有 `ypos`），但不能假设给。
      2. **带掩码**：滑块 alpha 边缘透明，转 RGB 变黑，不遮掉等于把"一圈黑边"
         也拿去匹配，结果系统性偏移。
      3. **减均值再归一化**：不减均值的话亮度高的区域永远得分高。

    返回 (H-th+1, W-tw+1) 的得分矩阵。
    """
    H, W = bg_g.shape
    th, tw = tile_g.shape
    if H < th or W < tw:
        raise ValueError("滑块图比背景还大：bg %dx%d / tile %dx%d" % (W, H, tw, th))
    win = np.lib.stride_tricks.sliding_window_view(bg_g, (th, tw)).astype(np.float32)

    m = np.ones((th, tw), dtype=np.float32) if mask is None else mask.astype(np.float32)
    msum = float(m.sum()) + 1e-9

    t = (tile_g.astype(np.float32) - float((tile_g * m).sum() / msum)) * m
    t_norm = float(np.sqrt((t * t).sum())) + 1e-9

    wmean = (win * m).sum(axis=(2, 3), keepdims=True) / msum
    wc = (win - wmean) * m
    wnorm = np.sqrt((wc * wc).sum(axis=(2, 3))) + 1e-9
    return (wc * t).sum(axis=(2, 3)) / (wnorm * t_norm)


def _shape_template(tile_rgba):
    """把 alpha 轮廓变成"形状模板"：边缘强、内部弱。

    不用 alpha 原值（内部也是 1，会跟背景内容乱配），取它的**梯度**，
    得到一圈轮廓线，正好对应缺口那圈边框。
    """
    a = np.array(tile_rgba.split()[-1]).astype(np.float32) / 255.0
    return _sobel(a * 255.0)


def _peak(scores, y, x, r=8):
    """最高分之外的"第二峰"——判断这次匹配有没有区分度。

    r 邻域内的点要挖掉再看：同一个缺口附近本来就是一片高分，不挖的话
    次高峰永远紧挨着最高峰，差值恒为 0，看不出问题。
    """
    s = scores.copy()
    y0, y1 = max(0, y - r), min(s.shape[0], y + r + 1)
    x0, x1 = max(0, x - r), min(s.shape[1], x + r + 1)
    s[y0:y1, x0:x1] = -9.0
    j = int(np.argmax(s))
    return -9.0 if s.flat[j] <= -9.0 else float(s.flat[j])


def _flat_map(bg_g, mask):
    """带掩码的局部方差图 —— 找「最平坦的那块」。

    为什么需要这条腿：缺口内容被完全抹掉（填纯色/半透明色块）时，
    局部方差骤降到接近 0，这比形状匹配强得多。形状匹配在真实场景经常对不上：
    缺口是"白边框双线"，滑块 alpha 是"单线"，本来就不是同一个东西。
    """
    th, tw = mask.shape
    win = np.lib.stride_tricks.sliding_window_view(bg_g, (th, tw)).astype(np.float32)
    m = mask.astype(np.float32)
    msum = float(m.sum()) + 1e-9
    mean = (win * m).sum(axis=(2, 3), keepdims=True) / msum
    return ((win - mean) ** 2 * m).sum(axis=(2, 3)) / msum


# ---------------------------------------------------------------- 缺口定位

def detect_gap(bg_img, tile_img, probe=None):
    """返回缺口位置与可信度明细（三条路径全给，裁决交给 solve_gap）。

    **三个护栏**（血泪：不加就会"自信地给出错误答案"）：
      · tile_std —— 滑块图自身纹理强度。太低说明是纯色块，NCC 无意义
      · margin   —— 最高分与次高峰的差。太小说明"到处都是这个图案"
      · 掩码内缩 —— 滑块图最外圈不可信（alpha 羽化、自带描边、缺口白边框），
                    边界像素两边永远对不上，留着只会拉低真值分数

    **别用 PIL 的 MinFilter 做腐蚀**：它在图像边界用边缘像素延拓，边界根本不被腐蚀。
    实测 7x7 MinFilter 后掩码仍剩 1688/1936 px，白边框（245）混进匹配，
    真值窗口 std 从 13 涨到 65、相关系数掉到 0.30。手工砍边最可靠。
    """
    _require()
    tile_rgba = tile_img.convert("RGBA")
    alpha = np.array(tile_rgba.split()[-1])
    ys, xs = np.nonzero(alpha > 32)
    if len(xs) == 0:
        raise ValueError("滑块图全透明，没有可匹配的内容")
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    tile_c = tile_rgba.crop(box)

    bg_g = _gray(np.array(bg_img.convert("RGB")))
    tile_g = _gray(np.array(tile_c.convert("RGB")))

    raw_m = (np.array(tile_c.split()[-1]) > 32)
    m = np.zeros_like(raw_m)
    pad = MASK_PAD
    if raw_m.shape[0] > pad * 2 + 2 and raw_m.shape[1] > pad * 2 + 2:
        m[pad:-pad, pad:-pad] = raw_m[pad:-pad, pad:-pad]
    if m.sum() < 100:                       # 小滑块砍过头就退回原掩码
        m = raw_m
    m = m.astype(np.float32)

    valid = tile_g[m > 0]
    tile_std = float(valid.std()) if valid.size else 0.0

    s_content = _ncc_2d(bg_g, tile_g, m)
    cy, cx = np.unravel_index(int(np.argmax(s_content)), s_content.shape)
    best_c = float(s_content[cy, cx])

    s_shape = _ncc_2d(_sobel(bg_g), _shape_template(tile_c))
    sy, sx = np.unravel_index(int(np.argmax(s_shape)), s_shape.shape)

    flat = _flat_map(bg_g, m)
    fy, fx = np.unravel_index(int(np.argmin(flat)), flat.shape)
    flat_here = float(flat[fy, fx])
    flat_med = float(np.median(flat))

    out = {
        # 内容匹配
        "x_content": int(cx), "y_content": int(cy),
        "score_content": round(best_c, 4),
        "margin_content": round(best_c - _peak(s_content, int(cy), int(cx)), 4),
        # 形状匹配（弱）
        "x_shape": int(sx), "y_shape": int(sy),
        "score_shape": round(float(s_shape[sy, sx]), 4),
        # 平坦检测
        "x_flat": int(fx), "y_flat": int(fy),
        "flat_ratio": round(flat_here / (flat_med + 1e-9), 4),
        # 护栏
        "tile_std": round(tile_std, 2),
        "alpha_box": list(box),
        # 尺寸：调用方要做图→CSS 的缩放换算
        "bg_size": [int(bg_g.shape[1]), int(bg_g.shape[0])],
        "tile_size": [int(tile_g.shape[1]), int(tile_g.shape[0])],
        # 分数矩阵形状：位置理论空间大小，调试用
        "positions": int(s_content.size),
    }
    if probe is not None:                   # 调试：真值处到底得多少分、排第几
        py, px = probe
        if 0 <= py < s_content.shape[0] and 0 <= px < s_content.shape[1]:
            v = float(s_content[py, px])
            out["score_at_probe"] = round(v, 4)
            out["rank_at_probe"] = int((s_content > v).sum()) + 1
    return out


def solve_gap(r):
    """三选一裁决，返回 (x, basis, confidence)。

    裁决顺序按**证据强度**，不是按分支顺序：

      0. 模板自身没纹理 → 一律 weak。纯色滑块的 content 分数恒为 0（减均值后全零），
         会掉到 shape 分支给出一个"看起来有依据"的错答案。这条护栏实测必要。
      1. 内容匹配分数高且与次高峰拉得开 → 硬证据，直接用（strong）
      2. 否则看有没有「方差比全图中位数低一个数量级」的平坦块 → 纯色缺口（medium）
      3. 都不成立用形状匹配，标 weak，调用方应当重试而不是硬提交

    滑块输一次通常只是换张图；乱提交一次可能触发风控降级。
    """
    if r["tile_std"] < TILE_STD_MIN:
        # 位置仍给 flat 那条腿（背景真有个纯色缺口的话它是对的），但没把握。
        return r["x_flat"], "flat", "weak"
    if r["score_content"] >= CONTENT_STRONG_SCORE and r["margin_content"] >= CONTENT_STRONG_MARGIN:
        return r["x_content"], "content", "strong"
    if r["flat_ratio"] <= FLAT_RATIO_LIMIT:
        return r["x_flat"], "flat", "medium"
    if r["score_content"] >= CONTENT_WEAK_SCORE:
        return r["x_content"], "content", "weak"
    return r["x_shape"], "shape", "weak"


def detect(bg_img, tile_img, viewport_width=None, probe=None):
    """高层封装：给定两张图，直接给出要拖多远。

    viewport_width 是**关键换算**：极验/腾讯的背景图常是 2x 像素密度
    （图 680px 显示 340px），按图片坐标去拖会差一倍。传了显示宽度就自动换算。
    """
    _require()
    r = detect_gap(bg_img, tile_img, probe=probe)
    x, basis, conf = solve_gap(r)
    ratio = 1.0
    if viewport_width and r["bg_size"][0] > 0:
        ratio = float(viewport_width) / float(r["bg_size"][0])
    r.update({
        "x": x,
        "x_display": round(x * ratio, 2),
        "scale": round(ratio, 4),
        "basis": basis,
        "confidence": conf,
        "warn": ("滑块模板纹理太弱（std %.1f < %d），匹配不可信"
                 % (r["tile_std"], TILE_STD_MIN)) if r["tile_std"] < TILE_STD_MIN else "",
    })
    return r


# ---------------------------------------------------------------- 轨迹生成

def gen_track(distance, seed=None, tile_width=44):
    """生成一条「像人」的拖动轨迹，返回 (points, timestamps, duration)。

    参考开源做法（HumanCursor / shy-mouse-playwright / MouseWont）的共识：
      · 三次贝塞尔，控制点带随机偏移 → 路径不是直线
      · 时长按 Fitts 定律 MT = a + b·log2(D/W+1) → 距离越大越久，但不线性
      · 速度曲线钟形（两端慢中间快）→ 三次 ease-in-out
      · 过冲 0.8%~2.2% 再回修（真人几乎不会精确停在目标上）
      · 每点微抖动（手抖，起手最不稳，越接近目标越稳）
      · 点间隔 8~30ms 随机（轮询率不是固定值）
    """
    rng = random.Random(seed)
    d = float(distance)
    if d <= 0:
        raise ValueError("拖动距离必须 > 0")
    dur = min(2.2, max(0.55, 0.28 + 0.22 * math.log2(d / float(tile_width) + 1.0)))
    n = max(26, int(dur * 90))

    bend = d * rng.uniform(0.03, 0.08) * rng.choice((-1, 1))
    c1 = (d * 0.30, bend * rng.uniform(0.6, 1.0))
    c2 = (d * 0.70, bend * rng.uniform(0.2, 0.7))

    pts = []
    for i in range(1, n + 1):
        t = i / n
        e = 4 * t ** 3 if t < 0.5 else 1 - ((-2 * t + 2) ** 3) / 2   # ease-in-out
        x = 3 * (1 - e) ** 2 * e * c1[0] + 3 * (1 - e) * e ** 2 * c2[0] + e ** 3 * d
        y = 3 * (1 - e) ** 2 * e * c1[1] + 3 * (1 - e) * e ** 2 * c2[1] + e ** 3 * 0
        j = (1 - e) * 1.6 + 0.3
        x += rng.gauss(0, j)
        y += rng.gauss(0, j)
        pts.append((x, y))

    if rng.random() < 0.55 and d > 60:
        over = d + d * rng.uniform(0.008, 0.022)
        for k in range(3):
            pts.append((over - (over - d) * (k + 1) / 3.0, rng.gauss(0, 0.8)))

    pts[-1] = (d, pts[-1][1] * 0.3)          # 末点对齐目标
    ts, acc = [], 0.0
    for _ in pts:
        acc += rng.uniform(0.008, 0.030)
        ts.append(acc)
    return pts, ts, dur


def to_cdp_events(pts, ts, x0=0.0, y0=0.0, button="left"):
    """转成 CDP / Playwright 的鼠标事件序列。

    关键：**逐点发 Input.dispatchMouseEvent**，不是 page.mouse.move(瞬移)。
    风控看的是 move 事件的数量、间隔分布、有没有 down/up 夹住。

    `buttons=1` 不能少 —— 缺了会被判成"悬停"而不是"按住拖动"，
    这条踩过（2026-11-02 实测，CDP 序列化丢 buttons 直接不触发拖拽）。
    """
    ev = [{"type": "mousePressed", "x": round(x0, 2), "y": round(y0, 2),
           "button": button, "buttons": 1, "clickCount": 1, "t": 0.0}]
    for (x, y), t in zip(pts, ts):
        ev.append({"type": "mouseMoved", "x": round(x0 + x, 2), "y": round(y0 + y, 2),
                   "button": button, "buttons": 1, "t": round(t, 4)})
    ev.append({"type": "mouseReleased", "x": round(x0 + pts[-1][0], 2),
               "y": round(y0 + pts[-1][1], 2),
               "button": button, "buttons": 0, "clickCount": 1,
               "t": round(ts[-1] + 0.02, 4)})
    return ev


# ---------------------------------------------------------------- 图像读取

def load_image(src):
    """把路径 / bytes / base64(data-url 也行) 变成 PIL.Image。"""
    _require()
    if isinstance(src, Image.Image):
        return src
    if isinstance(src, (bytes, bytearray)):
        return Image.open(io.BytesIO(bytes(src)))
    if isinstance(src, str):
        s = src.strip()
        if s.startswith("data:") and "," in s:
            s = s.split(",", 1)[1]
        if s.startswith("http://") or s.startswith("https://"):
            from . import net
            with net.urlopen(s, timeout=20) as r:
                return Image.open(io.BytesIO(r.read()))
        # 纯 base64（长度够长且不像路径）
        if len(s) > 256 and "/" not in s[:40] and "\\" not in s[:40]:
            try:
                return Image.open(io.BytesIO(base64.b64decode(s)))
            except Exception:
                pass
        return Image.open(s)
    raise TypeError("不认识的图像来源类型：%s" % type(src).__name__)


# ---------------------------------------------------------------- 自测

def _synth(mode="shadow", seed=7, true_x=187, tile=44, w=277, h=160):
    """造一张合成样本：背景 + 滑块块。

    三种背景纹理都是踩过的坑，写在这儿当地图：
      ① 大色块 + 渐变  → 缺口被一块纯色盖住，滑块退化成纯色块，NCC 失去区分度
      ② sin/cos 图案   → 严格周期，伪峰 0.95 压过真峰，margin 只剩 0.02（真实照片不长这样）
      ③ 正确做法       → 马赛克色块 + 双三次放大 + 高频噪声（≈自然照片频谱，无重复结构）
    """
    _require()
    rng = np.random.default_rng(seed)
    small = rng.integers(30, 225, size=(h // 8 + 2, w // 8 + 2, 3)).astype(np.uint8)
    base = Image.fromarray(small).resize((w, h), Image.BICUBIC)
    arr = np.array(base).astype(np.float32) + rng.normal(0, 7, (h, w, 3))
    clean = np.clip(arr, 0, 255).astype(np.uint8)

    y0 = (h - tile) // 2
    bg = clean.copy()
    hole = (slice(y0, y0 + tile), slice(true_x, true_x + tile))
    if mode == "shadow":
        bg[hole] = (bg[hole].astype(np.float32) * 0.40).astype(np.uint8)
    else:
        bg[hole] = np.array([18, 18, 22], dtype=np.uint8)
    bg_img = Image.fromarray(bg)
    # 缺口边框：圆角，与滑块 alpha 形状一致（形状不一致连人也对不齐）
    _d = __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw(bg_img)
    _d.rounded_rectangle([true_x, y0, true_x + tile - 1, y0 + tile - 1],
                         radius=8, outline=(245, 245, 245), width=2)

    t = Image.fromarray(clean).crop((true_x, y0, true_x + tile, y0 + tile)).convert("RGBA")
    mask = Image.new("L", (tile, tile), 0)
    __import__("PIL.ImageDraw", fromlist=["ImageDraw"]).Draw(mask).rounded_rectangle(
        [0, 0, tile - 1, tile - 1], radius=8, fill=255)
    t.putalpha(mask)
    return bg_img, t, true_x, y0


def _selftest():
    out = {"ok": False, "checks": []}

    def ck(cond, msg):
        out["checks"].append(("PASS" if cond else "FAIL") + " " + msg)
        return cond

    ok = True
    for mode in ("shadow", "solid"):
        bg, tile, tx, ty = _synth(mode=mode)
        r = detect(bg, tile, probe=(ty, tx))
        ok &= ck(r["x"] == tx, "%s 命中 x=%d（真值 %d，误差 %+d）"
                 % (mode, r["x"], tx, r["x"] - tx))
        ok &= ck(r["confidence"] in ("strong", "medium"),
                 "%s 置信度不为 weak（实得 %s/%s）" % (mode, r["confidence"], r["basis"]))
        ok &= ck(r["tile_std"] > TILE_STD_MIN, "%s 滑块有纹理（std %.1f）" % (mode, r["tile_std"]))
    # 缩放换算
    bg, tile, tx, ty = _synth(mode="shadow")
    r2 = detect(bg, tile, viewport_width=138.5, probe=(ty, tx))
    ok &= ck(abs(r2["x_display"] - tx * 0.5) <= 1.0,
             "2x 图按显示宽度换算正确（%.1f vs %.1f）" % (r2["x_display"], tx * 0.5))
    # 轨迹
    pts, ts, dur = gen_track(187, seed=11)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    steps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    mid = steps[len(steps) // 2 - 2: len(steps) // 2 + 3]
    ok &= ck(len(pts) >= 26, "点数 >= 26（实得 %d）" % len(pts))
    ok &= ck(0.5 <= dur <= 2.2, "时长 0.5~2.2s（实得 %.2f）" % dur)
    ok &= ck(abs(xs[-1] - 187) < 1.0, "末点对齐目标（实得 %.2f）" % xs[-1])
    ok &= ck(max(ys) - min(ys) >= 0.5, "y 有抖动（幅度 %.2fpx）" % (max(ys) - min(ys)))
    ok &= ck(sum(mid) / 5 > sum(steps[:4]) / 4 * 1.2,
             "速度钟形：中段 %.2f > 首段 %.2f×1.2"
             % (sum(mid) / 5, sum(steps[:4]) / 4))
    ok &= ck(all(b >= a for a, b in zip(ts, ts[1:])), "时间戳单调递增")
    # CDP 序列化
    ev = to_cdp_events(pts, ts, 100, 200)
    ok &= ck(ev[0]["type"] == "mousePressed" and ev[-1]["type"] == "mouseReleased",
             "首按下末抬起")
    ok &= ck(all(e.get("buttons") == 1 for e in ev if e["type"] == "mouseMoved"),
             "move 事件都带 buttons=1")
    ok &= ck(abs(ev[-1]["x"] - (100 + 187)) < 1.5, "净位移正确（%.1f）" % (ev[-1]["x"] - 100))

    out["ok"] = bool(ok)
    return out


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--selftest" in argv:
        r = _selftest()
        for line in r["checks"]:
            print("  " + line)
        print("\n%s" % ("PASS: slider_cv 全部通过" if r["ok"] else "FAIL: slider_cv 有失败项"))
        return 0 if r["ok"] else 1
    print("用法: python -m fleet.slider_cv --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
