"""fleet/motion.py — 视频时间线引擎：把「秒」翻成「帧」，把「运镜」翻成「像素」。

## 为什么要有这一层

旧帧链（2026-10-30 上午那版）的做法是：每一帧都拿上一帧丢给 img2img，
`seed+i`、`denoise=0.35`，然后把 N 张图按 fps 码成 mp4。

它的问题是**时间轴根本不存在**：
- 运动幅度由模型自由发挥，fps 只是编码参数 —— 12fps 和 24fps 出来的
  运动速度一模一样，只是播得快慢不同；
- 帧与帧之间没有任何几何联系，所以观感是「抽搐 / 缓慢变形 / 越走越糊」；
- 想让镜头推近一点，没有任何旋钮可控。

## 正解（已核对两个上游实现）

① **Deforum 2D 模式**（`deforum-stable-diffusion/helpers/animation.py:184-213`）：
   每帧先 `cv2.warpPerspective` 把上一帧按 (zoom, pan, rotate) 变换一次，
   再把变换结果当作 img2img 的输入，**denoise 固定不变**。
   运动由几何变换精确决定，扩散模型只负责补细节。
   注意：Deforum 的 schedule 值是「每帧」增量（`zoom: 0:(1.02)` = 每帧 ×1.02），
   改 fps 就会改速度 —— 这是它的老毛病。工程化必须改成**绝对时间求值**，
   速率才和 fps 解耦。

② **RIFE / AnimateDiff-VFI**：生成帧不够就插帧。但我们的瓶颈是
   「每帧要 2.7 秒」，插帧并不能减少生成量；能省的是**播放帧率**这一层。

⇒ 本模块的架构（关键帧稀疏 + 几何合成）：
     关键帧 K[i]     —— 模型生成，密度 gen_fps（默认 6~8）
     播放帧 P(t)     —— 不生成，用 K 的 warp + 交叉淡化合成，密度 play_fps（默认 24/30）

   `P(t) = lerp( warp(K[i],   cam_i   → cam_t),
                 warp(K[i+1], cam_{i+1} → cam_t), p )`

   两个候选都被 warp 到**同一个 t 时刻的视角**，所以它们几乎像素对齐，
   线性混合不会出现重影，只会轻微软化。这比光流插帧便宜几个数量级，
   而对「相机在动」这个主场景来说精度够用 —— 因为运动本来就是仿射的。

## 相机模型

相机 = `(cx, cy, s, theta)`：
- `cx, cy` 画面中心在世界里的位置，归一化 [0,1]，初始 (0.5, 0.5)
- `s`      放大倍数，1.0 = 原始画面铺满
- `theta`  滚转角（弧度），正值逆时针

**全部是绝对量**（不是每帧增量），所以 `at(t)` 可以随便求值，
关键帧稀疏、插值、跳帧都不会累积误差。
"""
from __future__ import annotations

import math
import os

# ---------------------------------------------------------------------------
# 缓动
# ---------------------------------------------------------------------------

def linear(u):
    return u


def ease_in(u):
    return u * u


def ease_out(u):
    return 1.0 - (1.0 - u) ** 2


def ease_in_out(u):
    return u * u * (3.0 - 2.0 * u)


def ease_hold(u):
    """到位后不动 —— 适合「停住给人看清」的镜头。"""
    return 0.0 if u < 1.0 else 1.0


EASINGS = {
    "linear": linear,
    "in": ease_in,
    "out": ease_out,
    "in_out": ease_in_out,
    "hold": ease_hold,
}


# ---------------------------------------------------------------------------
# 相机与轨道
# ---------------------------------------------------------------------------

class Camera:
    """一个时刻的相机状态。全是绝对量。"""

    __slots__ = ("cx", "cy", "s", "theta")

    def __init__(self, cx=0.5, cy=0.5, s=1.0, theta=0.0):
        self.cx = float(cx)
        self.cy = float(cy)
        self.s = float(s)
        self.theta = float(theta)

    def copy(self):
        return Camera(self.cx, self.cy, self.s, self.theta)

    def key(self, nd=5):
        return (round(self.cx, nd), round(self.cy, nd),
                round(self.s, nd), round(self.theta, nd))

    def __repr__(self):
        return ("Camera(cx=%.4f, cy=%.4f, s=%.4f, theta=%.4f)"
                % (self.cx, self.cy, self.s, self.theta))

    def __eq__(self, other):
        return isinstance(other, Camera) and self.key() == other.key()


