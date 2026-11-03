# -*- coding: utf-8 -*-
"""天网 · 验证码能力（captcha.*）
================================================================
2026-11-02 主人交办：「关于验证，滑块验证啊之类的各种各样验证，搜索开源项目，
把它融入到咱们自己的能力里面。」

选型只有一个主力：**ddddocr**（sml2h3/ddddocr，14.9k★，MIT）。
理由不是它最强，是它**最省事且不欠人情**：
    · 纯本地 ONNX 推理，不联网、不按次收费、不把验证码图片发给第三方
    · 模型内置于 wheel，pip 装完即用，不需要另外下权重
    · 覆盖三类常见验证：字符 OCR / 滑块缺口匹配 / 目标检测（点选）
    · CPU 就够，单张 100~500ms

没选的那些，说清原因（免得以后重复考察）：
    · 超级鹰 / YesCaptcha  → 按次收费，且要把图传给第三方，账号关联风险
    · AntiCAP             → YOLOv8 全题型，但要自己备权重、跑 GPU，重
    · hcaptcha-challenger → 专攻 hCaptcha，依赖视觉大模型，慢且贵

一条必须写死的认知（2026-11-02 实测得出）
------------------------------------------
**滑块验证的难点不在算缺口，在轨迹。** 缺口坐标算出来是一瞬间的事，但
服务端校验的是「拖动过程像不像人」：起始抖动、先加速后减速、纵向微漂、
偶尔过冲再回拉。坐标对了轨迹是直线，一样判死。

所以本模块把「算」和「做」分开：
    solve_slider()  只算坐标（纯视觉，可单测）
    human_path()    只造轨迹（纯算法，可单测）
真正拖的时候由 browser_reg 的 drag 动词按轨迹逐点发 CDP 鼠标事件 ——
走浏览器原生输入通道，不走 JS 赋值（`element.value=` 一眼就是机器）。

用法
----
    python -m fleet.captcha text  路径/图片.png
    python -m fleet.captcha slider 缺口.png 背景.png
    python -m fleet.captcha detect 图.png
    python -m fleet.captcha path 260          # 看一条 260px 的拖动轨迹

代码里：
    from fleet import captcha
    captcha.solve_text(img_bytes)              # -> {'text': 'AB3D', 'ms': 474}
    captcha.solve_slider(gap, bg)              # -> {'x': 132, 'y': 8, 'score': ..}
    captcha.human_path(260)                    # -> [{'x':3,'y':0,'dt':12}, ...]
"""
import base64
import io
import json
import math
import os
import random
import sys

_OCR = None
_DET = None
_SLIDE = None


# ---------------------------------------------------------------- 输入归一

def to_bytes(image):
    """把 路径 / bytes / dataURL / base64 / PIL.Image 统统变成 bytes。"""
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    if isinstance(image, str):
        s = image.strip()
        if s.startswith("data:"):
            return base64.b64decode(s.split(",", 1)[1])
        if os.path.exists(s):
            with open(s, "rb") as f:
                return f.read()
        return base64.b64decode(s)
    # numpy 数组（cv2 读进来的那种，BGR）
    if hasattr(image, "shape") and hasattr(image, "dtype"):
        import cv2
        ok, buf = cv2.imencode(".png", image)
        if not ok:
            raise ValueError("numpy 数组编码 PNG 失败")
        return buf.tobytes()
    # PIL.Image 之类
    if hasattr(image, "save"):
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    raise TypeError("不认识的图片输入类型：%r" % type(image))


