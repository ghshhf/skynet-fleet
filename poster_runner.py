"""把出好的图排成商用成品海报：版式 / 留白 / 标题层级 / 强调色。

出图只是素材。真正交付要的是「成片」——一张 768×1024 的插画排成
1080×1350 的社媒竖版，耗时 0.2 秒，可以批量套。

全部本地完成：PIL + 系统字体，不联网、不调模型。所以它对
image.gen 用哪个后端出的图都成立（手机 NPU / sd.cpp / PC sd-cli 一样）。

设计取舍：
  · 强调色从图里算（最高饱和且不太暗的那一簇），纯线上提亮到能在深色底读，
    所以每张海报的强调色跟画面自己走，不是硬编码的品牌色；
  · 标题按字数自动降字号（96 → 36），两行放不下就截断，不会溢出画布；
  · 全幅版式底部加渐变遮罩，否则白字压亮部直接看不见。
"""
import os

LAYOUTS = {
    # (画布宽, 画布高, 图区高, 是否全幅)
    "poster": (1080, 1350, 900, False),
    "square": (1080, 1080, 780, False),
    "story": (1080, 1920, 1300, False),
    "full": (1080, 1350, 1350, True),
}

FONT_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
]
FONT_REG = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size, bold=True):
    from PIL import ImageFont
    for p in (FONT_BOLD if bold else FONT_REG):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, int(size))
            except Exception:
                continue
    return ImageFont.load_default()


def _cover(im, w, h):
    src_ar = im.width / im.height
    dst_ar = w / h
    if src_ar > dst_ar:
        nh, nw = h, int(h * src_ar)
    else:
        nw, nh = w, int(w / src_ar)
    im = im.resize((nw, nh), 1)  # 1 = LANCZOS
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _accent(im):
    """取图里最「有颜色」的一簇当强调色；太灰就退回白色。"""
    import colorsys
    import numpy as np
    small = im.convert("RGB").resize((64, 64))
    arr = np.asarray(small).reshape(-1, 3).astype(float) / 255.0
    best, best_score = (1.0, 1.0, 1.0), -1.0
    for r, g, b in arr:
        h, l, s = colorsys.rgb_to_hls(r, g, b)
        if l < 0.18 or l > 0.9:
            continue
        score = s * (1 - abs(l - 0.55))
        if score > best_score:
            best_score, best = score, (r, g, b)
    if best_score < 0.08:
        return (255, 255, 255)
    h, l, s = colorsys.rgb_to_hls(*best)
    r, g, b = colorsys.hls_to_rgb(h, min(0.72, max(0.55, l)), min(1.0, s * 1.25))
    return (int(r * 255), int(g * 255), int(b * 255))


def _wrap(draw, text, fnt, max_w):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        t = cur + ch
        if draw.textlength(t, font=fnt) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def _fit(draw, text, max_w, max_lines=2,
         sizes=(96, 84, 72, 64, 56, 48, 42, 36)):
    for s in sizes:
        f = _font(s, True)
        ls = _wrap(draw, text, f, max_w)
        if len(ls) <= max_lines and all(draw.textlength(x, font=f) <= max_w for x in ls):
            return f, ls
    f = _font(sizes[-1], True)
    return f, _wrap(draw, text, f, max_w)[:max_lines]


def build(image_path, title="", sub="", tag="", footer="", layout="poster",
          dark=True, out=None):
    """排版并返回 (输出路径, (宽, 高), 强调色)。"""
    from PIL import Image, ImageDraw
    if layout not in LAYOUTS:
        raise RuntimeError(f"未知版式 {layout!r}，可选：{', '.join(sorted(LAYOUTS))}")
    if not os.path.isfile(image_path):
        raise RuntimeError(f"找不到底图：{image_path}")
    W, H, img_h, full = LAYOUTS[layout]
    src = Image.open(image_path).convert("RGB")

    canvas = Image.new("RGB", (W, H), (14, 15, 18) if dark else (250, 249, 246))
    canvas.paste(_cover(src, W, img_h), (0, 0))
    if full:
        grad = Image.new("L", (1, H), 0)
        for y in range(H):
            k = max(0.0, (y - H * 0.45) / (H * 0.55))
            grad.putpixel((0, y), int(min(1.0, k ** 1.35) * 235))
        shade = Image.new("RGB", (W, H), (8, 9, 12))
        canvas = Image.composite(shade, canvas, grad.resize((W, H)))

    ac = _accent(src)
    ink = (247, 246, 243) if dark else (22, 22, 26)
    d = ImageDraw.Draw(canvas)
    margin = int(W * 0.075)
    inner = W - margin * 2

    if not full:
        y = img_h
        d.rectangle([margin, y - 4, margin + int(inner * 0.18), y], fill=ac)

    cy = (img_h + int(H * 0.045)) if not full else int(H * 0.62)

    if tag:
        f = _font(int(W * 0.024), True)
        tw = d.textlength(tag.upper(), font=f)
        pad = int(W * 0.014)
        d.rectangle([margin, cy, margin + tw + pad * 2, cy + int(W * 0.048)], fill=ac)
        d.text((margin + pad, cy + int(W * 0.007)), tag.upper(), font=f, fill=(12, 12, 14))
        cy += int(W * 0.075)

    if title:
        f, lines = _fit(d, title, inner)
        for l in lines:
            d.text((margin, cy), l, font=f, fill=ink)
            cy += int(f.size * 1.28)
        cy += int(H * 0.012)

    if sub:
        f = _font(int(W * 0.031), False)
        for l in _wrap(d, sub, f, inner):
            d.text((margin, cy), l, font=f, fill=ac if dark else (70, 70, 78))
            cy += int(W * 0.045)

    f = _font(int(W * 0.021), False)
    fy = H - int(H * 0.055)
    grey = (150, 150, 158) if dark else (120, 120, 128)
    d.text((margin, fy), footer or "SKYNET FLEET  ·  NPU ON PHONE", font=f, fill=grey)
    right = "%d×%d · QNN HTP" % (src.width, src.height)
    d.text((W - margin - d.textlength(right, font=f), fy), right, font=f, fill=grey)

    # 一点颗粒，压住大色块上的色带（纯给观感，可关）
    try:
        import random
        import numpy as np
        random.seed(7)
        arr = np.asarray(canvas).astype("int16")
        idx = np.random.RandomState(7).randint(-7, 8, size=(H, W, 1))
        canvas = Image.fromarray(np.clip(arr + idx, 0, 255).astype("uint8"))
    except Exception:
        pass

    out = out or os.path.splitext(image_path)[0] + "_" + layout + ".png"
    canvas.save(out, quality=96)
    return out, canvas.size, ac