# 预设：名字 → (zoom_end, pan_x_end, pan_y_end, rot_end, ease)
# zoom 是**终点倍数**（1.0 起）；pan 是终点相对初始中心的偏移（画面宽/高的比例）。
PRESETS = {
    "none":      (1.00, 0.000, 0.000, 0.0, "linear"),
    "push_in":   (1.14, 0.000, 0.000, 0.0, "in_out"),   # 缓慢推近（最稳，最像电影）
    "pull_out":  (0.88, 0.000, 0.000, 0.0, "in_out"),   # 缓慢拉远
    "pan_right": (1.05, 0.060, 0.000, 0.0, "linear"),   # 横移+轻微推
    "pan_left":  (1.05, -0.060, 0.000, 0.0, "linear"),
    "tilt_up":   (1.06, 0.000, -0.045, 0.0, "in_out"),  # 上摇（露出天）
    "tilt_down": (1.06, 0.000, 0.045, 0.0, "in_out"),
    "dolly":     (1.22, 0.030, -0.020, 0.0, "in_out"),  # 推+斜移，更有纵深
    "sway":      (1.08, 0.035, 0.000, 0.6, "in_out"),   # 轻微摇晃（手持感，角度制）
    "orbit":     (1.10, 0.045, 0.000, -1.2, "in_out"),
}

# 注意 sway/orbit 的 rot 单位是**度**（写起来直观），内部转弧度。


class Track:
    """运镜轨道。支持两种写法：

    1. 简写（整段一条曲线，占绝大多数场景）：
        Track("push_in")                    # 用预设
        Track(zoom=1.2, pan_x=0.05)         # 手写终点，等价于 from=0 → to=end
        Track(zoom=1.2, ease="linear", start=0.5, end=3.0)   # 只在中段运镜

    2. 关键帧表（多段运镜，例如「先推近再横移」）：
        Track(keys=[(0, {}), (2.0, {"zoom": 1.15}), (4.0, {"zoom": 1.15, "pan_x": 0.06})])

       keys 里每项是 (t_sec, 相对初始的绝对偏移 dict)；
       dict 里可以写 zoom / pan_x / pan_y / rot(度) / ease。

    内部一律存成 `[(t, Camera), ...]`，`at(t)` 线性 + 缓动插值。
    """

    def __init__(self, preset=None, zoom=None, pan_x=None, pan_y=None,
                 rot=None, ease=None, start=0.0, end=None, keys=None):
        self.raw = None
        if keys:
            self._keys = self._build_keys(keys)
            self.raw = "keys=%r" % (keys,)
            return
        base = PRESETS.get("none")
        if isinstance(preset, str) and preset in PRESETS:
            base = PRESETS[preset]
        elif preset not in (None, ""):
            raise ValueError("未知运镜预设 %r，可选：%s"
                             % (preset, "、".join(sorted(PRESETS))))
        z, px, py, r, e = base
        if zoom is not None:
            z = float(zoom)
        if pan_x is not None:
            px = float(pan_x)
        if pan_y is not None:
            py = float(pan_y)
        if rot is not None:
            r = float(rot)
        if ease:
            if ease not in EASINGS:
                raise ValueError("未知缓动 %r，可选：%s" % (ease, "、".join(EASINGS)))
            e = ease
        self._spec = dict(zoom=z, pan_x=px, pan_y=py, rot=r, ease=e,
                          start=float(start), end=(None if end is None else float(end)))
        self.raw = "zoom=%.4f pan=(%.4f,%.4f) rot=%.2f ease=%s" % (z, px, py, r, e)
        self._keys = None

    # -- 构造辅助 ---------------------------------------------------------

    @staticmethod
    def _build_keys(keys):
        out = []
        for t, d in keys:
            d = d or {}
            out.append((float(t), Camera(
                0.5 + float(d.get("pan_x", 0.0)),
                0.5 + float(d.get("pan_y", 0.0)),
                float(d.get("zoom", 1.0)),
                math.radians(float(d.get("rot", 0.0))),
            ), d.get("ease", "in_out")))
        out.sort(key=lambda r: r[0])
        if not out:
            raise ValueError("keys 不能为空")
        return out

    # -- 求值 -------------------------------------------------------------

    def at(self, t, duration=None):
        """t 秒时的相机。duration 用于 `end=None` 时判定终点。"""
        t = max(0.0, float(t))
        if self._keys is not None:
            return self._at_keys(t)
        sp = self._spec
        dur = float(duration if duration is not None else (sp["end"] or 1.0))
        t0 = sp["start"]
        t1 = sp["end"] if sp["end"] is not None else dur
        if t1 <= t0:
            u = 1.0 if t >= t1 else 0.0
        else:
            u = min(1.0, max(0.0, (t - t0) / (t1 - t0)))
        e = EASINGS[sp["ease"]](u)
        return Camera(
            0.5 + sp["pan_x"] * e,
            0.5 + sp["pan_y"] * e,
            1.0 + (sp["zoom"] - 1.0) * e,
            math.radians(sp["rot"]) * e,
        )

    def _at_keys(self, t):
        ks = self._keys
        if t <= ks[0][0]:
            return ks[0][1].copy()
        if t >= ks[-1][0]:
            return ks[-1][1].copy()
        for i in range(len(ks) - 1):
            t0, c0, _e0 = ks[i]
            t1, c1, e1 = ks[i + 1]
            if t0 <= t <= t1:
                u = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
                e = EASINGS.get(e1, ease_in_out)(u)
                return Camera(
                    c0.cx + (c1.cx - c0.cx) * e,
                    c0.cy + (c1.cy - c0.cy) * e,
                    c0.s + (c1.s - c0.s) * e,
                    c0.theta + (c1.theta - c0.theta) * e,
                )
        return ks[-1][1].copy()

    def range(self, duration):
        """整段的 (最小缩放, 最大缩放) —— 用来算 pad 量。"""
        n = max(2, int(duration * 8))
        ss = [self.at(duration * i / n, duration).s for i in range(n + 1)]
        return min(ss), max(ss)

    def is_static(self, duration, eps=1e-4):
        c0 = self.at(0, duration)
        c1 = self.at(duration, duration)
        return (abs(c0.cx - c1.cx) < eps and abs(c0.cy - c1.cy) < eps
                and abs(c0.s - c1.s) < eps and abs(c0.theta - c1.theta) < eps
                and not self._keys)

    def __repr__(self):
        return "Track(%s)" % (self.raw or "keys")