def to_ndarray(image):
    import cv2
    import numpy as np
    b = to_bytes(image)
    arr = np.frombuffer(b, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("图片解码失败（不是有效图片？）")
    return img


# ---------------------------------------------------------------- 模型（进程内缓存）

def _get_ocr():
    """字符识别。首次 ~1s，之后常驻。"""
    global _OCR
    if _OCR is None:
        import ddddocr
        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def _get_det():
    """目标检测（点选类）。det=True 会强制关 OCR。"""
    global _DET
    if _DET is None:
        import ddddocr
        _DET = ddddocr.DdddOcr(det=True, show_ad=False)
    return _DET


def _get_slide():
    """滑块匹配器。"""
    global _SLIDE
    if _SLIDE is None:
        import ddddocr
        _SLIDE = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
    return _SLIDE


# ---------------------------------------------------------------- 三类识别

def solve_text(image, charset=None):
    """字符/数字验证码 → 文本。charset 可限定字符集提高准确率。"""
    import time
    t = time.time()
    b = to_bytes(image)
    ocr = _get_ocr()
    if charset:
        # ddddocr 支持 set_ranges；字符集用字符串形式
        try:
            ocr.set_ranges(charset)
        except Exception:
            pass
    text = ocr.classification(b)
    return dict(text=text, ms=int((time.time() - t) * 1000))


def solve_slider(gap, background, simple=True, coord="center"):
    """滑块缺口定位 → 返回缺口**左边沿**坐标（可直接当拖动距离用）。

    gap       = 滑块小图（那个拼图块）
    background= 背景大图

    coord 参数是踩坑换来的，别乱改（2026-11-02 实测）
    ---------------------------------------------------
    ddddocr 1.6.1 的 slide_match 返回的是**模板中心**，不是左上角。
    交叉验证：模板 30/46/60px 三档，识别值比真实左上角分别大 15/23/30，
    精确等于各自的一半边长。直接拿它当拖动距离，每次偏半个滑块。

    所以默认 coord="center" 做校正；换回老版本 ddddocr（返回左上角）时
    传 coord="topleft"。两种都保留，因为坐标语义只有调用方知道。
    """
    import time
    t = time.time()
    gb, bb = to_bytes(gap), to_bytes(background)
    gh, gw = to_ndarray(gap).shape[:2]
    slide = _get_slide()
    try:
        res = slide.slide_match(gb, bb, simple_target=simple)
    except TypeError:
        res = slide.slide_match(gb, bb)
    target = res.get("target") or [0, 0]
    cx, cy = float(target[0]), float(target[1])
    if coord == "center":
        left, top = cx - gw / 2.0, cy - gh / 2.0
    else:
        left, top = cx, cy
    return dict(
        x=int(round(left)), y=int(round(top)),      # 缺口左上角 = 要拖的距离
        cx=int(round(cx)), cy=int(round(cy)),       # 原始识别值，留档好对账
        gap_size=[gw, gh], coord=coord,
        score=float(res.get("confidence", 0) or 0),
        ms=int((time.time() - t) * 1000))


def match_template(gap, background, method="ccoeff"):
    """cv2 模板匹配兜底：坐标语义完全自控，不受 ddddocr 版本影响。

    ddddocr 的滑块是对验证码场景调过的，通常更准；但它的坐标基准会随版本变。
    这里给一条不依赖版本的路，两者都跑一遍取一致的做交叉验证最稳。
    """
    import cv2
    import time
    t = time.time()
    g = to_ndarray(gap)
    b = to_ndarray(background)
    # 统一转灰度，避免彩色通道权重差异
    gg = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY) if g.ndim == 3 else g
    bg = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY) if b.ndim == 3 else b
    m = {"ccoeff": cv2.TM_CCOEFF_NORMED, "sqdiff": cv2.TM_SQDIFF_NORMED}.get(method, cv2.TM_CCOEFF_NORMED)
    res = cv2.matchTemplate(bg, gg, m)
    _, maxv, minloc, maxloc = cv2.minMaxLoc(res)
    loc = minloc if method == "sqdiff" else maxloc
    return dict(x=int(loc[0]), y=int(loc[1]),
                score=float(maxv if method != "sqdiff" else 1 - maxv),
                gap_size=[g.shape[1], g.shape[0]],
                method="cv2/" + method, ms=int((time.time() - t) * 1000))


def detect_objects(image, min_score=0.3):
    """点选类验证：返回图里可点的目标框（汉字/图标）。"""
    b = to_bytes(image)
    boxes = _get_det().detection(b)
    out = [dict(x1=int(x[0]), y1=int(x[1]), x2=int(x[2]), y2=int(x[3]),
                cx=int((x[0] + x[2]) / 2), cy=int((x[1] + y[3]) / 2))
           for x in boxes if len(x) < 5 or x[4] >= min_score]
    return dict(count=len(out), boxes=out)


