"""看视频：把一段视频压成**一张可寻址的分镜图** + 一份**文字清单**。

（能力由 `fleet/kinds/video_watch.py` 承载，domain 用 `video.*`。）

## 为什么是三级，不是一级

以前的做法是「抽几帧丢给视觉模型」——一跳到底，模型没有「要不要看」的中间档。
三级各解决一件事：

    video.probe       0 token    只报时长/分辨率/有没有字幕轨/有没有平台故事板 → 决定要不要看
    video.storyboard  1 张图     全片概览：每格有时间、有档位，模型可以「引用第 5 格」
    video.zoom        高清帧     回原始源取某一格那一刻的原分辨率画面

`probe` 不产图是关键：**先判断值不值得看，再决定花不花这张图的 token。**

## 两条来源，成本差一个数量级

    A. 平台现成故事板（B站 videoshot）—— 不下载、不解码，裁一块就是帧。
       实测 BV1GJ411x7h7（213 秒）：33 个时间点、1 张 1600×900、每块 160×90、整张 61,488 字节。
       ⚠️ 160×90 **读不了任何小字**，只能判「这是什么画面」。这是拿精度换成本的极端端。
    B. 本地抽帧（ffmpeg 流式 rawvideo）—— 内存占用与视频时长无关，格子大小按预算自适应。

先试 A，A 不适用（非 B站 / 快照缺失）再走 B。**不假装 A 的分辨率是 B 的。**

## 三条纪律（照抄 dsh-bili-scroll 章程，那是一个用户实测二十多次买来的）

1. **回执 ≠ 看见**：没真取到的格子不许写成「看过了」。每格都带 `want_t` / `got_t`。
2. **空是一种结果，不是坏了**：字幕空 = 这条视频没有语音，不是工具故障 ——
   写成错误会让调用方去「修工具」，实测有人 27 次工具调用全耗在这上面、整轮报废。
3. **不声称看完整条**：返回里固定带 `coverage_gap_sec`（最大采样间隔）和
   `NOT_WHOLE_FILM`。抽样就是抽样。

## 算法来源与取舍

- **档位（非均匀网格）**：切点看成时间轴 0/1 函数 → 矩形窗卷积得切点密度 → 平滑 →
  7 道阈值切 8 档，带 5% 滞回；格子边长 = 1/档位。长镜头给整块保细节，快切段给小块
  只留「变了」这个信息。移植自 `dsh-video-storyboard/src/storyboard/tiers.js`（MIT）。
  ⚠️ 原实现的阈值是「每**帧**的切点概率」，本实现显式除以 native_fps 换算，
  不改阈值数值 —— 换 60fps 素材时判据不会被整体平移一倍。
- **打包**：归一化方形容器里按行流式排，行高 = 该行最大边长，格子 i 永远放第 i 帧。
  单位取 `UNIT = 840 = lcm(1..8)`，保证 8 个档位都是整数、不产生累积舍入误差。
  移植自同仓库 `pack.js`（MIT）。
- **编号和时间写进文字清单，不烧在图上**：学 `dsh-douyin-agent`。让小模型读图上的
  数字不可靠（要细粒度 OCR），弄成文本任务就当场上一个台阶。
- **去重**：缩到 16×16 灰度比亮度差，均值 < 2.0 判重直接丢（`claude-video` 的口径）。
  不用相似度百分比阈值 —— 阈值会损信息。

## 依赖

零第三方库：只用标准库 + 本仓 `imgscale`（纯 Python PNG 读写）。
ffmpeg / ffprobe 必须是外部程序（本机 9.0.2 实测可用）；没有它们时 `probe` 会说清缺什么。
B站那条路纯 `urllib`，**不需要 bilibili-api-python**。
"""
import hashlib
import json
import math
import os
import re
import shutil
import re
import struct
import subprocess
import time
import urllib.request

from . import imgscale

# ---------------------------------------------------------------- 常量

#: 扫描帧率。只用来算切点和去重，不参与出图。
SCAN_FPS = 2.0
SCAN_W, SCAN_H = 64, 36
#: 去重缩略图边长（16×16 灰度）与阈值（平均亮度差，0..255）。
DEDUP_THUMB = 16
DEDUP_MAD = 2.0
#: 默认总像素预算。对齐 tiers.js 的 DEFAULT_BUDGET（360k）——
#: 16:9 下约 800×450 ≈ 233 token，别自己另定一套。
DEFAULT_BUDGET = 360_000
BUDGET_MIN, BUDGET_MAX = 60_000, 1_300_000

#: 宿主按 14px patch 对齐、每轴 3:1 降采样、每行多 1 个分隔 token、整张 +2 框架 token。
IMAGE_PATCH = 14
IMAGE_DOWNSAMPLE = 3
IMAGE_CELL = IMAGE_PATCH * IMAGE_DOWNSAMPLE          # 42px
IMAGE_TOKEN_CAP = 1024

#: 变化量的平滑窗（原生帧）。对齐 tiers.js 的 movmean 窗口。
SMOOTH_FRAMES = 50
TIER_MIN, TIER_MAX = 1, 8
FREE = 0                     # 补位档：没有固定边长

#: 打包容器宽度。840 = lcm(1..8)，8 个档位全是整数。
UNIT = 840
GROW = (3, 4, 5, 6, 7, 8)

#: 格子数范围。少于 4 格看不出结构；多于 16 格单格就太小了。
CELLS_MIN, CELLS_MAX = 4, 16
#: 每多少秒一个格子（再夹到上下限）。
SEC_PER_CELL = 15.0

#: 缓存：内容寻址 + TTL + 总大小上限（学 video-understanding-mcp 的保留策略）。
CACHE_TTL_DAYS = 14
CACHE_MAX_BYTES = 512 * 1024 * 1024
#: zoom 样本有效期。过期就拒，不拿旧帧冒充新观察。
ZOOM_TTL = 600

#: 返回里固定带的一句：抽样≠看全。
NOT_WHOLE_FILM = ("这是抽样画面，不证明看完整条，也不含音轨证据；"
                  "引用时间以 got_t 为准（want_t 是要的、got_t 是实际取到的）")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_ZOOM_MEM = {}       # sample_id -> {src, aspect, cells:[{cell,t,w,h,fw,fh}], at}
_CACHE_HITS = {}


# ---------------------------------------------------------------- 小工具

def _run(cmd, timeout=120, binary=False):
    """跑外部程序，**不弹窗**（Windows 上 ffmpeg 每帧都会闪一个黑框）。"""
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
              stdin=subprocess.DEVNULL,
              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    try:
        p = subprocess.run(cmd, timeout=timeout, **kw)
    except FileNotFoundError:
        return False, b"" if binary else "", "没装 %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return False, b"" if binary else "", "%s 超时（%ss）" % (cmd[0], timeout)
    if binary:
        return p.returncode == 0, p.stdout, (p.stderr or b"").decode("utf-8", "replace")[-400:]
    return p.returncode == 0, (p.stdout or b"").decode("utf-8", "replace"), \
        (p.stderr or b"").decode("utf-8", "replace")[-400:]