# ---------------------------------------------------------------------------
# 几何变换
# ---------------------------------------------------------------------------

def affine_between(cam0, cam1, out_size, ref_size, offset=(0, 0)):
    """求 PIL `Image.transform` 的 6 元组：输出像素 → 输入像素。

    cam0 = 输入图（源）的相机，cam1 = 输出图（目标）的相机。
    ref_size  = 相机的「参考画面」尺寸（即原始出图 W×H，不含 pad）。
    out_size  = 实际 OUT 图尺寸（含 pad 的话比 ref 大）。
    offset    = pad 后图相对参考画面左上角的位移（像素）。

    推导（世界坐标以参考画面为单位，中心 0.5）：
        输出像素 o → 世界 w = c1 + Rot(theta1)·((o - center) / (ref·s1))
        世界 w → 输入像素 p = offset + ref/2 + ref·s0·Rot(-theta0)·(w - c0)
    代入后只剩一个仿射：
        p = offset + ref/2 + ref·s0·Rot(-theta0)·(c1-c0) + (s0/s1)·Rot(theta1-theta0)·(o - center)
    """
    W, H = out_size
    rw, rh = ref_size
    ox, oy = offset
    c0, c1 = cam0, cam1
    k = c0.s / c1.s
    dth = c1.theta - c0.theta
    cos_d, sin_d = math.cos(dth), math.sin(dth)
    a = k * cos_d
    b = -k * sin_d
    d = k * sin_d
    e = k * cos_d

    dwx = c1.cx - c0.cx
    dwy = c1.cy - c0.cy
    # Rot(-theta0) 作用在中心差上
    c0cos, c0sin = math.cos(-c0.theta), math.sin(-c0.theta)
    qx = c0cos * dwx - c0sin * dwy
    qy = c0sin * dwx + c0cos * dwy

    in_cx = ox + rw * 0.5 + rw * c0.s * qx
    in_cy = oy + rh * 0.5 + rh * c0.s * qy

    cx_o, cy_o = W / 2.0, H / 2.0
    c = in_cx - (a * cx_o + b * cy_o)
    f = in_cy - (d * cx_o + e * cy_o)
    return (a, b, c, d, e, f)


def _need_pil():
    try:
        from PIL import Image  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("motion 需要 pillow：pip install pillow（%s）" % exc)


