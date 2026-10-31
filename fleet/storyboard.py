"""fleet/storyboard.py — 画布 + 分镜：把「一场戏」翻成「一条成片」。

## 这一层对应市面上的什么

2026 年 AI 漫剧/短剧的主流工作台形态是「**节点式无限画布**」（Pixmax、LibTV、
纳米漫剧流水线那一类）：提示词、参考图、出图、图生视频、配音、剪辑各是一个节点，
连线成 DAG。它们的工业流程已经被总结得很固定了：

    剧本 → 分镜脚本(镜头ID/时长/景别/提示词/角色/表情/对白/BGM)
         → 角色定妆(一致性) → 图片生成 → 图生视频 → 配音配乐 → 剪辑成片
    规范：每集 10~14 个分镜，单分镜 2~5 秒；前 3 镜完成钩子；
          竖屏 1080P、字幕每行 ≤15 字。

我们把它压成「一份 board 数据 + 一次调用」，因为手机端要的是「派下去、回来一条片」。

## 手机端能做到哪一层（不粉饰）

| 环节 | 我们的做法 | 和成熟工作流的差距 |
|---|---|---|
| 分镜脚本 | board JSON，可直接喂给 LLM 生成 | 无 |
| 角色一致性 | 文字锚点 + 固定 seed | **做不到** IP-Adapter FaceID / LoRA 那级 |
| 出图 | 手机 QNN NPU，512~1024 | 分辨率低一档，需超分 |
| 图生视频 | **稀疏关键帧 + 几何运镜**（motion.py） | 没有真正的视频扩散模型 |
| 配音 | edge_tts（在线） | 可以，音色可选 |
| 剪辑 | 本层：画布适配 + 转场 + 字幕 + 混音 | ffmpeg 能干完 |

**时间轴的正解（也是这次改动的核心）**：漫剧里镜头时长不是拍脑袋定的，
是**台词念完需要多久**。所以 `run_board` 的顺序是
「先合成配音 → 拿真实语音时长 → 反过来定镜头时长 → 再出画面」，
而不是「先定 3 秒再配音」。这就是上一版视频「时间帧不对」的另一半原因。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time as _t

# ---------------------------------------------------------------------------
# 画布
# ---------------------------------------------------------------------------

# 画布 = 成片规格。竖屏漫剧 9:16，横屏剧情 16:9，朋友圈/小红书 3:4 或 4:5。
CANVAS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "3:4": (1080, 1440),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "2.39:1": (1920, 804),
}

# 手机（local-dream / QNN）真正支持的出图尺寸。表外尺寸没有对应 patch，
# 手机会静默回退 512 出彩色噪声 —— 所以画布适配必须从这里挑。
GEN_SIZES = [(512, 512), (512, 768), (768, 512), (768, 768),
             (768, 1024), (1024, 768), (1024, 1024)]


def canvas_size(name):
    """'9:16' → (1080, 1920)。也接受 (W, H) 元组。"""
    if isinstance(name, (list, tuple)) and len(name) == 2:
        return int(name[0]), int(name[1])
    key = str(name or "9:16").strip()
    if key not in CANVAS:
        raise ValueError("未知画布 %r，可选：%s"
                         % (name, "、".join(CANVAS)))
    return CANVAS[key]


def pick_gen_size(canvas, allowed=None):
    """按画布宽高比挑手机出图尺寸 —— 取对数距离最近的，避免极端裁切。

    9:16 (0.5625)  → 512x768 (0.667)   裁掉 15.6% 宽
    16:9 (1.778)   → 768x512 (1.500)   裁掉 15.6% 高
    """
    W, H = canvas_size(canvas)
    ar = W / float(H)
    pool = allowed or GEN_SIZES
    return min(pool, key=lambda wh: abs(math.log((wh[0] / float(wh[1])) / ar)))


def fit_canvas(img, canvas, mode="cover"):
    """把图铺进画布。cover = 裁满（推荐，竖屏短视频的常规做法）。"""
    from PIL import Image
    W, H = canvas_size(canvas)
    iw, ih = img.size
    if mode == "contain":
        s = min(W / float(iw), H / float(ih))
        canvas_bg = (0, 0, 0)
    else:
        s = max(W / float(iw), H / float(ih))
        canvas_bg = (0, 0, 0)
    nw, nh = max(1, int(round(iw * s))), max(1, int(round(ih * s)))
    im = img.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGB", (W, H), canvas_bg)
    out.paste(im, ((W - nw) // 2, (H - nh) // 2))
    return out


# ---------------------------------------------------------------------------
# 字幕
# ---------------------------------------------------------------------------

FONT_CANDIDATES = [
    r"C:/Windows/Fonts/msyhbd.ttc",        # 微软雅黑 Bold（中文竖屏最好看）
    r"C:/Windows/Fonts/msyh.ttc",
    r"C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

# 行首不能出现的标点
_NO_LINE_START = "，。！？、；：）」』】…—·%"


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    # 最后再翻一下常见目录，别静默放弃
    for d in ("C:/Windows/Fonts", "/usr/share/fonts"):
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith((".ttc", ".ttf", ".otf")) and \
                        ("cjk" in f.lower() or "hei" in f.lower() or "yahei" in f.lower()
                         or "pingfang" in f.lower()):
                    return os.path.join(root, f).replace("\\", "/")
    return ""


def _font(size):
    from PIL import ImageFont
    path = find_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def wrap_text(text, max_chars):
    """按字数折行（中文没有空格），并避免标点落在行首。"""
    text = str(text or "").replace("\r", "")
    lines = []
    for para in text.split("\n"):
        para = para.strip()
        while len(para) > max_chars:
            cut = max_chars
            while cut > 1 and para[cut] in _NO_LINE_START:
                cut -= 1
            lines.append(para[:cut].rstrip())
            para = para[cut:].lstrip()
        if para:
            lines.append(para)
    return lines


def subtitle_layer(size, text, pos="bottom", font_ratio=0.029, opacity=150,
                   color=(255, 255, 255)):
    """把一句字幕渲染成一张**透明 RGBA 图层**。

    为什么单独拆出来：同一镜头里字幕是固定的，但要烧到每一帧上。
    早先是每帧从头排版一次（量文本、算折行、画圆角底板、逐行 draw.text），
    实测 300 多帧光这一步就几十秒。现在整镜头只渲染一次，之后每帧只做
    alpha composite —— 快 3~5 倍，视觉完全一样。
    """
    from PIL import Image, ImageDraw
    W, H = int(size[0]), int(size[1])
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not str(text or "").strip():
        return layer
    fs = max(12, int(round(H * font_ratio)))
    f = _font(fs)
    if f is None:
        return layer
    max_chars = max(6, int(W * 0.86 / fs))
    lines = wrap_text(text, max_chars)
    if not lines:
        return layer

    line_h = int(fs * 1.42)
    pad = int(fs * 0.42)
    block_h = line_h * len(lines) + pad * 2

    # 量最宽一行
    d0 = ImageDraw.Draw(layer)
    tw = 0
    for ln in lines:
        try:
            bb = d0.textbbox((0, 0), ln, font=f)
            tw = max(tw, bb[2] - bb[0])
        except Exception:
            tw = max(tw, int(len(ln) * fs * 0.95))

    margin_x = int(W * 0.05)
    bw = min(W - margin_x * 2, tw + pad * 2)
    bx = (W - bw) // 2
    if pos in ("top", "上"):
        by = int(H * 0.06)
    else:
        by = H - int(H * 0.07) - block_h

    # 半透明底板：纯描边在浅色画面上看不清，加底板最稳
    radius = max(6, pad)
    d0.rounded_rectangle([bx, by, bx + bw, by + block_h], radius=radius,
                         fill=(0, 0, 0, int(opacity)))
    stroke = max(1, fs // 22)
    y = by + pad
    for ln in lines:
        try:
            bb = d0.textbbox((0, 0), ln, font=f)
            lw = bb[2] - bb[0]
        except Exception:
            lw = int(len(ln) * fs * 0.95)
        x = (W - lw) // 2
        d0.text((x, y), ln, font=f, fill=tuple(color) + (255,),
                stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += line_h
    return layer


def apply_layer(img, layer):
    """把 RGBA 图层合成到 RGB 图上，返回 RGB。"""
    if layer is None:
        return img
    from PIL import Image
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def draw_subtitle(img, text, pos="bottom", font_ratio=0.029, opacity=150,
                  color=(255, 255, 255)):
    """在画布上烧一句字幕。返回新图（不改原图）。

    规范（抄的是行业里的通用值）：水平安全区 5%，底部安全区 7%，
    每行不超过 ~15 个汉字（由 font_ratio 反推，1080 宽 / 56px ≈ 17 字）。
    """
    if not str(text or "").strip():
        return img
    layer = subtitle_layer(img.size, text, pos, font_ratio, opacity, color)
    return apply_layer(img, layer)


# ---------------------------------------------------------------------------
# 编码 / 转场
# ---------------------------------------------------------------------------

def _ffmpeg():
    ff = os.environ.get("FLEET_FFMPEG") or shutil.which("ffmpeg") or ""
    if ff and os.path.isfile(ff):
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def encode_frames(paths, fps, out_path, size=None):
    """帧序列 → mp4。用 concat demuxer：不要求文件名连号，转场后也能编。"""
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("编 mp4 需要 ffmpeg：设 FLEET_FFMPEG 或 pip install imageio-ffmpeg")
    if not paths:
        raise RuntimeError("没有帧可编")
    fps = max(1, int(fps))
    tmpdir = tempfile.mkdtemp(prefix="sb_concat_")
    lst = os.path.join(tmpdir, "list.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for p in paths:
            fh.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/")
                     .replace("'", "'\\''"))
            fh.write("duration %.6f\n" % (1.0 / fps))
        # concat demuxer 的坑：最后一张要再写一次，否则末帧时长被吃掉
        fh.write("file '%s'\n" % os.path.abspath(paths[-1]).replace("\\", "/")
                 .replace("'", "'\\''"))
    vf = ""
    if size:
        vf = "scale=%d:%d:flags=lanczos,setsar=1" % (int(size[0]), int(size[1]))
    cmd = [ff, "-y", "-f", "concat", "-safe", "0", "-i", lst]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-r", str(fps), "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-crf", "20", "-movflags", "+faststart",
            out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    try:
        shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception:
        pass
    if proc.returncode != 0 or not os.path.isfile(out_path):
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg 编码失败（rc=%s）：%s" % (proc.returncode, tail))
    return out_path


def _dim(paths, head=0, tail=0, head_from=0.0, tail_to=0.15, tmpdir=None):
    """给一段帧做「淡入淡出」（乘系数），返回写好的新帧路径。

    fade 转场就靠这个：前一段尾部渐黑、后一段头部从黑渐亮，
    时长不变，只是两端压暗 —— 比跨段混合简单，且不会拖慢节奏。
    """
    _need_pil()
    import numpy as np
    from PIL import Image
    out = []
    n = len(paths)
    for i, p in enumerate(paths):
        k = 1.0
        if head and i < head:
            k = head_from + (1.0 - head_from) * (i / float(head))
        if tail and i >= n - tail:
            # 越靠后越暗，最后一帧到 tail_to。早先写成「越靠后越亮」，方向反了 ——
            # 自测里第 5 帧反而比第 3 帧亮才暴露出来。
            prog = (i - (n - tail) + 1) / float(tail)
            k = min(k, 1.0 - (1.0 - tail_to) * prog)
        if k >= 0.999:
            out.append(p)
            continue
        im = Image.open(p).convert("RGB")
        a = (np.asarray(im, dtype=np.float32) * k).astype("uint8")
        dst = os.path.join(tmpdir or os.path.dirname(p),
                           "tr_%05d.jpg" % i)
        Image.fromarray(a).save(dst, quality=95, subsampling=0)
        out.append(dst)
    return out


def _dissolve(paths_a, paths_b, n, tmpdir):
    """A 尾 n 帧 与 B 头 n 帧 交叉叠化，返回 (A 去掉尾 n, 过渡帧, B 去掉头 n)。

    叠化帧数从两段里扣 —— 总时长 = A + B - n/fps，和剪辑软件里的行为一致。
    """
    _need_pil()
    import numpy as np
    from PIL import Image
    if n <= 0 or not paths_a or not paths_b:
        return paths_a, [], paths_b
    n = min(n, len(paths_a), len(paths_b))
    head_a, tail_a = paths_a[:-n], paths_a[-n:]
    head_b, tail_b = paths_b[:n], paths_b[n:]
    mix = []
    for i in range(n):
        p = (i + 1) / float(n + 1)
        a = np.asarray(Image.open(tail_a[i]).convert("RGB"), dtype=np.float32)
        b = np.asarray(Image.open(head_b[i]).convert("RGB"), dtype=np.float32)
        arr = (a * (1 - p) + b * p).astype("uint8")
        dst = os.path.join(tmpdir, "dis_%05d.jpg" % i)
        Image.fromarray(arr).save(dst, quality=95, subsampling=0)
        mix.append(dst)
    return head_a, mix, tail_b


def _need_pil():
    try:
        from PIL import Image  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("storyboard 需要 pillow：pip install pillow（%s）" % exc)


# ---------------------------------------------------------------------------
# 配音（台词决定镜头时长，所以必须先跑）
# ---------------------------------------------------------------------------

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


def tts_local(text, out_wav, voice=None, engine=None, ref=None, ref_text=None,
              timeout=180):
    """本地 TTS（手机 sherpa-onnx 优先，退 PC）。转成 24k 单声道后落到 out_wav。

    为什么统一 24k：后面的混音/拼接路径全是按 edge-tts 的输出（24k 单声道）
    写的，本地 TTS 出的是 44.1k。不归一化的话拼接点会变调、时长也会错。

    engine / ref / ref_text 透传给 tts_runner（三档质量梯度，见 tts_runner.ENGINES）：
      · engine="matcha"（默认）快档单人旁白，RTF 0.12，不吞字
      · engine="zipv" + ref="新闻女" + ref_text=... 克隆任意音色，24kHz（RTF 1.37）
      · engine="indextts2" 精品档（RTF 33，必须配合 voicebank.prewarm 批量预合成）
    """
    try:
        from fleet import tts_runner
    except ImportError:
        import tts_runner
    res = tts_runner.run(text, None, voice=voice, engine=engine, ref=ref,
                         ref_text=ref_text)
    if not res.get("ok"):
        raise RuntimeError(res.get("err") or "本地 TTS 失败")
    src = res["path"]
    if abs(tts_runner.wav_seconds(src) - res["sec"]) > 3.0:
        raise RuntimeError("本地 TTS 时长对不上（%s vs %s）" % (res["sec"], src))
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("转 wav 需要 ffmpeg")
    proc = subprocess.run([ff, "-y", "-i", src, "-ar", "24000", "-ac", "1",
                           "-c:a", "pcm_s16le", out_wav],
                          capture_output=True, text=True, timeout=timeout)
    try:
        if os.path.abspath(src) != os.path.abspath(out_wav):
            os.remove(src)
    except Exception:
        pass
    if proc.returncode != 0 or not os.path.isfile(out_wav):
        raise RuntimeError("ffmpeg 归一化失败：%s" % (proc.stderr or "")[-200:])


def tts(text, out_wav, voice=None, rate="+0%", timeout=60, engine=None,
        ref=None, ref_text=None):
    """合成一句台词 → wav。**先本地，再云**。

    本地（手机 sherpa-onnx）不联网、每句 2 秒级；云 edge-tts 音色更多
    但要出网。漫剧几十条台词，默认走本地才成立 —— 云只是兜底。
    engine/ref/ref_text 透传给本地引擎（见 tts_local）。
    """
    try:
        return tts_local(text, out_wav, voice=voice, engine=engine, ref=ref,
                         ref_text=ref_text, timeout=timeout)
    except Exception as local_err:
        _edge_tts(text, out_wav, voice=voice, rate=rate, timeout=timeout,
                  local_err=local_err)


def _edge_tts(text, out_wav, voice=None, rate="+0%", timeout=60, local_err=None):
    """edge_tts 云合成（要求联网）。失败抛异常，异常里带上本地失败原因。"""
    text = str(text or "").strip()
    if not text:
        raise RuntimeError("台词为空")
    try:
        import asyncio
        import edge_tts
    except Exception as e:
        raise RuntimeError("没装 edge-tts：pip install edge-tts（%s）；%s"
                           % (e, _note_local(local_err)))
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("转 wav 需要 ffmpeg")
    mp3 = out_wav + ".mp3"

    async def _go():
        c = edge_tts.Communicate(text, voice or DEFAULT_VOICE, rate=rate)
        await c.save(mp3)

    try:
        asyncio.run(_go())
    except Exception as e:
        raise RuntimeError("edge-tts 合成失败（多半是断网）：%s: %s；%s"
                           % (type(e).__name__, e, _note_local(local_err)))
    cmd = [ff, "-y", "-i", mp3, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", out_wav]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    try:
        os.remove(mp3)
    except Exception:
        pass
    if proc.returncode != 0 or not os.path.isfile(out_wav):
        raise RuntimeError("ffmpeg 转 wav 失败：%s" % (proc.stderr or "")[-200:])


def _note_local(local_err):
    return ("本地 TTS 也没成（%s）" % local_err) if local_err else ""
    return out_wav


def wav_seconds(path):
    import wave
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _wav_join(pieces, out_path, sample_rate=24000):
    """把 [(wav, pad_head_sec), ...] 拼成一条，保持每段起点对齐。"""
    import wave
    with wave.open(out_path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        for path, pad_head, total_sec in pieces:
            if pad_head > 0:
                out.writeframes(b"\x00\x00" * int(sample_rate * pad_head))
            with wave.open(path, "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
                used = w.getnframes() / float(w.getframerate())
            rest = total_sec - pad_head - used
            if rest > 0:
                out.writeframes(b"\x00\x00" * int(sample_rate * rest))
    return out_path


# ---------------------------------------------------------------------------
# board 校验
# ---------------------------------------------------------------------------

DEFAULT_TRANSITION = "cut"


def validate(board, strict=True):
    """校验 + 补默认值，返回新 dict（不改入参）。"""
    if not isinstance(board, dict):
        raise ValueError("board 必须是 dict")
    shots = board.get("shots") or []
    if not isinstance(shots, list) or not shots:
        raise ValueError("board.shots 不能为空（一集至少 1 个镜头）")
    if len(shots) > 60:
        raise ValueError("一集 %d 个镜头太多了（漫剧常规 10~14 个）" % len(shots))

    out = dict(board)
    out["canvas"] = board.get("canvas") or "9:16"
    canvas_size(out["canvas"])           # 早失败
    out["fps"] = int(board.get("fps") or 24)
    out["gen_fps"] = float(board.get("gen_fps") or 2.0)
    out["steps"] = int(board.get("steps") or 12)
    out["denoise"] = float(board.get("denoise") or 0.28)
    out["style"] = board.get("style") or "cinematic lighting, ultra detailed, 8k"
    out["voice"] = board.get("voice") or DEFAULT_VOICE
    out["size"] = board.get("size") or "%dx%d" % pick_gen_size(out["canvas"])
    out["bgm"] = board.get("bgm")

    chars = {}
    for c in (board.get("characters") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        chars[c["name"]] = {"anchor": str(c.get("anchor") or "").strip()}
    out["_chars"] = chars

    norm = []
    for i, s in enumerate(shots):
        if not isinstance(s, dict):
            raise ValueError("第 %d 个镜头不是 dict" % (i + 1))
        if not str(s.get("prompt") or "").strip():
            raise ValueError("第 %d 个镜头没有 prompt" % (i + 1))
        sec = s.get("sec")
        try:
            sec = float(sec) if sec is not None else 2.5
        except (TypeError, ValueError):
            raise ValueError("第 %d 个镜头的 sec 不是数字：%r" % (i + 1, s.get("sec")))
        if sec <= 0.2 or sec > 30:
            raise ValueError("第 %d 个镜头时长 %.2fs 不合理（0.2~30s）" % (i + 1, sec))
        cast = s.get("cast") or []
        if isinstance(cast, str):
            cast = [cast]
        for name in cast:
            if name not in chars:
                raise ValueError("第 %d 个镜头引用了未定义的角色 %r" % (i + 1, name))
        norm.append({
            "id": s.get("id") or (i + 1),
            "prompt": str(s["prompt"]).strip(),
            "sec": sec,
            "camera": s.get("camera") or "push_in",
            "cast": cast,
            "subtitle": str(s.get("subtitle") or "").strip(),
            "transition": (s.get("transition") or DEFAULT_TRANSITION).lower(),
            "denoise": float(s.get("denoise") or out["denoise"]),
            "seed": int(s.get("seed") if s.get("seed") is not None else 1000 + i * 7),
            # 配音引擎覆盖项（透传给 tts_runner；None 表示用整片默认）
            "tts_engine": s.get("tts_engine"),
            "tts_ref": s.get("tts_ref"),
            "tts_ref_text": s.get("tts_ref_text"),
            "voice": s.get("voice"),
        })
        if norm[-1]["transition"] not in ("cut", "fade", "dissolve"):
            raise ValueError("第 %d 个镜头的转场 %r 不认识（cut/fade/dissolve）"
                             % (i + 1, norm[-1]["transition"]))
    out["shots"] = norm
    return out


def compose_prompt(shot, board):
    """镜头提示词 = 画面 + 角色锚点 + 全局风格。

    角色一致性只能靠「固定文字锚点 + 固定 seed」—— 手机端没有 IP-Adapter /
    LoRA，做不到多角度锁定。这是事实差距，写在这里不装。
    """
    parts = [shot["prompt"]]
    for name in shot.get("cast") or []:
        a = (board.get("_chars") or {}).get(name, {}).get("anchor")
        if a:
            parts.append(a)
    if board.get("style"):
        parts.append(board["style"])
    return ", ".join(p for p in parts if p)


def estimate(board, sec_per_keyframe=7.5):
    """不跑，只算账：关键帧数 / 生成耗时 / 成片时长。"""
    b = validate(board)
    kf = 0
    for s in b["shots"]:
        kf += int(round(s["sec"] * b["gen_fps"])) + 1
    total = sum(s["sec"] for s in b["shots"])
    # 叠化会扣时长
    fps = b["fps"]
    cut = 0
    for i, s in enumerate(b["shots"]):
        if i and s["transition"] == "dissolve":
            cut += int(round(fps * 0.3))
    return {
        "shots": len(b["shots"]),
        "seconds": round(total, 2),
        "keyframes": kf,
        "gen_seconds": round(kf * sec_per_keyframe, 1),
        "playframes": int(round(total * fps)) - cut,
        "canvas": b["canvas"],
        "gen_size": b["size"],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_board(board, out_dir=None, model=None, on_shot=None, with_voice=None,
              dry_run=False):
    """把一份 board 跑成一条成片。

    顺序很关键（这也是「时间轴」的正解）：
      ① 先合成台词配音，拿到**真实语音时长**
      ② 镜头时长 = max(编剧写的时长, 语音时长 + 0.35s 呼吸) —— 台词说了算
      ③ 逐镜头出关键帧（手机 NPU）→ 运镜合成播放帧 → 画布适配 → 烧字幕
      ④ 转场拼接 → 混 BGM → 出片

    返回 {"ok","path","shots":[{...}],"err"}。异常不外抛，写进 err。
    """
    try:
        b = validate(board)
    except Exception as e:
        return {"ok": False, "path": "", "shots": [], "err": str(e)}

    if dry_run:
        return {"ok": True, "path": "", "shots": [], "err": "", "plan": estimate(board)}

    _need_pil()
    import video_gen_runner as vg

    out_dir = out_dir or os.environ.get("FLEET_VIDEO_OUT") or os.path.join(
        os.path.expanduser("~"), ".fleet", "video_out")
    os.makedirs(out_dir, exist_ok=True)
    stamp = str(int(__import__("time").time() * 1000))
    work = os.path.join(out_dir, "sb_" + stamp)
    os.makedirs(work, exist_ok=True)
    fps = b["fps"]
    canvas = b["canvas"]
    W, H = canvas_size(canvas)
    want_voice = (b.get("voice") is not None) if with_voice is None else bool(with_voice)
    # 整片默认引擎：board["tts_engine"] 指定（如 "matcha"/"zipv"），缺省走
    # tts_runner 默认（matcha）。克隆类引擎（zipv）配 board["tts_ref"]/["tts_ref_text"]。
    board_engine = b.get("tts_engine")
    board_ref = b.get("tts_ref")
    board_ref_text = b.get("tts_ref_text")

    # ---- ① 配音先行，拿真实时长 -----------------------------------------
    voices = []
    voice_note = ""
    for i, s in enumerate(b["shots"]):
        entry = {"wav": "", "sec": 0.0, "err": ""}
        if want_voice and s["subtitle"]:
            wav = os.path.join(work, "v_%02d.wav" % i)
            # 镜头可单独覆盖：s["tts_engine"] / s["voice"] / s["tts_ref"] / s["tts_ref_text"]
            # 键存在但为 None 时回退到整片默认
            sh_engine = s.get("tts_engine") or board_engine
            sh_ref = s.get("tts_ref") or board_ref
            sh_ref_text = s.get("tts_ref_text") or board_ref_text
            sh_voice = s.get("voice") or b.get("voice")
            try:
                tts(s["subtitle"], wav, voice=sh_voice, engine=sh_engine,
                    ref=sh_ref, ref_text=sh_ref_text)
                entry["wav"] = wav
                entry["sec"] = wav_seconds(wav)
            except Exception as e:
                entry["err"] = "%s: %s" % (type(e).__name__, e)
                voice_note = voice_note or entry["err"]
        voices.append(entry)

    # ---- ② 台词定镜头时长 ------------------------------------------------
    for s, v in zip(b["shots"], voices):
        need = v["sec"] + 0.35
        if v["sec"] > 0 and need > s["sec"]:
            s["sec"] = round(need, 3)

    # ---- ③ 逐镜头出片 ----------------------------------------------------
    segs = []
    report = []
    gen_s = 0.0
    post_s = 0.0
    for i, s in enumerate(b["shots"]):
        if on_shot:
            try:
                on_shot(i, len(b["shots"]), "gen", s)
            except Exception:
                pass
        prompt = compose_prompt(s, b)

        def _kf_cb(a, tot, t, _i=i):
            if on_shot:
                try:
                    on_shot(_i, len(b["shots"]), "kf:%d/%d" % (a + 1, tot), s)
                except Exception:
                    pass

        t_gen = _t.time()
        mp4x, fdir = vg.localdream_backend(
            prompt, duration=s["sec"], fps=fps, size=b["size"], seed=s["seed"],
            model=model or b.get("model"), out_dir=work, steps=b["steps"],
            gen_fps=b["gen_fps"], motion=s["camera"], denoise=s["denoise"],
            encode=False, on_frame=_kf_cb)
        gen_s += _t.time() - t_gen

        raw = sorted(f for f in os.listdir(fdir) if f.startswith("f_"))
        if not raw:
            raise RuntimeError("镜头 %s 没出帧" % s["id"])
        # 画布适配 + 字幕
        from PIL import Image
        # 字幕整镜头只排版一次，逐帧只做 alpha composite
        sub_layer = subtitle_layer((W, H), s["subtitle"]) if s["subtitle"] else None
        t_post = _t.time()
        adapted = []
        for k, fn in enumerate(raw):
            im = fit_canvas(Image.open(os.path.join(fdir, fn)).convert("RGB"), canvas)
            if sub_layer is not None:
                im = apply_layer(im, sub_layer)
            p = os.path.join(work, "s%02d_%04d.jpg" % (i, k))
            im.save(p, quality=95, subsampling=0)
            adapted.append(p)
        post_s += _t.time() - t_post
        segs.append({"paths": adapted, "shot": s})
        report.append({"id": s["id"], "sec": round(s["sec"], 2),
                       "keyframes": len(sorted(f for f in os.listdir(fdir)
                                               if f.startswith("kf_"))),
                       "voice_sec": round(voices[i]["sec"], 2),
                       "voice_err": voices[i]["err"],
                       "gen_s": round(gen_s - sum(r.get("gen_s", 0)
                                                  for r in report), 1)})

    # ---- ④ 转场 + 拼接 ---------------------------------------------------
    trdir = os.path.join(work, "tr")
    os.makedirs(trdir, exist_ok=True)
    final = []
    fade_n = max(2, int(round(fps * 0.25)))
    dis_n = max(2, int(round(fps * 0.3)))
    for i, seg in enumerate(segs):
        paths = seg["paths"]
        tr = seg["shot"]["transition"]
        if i < len(segs) - 1 and tr == "dissolve":
            nxt = segs[i + 1]["paths"]
            head_a, mix, tail_b = _dissolve(paths, nxt, dis_n, trdir)
            final.extend(head_a + mix)
            segs[i + 1]["paths"] = tail_b
            continue
        if tr == "fade":
            paths = _dim(paths, head=fade_n, tail=fade_n, tail_to=0.05, tmpdir=trdir)
        final.extend(paths)

    out_mp4 = os.path.join(out_dir, "board_%s.mp4" % stamp)
    encode_frames(final, fps, out_mp4, size=(W, H))

    # ---- 音轨 ------------------------------------------------------------
    audio_note = ""
    try:
        track = _build_audio(b, voices, work, os.path.join(out_dir, "board_%s.m4a" % stamp))
        if track:
            merged = os.path.join(out_dir, "board_%s_av.mp4" % stamp)
            _mux(out_mp4, track, merged)
            if os.path.isfile(merged) and os.path.getsize(merged) > 1024:
                out_mp4 = merged
    except Exception as e:
        audio_note = "%s: %s" % (type(e).__name__, e)

    try:
        with open(os.path.join(out_dir, "board_%s.json" % stamp), "w",
                  encoding="utf-8") as fh:
            json.dump({"board": {k: v for k, v in b.items() if not k.startswith("_")},
                       "report": report, "voice_note": voice_note,
                       "audio_note": audio_note,
                       "plan": estimate(board)}, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {"ok": True, "path": out_mp4, "shots": report, "err": "",
            "voice_note": voice_note, "audio_note": audio_note}


def _build_audio(b, voices, work, out_path):
    """台词轨（准）+ BGM（压低）。没有台词也没有 BGM 就返回 ""。"""
    total = sum(s["sec"] for s in b["shots"])
    pieces = []
    cursor = 0.0
    for s, v in zip(b["shots"], voices):
        if v["wav"]:
            pieces.append((v["wav"], 0.18, s["sec"]))
        else:
            pieces.append((None, 0.0, s["sec"]))
        cursor += s["sec"]
    voice_track = os.path.join(work, "voice_all.wav")
    # 无台词段用静音占位
    real = []
    for p, pad, total_sec in pieces:
        if p:
            real.append((p, pad, total_sec))
        else:
            silence = os.path.join(work, "sil_%d.wav" % len(real))
            _write_silence(silence, total_sec)
            real.append((silence, 0.0, total_sec))
    _wav_join(real, voice_track)

    bgm = None
    spec = b.get("bgm")
    if spec:
        try:
            audio_synth = _import_audio()
            preset = spec.get("preset", "ambient") if isinstance(spec, dict) else str(spec)
            gain = float(spec.get("gain", -20)) if isinstance(spec, dict) else -20.0
            one = audio_synth.synth({"preset": preset}, seed=b.get("title") or "board",
                                    out=os.path.join(work, "bgm.wav"))
            bgm = _loop_to(one["path"], total, os.path.join(work, "bgm_loop.wav"))
            b["_bgm_gain"] = gain
        except Exception:
            bgm = None

    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("混音需要 ffmpeg")
    if bgm:
        gain = b.get("_bgm_gain", -20)
        cmd = [ff, "-y", "-i", voice_track, "-i", bgm,
               "-filter_complex",
               "[1:a]volume=%.1fdB[b];[0:a][b]amix=inputs=2:duration=first:"
               "dropout_transition=0[a]" % gain,
               "-map", "[a]", "-ar", "24000", "-ac", "1", "-c:a", "aac",
               "-b:a", "128k", out_path]
    else:
        cmd = [ff, "-y", "-i", voice_track, "-ar", "24000", "-ac", "1",
               "-c:a", "aac", "-b:a", "128k", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError("混音失败：%s" % (proc.stderr or "")[-240:])
    return out_path


def _write_silence(path, sec, sample_rate=24000):
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * int(sample_rate * max(0.0, sec)))
    return path


def _loop_to(path, total, out_path, sample_rate=24000):
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        fr = w.readframes(w.getnframes())
        nfr = w.getnframes()
    need = int(sr * total)
    if nfr <= 0:
        raise RuntimeError("BGM 是空的")
    reps = need // nfr + 1
    data = (fr * reps)[:need * 2]
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)
    return out_path


def _mux(video, audio, out_path):
    ff = _ffmpeg()
    cmd = [ff, "-y", "-i", video, "-i", audio, "-c:v", "copy", "-c:a", "aac",
           "-b:a", "128k", "-shortest", "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError("合成音视频失败：%s" % (proc.stderr or "")[-240:])
    return out_path


def _import_audio():
    try:
        from fleet import audio_synth
        return audio_synth
    except Exception:
        import audio_synth
        return audio_synth


# ---------------------------------------------------------------------------
# 自测（纯 CPU，不碰手机）
# ---------------------------------------------------------------------------

def _selftest():
    ok, bad = [], []

    def chk(cond, msg):
        (ok if cond else bad).append(msg)

    _need_pil()
    from PIL import Image
    import numpy as np

    # 1. 画布
    chk(canvas_size("9:16") == (1080, 1920), "9:16 → 1080x1920")
    chk(canvas_size((800, 600)) == (800, 600), "元组直传")
    try:
        canvas_size("5:4")
        chk(False, "未知画布应报错")
    except ValueError:
        chk(True, "未知画布报错")

    # 2. 挑出图尺寸：宽高比最接近
    chk(pick_gen_size("9:16") == (512, 768), "9:16 → 512x768")
    chk(pick_gen_size("16:9") == (768, 512), "16:9 → 768x512")
    chk(pick_gen_size("1:1") == (512, 512), "1:1 → 512x512")
    chk(pick_gen_size("3:4") == (768, 1024), "3:4 → 768x1024")

    # 3. fit_canvas：cover 填满且不变形（用色块验证裁切）
    src = Image.new("RGB", (512, 768), (10, 20, 30))
    src.paste((250, 250, 250), [0, 300, 512, 468])   # 中间一条白带
    out = fit_canvas(src, "9:16")
    chk(out.size == (1080, 1920), "fit_canvas 输出画布尺寸")
    a = np.asarray(out)
    chk(abs(a.mean() - np.asarray(src).mean()) < 6.0,
        "cover 裁切后平均亮度基本不变（%.1f vs %.1f）"
        % (a.mean(), np.asarray(src).mean()))
    # 白带仍应横向拉满（cover 没加黑边）
    row = a[960]
    chk(row[:, 0].min() > 150, "cover 后白带仍横贯全宽（无黑边）")

    # 4. 折行
    ls = wrap_text("今天下雨了，我们一起去看海吧，好不好呀朋友", 8)
    chk(all(len(x) <= 8 for x in ls), "折行每行 ≤ 8 字（%r）" % (ls,))
    chk("".join(ls).replace(" ", "") == "今天下雨了，我们一起去看海吧，好不好呀朋友",
        "折行不丢字")
    chk(not any(x[0] in "，。！？" for x in ls if x), "标点不落行首")

    # 5. 字幕：画上去画面确实变了，且尺寸不变
    blank = Image.new("RGB", (1080, 1920), (30, 30, 30))
    sub = draw_subtitle(blank, "海边的灯塔亮了一下。", pos="bottom")
    chk(sub.size == blank.size, "字幕不改画布尺寸")
    d = float(np.abs(np.asarray(sub, dtype=np.float32)
                     - np.asarray(blank, dtype=np.float32)).mean())
    chk(d > 1.0, "字幕真的画上去了（像素差 %.2f）" % d)
    chk(draw_subtitle(blank, "") is blank, "空字幕原样返回")

    # 6. 编码：3 帧 → mp4
    tmp = tempfile.mkdtemp(prefix="sb_test_")
    paths = []
    for i in range(6):
        p = os.path.join(tmp, "f_%04d.png" % i)
        Image.new("RGB", (108, 192), (i * 40, 60, 90)).save(p)
        paths.append(p)
    try:
        mp4 = encode_frames(paths, 6, os.path.join(tmp, "a.mp4"), size=(108, 192))
        chk(os.path.getsize(mp4) > 512, "encode_frames 出 mp4（%d 字节）"
            % os.path.getsize(mp4))
    except RuntimeError as e:
        chk("ffmpeg" in str(e), "没 ffmpeg 时明确报错而不是静默（%s）" % str(e)[:40])

    # 7. 淡入淡出：首尾变暗、中间不变
    dim = _dim(paths, head=2, tail=2, tail_to=0.05, tmpdir=tmp)
    l0 = np.asarray(Image.open(dim[0])).mean()
    l3 = np.asarray(Image.open(dim[3])).mean()
    l5 = np.asarray(Image.open(dim[5])).mean()
    chk(l0 < l3, "fade 头部变暗（%.0f < %.0f）" % (l0, l3))
    chk(l5 < l3, "fade 尾部变暗（%.0f < %.0f）" % (l5, l3))

    # 8. 叠化：帧数按 n 扣
    ha, mix, tb = _dissolve(paths[:4], paths[2:], 2, tmp)
    chk(len(ha) == 2 and len(mix) == 2 and len(tb) == 2,
        "dissolve：A 去尾 2 / 过渡 2 / B 去头 2（%d/%d/%d）"
        % (len(ha), len(mix), len(tb)))

    # 9. board 校验
    base = {"title": "测试", "canvas": "9:16", "shots": [
        {"prompt": "a lighthouse", "sec": 2, "camera": "push_in",
         "subtitle": "灯塔亮了"}]}
    b = validate(base)
    chk(b["fps"] == 24 and b["gen_fps"] == 2.0, "默认 fps=24 gen_fps=2")
    chk(b["size"] == "512x768", "9:16 自动挑 512x768")
    chk(b["shots"][0]["transition"] == "cut", "默认转场 cut")
    for bad_board, why in (
        ({"shots": []}, "空 shots"),
        ({"shots": [{"sec": 2}]}, "缺 prompt"),
        ({"shots": [{"prompt": "x", "sec": 0}]}, "时长为 0"),
        ({"shots": [{"prompt": "x", "sec": 2, "transition": "wipe"}]}, "未知转场"),
        ({"shots": [{"prompt": "x", "sec": 2, "cast": ["不存在"]}]}, "引用未定义角色"),
    ):
        try:
            validate(bad_board)
            chk(False, "%s 应该被拒" % why)
        except ValueError:
            chk(True, "%s 被拒" % why)

    # 10. 角色锚点拼进 prompt
    b2 = validate({"canvas": "9:16",
                   "characters": [{"name": "阿岚", "anchor": "white hair, red scarf"}],
                   "shots": [{"prompt": "standing on a cliff", "sec": 2,
                              "cast": ["阿岚"]}]})
    pr = compose_prompt(b2["shots"][0], b2)
    chk("standing on a cliff" in pr and "white hair" in pr,
        "角色锚点拼进镜头提示词")
    chk(b2["style"] in pr, "全局风格后缀也拼进去了")

    # 11. estimate 不跑也能算账
    est = estimate(base)
    chk(est["keyframes"] == int(round(2 * 2.0)) + 1,
        "estimate 关键帧数 = sec*gen_fps+1 = %d" % est["keyframes"])
    chk(est["playframes"] == 48, "estimate 播放帧 = 2*24 = %d" % est["playframes"])

    # 12. 声音拼接（不依赖 TTS）
    w1 = _write_silence(os.path.join(tmp, "s1.wav"), 0.5)
    joined = _wav_join([(w1, 0.2, 1.0), (w1, 0.0, 0.5)],
                       os.path.join(tmp, "j.wav"))
    chk(abs(wav_seconds(joined) - 1.5) < 0.02,
        "wav_join 时长对得上（%.2fs）" % wav_seconds(joined))
    loop = _loop_to(w1, 2.0, os.path.join(tmp, "l.wav"))
    chk(abs(wav_seconds(loop) - 2.0) < 0.02,
        "BGM 循环到全长（%.2fs）" % wav_seconds(loop))

    shutil.rmtree(tmp, ignore_errors=True)
    return ok, bad


if __name__ == "__main__":
    o, f = _selftest()
    for name in o:
        print("  ok   " + name)
    for name in f:
        print("  FAIL " + name)
    print("\n%d/%d 通过" % (len(o), len(o) + len(f)))
    raise SystemExit(1 if f else 0)