def run(image_path, title="", sub="", tag="", footer="", layout="poster",
        dark=True, out=None):
    """能力入口：返回 {"ok", "path", "size", "accent", "err"}，异常不外抛。"""
    try:
        if not image_path:
            return {"ok": False, "path": "", "size": "", "accent": "", "err": "image_path 不能为空"}
        p, size, ac = build(image_path, title, sub, tag, footer, layout, dark, out)
        return {"ok": True, "path": p, "size": "%dx%d" % size,
                "accent": "#%02X%02X%02X" % ac, "err": ""}
    except Exception as e:
        return {"ok": False, "path": "", "size": "", "accent": "",
                "err": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 自测：不依赖任何外部服务，造一张假图跑四种版式
# ---------------------------------------------------------------------------

def _selftest():
    import tempfile

    import numpy as np
    from PIL import Image

    ok = fail = 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ✓ {name}")
        else:
            fail += 1
            print(f"  ✗ {name} {extra}")

    tmp = tempfile.mkdtemp(prefix="poster_selftest_")
    rng = np.random.RandomState(3)
    arr = np.zeros((1024, 768, 3), dtype="uint8")
    arr[:, :, 0] = 40
    arr[:, :, 1] = np.linspace(20, 120, 1024, dtype="uint8")[:, None]
    arr[:, :, 2] = 90
    arr[200:800, 150:600] = rng.randint(120, 255, (600, 450, 3))
    src = os.path.join(tmp, "fake.png")
    Image.fromarray(arr).save(src)

    for lay, want in (("poster", (1080, 1350)), ("square", (1080, 1080)),
                      ("story", (1080, 1920)), ("full", (1080, 1350))):
        r = run(src, title="雨夜霓虹", sub="NEON RAIN · 城市在雨里重新发光",
                tag="Editorial 02", layout=lay,
                out=os.path.join(tmp, lay + ".png"))
        chk(f"{lay} 出图", r["ok"] and os.path.isfile(r["path"]), r["err"])
        chk(f"{lay} 尺寸 {want[0]}x{want[1]}", r["size"] == "%dx%d" % want, r.get("size"))
        im = Image.open(r["path"])
        chk(f"{lay} 真的能打开", im.size == want, str(im.size))

    r = run(src, title="A" * 80, layout="poster", out=os.path.join(tmp, "long.png"))
    chk("超长标题不崩", r["ok"], r["err"])
    r = run(src, title="短", layout="full",
            out=os.path.join(tmp, "short.png"))
    chk("短标题不崩", r["ok"], r["err"])

    r = run("/no/such/file.png")
    chk("缺图如实报错", (not r["ok"]) and "找不到底图" in r["err"], r["err"])
    r = run(src, layout="不存在的版式")
    chk("未知版式如实报错", (not r["ok"]) and "未知版式" in r["err"], r["err"])
    r = run("")
    chk("空路径如实报错", (not r["ok"]) and r["err"], r["err"])

    r = run(src, layout="poster", out=os.path.join(tmp, "accent.png"))
    chk("强调色是 #RRGGBB", r["accent"].startswith("#") and len(r["accent"]) == 7, r["accent"])

    print(f"\n{'ALL PASS' if fail == 0 else 'FAILED'}  {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    if len(sys.argv) < 2:
        print("用法：python poster_runner.py <图> [--layout poster|square|story|full]")
        raise SystemExit(2)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--title", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--tag", default="")
    ap.add_argument("--footer", default="")
    ap.add_argument("--layout", default="poster", choices=sorted(LAYOUTS))
    ap.add_argument("--light", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    out = run(a.image, a.title, a.sub, a.tag, a.footer, a.layout, not a.light, a.out or None)
    print(out)
    raise SystemExit(0 if out["ok"] else 1)