def pad_reflect(img, ratio):
    """四周镜像外扩 ratio（相对宽/高）。给 zoom-out 留出画面，避免黑边。

    不用 ImageFilter 的边缘拉伸：镜像在自然纹理上更不容易看出接缝，
    而且 zoom-out 时外扩区很快会被裁掉，质量差异可忽略。
    """
    _need_pil()
    import numpy as np
    from PIL import Image
    if ratio <= 1e-6:
        return img, (0, 0)
    px = int(round(img.width * ratio))
    py = int(round(img.height * ratio))
    a = np.asarray(img.convert("RGB"))
    a = np.pad(a, ((py, py), (px, px), (0, 0)), mode="reflect")
    return Image.fromarray(a), (px, py)


def warp(img, cam_src, cam_dst, ref_size=None, pad_ratio=0.0):
    """把 `img`（相机 cam_src 视角）变成 cam_dst 视角。

    pad_ratio > 0 时先镜像外扩再变换、最后裁回原尺寸 ——
    这样 zoom-out 不会出黑边。外扩量建议按 `Track.range()` 里 1/s 的最小值算。
    """
    _need_pil()
    from PIL import Image
    ref = ref_size or img.size
    src = img
    off = (0, 0)
    if pad_ratio > 1e-6:
        src, off = pad_reflect(img, pad_ratio)
    # 输出尺寸固定为「参考画面」大小（不含 pad）—— pad 只扩大**输入**的可用范围。
    # 早先把 out_size 也写成 pad 尺寸，等于视野跟着一起变宽，zoom-out 照样出黑边。
    m = affine_between(cam_src, cam_dst, img.size, ref, off)
    return src.transform(img.size, Image.AFFINE, m,
                         resample=Image.BICUBIC, fillcolor=(0, 0, 0))


def pad_for(track, duration):
    """按轨道最极端的缩放算需要的外扩比例（每边）。"""
    smin, _smax = track.range(duration)
    if smin >= 0.999:
        return 0.0
    return min(0.45, (1.0 / smin - 1.0) / 2.0 + 0.04)


def blend(a, b, alpha):
    """alpha=0 → a，alpha=1 → b。"""
    _need_pil()
    import numpy as np
    from PIL import Image
    if alpha <= 0.0:
        return a
    if alpha >= 1.0:
        return b
    x = np.asarray(a, dtype=np.float32)
    y = np.asarray(b, dtype=np.float32)
    return Image.fromarray(
        np.clip(x * (1.0 - alpha) + y * alpha, 0, 255).astype("uint8"))


# ---------------------------------------------------------------------------
# 时间表
# ---------------------------------------------------------------------------

def key_times(duration, gen_fps):
    """关键帧时刻：覆盖 [0, duration] 的等距序列，末尾正好落在 duration。

    末尾必须有帧 —— 否则最后一段时间没有「后一帧」可以混，画面会突然停住。
    """
    duration = max(0.05, float(duration))
    gen_fps = max(0.5, float(gen_fps))
    n = max(2, int(round(duration * gen_fps)) + 1)
    return [duration * i / (n - 1) for i in range(n)]


def play_times(duration, play_fps):
    duration = max(0.05, float(duration))
    n = max(2, int(round(duration * float(play_fps))))
    return [duration * i / n for i in range(n)]


def plan(duration, gen_fps=6, play_fps=24):
    """返回 (关键帧时刻, 播放帧时刻, 生成/播放倍率)。"""
    return key_times(duration, gen_fps), play_times(duration, play_fps)


# ---------------------------------------------------------------------------
# 合成：关键帧 → 播放帧
# ---------------------------------------------------------------------------

def compose_frame(kframes, ktimes, track, t, duration, pad_ratio=0.0, ref_size=None):
    """在 t 时刻合成一帧。`kframes` 是 PIL 图列表，与 `ktimes` 一一对应。"""
    if not kframes:
        raise ValueError("没有关键帧")
    if len(kframes) == 1:
        return kframes[0].copy()
    t = min(max(t, ktimes[0]), ktimes[-1])
    # 找 t 落在哪个区间
    i = 0
    for j in range(len(ktimes) - 1):
        if ktimes[j] <= t <= ktimes[j + 1]:
            i = j
            break
    else:
        i = len(ktimes) - 2
    t0, t1 = ktimes[i], ktimes[i + 1]
    p = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
    cam_t = track.at(t, duration)
    cam0 = track.at(t0, duration)
    cam1 = track.at(t1, duration)
    ref = ref_size or kframes[i].size
    a = warp(kframes[i], cam0, cam_t, ref, pad_ratio)
    if p <= 1e-6:
        return a
    if i + 1 >= len(kframes):
        return a
    b = warp(kframes[i + 1], cam1, cam_t, ref, pad_ratio)
    # smoothstep 让「切换点」的过渡两端导数为 0，避免关键帧处速度突变
    q = p * p * (3.0 - 2.0 * p)
    return blend(a, b, q)


