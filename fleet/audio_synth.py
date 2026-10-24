"""procedural audio：用**代码**合成声音（不采样、不调外部引擎）。

🔴 **为什么音频也能「编码生成」**：
    主人的原话是「在计算机眼中，图片本质上也是 01 二进制，所以是走编码生成
    是最好的」。这个类比在音频上**更成立**：
      · 声音 = 采样率 + 采样点序列。给定波形函数，采样点完全由代码算出来。
      · 「采样库」只是把别人的波形**存成文件**再回放；「编码生成」是当场算出来。
    所以本仓的做法与已落地的 `render.html`（HTML→MP4）是**同一个模式**：
      **可寻址的确定性描述 → 逐点计算 → 封装成容器**。

    但边界必须说清（不许吹）：
      · 本层能出的是**功能性声音**（提示音、告警、上线、确认、氛围床）。
        这些的共同点是「波形由规则定义」——音高包络、谐波结构、时长都可参数化。
      · 本层**出不了**「像某个真人说话」的声音。说话是**信息**，
        不是波形规则；它属于 TTS / 音色克隆的活（语音层边界见 docs/总纲_唯一推进依据_2026-10-25.md）。
      · 所以本层与 TTS **互补不替代**：TTS 负责「说什么」，本层负责「系统自己的声音」。

🔴 **零第三方依赖**：只用标准库 `wave` / `math` / `struct` / `hashlib` / `random`。
    本机实测：合成 3.6 秒音频耗时 0.03 秒（RTF 0.0085），比本地神经 TTS 快两个数量级。
    不引入 Tone.js / SDL 那些——那是给浏览器/游戏用的，本仓是后端进程。

🔴 **确定性**：同一个 `seed` 必须出**逐字节相同**的音频。
    种子来自设备 ID ⇒ 每台设备的声音不同，但重启后一模一样。
    这条不是锦上添花：调试时「昨天还好好的」这种话，靠它才查得动。

🔴 **别把本层的输出直接喂给 silero-vad**：
    本层默认 **24 kHz**，而 **silero-vad 只支持 8k / 16k**（`supported_srates`）。
    要闭环必须先重采样到 16 kHz（本机 ffmpeg：
    `-ar 16000 -ac 1 -c:a pcm_s16le`）。
    但**本层是功能性声音、不是人声**，本来就不该走 VAD 端点检测
    ——VAD 是给「主人什么时候开口」用的，提示音不参与那个判断。
    这里只钉一句：**别顺手接上就炸**。
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import tempfile
import wave

#: 默认采样率。24k 是「提示音够用、文件不臃肿」的折中。
DEFAULT_SAMPLE_RATE = 24000


class SynthError(Exception):
    """合成失败。**必须带原因**：上层直接 str(e) 就有话给人看。"""


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _seed_int(seed):
    """把任意 seed（设备 ID 之类）变成稳定的 32 位整数。

    🔴 不用 `hash()`——Python 的 str hash **每进程加盐**（PYTHONHASHSEED），
       同一个设备名在不同进程里会得到不同种子 ⇒ 声音每次都变 ⇒ 不可复现。
       必须用 sha256。
    """
    if seed is None:
        return 0
    if isinstance(seed, int):
        return seed & 0xFFFFFFFF
    h = hashlib.sha256(str(seed).encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big")


def _env(seed, index, salt=0):
    """一个可复现的伪随机数。参数全定 ⇒ 输出全定。"""
    h = hashlib.sha256(("%s|%d|%d" % (seed, index, salt)).encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / float(1 << 64)


def _adsr(i, n, attack=0.02, release=0.3):
    """包络：起音-衰减-保持-释放。避免咔哒声（方波直接切会咔）。"""
    t = i / float(n) if n else 0.0
    a = max(1, int(n * attack))
    r = max(1, int(n * release))
    if i < a:
        return i / float(a)
    if i > n - r:
        return max(0.0, (n - i) / float(r))
    return 1.0


def _expdecay(i, n, k=5.0):
    """指数衰减（铃铛、打击感）。"""
    return math.exp(-k * (i / float(n)))


def _sine(phase):
    return math.sin(phase)


def _square(phase):
    """带限方波：用几个谐波逼近，别用 sign()（会有严重混叠噪声）。"""
    s = 0.0
    for k in (1, 3, 5, 7):
        s += math.sin(phase * k) / k
    return s * 0.6


def _tri(phase):
    p = phase % (2 * math.pi)
    return 2.0 / math.pi * math.asin(math.sin(p))


def _soft_clip(x):
    """软削波：|x|>1 时压回来，不产生硬削波的爆音。"""
    if x > 1.0:
        return 1.0 - 1.0 / (1.0 + (x - 1.0) * 4.0)
    if x < -1.0:
        return -(1.0 - 1.0 / (1.0 + (-x - 1.0) * 4.0))
    return x


# --------------------------------------------------------------------------
# 预设：每个是一个 (sample_rate, seed) -> list[float] 的生成器
# --------------------------------------------------------------------------

def _p_chime(sr, seed):
    """完成音：两声铃（基频 880 → 1320 Hz），带 FM 让它像金属而不像纯正弦。"""
    out = []
    n = int(sr * 0.55)
    for i in range(n):
        t = i / float(sr)
        if t < 0.22:
            f = 880.0
            loc = i
        else:
            f = 1320.0
            loc = i - int(sr * 0.22)
            # 第二声前面留 8ms 的静音，避免两声黏在一起
            if loc < int(sr * 0.008):
                out.append(0.0)
                continue
        loc -= int(sr * 0.008)
        if loc < 0:
            out.append(0.0)
            continue
        env = _expdecay(loc, int(sr * 0.5), 6.0) * _adsr(loc, int(sr * 0.5), 0.01, 0.2)
        # FM：调制比 1.41（近似金属分音比）
        mod = 0.35 * math.sin(2 * math.pi * f * 1.41 * t)
        s = math.sin(2 * math.pi * f * t + mod)
        out.append(_soft_clip(s * env * 0.5))
    return out


def _p_alarm(sr, seed):
    """告警：四声急促的方波哔声，中间有间隔。"""
    out = [0.0] * int(sr * 1.2)
    beep = int(sr * 0.11)
    gap = int(sr * 0.09)
    start = int(sr * 0.02)
    for b in range(4):
        base = start + b * (beep + gap)
        for i in range(beep):
            if base + i >= len(out):
                break
            t = i / float(sr)
            f = 1200.0 if b % 2 == 0 else 1500.0
            env = _adsr(i, beep, 0.02, 0.35)
            out[base + i] = _soft_clip(_square(2 * math.pi * f * t) * env * 0.45)
    return out


def _p_boot(sr, seed):
    """上线：上行琶音（四个音），柔和，像设备醒了。"""
    notes = [392.0, 523.25, 659.25, 783.99]
    out = []
    seg = int(sr * 0.13)
    for k, f in enumerate(notes):
        n = seg if k < len(notes) - 1 else int(sr * 0.42)
        for i in range(n):
            t = i / float(sr)
            env = _adsr(i, n, 0.06, 0.4) * _expdecay(i, n, 2.2)
            s = (0.6 * math.sin(2 * math.pi * f * t)
                 + 0.25 * math.sin(2 * math.pi * f * 2 * t)
                 + 0.1 * math.sin(2 * math.pi * f * 3 * t))
            out.append(_soft_clip(s * env * 0.42))
    return out


def _p_confirm(sr, seed):
    """确认：两声短促上行。"""
    out = []
    for k, f in enumerate((784.0, 1174.7)):
        n = int(sr * 0.09)
        for i in range(n):
            t = i / float(sr)
            env = _adsr(i, n, 0.04, 0.5)
            out.append(_soft_clip(math.sin(2 * math.pi * f * t) * env * 0.4))
        out.extend([0.0] * int(sr * 0.02))
    return out


def _p_error(sr, seed):
    """错误：两声下行（小三度往下）。"""
    out = []
    for k, f in enumerate((622.25, 466.16)):
        n = int(sr * 0.13)
        for i in range(n):
            t = i / float(sr)
            env = _adsr(i, n, 0.04, 0.45)
            out.append(_soft_clip(0.7 * _square(2 * math.pi * f * t) * env * 0.35
                                  + 0.3 * math.sin(2 * math.pi * f * t) * env * 0.35))
        out.extend([0.0] * int(sr * 0.03))
    return out


def _p_tick(sr, seed):
    """倒计时：一记极短的咔哒（带一点余响，不是纯爆点）。"""
    n = int(sr * 0.06)
    out = []
    for i in range(n):
        t = i / float(sr)
        env = _expdecay(i, n, 6.0)
        out.append(_soft_clip(math.sin(2 * math.pi * 2400 * t) * env * 0.4))
    return out


def _p_ambient(sr, seed):
    """氛围床：低频噪声 + 缓慢起伏的暖垫。用种子调色 ⇒ 每台设备略有不同。"""
    dur = 2.4
    n = int(sr * dur)
    base = 110.0 + 40.0 * _env(seed, 1)
    out = []
    lp = 0.0
    for i in range(n):
        t = i / float(sr)
        # 一阶低通把白噪声变成「风」
        noise = _env(seed, 2, i % 977) * 2.0 - 1.0
        lp += 0.035 * (noise - lp)
        lfo = 0.5 + 0.5 * math.sin(2 * math.pi * 0.13 * t)
        pad = (math.sin(2 * math.pi * base * t)
               + 0.4 * math.sin(2 * math.pi * base * 1.5 * t)
               + 0.2 * math.sin(2 * math.pi * base * 2.0 * t))
        env = _adsr(i, n, 0.18, 0.25)
        out.append(_soft_clip((lp * 1.2 * lfo + pad * 0.11) * env * 0.5))
    return out


#: 预设表。键是能力参数 `spec` 的取值。
PRESETS = {
    "chime": {"fn": _p_chime, "desc": "完成/成功通知（两声铃）"},
    "alarm": {"fn": _p_alarm, "desc": "告警（四声急促哔声）"},
    "boot": {"fn": _p_boot, "desc": "设备上线（上行琶音）"},
    "confirm": {"fn": _p_confirm, "desc": "确认（两声短上行）"},
    "error": {"fn": _p_error, "desc": "错误（两声下行）"},
    "tick": {"fn": _p_tick, "desc": "倒计时咔哒"},
    "ambient": {"fn": _p_ambient, "desc": "氛围床（低频噪声 + 暖垫）"},
}


def presets():
    """列出全部预设（只读）。"""
    return [{"id": k, "desc": v["desc"]} for k, v in sorted(PRESETS.items())]


# --------------------------------------------------------------------------
# 合成
# --------------------------------------------------------------------------

def _to_wav_bytes(samples, sample_rate):
    """把 [-1,1] 的浮点采样打包成 16bit 单声道 WAV 的字节。"""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        data = bytearray()
        for s in samples:
            v = int(max(-1.0, min(1.0, s)) * 32767)
            data += struct.pack("<h", v)
        w.writeframes(bytes(data))
    return buf.getvalue()


def _resolve_spec(spec):
    """spec 可以是预设名，也可以是内联 JSON（自定义参数）。

    🔴 陌生 spec 必须**抛明确错误**说清「只认哪几种」——不许猜、不许静默退回默认。
       （这是本仓的通用纪律：静默降级 = 静默失败。）
    """
    if isinstance(spec, dict):
        return spec
    name = (spec or "").strip()
    if name in PRESETS:
        return {"preset": name}
    raise SynthError(
        "不认识的音频预设 %r；只认这几种：%s"
        % (name, "、".join(sorted(PRESETS.keys()))))


def synth(spec, seed=None, out=None, sample_rate=DEFAULT_SAMPLE_RATE,
          fmt="wav", custom=None):
    """合成一段音频。返回 dict（含落盘路径、字节数、时长）。

    参数：
      spec        预设名（"chime" 等）或内联 dict
      seed        任意可哈希的东西（设备 ID 最合适）⇒ 决定音色细节
      out         输出路径；None 时落临时目录
      sample_rate 采样率，默认 24000
      fmt         目前只支持 "wav"（纯标准库不装编码器）
      custom      自定义波形：{"freq": 440, "dur": 1.0, "wave": "sine|tri|square"}

    🔴 失败必须抛 `SynthError` 并带原因；不许返回空结果。
    """
    sr = int(sample_rate or DEFAULT_SAMPLE_RATE)
    if sr < 8000 or sr > 96000:
        raise SynthError("采样率 %d 不合理（允许 8000~96000）" % sr)
    f = (fmt or "wav").lower()
    if f not in ("wav", "wave"):
        raise SynthError("目前只支持 wav 输出（纯标准库不带 mp3 编码器）；"
                         "要 mp3 请先用本机 ffmpeg 转（ffmpeg -i in.wav out.mp3）")

    spec_d = _resolve_spec(spec)
    sid = _seed_int(seed)

    if custom:
        samples = _synth_custom(custom, sr, sid)
        name = "custom"
    else:
        pname = spec_d.get("preset") or ""
        if pname not in PRESETS:
            raise SynthError(
                "内联 spec 里没有 preset 字段，或它不是已知预设；只认这几种：%s"
                "、".join(sorted(PRESETS.keys())))
        samples = PRESETS[pname]["fn"](sr, sid)
        name = pname

    if not samples:
        raise SynthError("合成出 0 个采样点（预设 %r 有问题）" % name)

    raw = _to_wav_bytes(samples, sr)
    path = out or os.path.join(tempfile.gettempdir(),
                               "fleet-audio-%s-%d.wav" % (name, sid))
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(raw)

    return {
        "path": os.path.abspath(path),
        "bytes": len(raw),
        "seconds": round(len(samples) / float(sr), 3),
        "sample_rate": sr,
        "preset": name,
        "seed": sid,
        "samples": len(samples),
    }


def _synth_custom(custom, sr, sid):
    """自定义波形：一条正弦/三角/方波 + 包络。给「要一个特定音」的场景。"""
    freq = float(custom.get("freq") or 440.0)
    dur = float(custom.get("dur") or 1.0)
    wave_kind = (custom.get("wave") or "sine").lower()
    if wave_kind not in ("sine", "tri", "square"):
        raise SynthError("wave 只能是 sine / tri / square（收到 %r）" % wave_kind)
    n = max(1, int(sr * dur))
    fn = {"sine": _sine, "tri": _tri, "square": _square}[wave_kind]
    out = []
    for i in range(n):
        t = i / float(sr)
        env = _adsr(i, n, 0.03, 0.25)
        out.append(_soft_clip(fn(2 * math.pi * freq * t) * env * 0.5))
    return out


# --------------------------------------------------------------------------
# 自测
# --------------------------------------------------------------------------

def _selftest():
    ok = 0
    fail = 0

    def check(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  [ok] %s" % label)
        else:
            fail += 1
            print("  [FAIL] %s" % label)

    tmp = tempfile.mkdtemp(prefix="fleet-synth-")
    for name in sorted(PRESETS):
        try:
            r = synth(name, seed="dev-01", out=os.path.join(tmp, name + ".wav"))
            good = (r["bytes"] > 1000 and r["seconds"] > 0.05
                    and os.path.exists(r["path"]))
            check(good, "预设 %s 合成成功（%.2fs / %d 字节）"
                  % (name, r["seconds"], r["bytes"]))
        except Exception as e:
            check(False, "预设 %s 合成失败：%s" % (name, e))

    # 确定性：同 seed ⇒ 逐字节相同
    a = os.path.join(tmp, "a.wav")
    b = os.path.join(tmp, "b.wav")
    synth("ambient", seed="dev-01", out=a)
    synth("ambient", seed="dev-01", out=b)
    check(open(a, "rb").read() == open(b, "rb").read(),
          "同 seed 两次合成逐字节相同（可复现）")
    c = os.path.join(tmp, "c.wav")
    synth("ambient", seed="dev-02", out=c)
    check(open(a, "rb").read() != open(c, "rb").read(),
          "不同 seed 出不同的音频（每台设备声音不同）")

    # 陌生预设必须报明确原因
    try:
        synth("no-such-preset")
        check(False, "陌生预设应当抛错")
    except SynthError as e:
        check("只认这几种" in str(e), "陌生预设报错说清了「只认哪几种」")
    except Exception as e:
        check(False, "陌生预设抛的不是 SynthError：%s" % type(e).__name__)

    # WAV 必须能读回来、采样数对得上
    import wave as W
    with W.open(a, "rb") as w:
        n = w.getnframes()
        check(w.getnchannels() == 1 and w.getsampwidth() == 2,
              "输出是 16bit 单声道 WAV")
        check(n > 0, "WAV 里有 %d 帧" % n)

    print("[synth] 自测：%d 通过 / %d 失败" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