def _has_ffmpeg():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _cache_root():
    p = os.environ.get("FLEET_VIDEOLOOK_CACHE")
    if not p:
        p = os.path.join(os.path.expanduser("~"), ".skynet", "videolook")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass
    return p


def _cache_key(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def _cache_get(key):
    d = os.path.join(_cache_root(), key)
    man = os.path.join(d, "manifest.json")
    sheet = os.path.join(d, "sheet.png")
    if not (os.path.isfile(man) and os.path.isfile(sheet)):
        return None
    try:
        age = time.time() - os.path.getmtime(man)
        if age > CACHE_TTL_DAYS * 86400:
            return None
        with open(man, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _cache_put(key, manifest, sheet_path, sheet_bytes):
    d = os.path.join(_cache_root(), key)
    try:
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, "sheet.png")
        if sheet_path and os.path.isfile(sheet_path) and os.path.abspath(sheet_path) != os.path.abspath(dst):
            shutil.copyfile(sheet_path, dst)
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    except OSError:
        return
    _cache_prune()


def _cache_prune():
    """超总量就按最久没用的丢（LRU）。"""
    root = _cache_root()
    try:
        items = []
        total = 0
        for name in os.listdir(root):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            sz, mt = 0, 0.0
            for f in os.listdir(d):
                fp = os.path.join(d, f)
                try:
                    sz += os.path.getsize(fp)
                    mt = max(mt, os.path.getmtime(fp))
                except OSError:
                    pass
            items.append((mt, sz, d))
            total += sz
        if total <= CACHE_MAX_BYTES:
            return
        for mt, sz, d in sorted(items):
            if total <= CACHE_MAX_BYTES:
                break
            shutil.rmtree(d, ignore_errors=True)
            total -= sz
    except OSError:
        pass


# ---------------------------------------------------------------- 像素 → token

def image_tokens(w, h):
    """一张 w×h 的图在宿主那边算多少 token。

    照抄服务端的实现，不是「像素÷1650」这种粗估 —— 报数要等于计费。
    图先按 14 的倍数向上对齐，每轴 3:1 降采样（一个 token 格 = 42×42px），
    每行多 1 个分隔 token、整张 +2 框架 token；超过 1024 就按长宽比缩到能塞进预算的最大网格。
    """
    w, h = max(1, int(w)), max(1, int(h))
    cols = (w + IMAGE_PATCH - 1) // IMAGE_PATCH
    rows = (h + IMAGE_PATCH - 1) // IMAGE_PATCH
    cols = (cols + IMAGE_DOWNSAMPLE - 1) // IMAGE_DOWNSAMPLE
    rows = (rows + IMAGE_DOWNSAMPLE - 1) // IMAGE_DOWNSAMPLE
    t = rows * (cols + 1) + 2
    if t <= IMAGE_TOKEN_CAP:
        return t
    # 超了：服务端按长宽比缩进预算 ⇒ 反解能装下的最大网格
    ratio = cols / float(rows) if rows else 1.0
    best = IMAGE_TOKEN_CAP
    for r in range(1, 200):
        c = max(1, int(round(r * ratio)))
        while r * (c + 1) + 2 > IMAGE_TOKEN_CAP and c > 1:
            c -= 1
        v = r * (c + 1) + 2
        if v <= IMAGE_TOKEN_CAP:
            best = max(best, v)
    return min(best, IMAGE_TOKEN_CAP)


# ---------------------------------------------------------------- 档位（移植 tiers.js）

def bounds(k, grow=GROW):
    """档位 k 的边长区间（UNIT 单位）。0 档 = 自由尺寸，只给物理下限/上限。"""
    if k == FREE:
        return UNIT / 24.0, UNIT / 2.0
    lo = UNIT / (k + 1.0)
    up = UNIT / (k - 1.0) if (k >= 3 and k in grow) else UNIT / k
    return lo, up


def _smooth(series, win):
    if win <= 1 or len(series) < 3:
        return list(series)
    h = win // 2
    out = []
    for i in range(len(series)):
        a, b = max(0, i - h), min(len(series), i + h + 1)
        out.append(sum(series[a:b]) / float(b - a))
    return out


def variation_profile(frames, native_fps):
    """每帧的「这一带变了多少」：相邻扫描帧平均绝对差，再做时间平滑。

    这就是分配格子大小的信号源 —— 变化小的地方值得看清（给大格），
    变化大的地方每一瞬单看都不重要（给小格）。
    """
    d = [0.0]
    for i in range(1, len(frames)):
        d.append(_mad(frames[i - 1], frames[i]))
    win = max(1, int(round(SMOOTH_FRAMES / float(native_fps or 30.0) * SCAN_FPS)))
    return _smooth(d, win)


def tiers_by_rank(vals, population, lo_tier=1, hi_tier=7):
    """把变化量按**分位**映射到档位。

    🔴 为什么不用绝对阈值（这是实测踩出来的）：
    移植过来的 7 道阈值是「每原生帧的**切点概率**」，它假定你有一个标定过的镜头切分器。
    我们手上只有 2fps 的帧差 —— **镜头运动会被当成切点**。实测一条 213 秒 MV：
    每个 4 秒窗里都有 8 个「切点」，密度全部落在 7 档以上 ⇒ 14 个格子全是一个尺寸，
    档位形同不存在（第一版跑出来正是这样）。

    绝对尺度在这里没有意义，但**相对**尺度有意义：我们只需要知道
    「这一段比那一段变化多」，不需要知道「每秒几刀」。
    所以按分位分配 —— 换分辨率、换帧率、换码率都不会让结果漂。
    """
    if not population or not vals:
        return [3] * len(vals)
    s = sorted(population)
    n = max(1, len(s))
    out = []
    for v in vals:
        # 有多少比例的变化量比它小 ⇒ 0（最静）..1（最动）
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if s[mid] <= v:
                lo = mid + 1
            else:
                hi = mid
        p = lo / float(n)
        k = lo_tier + int(round(p * (hi_tier - lo_tier)))
        out.append(max(lo_tier, min(hi_tier, k)))
    return out


def tier_at(profile, t, native_fps):
    i = int(round(t * SCAN_FPS))
    if not profile:
        return 0.0
    return profile[max(0, min(len(profile) - 1, i))]


# ---------------------------------------------------------------- 打包（移植 pack.js）

def pack_rows(tiers):
    """把一串档位排成行。帧的时序**一格不动**：格子 i 就是第 i 帧。

    🔴 每行放几个由**首选宽 1/档位**决定，不是「一直塞到撞下界」。
    以前那样写，9 个 3 档会被排成 4+4+1（最后一格撑满整行），
    同一档位的格子大小差 4 倍 —— 档位就白设了。

    3 档 → 一行 3 个；1 档 → 一行 1 个；8 档 → 一行 8 个。
    行内按首选宽比例铺满 UNIT；档位混排导致凑不整时按比例缩放（记 stretched）。
    """
    rows, cur, acc = [], [], 0.0
    for k in tiers:
        pref = UNIT / float(max(TIER_MIN, min(TIER_MAX, int(k))))
        if cur and acc + pref > UNIT + 1e-9:
            rows.append(cur)
            cur, acc = [k], pref
        else:
            cur.append(k)
            acc += pref
    if cur:
        rows.append(cur)

    out = []
    for r in rows:
        prefs = [UNIT / float(max(TIER_MIN, min(TIER_MAX, int(k)))) for k in r]
        tot = sum(prefs) or 1.0
        out.append({"s": [p * UNIT / tot for p in prefs],
                    "stretched": abs(tot - UNIT) > 1e-6})
    return out


def layout_cells(tiers, aspect, budget):
    """算出画布与每格像素矩形。

    格子是**视频比例**的矩形：宽 w，高 w/aspect。
    所以行高 = 该行最宽那格的高度，画布高 = 各行高之和 —— 不是方格的行高。
    画布比例是算出来的，格内没有黑边。
    """
    aspect = max(0.2, min(6.0, float(aspect or 16 / 9.0)))
    rows = pack_rows(tiers)
    row_h = [(max(r["s"]) / UNIT) / aspect for r in rows]
    total_h_norm = sum(row_h) or 0.01           # 1.0 = 满宽
    px = math.sqrt(max(1.0, budget) / total_h_norm)   # 画布宽 = px 像素
    gap = max(1, int(round(px * 0.004)))
    cells, y = [], 0.0
    for r, rh in zip(rows, row_h):
        x = 0.0
        for s in r["s"]:
            w_norm = s / UNIT
            cw = max(2, int(round(w_norm * px)) - gap)
            ch = max(2, int(round(w_norm * px / aspect)) - gap)
            cells.append({"x": int(round(x * px)), "y": int(round(y * px)),
                          "w": cw, "h": ch, "tier_w": round(w_norm, 4)})
            x += w_norm
        y += rh
    W = max(c["x"] + c["w"] for c in cells)
    H = max(c["y"] + c["h"] for c in cells)
    return {"W": W, "H": H, "gap": gap, "cells": cells, "rows": len(rows),
            "tokens": image_tokens(W, H)}


# ---------------------------------------------------------------- 探元数据

def _ffprobe_info(path):
    ok, out, err = _run(["ffprobe", "-v", "error", "-print_format", "json",
                         "-show_format", "-show_streams", path], timeout=40)
    if not ok:
        return None, "ffprobe 读不出来：%s" % (err or "未知原因")
    try:
        d = json.loads(out)
    except ValueError:
        return None, "ffprobe 输出不是 JSON"
    vst = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    if not vst:
        return None, "这个文件没有视频轨"
    dur = 0.0
    for src in (vst.get("duration"), (d.get("format") or {}).get("duration")):
        try:
            dur = float(src)
            if dur > 0:
                break
        except (TypeError, ValueError):
            continue
    fps = 30.0
    fr = vst.get("avg_frame_rate") or vst.get("r_frame_rate") or "0/1"
    m = re.match(r"^(\d+)/(\d+)$", str(fr))
    if m and int(m.group(2)):
        fps = max(1.0, int(m.group(1)) / float(int(m.group(2))))
    subs = [s for s in d.get("streams", []) if s.get("codec_type") == "subtitle"]
    auds = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    return {
        "duration": round(dur, 3), "fps": round(fps, 3),
        "width": int(vst.get("width") or 0), "height": int(vst.get("height") or 0),
        "video_codec": vst.get("codec_name") or "",
        "audio_streams": len(auds), "subtitle_streams": len(subs),
        "size": int((d.get("format") or {}).get("size") or 0),
    }, ""


_BILI_RE = re.compile(r"(BV[0-9A-Za-z]{10})")


def _http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _bili_meta(bvid):
    """B站元数据 + 故事板索引。纯 urllib，不需要 bilibili-api-python。

    ⚠️ `videoshot` 只给 cid **会返回 -400**，必须带 bvid（或 aid）—— 实测踩过。
    """
    try:
        v = _http_json("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid)
    except Exception as e:
        return None, "B站元数据取不到（%s: %s）" % (type(e).__name__, e)
    if v.get("code") != 0 or not v.get("data"):
        return None, "B站元数据接口回了 code=%s" % v.get("code")
    d = v["data"]
    pages = d.get("pages") or []
    cid = d.get("cid") or ((pages or [{}])[0].get("cid") or 0)
    out = {
        "bvid": bvid, "cid": cid, "title": d.get("title") or "",
        "duration": float(d.get("duration") or 0),
        "width": int((d.get("dimension") or {}).get("width") or 0),
        "height": int((d.get("dimension") or {}).get("height") or 0),
        "pages": max(1, len(pages)),
        "owner": (d.get("owner") or {}).get("name") or "",
        "storyboard": None,
    }
    try:
        s = _http_json("https://api.bilibili.com/x/player/videoshot"
                       "?bvid=%s&cid=%s&index=1" % (bvid, cid))
        sd = s.get("data") or {}
        idx = sd.get("index") or []
        imgs = sd.get("image") or []
        if s.get("code") == 0 and idx and imgs:
            out["storyboard"] = {
                "tiles": [float(x) for x in idx],
                "sheets": [("https:" + u) if str(u).startswith("//") else u for u in imgs],
                "tile_w": int(sd.get("img_x_size") or 0),
                "tile_h": int(sd.get("img_y_size") or 0),
            }
    except Exception as e:
        out["storyboard_err"] = "%s: %s" % (type(e).__name__, e)
    return out, ""


# ---------------------------------------------------------------- 扫描：灰度 + 切点

def _scan_gray(path, timeout=300):
    """把整条视频扫成 64×36 灰度序列。**流式**读，内存与时长无关（三小时直播也不会 OOM）。"""
    ok, raw, err = _run(["ffmpeg", "-v", "error", "-i", path, "-an",
                         "-vf", "fps=%g,scale=%d:%d" % (SCAN_FPS, SCAN_W, SCAN_H),
                         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                        timeout=timeout, binary=True)
    if not ok or not raw:
        return None, "扫描失败：%s" % (err or "ffmpeg 没吐出像素")
    n = len(raw) // (SCAN_W * SCAN_H)
    frames = [raw[i * SCAN_W * SCAN_H:(i + 1) * SCAN_W * SCAN_H] for i in range(n)]
    return frames, ""


def _mad(a, b):
    """两个等长灰度帧的平均绝对差（0..255）。"""
    n = min(len(a), len(b))
    if not n:
        return 0.0
    s = 0
    for i in range(n):
        s += abs(a[i] - b[i])
    return s / float(n)


def _thumb(frame):
    """64×36 → 16×16 灰度（最近邻）。去重只比这个。"""
    out = bytearray(DEDUP_THUMB * DEDUP_THUMB)
    for y in range(DEDUP_THUMB):
        sy = int(y * SCAN_H / DEDUP_THUMB)
        base = sy * SCAN_W
        for x in range(DEDUP_THUMB):
            out[y * DEDUP_THUMB + x] = frame[base + int(x * SCAN_W / DEDUP_THUMB)]
    return bytes(out)


# ---------------------------------------------------------------- 取帧

def _extract_frame(path, t, w, h, timeout=60):
    """从原始源在 t 秒取一帧，**由 ffmpeg 直接缩到 w×h**（不在 Python 里重采样）。"""
    ok, raw, err = _run(["ffmpeg", "-v", "error", "-ss", "%.3f" % max(0.0, t),
                         "-i", path, "-frames:v", "1",
                         "-vf", "scale=%d:%d" % (max(2, w), max(2, h)),
                         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                        timeout=timeout, binary=True)
    if not ok:
        return None, "t=%.2fs 取帧失败：%s" % (t, err or "未知原因")
    need = w * h * 3
    if len(raw) < need:
        return None, "t=%.2fs 只拿到 %d 字节（要 %d），可能超出片长" % (t, len(raw), need)
    return raw[:need], ""


def _fetch_bytes(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _decode_tile(sheet_bytes, tile_w, tile_h, cols, pos):
    """从平台拼图里裁一块。用 imgscale 解 PNG；JPEG 走 Pillow（有就用）。

    B站 故事板是 JPEG，本机装了 Pillow（vision.py 也在用）⇒ 这一条不算新依赖。
    """
    try:
        from PIL import Image
    except Exception:
        return None, "平台故事板是 JPEG，需要 Pillow 才能裁（pip install Pillow）"
    import io
    try:
        im = Image.open(io.BytesIO(sheet_bytes)).convert("RGB")
    except Exception as e:
        return None, "拼图解不开（%s: %s）" % (type(e).__name__, e)
    r, c = pos // cols, pos % cols
    box = (c * tile_w, r * tile_h, (c + 1) * tile_w, (r + 1) * tile_h)
    if box[2] > im.width or box[3] > im.height:
        return None, "块坐标越界（拼图 %dx%d）" % (im.width, im.height)
    tile = im.crop(box)
    return tile.tobytes(), ""


def _resize_rgb(px, sw, sh, dw, dh):
    """纯 Python 双线性放大/缩小。只在平台故事板那条（块小）用到。"""
    if sw == dw and sh == dh:
        return px
    out = bytearray(dw * dh * 3)
    for y in range(dh):
        sy = max(0.0, min(sh - 1.0, (y + 0.5) * sh / dh - 0.5))
        y0 = int(sy)
        y1 = min(sh - 1, y0 + 1)
        fy = sy - y0
        r0, r1 = y0 * sw * 3, y1 * sw * 3
        o = y * dw * 3
        for x in range(dw):
            sx = max(0.0, min(sw - 1.0, (x + 0.5) * sw / dw - 0.5))
            x0 = int(sx)
            x1 = min(sw - 1, x0 + 1)
            fx = sx - x0
            a, b = x0 * 3, x1 * 3
            for ch in range(3):
                va = px[r0 + a + ch] * (1 - fx) + px[r0 + b + ch] * fx
                vb = px[r1 + a + ch] * (1 - fx) + px[r1 + b + ch] * fx
                out[o + x * 3 + ch] = int(va * (1 - fy) + vb * fy + 0.5)
    return out


# ---------------------------------------------------------------- 拼图

def _compose(layout, frames, bg=(24, 24, 26)):
    """把各格帧贴进画布。背景用深灰——空白格模型看得见「这里没内容」。"""
    W, H = layout["W"], layout["H"]
    canvas = bytearray(bg * (W * H))
    for c, f in zip(layout["cells"], frames):
        if not f:
            continue
        px, fw, fh = f
        cw, ch = c["w"], c["h"]
        if fw != cw or fh != ch:
            px = _resize_rgb(px, fw, fh, cw, ch)
        for y in range(ch):
            src = y * cw * 3
            dst = ((c["y"] + y) * W + c["x"]) * 3
            canvas[dst:dst + cw * 3] = px[src:src + cw * 3]
    return bytes(canvas)


def _write_sheet(layout):
    """写 PNG 到缓存目录，返回路径。"""
    key = layout["_key"]
    d = os.path.join(_cache_root(), key)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "sheet.png")
    imgscale.write_png(p, layout["W"], layout["H"], 3, layout["_canvas"])
    return p


# ---------------------------------------------------------------- 字幕

def _bili_subtitle(bvid, cid):
    """尽量取 B站 字幕。取不到就如实报状态，**不把「没字幕」写成故障**。"""
    try:
        d = _http_json("https://api.bilibili.com/x/player/v2?bvid=%s&cid=%s" % (bvid, cid))
    except Exception as e:
        return {"state": "unavailable", "why": "%s: %s" % (type(e).__name__, e),
                "means": "取不到字幕接口（不是工具坏了）"}
    if d.get("code") != 0:
        return {"state": "unavailable", "why": "code=%s" % d.get("code"),
                "means": "接口要登录/风控，没取到；不影响看画面"}
    sub = ((d.get("data") or {}).get("subtitle") or {})
    items = sub.get("subtitles") or []
    if not items:
        return {"state": "empty",
                "means": "这条视频没有字幕轨（不是工具坏了，按「没有语音」处理、别重试）"}
    u = items[0].get("subtitle_url") or ""
    if u.startswith("//"):
        u = "https:" + u
    try:
        j = _http_json(u)
        body = j.get("body") or []
        lines = [{"t": round(float(b.get("from") or 0), 2),
                  "text": (b.get("content") or "").strip()} for b in body]
        lines = [l for l in lines if l["text"]]
    except Exception as e:
        return {"state": "error", "why": "%s: %s" % (type(e).__name__, e),
                "means": "字幕地址取到了但内容拉不下来"}
    if not lines:
        return {"state": "empty", "means": "字幕是空的（纯音乐/纯画面），按「没有语音」处理"}
    return {"state": "ok", "lang": items[0].get("lan") or "", "lines": lines,
            "chars": sum(len(l["text"]) for l in lines)}


def _subs_in_range(subs, t0, t1, limit=8):
    if not subs or subs.get("state") != "ok":
        return []
    out = []
    for l in subs.get("lines") or []:
        if t0 - 0.4 <= l["t"] <= t1 + 0.4:
            out.append(l)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- 对外：probe

def probe(src, timeout=30):
    """看一眼「这视频值不值得看」——**不产图、不花 token**。"""
    src = (src or "").strip()
    if not src:
        return {"ok": False, "err": "没给 src（本地路径或 B站链接）"}
    info = {"src": src, "ffmpeg": _has_ffmpeg()}

    m = _BILI_RE.search(src)
    if m and "bilibili" in src.lower() or (m and not os.path.isfile(src)):
        bvid = m.group(1) if m else ""
        meta, err = _bili_meta(bvid)
        if not meta:
            return {"ok": False, "err": err}
        sb = meta.get("storyboard")
        info.update({
            "kind": "bilibili", "title": meta["title"], "owner": meta["owner"],
            "duration": meta["duration"], "width": meta["width"],
            "height": meta["height"], "pages": meta["pages"],
            "subtitle_streams": 1,       # 平台可能有；真取见 storyboard
            "storyboard": {"available": bool(sb),
                           "tiles": len((sb or {}).get("tiles") or []),
                           "tile": "%sx%s" % ((sb or {}).get("tile_w"), (sb or {}).get("tile_h"))
                           if sb else ""},
            "cheap_path": "平台故事板：不下载、不解码，裁块就是帧（代价：每块只有 %s）"
                          % ("%sx%s" % (sb["tile_w"], sb["tile_h"]) if sb else "无"),
            "quality_path": "要更清楚的格子得先下视频再本地抽帧（本模块没做下载）",
        })
    elif os.path.isfile(src):
        if not _has_ffmpeg():
            return {"ok": False, "err": "本机没有 ffmpeg/ffprobe，看不了本地视频",
                    "data": info}
        meta, err = _ffprobe_info(src)
        if not meta:
            return {"ok": False, "err": err, "data": info}
        info.update(meta)
        info["kind"] = "file"
        info["storyboard"] = {"available": False, "note": "本地文件没有平台故事板，走本地抽帧"}
        info["cheap_path"] = "本地抽帧：ffmpeg 流式，内存与时长无关；格子大小按预算自适应"
        info["quality_path"] = "zoom 回原流取指定时刻的原分辨率帧"
    else:
        return {"ok": False, "err": "既不是存在的文件，也不是能识别的 B站 链接：%s" % src[:120]}

    dur = float(info.get("duration") or 0)
    n = max(CELLS_MIN, min(CELLS_MAX, int(round(dur / SEC_PER_CELL)) or CELLS_MIN))
    info["plan"] = {
        "cells": n,
        "why": "约每 %.0f 秒一格，夹在 %d–%d 之间" % (SEC_PER_CELL, CELLS_MIN, CELLS_MAX),
        "est_tokens": image_tokens(int(math.sqrt(DEFAULT_BUDGET * 16 / 9.0)),
                                   int(math.sqrt(DEFAULT_BUDGET * 9 / 16.0))),
        "est_tokens_note": "按默认预算 %d 像素、16:9 估；真值看 storyboard 返回" % DEFAULT_BUDGET,
    }
    info["next"] = "觉得值得再调 video.storyboard；只想看结构可以先看 plan 里的 cells/est_tokens"
    return {"ok": True, "data": info}


# ---------------------------------------------------------------- 对外：storyboard

def storyboard(src, cells=None, budget=DEFAULT_BUDGET, with_subtitle=True,
               prefer="auto", timeout=300):
    """把视频压成一张带格的分镜图 + 文字清单。

    prefer: `auto`（能走平台就走平台）/ `local`（强制本地抽帧）/ `platform`（只用平台）
    """
    t0 = time.time()
    src = (src or "").strip()
    if not src:
        return {"ok": False, "err": "没给 src"}
    budget = max(BUDGET_MIN, min(BUDGET_MAX, int(budget or DEFAULT_BUDGET)))
    m = _BILI_RE.search(src)
    is_bili = bool(m) and (not os.path.isfile(src))
    is_file = os.path.isfile(src)

    if not is_bili and not is_file:
        return {"ok": False, "err": "src 既不是存在的文件也不是 B站 链接：%s" % src[:120]}

    if prefer == "local" and is_bili:
        return {"ok": False, "err": "B站 链接走本地抽帧需要先下载视频，本模块不做下载；"
                                    "要高清格子请先用别的方式落盘再传本地路径"}

    # ---- B站：先看有没有现成故事板（成本≈0）----
    if is_bili and prefer in ("auto", "platform"):
        r = _storyboard_bili(m.group(1), cells, budget, with_subtitle)
        if r.get("ok") or prefer == "platform":
            return r
        if not is_file:
            return r                        # 只有平台这条路，失败就如实报

    # ---- 本地抽帧 ----
    if not _has_ffmpeg():
        return {"ok": False, "err": "本机没有 ffmpeg/ffprobe，看不了本地视频"}
    return _storyboard_local(src, cells, budget, timeout)


def _storyboard_bili(bvid, cells, budget, with_subtitle):
    t0 = time.time()
    meta, err = _bili_meta(bvid)
    if not meta:
        return {"ok": False, "err": err}
    sb = meta.get("storyboard")
    if not sb:
        return {"ok": False, "err": "这条视频没有平台故事板",
                "note": meta.get("storyboard_err") or "接口没给 image/index"}
    tiles = sb["tiles"]
    tw, th = sb["tile_w"] or 160, sb["tile_h"] or 90
    dur = meta["duration"] or (tiles[-1] if tiles else 0)

    aspect = (meta["width"] / float(meta["height"])) if meta.get("height") else 16 / 9.0
    n = max(CELLS_MIN, min(CELLS_MAX, int(cells or round(dur / SEC_PER_CELL) or CELLS_MIN)))
    n = min(n, len(tiles))

    # 时间点：均匀取 n 个，再吸附到最近的块时刻（块是固定间隔切的，别自己造时间）
    want = [dur * (i + 0.5) / n for i in range(n)]
    picked, used = [], set()
    for w in want:
        best = min(range(len(tiles)), key=lambda j: abs(tiles[j] - w))
        while best in used and best + 1 < len(tiles):
            best += 1
        used.add(best)
        picked.append(best)
    picked.sort()

    # 🔴 平台这条路**不做切点检测**：故事板的块是等间隔切的，块与块之间「有没有换镜头」
    #   不下载像素就不知道。以前拿「相邻块时间有间隔」当切点是假的 —— 那等于把
    #   均匀采样说成非均匀，模型会以为哪里变化剧烈，实际只是切块位置。
    #   ⇒ 一律用等档（3 档 = 1/3 宽），把非均匀网格留给真能解码的本地路径。
    tiers = [3] * len(picked)

    # 平台这条路**不能放大**：格子就按块的原始像素来，别拿 160×90 冒充高清
    lay = layout_cells(tiers, aspect, budget)
    scale = min(1.0, tw / float(max(1, max(c["w"] for c in lay["cells"]))))
    if scale < 1.0:
        for c in lay["cells"]:
            c["w"] = max(2, int(c["w"] * scale))
            c["h"] = max(2, int(c["h"] * scale))
        W = max(c["x"] + c["w"] for c in lay["cells"])
        H = max(c["y"] + c["h"] for c in lay["cells"])
        lay.update({"W": W, "H": H, "tokens": image_tokens(W, H)})

    cols = max(1, (max(1, lay["W"]) // max(1, tw)))
    sheets = [_fetch_bytes(u) for u in sb["sheets"]]
    per_sheet = max(1, cols * max(1, (max(1, lay["H"]) // max(1, th))))
    cap0 = None
    frames, got = [], []
    for j in picked:
        try:
            first = sheets[0]
            if cap0 is None:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(first))
                cap0 = max(1, (im.width // tw) * (im.height // th))
            sn, pos = j // cap0, j % cap0
            px, e = _decode_tile(sheets[min(sn, len(sheets) - 1)], tw, th, cap0, pos)
            if px is None:
                frames.append(None)
                got.append({"want_t": tiles[j], "got_t": tiles[j], "err": e})
                continue
            frames.append((px, tw, th))
            got.append({"want_t": tiles[j], "got_t": tiles[j]})
        except Exception as e:
            frames.append(None)
            got.append({"want_t": tiles[j], "got_t": tiles[j],
                        "err": "%s: %s" % (type(e).__name__, e)})

    key = _cache_key("bili", bvid, tuple(picked), lay["W"], lay["H"], budget)
    lay["_key"] = key
    lay["_canvas"] = _compose(lay, frames)
    p = _write_sheet(lay)
    subs = _bili_subtitle(bvid, meta["cid"]) if with_subtitle else {"state": "skipped"}

    return _finish(p, lay, got, bvid, meta, subs, tiers, frames, "platform",
                   "平台故事板（不下载、不解码）", dur, aspect, 0, time.time() - t0)


# ---------------------------------------------------------------- 本地字幕提取

def _srt_time(s):
    """'00:00:01,000' / '00:00:01.000' / '01.000' / '1' → 秒（浮点）。"""
    s = (s or "").strip().replace(",", ".")
    parts = s.split(":")
    try:
        parts = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0.0


def _parse_srt_block(block):
    """一段 srt/vtt 块 → (t, text)；不是合法块返回 None。"""
    lines = [l.strip() for l in block.strip().split("\n") if l.strip()]
    t, txt = None, []
    for l in lines:
        if "-->" in l:
            t = _srt_time(l.split("-->")[0])
        elif t is None and l.isdigit():
            continue                      # 序号行
        elif t is not None:
            txt.append(l)
    if t is None or not txt:
        return None
    return {"t": round(t, 2), "text": " ".join(txt)}


def _parse_ass(raw):
    """[Events] 段里的 Dialogue 行 → (t, text)，去掉样式标签 {\\...}。"""
    out, in_ev = [], False
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith("[Events]"):
            in_ev = True
            continue
        if line.startswith("[") and not line.startswith("[Events]"):
            in_ev = False
        if in_ev and line.startswith("Dialogue:"):
            segs = line[len("Dialogue:"):].split(",")
            if len(segs) < 10:
                continue
            start = _srt_time(segs[1].strip())
            text = ",".join(segs[9:]).replace("\\N", " ").replace("\\n", " ")
            text = re.sub(r"\{[^}]*\}", "", text).strip()
            if text:
                out.append({"t": round(start, 2), "text": text})
    return out


def _local_subtitle(path):
    """尽量取字幕文本：先看外挂 srt/vtt/ass（同名），再抽内嵌轨。取不到如实报 empty。"""
    base = os.path.splitext(path)[0]
    for ext in (".srt", ".vtt", ".ass"):
        p = base + ext
        if not os.path.isfile(p):
            continue
        try:
            raw = open(p, encoding="utf-8-sig", errors="replace").read()
        except Exception:
            continue
        if ext == ".ass":
            lines = _parse_ass(raw)
        else:
            lines = [b for b in (_parse_srt_block(b) for b in re.split(r"\n\s*\n", raw)) if b]
        lines = [l for l in lines if l]
        if lines:
            return {"state": "ok", "lang": ext[1:], "source": "external",
                    "lines": lines, "chars": sum(len(l["text"]) for l in lines)}
    # 内嵌字幕轨：ffmpeg 抽第一条为 srt 文本
    ok, raw, err = _run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:s:0",
                         "-f", "srt", "-"], timeout=60, binary=True)
    if ok and raw:
        txt = raw.decode("utf-8", "replace")
        lines = [b for b in (_parse_srt_block(b) for b in re.split(r"\n\s*\n", txt)) if b]
        if lines:
            return {"state": "ok", "lang": "embedded", "source": "embedded",
                    "lines": lines, "chars": sum(len(l["text"]) for l in lines)}
    return {"state": "empty", "means": "文件内外都没有可提取的字幕文本（不是工具坏了）"}


def _storyboard_local(path, cells, budget, timeout):
    t0 = time.time()
    meta, err = _ffprobe_info(path)
    if not meta:
        return {"ok": False, "err": err}
    dur = meta["duration"]
    aspect = (meta["width"] / float(meta["height"])) if meta["height"] else 16 / 9.0

    frames, err = _scan_gray(path, timeout=timeout)
    if frames is None:
        return {"ok": False, "err": err}
    if len(frames) < 2:
        return {"ok": False, "err": "扫出来只有 %d 帧，视频太短或解码失败" % len(frames)}

    prof = variation_profile(frames, meta["fps"])

    n = max(CELLS_MIN, min(CELLS_MAX, int(cells or round(dur / SEC_PER_CELL) or CELLS_MIN)))
    n = min(n, max(1, len(frames)))
    want = [dur * (i + 0.5) / n for i in range(n)]

    # 时间点吸附到「变化最小的那几处附近」：按钟表切会把一个镜头劈成两半。
    # 找每个目标点 ±dur/(4n) 内变化最小的扫描帧（不是变化最大的 —— 我们要的是镜头内的稳定处）。
    half_win = dur / n * 0.25
    times = []
    for w in want:
        lo = max(0, int((w - half_win) * SCAN_FPS))
        hi = min(len(prof) - 1, int((w + half_win) * SCAN_FPS))
        if hi <= lo:
            times.append(w)
            continue
        j = min(range(lo, hi + 1), key=lambda i: prof[i])
        times.append(j / SCAN_FPS)
    times = sorted(set(round(t, 3) for t in times))

    # 档位：相对分位（见 tiers_by_rank），下限随格子数抬高 ——
    # 9 个格子时全给 1 档会排出 9 行巨格，画布变成一个瘦长条。
    base = max(2, min(5, int(round(math.sqrt(len(times))))))
    vals = [tier_at(prof, t, meta["fps"]) for t in times]
    tiers = tiers_by_rank(vals, prof, lo_tier=base, hi_tier=min(7, base + 4))

    lay = layout_cells(tiers, aspect, budget)

    got, keep_frames, keep_times, keep_tiers = [], [], [], []
    prev_thumb = None
    for t, k in zip(times, tiers):
        c = lay["cells"][len(got)]
        px, e = _extract_frame(path, t, c["w"], c["h"], timeout=60)
        if px is None:
            got.append({"want_t": round(t, 2), "got_t": round(t, 2), "err": e,
                        "cell": len(got) + 1})
            keep_frames.append(None)
            keep_times.append(t)
            keep_tiers.append(k)
            continue
        # 去重：比 16×16 灰度缩略图（把刚到手的 RGB 降成灰度再降采样）
        th = _thumb_gray(px, c["w"], c["h"])
        if prev_thumb is not None and _mad(prev_thumb, th) < DEDUP_MAD:
            got.append({"want_t": round(t, 2), "got_t": round(t, 2),
                        "cell": len(got) + 1, "dropped": "与上一格几乎一样"})
            keep_frames.append(None)
            keep_times.append(t)
            keep_tiers.append(k)
            continue
        prev_thumb = th
        keep_frames.append((px, c["w"], c["h"]))
        keep_times.append(t)
        keep_tiers.append(k)
        got.append({"want_t": round(t, 2), "got_t": round(t, 2), "cell": len(got) + 1})

    key = _cache_key("file", os.path.abspath(path), os.path.getsize(path),
                     tuple(round(t, 2) for t in keep_times), lay["W"], lay["H"], budget)
    lay["_key"] = key
    lay["_canvas"] = _compose(lay, keep_frames)
    p = _write_sheet(lay)

    subs = _local_subtitle(path)

    gaps = [keep_times[i + 1] - keep_times[i] for i in range(len(keep_times) - 1)]
    return _finish(p, lay, got, path, meta, subs, keep_tiers, keep_frames, "local",
                   "本地抽帧（ffmpeg 流式，内存与时长无关）", dur, aspect,
                   max(gaps) if gaps else 0.0, time.time() - t0)


def _thumb_gray(px, w, h):
    """RGB → 16×16 灰度（去重专用）。"""
    out = bytearray(DEDUP_THUMB * DEDUP_THUMB)
    for y in range(DEDUP_THUMB):
        sy = int(y * h / DEDUP_THUMB)
        for x in range(DEDUP_THUMB):
            sx = int(x * w / DEDUP_THUMB)
            o = (sy * w + sx) * 3
            r, g, b = px[o], px[o + 1], px[o + 2]
            out[y * DEDUP_THUMB + x] = (r * 299 + g * 587 + b * 114) // 1000
    return bytes(out)


def _finish(sheet_path, lay, got, src_id, meta, subs, tiers, frames, mode, mode_note,
            dur, aspect, max_gap, elapsed):
    """收口：算清单、存缓存、返回。清单里的每个数都是真取到的，不是估的。"""
    cells_txt = []
    for i, c in enumerate(lay["cells"]):
        g = got[i] if i < len(got) else {}
        cno = i + 1
        line = "第%d格 时间%.2f秒" % (cno, g.get("got_t", 0.0))
        if g.get("dropped"):
            line += "（%s，已留空）" % g["dropped"]
        elif g.get("err"):
            line += "（没取到：%s）" % str(g["err"])[:60]
        cells_txt.append(line)

    if subs.get("state") == "ok":
        for i, c in enumerate(lay["cells"]):
            t = got[i].get("got_t", 0.0) if i < len(got) else 0.0
            nxt = got[i + 1].get("got_t", dur) if i + 1 < len(got) else dur
            lines = _subs_in_range(subs, t, nxt)
            if lines:
                cells_txt[i] += "｜台词：" + " ".join(l["text"] for l in lines)[:90]

    summary = (
        "这是一张按格编号的视频分镜图，深色空格没有内容。%s。"
        "这些是抽样画面，不证明看完整条；没有音轨证据。"
        % "；".join(cells_txt)
    )

    manifest = {
        "sheet": os.path.basename(sheet_path), "sheet_path": sheet_path,
        "mode": mode, "mode_note": mode_note,
        "source": src_id if isinstance(src_id, str) else str(src_id),
        "duration": round(dur, 2), "aspect": round(aspect, 4),
        "canvas": {"w": lay["W"], "h": lay["H"], "rows": lay["rows"],
                   "gap": lay["gap"], "tokens": lay["tokens"]},
        "cells": [dict(c, cell=i + 1,
                       time=round(got[i].get("got_t", 0.0), 2) if i < len(got) else 0.0,
                       tier=(tiers[i] if i < len(tiers) else 1),
                       empty=not (i < len(frames) and frames[i]))
                  for i, c in enumerate(lay["cells"])],
        "coverage_gap_sec": round(max_gap, 2),
        "subtitle": {k: v for k, v in subs.items() if k != "lines"},
        "not_whole_film": NOT_WHOLE_FILM,
        "elapsed": round(elapsed, 2),
        "at": time.time(),
    }
    _cache_put(lay["_key"], manifest, sheet_path, lay["W"] * lay["H"] * 3)

    sample_id = lay["_key"]
    _ZOOM_MEM[sample_id] = {
        "src": src_id, "mode": mode, "aspect": aspect, "at": time.time(),
        "cells": [{"cell": i + 1,
                   "t": got[i].get("got_t", 0.0) if i < len(got) else 0.0,
                   "w": c["w"], "h": c["h"]} for i, c in enumerate(lay["cells"])],
    }
    _prune_zoom()

    return {
        "ok": True,
        "sheet": sheet_path,
        "image": {"path": sheet_path, "w": lay["W"], "h": lay["H"],
                  "tokens": lay["tokens"], "cells": len(lay["cells"]),
                  "rows": lay["rows"]},
        "manifest": manifest,
        "summary": summary,
        "sample_id": sample_id,
        "zoom_hint": "要看某一格的原分辨率画面：video.zoom 传 sample_id=%s、cell=格号" % sample_id,
    }


def _prune_zoom():
    now = time.time()
    for k in [k for k, v in _ZOOM_MEM.items() if now - v["at"] > ZOOM_TTL]:
        _ZOOM_MEM.pop(k, None)


# ---------------------------------------------------------------- 对外：zoom

def zoom(sample_id, cell, width=960):
    """回原始源取某一格的**原分辨率**画面。过期/换片就拒，不拿旧帧冒充。"""
    _prune_zoom()
    s = _ZOOM_MEM.get(sample_id)
    if not s:
        return {"ok": False, "err": "样本已过期或不存在（%ds 有效）；重新调 video.storyboard"
                                    % ZOOM_TTL}
    try:
        cell = int(cell)
    except (TypeError, ValueError):
        return {"ok": False, "err": "cell 要整数（格号从 1 开始）"}
    hit = next((c for c in s["cells"] if c["cell"] == cell), None)
    if not hit:
        return {"ok": False, "err": "没有第 %d 格（共 %d 格）" % (cell, len(s["cells"]))}

    t = hit["t"]
    aspect = s["aspect"]
    w = max(64, min(1600, int(width)))
    h = max(2, int(round(w / max(0.2, aspect))))

    if s["mode"] == "local":
        px, e = _extract_frame(s["src"], t, w, h)
        if px is None:
            return {"ok": False, "err": e}
        d = os.path.join(_cache_root(), sample_id, "zoom")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "cell%02d.png" % cell)
        imgscale.write_png(p, w, h, 3, px)
    else:
        # 平台故事板是这条路的天花板：只能回原拼图裁，**不假装有更清楚的**
        meta, err = _bili_meta(s["src"]) if isinstance(s["src"], str) else (None, "源不是 BV 号")
        sb = (meta or {}).get("storyboard")
        if not sb:
            return {"ok": False, "err": "平台故事板取不到了；重新调 video.storyboard"}
        tw, th = sb["tile_w"], sb["tile_h"]
        tiles = sb["tiles"]
        j = min(range(len(tiles)), key=lambda i: abs(tiles[i] - t))
        cols = None
        import io
        from PIL import Image
        first = Image.open(io.BytesIO(_fetch_bytes(sb["sheets"][0])))
        cols = max(1, first.width // tw)
        cap = max(1, (first.width // tw) * (first.height // th))
        sn, pos = j // cap, j % cap
        # 🔴 这里 cols 是「每行块数」(first.width//tw)，不是 cap（单张总块数）。
        #   之前误传 cap=100，decode_tile 按 100 列算块坐标，c 直接超过 1600 越界。
        px, e = _decode_tile(_fetch_bytes(sb["sheets"][min(sn, len(sb["sheets"]) - 1)]),
                             tw, th, cols, pos)
        if px is None:
            return {"ok": False, "err": e}
        w, h = tw, th
        d = os.path.join(_cache_root(), sample_id, "zoom")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "cell%02d.png" % cell)
        imgscale.write_png(p, w, h, 3, px)

    fit = max(hit["w"], hit["h"]) >= max(w, h) or s["mode"] == "local"
    return {"ok": True, "image": {"path": p, "w": w, "h": h},
            "cell": cell, "t": round(t, 2),
            "honest": ("本地源：这是原分辨率的帧" if s["mode"] == "local"
                       else "平台源：这是平台故事板的原始块 %dx%d，已经是最清楚的了" % (w, h)),
            "note": "取的是这一格的时刻，不是新的采样，也没增加音轨证据"}


# ---------------------------------------------------------------- 自测

def _selftest():
    """不联网：只验纯算术部分（档位 / 打包 / token）。真链路见 evals/。"""
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  ok   " if cond else "  FAIL ") + name + (("  " + extra) if extra else ""))
        ok = ok and bool(cond)

    print("[videolook] 纯算术自测")
    chk("840 = lcm(1..8)", all(UNIT % k == 0 for k in range(1, 9)))
    for k in range(1, 9):
        lo, hi = bounds(k)
        chk("档位 %d 区间有效 (%.1f, %.1f)" % (k, lo, hi), 0 < lo <= hi <= UNIT + 1e-9)
    chk("档位 1 是整幅", abs(bounds(1)[1] - UNIT) < 1e-9)
    chk("边长 = 1/档位：k=4 → 210", abs(UNIT / 4 - 210) < 1e-9)

    rows = pack_rows([1, 3, 3, 3, 2, 2])
    chk("打包出多行", len(rows) >= 2, "%d 行" % len(rows))
    for i, r in enumerate(rows):
        chk("第 %d 行填满 UNIT (%.3f)" % (i + 1, sum(r["s"])),
            abs(sum(r["s"]) - UNIT) < 1e-3)

    lay = layout_cells([1, 3, 3, 3], 16 / 9.0, DEFAULT_BUDGET)
    chk("画布有像素", lay["W"] > 300 and lay["H"] > 200, "%dx%d" % (lay["W"], lay["H"]))
    chk("格子在画布内", all(c["x"] + c["w"] <= lay["W"] and c["y"] + c["h"] <= lay["H"]
                            for c in lay["cells"]))
    chk("整张在预算内", lay["W"] * lay["H"] <= DEFAULT_BUDGET * 1.02,
        "%d px" % (lay["W"] * lay["H"]))
    chk("格子是视频比例（16:9）",
        all(abs(c["w"] / float(c["h"]) - 16 / 9.0) < 0.06 for c in lay["cells"]),
        " / ".join("%dx%d" % (c["w"], c["h"]) for c in lay["cells"][:3]))
    uni = layout_cells([3] * 9, 16 / 9.0, DEFAULT_BUDGET)
    chk("等档 9 格 = 3 行 3 列", uni["rows"] == 3 and len(uni["cells"]) == 9)
    chk("等档格子大小一致",
        len({(c["w"], c["h"]) for c in uni["cells"]}) <= 2,
        str({(c["w"], c["h"]) for c in uni["cells"]}))

    chk("800×450 = 233 token（不是 384）", image_tokens(800, 450) == 233,
        str(image_tokens(800, 450)))
    chk("1300×1300 不超过 1024 封顶", image_tokens(1300, 1300) <= 1024,
        str(image_tokens(1300, 1300)))

    prof = [float(i) for i in range(100)]
    t_all = tiers_by_rank([0.0] * 9, prof, lo_tier=3, hi_tier=7)
    chk("全都最静时落在下限档", all(k == 3 for k in t_all), str(t_all))
    t_all = tiers_by_rank([99.0] * 9, prof, lo_tier=3, hi_tier=7)
    chk("全都最动时落在上限档", all(k == 7 for k in t_all), str(t_all))
    t_mix = tiers_by_rank([0.0, 25.0, 50.0, 75.0, 99.0], prof, lo_tier=1, hi_tier=8)
    chk("分位把静→动铺开到不同档", t_mix == sorted(t_mix) and len(set(t_mix)) >= 4, str(t_mix))
    lay_static = layout_cells(tiers_by_rank([0.0] * 9, prof, lo_tier=3, hi_tier=7), 16 / 9.0, 360000)
    chk("静止视频不会排成瘦长条",
        lay_static["W"] / float(lay_static["H"]) > 0.9,
        "%.2f:1" % (lay_static["W"] / float(lay_static["H"])))
    print("[videolook] %s" % ("全部通过" if ok else "有失败项"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