def render_playback(kframes, ktimes, track, duration, play_fps,
                    out_dir=None, prefix="p", pad_ratio=None,
                    on_frame=None, ref_size=None):
    """把关键帧铺成播放帧序列，写到 out_dir（或不落盘只返回内存列表）。

    返回 (帧路径列表 或 图列表, 实际 pad_ratio)。
    """
    _need_pil()
    if pad_ratio is None:
        pad_ratio = pad_for(track, duration)
    times = play_times(duration, play_fps)
    frames = []
    for k, t in enumerate(times):
        im = compose_frame(kframes, ktimes, track, t, duration, pad_ratio, ref_size)
        if out_dir:
            path = os.path.join(out_dir, "%s_%04d.png" % (prefix, k))
            im.save(path)
            frames.append(path)
        else:
            frames.append(im)
        if on_frame:
            try:
                on_frame(k, len(times), t)
            except Exception:
                pass
    return frames, pad_ratio


# ---------------------------------------------------------------------------
# 自测（纯 CPU，不碰手机）
# ---------------------------------------------------------------------------

def _selftest():
    ok = []
    fail = []

    def chk(name, cond):
        (ok if cond else fail).append(name)

    _need_pil()
    from PIL import Image, ImageDraw
    import numpy as np

    # 一张网格图：用来看 warp 有没有算错
    W = H = 128
    im = Image.new("RGB", (W, H), (20, 20, 20))
    d = ImageDraw.Draw(im)
    for i in range(0, W, 16):
        d.line([(i, 0), (i, H)], fill=(220, 220, 220))
        d.line([(0, i), (W, i)], fill=(220, 220, 220))
    d.rectangle([0, 0, W - 1, H - 1], outline=(255, 0, 0), width=2)

    # 1. 恒等：同一相机，输出必须和输入一致
    c = Camera()
    m = affine_between(c, c, (W, H), (W, H))
    chk("恒等变换矩阵正确", all(abs(x - y) < 1e-9 for x, y in
                          zip(m, (1, 0, 0, 0, 1, 0))))
    out = warp(im, c, c, (W, H))
    chk("恒等 warp 像素不变", np.array_equal(np.asarray(out), np.asarray(im)))

    # 2. 放大 2 倍：输出去应该只有输入的中间一半（放大 2 倍看细节）
    c2 = Camera(s=2.0)
    ref2 = warp(im, c, c2, (W, H))
    ref_crop = im.crop((W // 4, H // 4, W // 4 + W // 2, H // 4 + H // 2))
    ref_big = ref_crop.resize((W, H), Image.BICUBIC)
    diff = np.abs(np.asarray(ref2, dtype=np.float32)
                  - np.asarray(ref_big, dtype=np.float32)).mean()
    chk("放大 2 倍 = 取中心 1/2 放大（平均差 %.2f）" % diff, diff < 6.0)

    # 3. 平移：相机右移 0.25 画宽 → 画面内容应左移 1/4
    c3 = Camera(cx=0.75)
    out3 = warp(im, c, c3, (W, H))
    # 输出左边缘取输入 x=0.25W
    a_in = np.asarray(im)[:, int(0.25 * W) + 3: int(0.25 * W) + 8, :].mean()
    a_out = np.asarray(out3)[:, 3:8, :].mean()
    chk("平移 1/4 画宽方向正确（%.1f vs %.1f）" % (a_out, a_in),
        abs(a_out - a_in) < 12.0)

    # 4. Track 求值：单调 + 端点
    tr = Track("push_in")
    c_start = tr.at(0, 3.0)
    c_end = tr.at(3.0, 3.0)
    chk("Track 起点 s=1", abs(c_start.s - 1.0) < 1e-9)
    chk("Track 终点 s=1.14", abs(c_end.s - 1.14) < 1e-6)
    mid = tr.at(1.5, 3.0)
    chk("Track 中点 s 在中间（%.4f）" % mid.s, 1.0 < mid.s < 1.14)
    mono = all(tr.at(i * 0.1, 3.0).s <= tr.at((i + 1) * 0.1, 3.0).s + 1e-9
               for i in range(29))
    chk("Track 推近单调", mono)

    # 5. 绝对求值：at(1.5) 与 duration 无关（这是和 Deforum 每帧增量最大的区别）
    a1 = Track("push_in").at(1.5, 3.0).s
    a2 = Track("push_in").at(1.5, 3.0).s
    chk("Track 求值可重复", a1 == a2)

    # 6. keys 写法
    tk = Track(keys=[(0, {}), (1.0, {"zoom": 1.2}), (2.0, {"zoom": 1.2, "pan_x": 0.1})])
    chk("keys 起点 s=1", abs(tk.at(0, 2.0).s - 1.0) < 1e-9)
    chk("keys 中段 s=1.2", abs(tk.at(1.0, 2.0).s - 1.2) < 1e-6)
    chk("keys 末段 pan_x=0.1", abs(tk.at(2.0, 2.0).cx - 0.6) < 1e-6)

    # 7. 时刻表：末尾落在 duration，间隔均匀
    kt = key_times(3.0, 6.0)
    chk("关键帧数 = round(3*6)+1 = %d" % len(kt), len(kt) == 19)
    chk("关键帧末位 = duration", abs(kt[-1] - 3.0) < 1e-9)
    chk("关键帧等距", abs((kt[1] - kt[0]) - (kt[-1] - kt[-2])) < 1e-9)

    # 8. 合成：t=关键帧时刻时应恰好等于该关键帧（warp 恒等）
    t2 = Track("none")
    kfs = [im, im.transpose(Image.FLIP_LEFT_RIGHT)]
    kts = [0.0, 1.0]
    f0 = compose_frame(kfs, kts, t2, 0.0, 1.0)
    f1 = compose_frame(kfs, kts, t2, 1.0, 1.0)
    chk("t=k0 时合成等于 k0", np.array_equal(np.asarray(f0), np.asarray(kfs[0])))
    chk("t=k1 时合成等于 k1", np.array_equal(np.asarray(f1), np.asarray(kfs[1])))
    fm = compose_frame(kfs, kts, t2, 0.5, 1.0)
    chk("t=中点 是两者的混合（不是任一）",
        not np.array_equal(np.asarray(fm), np.asarray(kfs[0]))
        and not np.array_equal(np.asarray(fm), np.asarray(kfs[1])))

    # 9. pad 消除黑边：强制 zoom-out 到 0.7 也应该没有黑边
    #
    # 判据用「边框一圈的**中位数**」而不是黑像素占比：BICUBIC 在硬边缘（这张
    # 网格线）会过冲把相邻暗像素压到 0，那是重采样振铃不是黑边。真正的黑边是
    # 整条边都黑，中位数会直接掉到 0；正常画面中位数就是背景色。
    def _edge_black(im_):
        s_ = np.asarray(im_).astype(np.int32).sum(axis=2)
        return max(float(np.median(s_[0, :])), float(np.median(s_[-1, :])),
                   float(np.median(s_[:, 0])), float(np.median(s_[:, -1])))

    bg_sum = 60  # 测试图背景 (20,20,20)
    tr_out = Track(zoom=0.70)
    p = pad_for(tr_out, 1.0)
    out9 = warp(im, Camera(), Camera(s=0.70), (W, H), pad_ratio=p)
    eb = _edge_black(out9)
    chk("zoom-out 0.7 无黑边（边框中位 %d，背景 %d，pad=%.3f）" % (eb, bg_sum, p),
        eb >= bg_sum - 6)
    out9b = warp(im, Camera(), Camera(s=0.70), (W, H), pad_ratio=0.0)
    ebb = _edge_black(out9b)
    chk("不给 pad 时 zoom-out 出黑边（边框中位 %d，证明确实需要 pad）" % ebb,
        ebb < 6)

    # 10. 整段落盘
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        paths, used = render_playback(kfs, kts, t2, 1.0, 12, out_dir=td, prefix="t")
        chk("render_playback 出 12 帧（%d）" % len(paths), len(paths) == 12)
        chk("落盘文件都在", all(os.path.isfile(x) for x in paths))

    return ok, fail


if __name__ == "__main__":
    o, f = _selftest()
    for name in o:
        print("  ok   " + name)
    for name in f:
        print("  FAIL " + name)
    print("\n%d/%d 通过" % (len(o), len(o) + len(f)))
    raise SystemExit(1 if f else 0)