def find_notch(background, blur=5):
    """只有背景图（缺口直接画在图上，没有独立滑块图）时，用边缘找缺口。

    思路：缺口边缘是强梯度，先 Canny 再按列统计竖直边缘能量，峰值列即缺口左沿。
    精度不如模板匹配，但不需要滑块图，适合「整图拼图」型验证。
    """
    import cv2
    import numpy as np
    img = to_ndarray(background)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (blur, blur), 0)
    edges = cv2.Canny(gray, 60, 180)
    col = edges.sum(axis=0).astype(float)
    if col.max() <= 0:
        return dict(x=0, score=0.0, method="canny", note="无边缘")
    # 取能量最集中的一段（缺口通常是两条竖边，宽度约滑块宽）
    k = max(3, img.shape[0] // 20)
    smooth = np.convolve(col, np.ones(k) / k, mode="same")
    x = int(smooth.argmax())
    return dict(x=x, score=float(smooth[x] / (smooth.max() or 1)),
                width=img.shape[1], height=img.shape[0], method="canny")


# ---------------------------------------------------------------- 人类轨迹

def human_path(distance, y_jitter=2.0, overshoot=None, seed=None):
    """生成一条「像人拖出来」的轨迹。返回 [{x, y, dt}, ...]，最后一点必落在 distance。

    模拟的四个特征（服务端风控主要看这几样）：
      1. 起步有反向小回拉（人手按下时会先抖一下）
      2. 中段快、尾段慢（先加速后减速，ease-out）
      3. 纵向持续微漂（不可能一条直线）
      4. 到点偶尔过冲再回拉（比例约 30%）
    dt 是这一步到下一步的毫秒间隔，不是固定值（固定间隔 = 机器）。
    """
    rnd = random.Random(seed)
    distance = float(distance)
    if distance <= 0:
        return []

    if overshoot is None:
        overshoot = rnd.random() < 0.3
    over = rnd.uniform(2.0, 7.0) if overshoot else 0.0
    total = distance + over

    # 三次贝塞尔：控制点带纵向随机，保证不是直线
    p0 = (0.0, 0.0)
    p1 = (total * rnd.uniform(0.15, 0.30), rnd.uniform(-y_jitter, y_jitter))
    p2 = (total * rnd.uniform(0.65, 0.85), rnd.uniform(-y_jitter, y_jitter))
    p3 = (total, rnd.uniform(-y_jitter * 0.4, y_jitter * 0.4))

    # 缓动指数**只能取一次**：每步重新随机会让曲线不连续，出现 92→71 的倒退
    # （2026-11-02 实测踩到）。同一轮拖动的加速度曲线必须是同一个函数。
    ease_exp = rnd.uniform(2.0, 3.2)
    steps = max(12, int(total / rnd.uniform(3.0, 5.5)))
    pts = []
    prev_x = 0.0
    for i in range(1, steps + 1):
        t = i / steps
        # ease-out：前段位移大，后段细碎，符合人手减速
        te = 1 - (1 - t) ** ease_exp
        x = ((1 - te) ** 3 * p0[0] + 3 * (1 - te) ** 2 * te * p1[0]
             + 3 * (1 - te) * te ** 2 * p2[0] + te ** 3 * p3[0])
        y = ((1 - te) ** 3 * p0[1] + 3 * (1 - te) ** 2 * te * p1[1]
             + 3 * (1 - te) * te ** 2 * p2[1] + te ** 3 * p3[1])
        # 单调兜底：人拖动可能停顿（dx=0），但不会倒退
        if x < prev_x:
            x = prev_x
        prev_x = x
        # 起步那几步手会抖，间隔更长
        dt = int(rnd.uniform(9, 22) + (18 if t < 0.15 else 0))
        pts.append(dict(x=round(x, 2), y=round(y, 2), dt=dt))

    # 过冲 → 回拉到真实距离
    if over:
        back_steps = rnd.randint(2, 4)
        for i in range(back_steps, 0, -1):
            pts.append(dict(x=round(distance + over * i / (back_steps + 1), 2),
                            y=round(rnd.uniform(-1, 1), 2),
                            dt=int(rnd.uniform(14, 30))))
    pts.append(dict(x=round(distance, 2), y=round(rnd.uniform(-0.8, 0.8), 2), dt=7))
    return pts


# ---------------------------------------------------------------- CLI

def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "text":
        print(json.dumps(solve_text(argv[2]), ensure_ascii=False))
        return 0
    if cmd == "slider":
        print(json.dumps(solve_slider(argv[2], argv[3]), ensure_ascii=False))
        return 0
    if cmd == "detect":
        print(json.dumps(detect_objects(argv[2]), ensure_ascii=False))
        return 0
    if cmd == "notch":
        print(json.dumps(find_notch(argv[2]), ensure_ascii=False))
        return 0
    if cmd == "path":
        p = human_path(float(argv[2]))
        print(json.dumps(p, ensure_ascii=False))
        print("点数：%d  总耗时约 %dms  终点 x=%.1f" % (
            len(p), sum(i["dt"] for i in p), p[-1]["x"]))
        return 0
    print("未知命令：%s" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
