"""语音输入输出：麦克风听 → 转文字 → 模型回 → 扬声器念出来（stdlib 优先，全链路懒加载）。

设计原则（沿用本项目 llm.py 的「多后端 + 优雅降级」思路）：

  1) **模块 import 永远成功**。sounddevice / faster_whisper / edge_tts / onnxruntime
     全部在函数内部 import；缺任何一个包，这个模块照样能 import，
     能力差距通过 caps() 说清楚，而不是让整个桌面端起不来。
  2) **纯标准库优先**。录音写 wav 用 wave，计算 RMS 用 math + array，
     只有自测合成波形时才用 numpy（且 numpy 缺失就跳过该项）。
  3) **不弹窗**。播放走 winsound（Windows 自带），ffmpeg 子进程带 CREATE_NO_WINDOW。

能力探测：
  caps() -> {"asr": bool, "tts": "edge"|"kokoro"|None, "why": {"asr": str, "tts": str}}

主链路：
  record()    -> 16k/mono/int16 wav（能量 VAD 自动切出话段）
  transcribe()-> faster_whisper 文字（cuda 失败自动退 cpu+int8）
  listen()    -> record + transcribe 组合
  say()       -> edge_tts -> kokoro -> none 依次降级，wav 由 ffmpeg 转码后 winsound 播放
"""
import array
import asyncio
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave

def _pick_tmp_dir():
    """语音临时目录：环境变量 > 按盘存在性探测（E→D→G 兼容）> 系统 temp。

    历史上这里写死 `G:/_tmp/fleet_voice`，换到没有 G 盘的机器时
    makedirs 被 except 静默吞掉，直到写盘才炸出一个看不懂的 Errno 2——
    典型的「静默失败最阴」。现在：探测时真写一个探针文件，确认可写
    才采用；全部候选都不可用时退系统 temp。
    """
    env = os.environ.get("FLEET_VOICE_TMP")
    cands = ([env] if env else []) + [
        "E:/_tmp/fleet_voice", "D:/_tmp/fleet_voice",
        "G:/_tmp/fleet_voice",                     # 兼容老配置
        os.path.join(os.environ.get("TEMP") or ".", "fleet_voice"),
    ]
    for c in cands:
        if not c:
            continue
        try:
            os.makedirs(c, exist_ok=True)
            probe = os.path.join(c, ".probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            return c
        except Exception:
            continue
    return os.path.join(os.environ.get("TEMP") or ".", "fleet_voice")


# ---- 全局常量（可被环境变量覆盖）----
TMP_DIR = _pick_tmp_dir()
KEEP_TMP = os.environ.get("FLEET_VOICE_KEEP", "") == "1"
SAMPLE_RATE = 16000
BLOCK_SEC = 0.03                       # 30ms 一块：够短做 VAD，够长不空转
DEFAULT_EDGE_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_KOKORO_VOICE = "zf_xiaoxiao"

# ----------------------------------------------------------------------
# 每张角色卡一套音色
# ----------------------------------------------------------------------
# 为什么要有这个：同一个「晓晓」念所有人，听感就是「换了个人但嗓子没换」——
# 机械感一大半来自音色不分人。角色卡带「音色」字段，切卡就换嗓子。
#
# edge 音色只认微软那 8 个中文神经音；kokoro 是本地多音色，更细腻。
# 两套都列上，谁装了就用谁。
VOICE_PROFILES = {
    "whale_girl": {
        "edge": "zh-CN-XiaoyiNeural",       # 活泼
        # 注意：zf_xiaobei 是东北腔（小北），听着偏东北，不要用
        "kokoro": "zf_xiaoxiao",
        "rate": 6, "pitch": "+8Hz",
    },
    "beijixing": {
        "edge": "zh-CN-XiaoxiaoNeural",     # 温暖但直球
        "kokoro": "zf_xiaoxiao",
        "rate": 0, "pitch": "+0Hz",
    },
    "tianshu": {
        "edge": "zh-CN-YunyangNeural",      # 专业、稳定
        "kokoro": "zm_yunjian",
        "rate": -4, "pitch": "-4Hz",
    },
    "tianwang_default": {
        "edge": "zh-CN-XiaoxiaoNeural",
        "kokoro": "zf_xiaoxiao",
        "rate": -2, "pitch": "+0Hz",
    },
    "ansying": {
        "edge": "zh-CN-YunxiNeural",        # 市井、活
        "kokoro": "zm_yunxi",
        "rate": 4, "pitch": "+0Hz",
    },
    "duya": {
        "edge": "zh-CN-YunyangNeural",      # 偏执理想主义
        "kokoro": "zm_yunyang",
        "rate": -6, "pitch": "-6Hz",
    },
    "weila": {
        "edge": "zh-CN-YunjianNeural",      # 零共情、冷静
        "kokoro": "zm_yunjian",
        "rate": -8, "pitch": "-8Hz",
    },
    "qitongwei": {
        "edge": "zh-CN-YunxiNeural",
        "kokoro": "zm_yunxi",
        "rate": 0, "pitch": "-2Hz",
    },
    "helingyi": {
        "edge": "zh-CN-XiaoxiaoNeural",
        "kokoro": "zf_xiaoxiao",
        "rate": 0, "pitch": "+2Hz",
    },
    "lidakang": {
        "edge": "zh-CN-YunyangNeural",
        "kokoro": "zm_yunyang",
        "rate": -2, "pitch": "-4Hz",
    },
}

# edge-tts 7.3.1 只支持 rate / volume / pitch，不开放 style。
# 想让声音有情绪，能动的只有这三根旋钮 + 换音色；「机械感」主要靠
# 音色分人 + 语速略快 + 音调微调来缓解，别指望一个参数翻天。


def voice_profile(card_id=None):
    """取某张卡的音色配置；没配就返回默认。"""
    if card_id and card_id in VOICE_PROFILES:
        return dict(VOICE_PROFILES[card_id])
    return {"edge": DEFAULT_EDGE_VOICE, "kokoro": DEFAULT_KOKORO_VOICE,
            "rate": 0, "pitch": "+0Hz", "volume": None}


def say_for_card(text, card_id=None, play=True):
    """按角色卡念出来：自动选音色 / 语速 / 音调。"""
    prof = voice_profile(card_id)
    # edge 与 kokoro 的音色名不同，say() 里按后端各取一次；这里先给 edge 名，
    # 若 edge 没装上而回落到 kokoro，由 _say_kokoro 用 tar 兜底
    return say(text, voice=prof.get("edge"), rate=prof.get("rate", 0),
               play=play, pitch=prof.get("pitch"),
               volume=prof.get("volume"), kokoro_voice=prof.get("kokoro"))


_WHISPER_CACHE = {}                    # {(model, device): WhisperModel} 模块级单例，只加载一次


# ============================ 小工具 ============================

def _have(mod_name):
    """只查包是否可导入，不真正 import（caps 要快，且不能拖出重依赖）。"""
    try:
        import importlib.util
        return importlib.util.find_spec(mod_name) is not None
    except Exception:
        return False


def _tmp_dir():
    """临时目录（不存在就建）。建失败必须带原因抛，不许静默。"""
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
    except Exception as e:
        raise RuntimeError(
            "语音临时目录不可用：%s（%s）。设 FLEET_VOICE_TMP 指向一个存在的盘。"
            % (TMP_DIR, e))
    return TMP_DIR


def _cleanup_tmp(keep=None):
    """清空临时目录里除 keep 之外的文件（用完即删；FLEET_VOICE_KEEP=1 时不清）。"""
    if KEEP_TMP:
        return
    try:
        for name in os.listdir(TMP_DIR):
            path = os.path.join(TMP_DIR, name)
            if keep and os.path.abspath(path) == os.path.abspath(keep):
                continue
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
    except Exception:
        pass


def _rms_i16(buf):
    """int16 字节块的 RMS（0.0~1.0）。纯标准库，不依赖 numpy。"""
    a = array.array("h")
    try:
        a.frombytes(buf)
    except Exception:
        return 0.0
    if not len(a):
        return 0.0
    total = 0
    for v in a:
        total += v * v
    return math.sqrt(total / len(a)) / 32768.0


# ---------------- Silero VAD（生产框架标配的人声判别） ----------------

_SILERO = {"model": None, "opts": None, "err": None}


def _silero():
    """懒加载 Silero VAD 模型（faster-whisper 自带，无需联网下载）。"""
    if _SILERO["model"] is not None or _SILERO["err"] is not None:
        return _SILERO["model"], _SILERO["opts"]
    try:
        from faster_whisper.vad import get_vad_model, VadOptions
        _SILERO["model"] = get_vad_model()
        _SILERO["opts"] = VadOptions(min_silence_duration_ms=350,
                                     speech_pad_ms=120,
                                     min_speech_duration_ms=250)
    except Exception as e:
        _SILERO["err"] = "Silero 不可用（退回纯能量 VAD）：%s" % e
        print("[voice] %s" % _SILERO["err"])
    return _SILERO["model"], _SILERO["opts"]


def _silero_filter(wav_path, samplerate):
    """用 Silero 判一段 wav 里是否真有人声。

    返回 None 表示 Silero 不可用（调用方按原样放行）；
    返回 (True, detail) 表示有人声；（False, detail) 表示纯噪声/回声，应丢弃。
    """
    model, opts = _silero()
    if model is None:
        return None
    try:
        import numpy as np
        with wave.open(wav_path, "rb") as w:
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if sr != 16000:      # Silero 只吃 16k；录音本来就是 16k，保险起见
            return None
        audio = pcm.astype(np.float32) / 32768.0
        if len(audio) < 400:
            return False, "太短"
        # Silero 按 512 样本（32ms@16k）一块推理，末尾补零对齐
        pad = (-len(audio)) % 512
        if pad:
            audio = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
        speech = model(audio)   # 逐帧概率 (0~1)
        prob = float(speech.max()) if hasattr(speech, "max") else 0.0
        if prob >= 0.5:
            return True, "speech_prob=%.2f" % prob
        return False, "speech_prob=%.2f" % prob
    except Exception as e:
        # 判别失败就放行（宁可多听，不可把真话丢了）
        return True, "silero_error:%s" % e


def _write_wav(path, pcm, samplerate=SAMPLE_RATE, channels=1):
    """把 int16 原始字节写成标准 wav。"""
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(samplerate)
        w.writeframes(pcm)
    return path


def _wav_duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def envelope(wav_path, hop_ms=50, smooth=3):
    """读 wav，算一条响度包络（0~1 的列表，每 hop_ms 一格）。

    用途：让界面上的球跟着「说话的音量」起伏——这是 ChatGPT 那个光球
    真正的魔法所在，它不是在随机呼吸，是在跟着声音跳。

    winsound 播放时拿不到播放进度，但 wav 时长是已知的：
    起播时记 t0，每帧用 (now-t0) 去查包络，就能对上嘴型。

    mp3 读不了（wave 不支持），返回空列表，调用方退化成匀速呼吸。
    """
    if not wav_path or not os.path.isfile(wav_path):
        return []
    try:
        with wave.open(wav_path, "rb") as w:
            sr = w.getframerate() or 16000
            ch = w.getnchannels() or 1
            sw = w.getsampwidth() or 2
            raw = w.readframes(w.getnframes())
    except Exception:
        return []
    if sw != 2:
        return []
    a = array.array("h")
    try:
        a.frombytes(raw)
    except Exception:
        return []
    if ch > 1:
        a = array.array("h", a[::ch])
    hop = max(1, int(sr * hop_ms / 1000.0))
    vals = []
    total = len(a)
    for i in range(0, total, hop):
        block = a[i:i + hop]
        if not block:
            break
        s = 0
        for v in block:
            s += v * v
        vals.append(math.sqrt(s / float(len(block))) / 32768.0)
    if not vals:
        return []
    peak = max(vals)
    if peak <= 1e-6:
        return [0.0] * len(vals)
    vals = [min(1.0, v / peak) for v in vals]
    # 平滑，避免球抖成一团
    if smooth > 1 and len(vals) > smooth:
        out = []
        for i in range(len(vals)):
            lo = max(0, i - smooth)
            seg = vals[lo:i + 1]
            out.append(sum(seg) / len(seg))
        vals = out
    return vals


def _wav_is_silent(path, thresh=0.0015):
    """整段音频是否基本静音（峰值 RMS 低于阈值）。

    有了这一层，静音 wav 无论装没装 faster_whisper 都返回空串——
    否则某些模型会对纯静音"幻觉"出一串文字，污染上层对话。
    """
    try:
        with wave.open(path, "rb") as w:
            raw = w.readframes(w.getnframes())
    except Exception:
        return False
    if not raw:
        return True
    a = array.array("h")
    try:
        a.frombytes(raw)
    except Exception:
        return False
    if not len(a):
        return True
    peak = max(abs(v) for v in a) / 32768.0
    return peak < thresh


def _ffmpeg():
    """找 ffmpeg：环境变量 FLEET_FFMPEG > 已知路径 > PATH。找不到返回 None。"""
    cand = os.environ.get("FLEET_FFMPEG")
    if cand and os.path.isfile(cand):
        return cand
    for fixed in ("E:/dev/ffmpeg/bin/ffmpeg.exe",
                  "E:/_tools/ffmpeg/bin/ffmpeg.exe",
                  "D:/tools/ffmpeg/bin/ffmpeg.exe",
                  "G:/dev/ffmpeg/bin/ffmpeg.exe"):     # 兼容老配置
        if os.path.isfile(fixed):
            return fixed
    return shutil.which("ffmpeg")


def _run_hidden(cmd):
    """跑子进程且不弹黑框。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          creationflags=flags, check=False)


# ============================ 能力探测 ============================

def caps():
    """探测本机语音能力。任何依赖缺失都不抛异常，只在 why 里说明缺哪个包。"""
    why = {"asr": "", "tts": ""}

    # --- ASR：需要 sounddevice（录音）+ faster_whisper（识别）都在 ---
    has_sd = _have("sounddevice")
    has_fw = _have("faster_whisper")
    missing = []
    if not has_sd:
        missing.append("sounddevice")
    if not has_fw:
        missing.append("faster_whisper")
    asr = has_sd and has_fw
    if asr:
        why["asr"] = "可用：sounddevice + faster_whisper 均已安装"
    else:
        why["asr"] = ("不可用：缺少 " + "、".join(missing)
                      + "（pip install " + " ".join(missing) + "）")

    # --- TTS：与 say() 的实际顺序保持一致（kokoro 先，edge 后）---
    # 顺序必须和 say() 一致，否则体检说「用 edge」、实际走 kokoro，人会被绕晕。
    has_edge = _have("edge_tts")
    has_kokoro = _have("kokoro") and _have("onnxruntime")
    extra = []
    if has_kokoro:
        tts = "kokoro"
        extra.append("kokoro + onnxruntime（本地，离线可用；首次加载约 20 秒，之后每句数秒）")
    elif has_edge:
        tts = "edge"
        extra.append("edge_tts（在线；需要网络）")
        if not _ffmpeg():
            extra.append("但没找到 ffmpeg，mp3→wav 转码会跳过")
    else:
        tts = None
        miss = []
        if not has_edge:
            miss.append("edge_tts")
        if not has_kokoro:
            miss.append("kokoro/onnxruntime")
        extra.append("不可用：缺少 " + "、".join(miss) + "（pip install edge_tts 即可）")
    # 兜底后端也报出来，别让人以为只有一个
    if tts == "kokoro" and has_edge:
        extra.append("；edge 作为兜底")
    elif tts == "edge" and has_kokoro:
        extra.append("；kokoro 也可用")
    why["tts"] = "；".join(extra)

    return {"asr": asr, "tts": tts, "why": why}


# ============================ 录音（能量 VAD） ============================

# AGC 自动增益（成熟 ASR 产线标配，参考 pyloudnorm/RMS 归一化做法）：
# 先测环境底噪，把整段录音统一放大到目标响度再送识别——
# 修「PC 麦克风电平低，必须大声说才被当成说话」的问题。
AGC_FLOOR_TARGET = 0.004     # 底噪放大后映射到的目标 RMS（很安静的水平）
AGC_MAX_GAIN = 40.0          # 增益上限（~32dB），防止纯静音被放大成一坨噪声


def _agc_boost_pcm(pcm, gain):
    """把 int16 pcm 乘以 gain 并软限幅。gain<=1 时原样返回。"""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return pcm
    if g <= 1.001:
        return pcm
    a = array.array("h")
    try:
        a.frombytes(pcm)
    except Exception:
        return pcm
    for i, v in enumerate(a):
        x = int(v * g)
        if x > 32767:
            x = 32767
        elif x < -32768:
            x = -32768
        a[i] = x
    return a.tobytes()


class _Vad:
    """能量 VAD 状态机。

    规则：
      - RMS > vad_rms 视为「有人在说」
      - 说过话之后连续静音 silence_end 秒 -> 收尾
      - 一直没声音超过 no_speech_timeout 秒 -> 判定为没人说话
      - 总时长超过 max_s -> 强制收尾
    """

    def __init__(self, samplerate=SAMPLE_RATE, vad_rms=0.012, silence_end=0.8,
                 no_speech_timeout=3.0, max_s=15.0, block_sec=BLOCK_SEC):
        self.block_sec = block_sec
        self.vad_rms = vad_rms
        self.silence_end = silence_end
        self.no_speech_timeout = no_speech_timeout
        self.max_s = max_s
        self.idx = 0                     # 已处理块数
        self.elapsed = 0.0               # 已录制秒数
        self.first = None                # 首个语音块下标
        self.last = None                 # 末个语音块下标
        self.silence_run = 0.0           # 说话之后的连续静音秒数
        self.has_speech = False
        self.reason = None
        self.gain = 1.0                  # AGC 增益（底噪标定后更新）
        self._noise_floor = []           # 开头几块估环境噪声
        self._floor_done = False

    def feed(self, rms):
        """喂一块的 RMS；返回 True 表示该收尾了。

        自适应阈值 + AGC：开头 8 块（~0.24s）估计环境噪声底，据此算一个增益
        （把底噪抬到 AGC_FLOOR_TARGET 的水平，上限 AGC_MAX_GAIN），之后所有
        RMS 先乘增益再判定——小声说话也能过阈值，不用喊。
        阈值 = max(固定值, 放大后底噪×2.2)，防止键盘/风扇把能量 VAD 打穿。
        """
        self.idx += 1
        self.elapsed += self.block_sec
        if not self._floor_done:
            self._noise_floor.append(rms)
            if len(self._noise_floor) >= 8:
                floor = sorted(self._noise_floor)[len(self._noise_floor) // 2]
                self.gain = min(AGC_MAX_GAIN,
                                max(1.0, AGC_FLOOR_TARGET / (floor + 1e-6)))
                self.vad_rms = max(self.vad_rms, floor * self.gain * 2.2)
                self._floor_done = True
            # 估底噪期间不算语音
            return False
        rms = rms * self.gain
        if rms > self.vad_rms:
            if self.first is None:
                self.first = self.idx - 1
            self.last = self.idx - 1
            self.silence_run = 0.0
            self.has_speech = True
        elif self.has_speech:
            self.silence_run += self.block_sec
            if self.silence_run >= self.silence_end:
                self.reason = "silence_end"
                return True
        if not self.has_speech and self.elapsed >= self.no_speech_timeout:
            self.reason = "no_speech"
            return True
        if self.elapsed >= self.max_s:
            self.reason = "max_s" if self.has_speech else "no_speech"
            return True
        return False

    def speech_duration(self, block_sec=None):
        """话段时长（秒）。"""
        if self.first is None or self.last is None:
            return 0.0
        bs = block_sec or self.block_sec
        return (self.last - self.first + 1) * bs


def record(max_s=15.0, samplerate=16000, silence_end=0.8, no_speech_timeout=3.0,
           vad_rms=0.012, device=None, on_level=None):
    """录一段话并自动切出话段。

    `on_level` 是可选回调，每块音频调一次 `on_level(rms01)`（0~1）。
    界面拿它驱动「球跟着你说话的音量跳」——这就是语音模式里那个球的跃动感。

    返回 (wav_path|None, info)：
      info["reason"] = "ok" | "no_speech" | "audio_error" | "no_sounddevice"
      info["duration"] 实际写进 wav 的秒数；info["speech_duration"] VAD 判定的话段秒数
    """
    try:
        import sounddevice as sd
    except Exception as e:
        return None, {"reason": "no_sounddevice", "error": "缺少 sounddevice：%s" % e}

    blocksize = max(1, int(samplerate * BLOCK_SEC))
    frames = []
    vad = _Vad(samplerate=samplerate, vad_rms=vad_rms, silence_end=silence_end,
               no_speech_timeout=no_speech_timeout, max_s=max_s, block_sec=BLOCK_SEC)

    try:
        with sd.InputStream(samplerate=samplerate, channels=1, dtype="int16",
                            blocksize=blocksize, device=device) as stream:
            while True:
                data, _overflowed = stream.read(blocksize)
                raw = data.tobytes() if hasattr(data, "tobytes") else bytes(data)
                frames.append(raw)
                rms = _rms_i16(raw)
                if on_level is not None:
                    try:
                        # AGC 增益同步用于视觉电平：小声说话球也跳得起来
                        on_level(min(1.0, rms * vad.gain * 18.0))
                    except Exception:
                        pass
                if vad.feed(rms):
                    break
    except Exception as e:
        return None, {"reason": "audio_error", "error": "录音失败：%s" % e}

    if not vad.has_speech:
        # 没人说话：丢弃音频，只回原因
        return None, {"reason": "no_speech",
                      "detail": "连续 %.1f 秒没有检测到声音" % no_speech_timeout,
                      "elapsed": round(vad.elapsed, 2)}

    # 切出话段（去掉前后静音），AGC 放大到识别最佳响度，写标准 wav
    start = max(0, vad.first)
    end = min(len(frames) - 1, vad.last)
    pcm = b"".join(frames[start:end + 1])
    if vad.gain > 1.001:
        pcm = _agc_boost_pcm(pcm, vad.gain)
    try:
        path = os.path.join(_tmp_dir(), "rec_%d.wav" % int(time.time() * 1000))
        _write_wav(path, pcm, samplerate=samplerate)
    except Exception as e:
        return None, {"reason": "audio_error", "error": "写 wav 失败：%s" % e}

    # ---- Silero 后验（生产框架标配，faster-whisper 自带）----
    # 能量 VAD 只能听「响不响」；键盘声、关门、咳嗽、扬声器回声都够响。
    # Silero 是神经网络判「是不是人声」，判不是就整个丢掉——
    # 这是「她突然自己搭话/假死」这类闹鬼现象的根治手段。
    sil = _silero_filter(path, samplerate)
    if sil is not None:
        kept, detail = sil
        if not kept:
            return None, {"reason": "no_speech",
                          "detail": "Silero 判非人声（噪声/回声）：" + detail,
                          "elapsed": round(vad.elapsed, 2)}

    return path, {"reason": "ok",
                  "duration": round(len(pcm) / 2.0 / samplerate, 2),
                  "speech_duration": round(vad.speech_duration(), 2),
                  "elapsed": round(vad.elapsed, 2),
                  "stop": vad.reason,
                  "vad_rms": round(vad.vad_rms, 4),
                  "agc_gain": round(vad.gain, 1),
                  "samplerate": samplerate}


# ============================ 语音识别 ============================

def _load_whisper(model, device):
    """懒加载 faster_whisper 模型（模块级单例，只加载一次）。

    ## 模型从哪儿来（这里踩过坑）

    默认让 faster-whisper 自己去 HuggingFace 下模型，国内网络会**卡在
    ConnectTimeout**，然后它把错误吞成一句「找不到快照」——看起来像模型名写错，
    实际是下载不通。所以这里硬定两件事：

    * `HF_ENDPOINT` 默认指镜像 `https://hf-mirror.com`
    * `download_root` 固定到 `G:/_models/whisper`，模型落盘位置可预期，
      重装 Python 也不会丢

    `FLEET_WHISPER_LOCAL=1` 时只认本地已下载的模型，不再联网（飞机上用）。

    device="auto"：先试 cuda，失败自动退到 cpu + int8。
    返回 (WhisperModel|None, 实际后端描述, 错误信息)
    """
    key = (model, device)
    if key in _WHISPER_CACHE:
        m = _WHISPER_CACHE[key]
        return m[0], m[1], m[2]

    # 镜像与落盘位置：必须在 import faster_whisper 之前设好
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env_root = os.environ.get("FLEET_WHISPER_DIR")
    if env_root:
        root = env_root
    else:
        root = next((c for c in ("E:/_models/whisper", "D:/work/_local_ai/whisper",
                                 "G:/_models/whisper")      # 兼容老配置
                     if os.path.isdir(c)), "E:/_models/whisper")
    local_only = os.environ.get("FLEET_WHISPER_LOCAL", "") == "1"

    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        _WHISPER_CACHE[key] = (None, "none", "缺少 faster_whisper：%s" % e)
        return None, "none", "缺少 faster_whisper：%s" % e

    def _mk(dev, compute):
        kw = {"download_root": root}
        if local_only:
            kw["local_files_only"] = True
        return WhisperModel(model, device=dev, compute_type=compute, **kw)

    backend = "none"
    err = None
    obj = None
    if device == "auto":
        try:
            obj = _mk("cuda", "float16")
            backend = "faster_whisper:%s@cuda" % model
        except Exception as e:
            err = "cuda 不可用（%s），已退回 cpu/int8" % str(e)[:120]
            try:
                obj = _mk("cpu", "int8")
                backend = "faster_whisper:%s@cpu/int8" % model
                err = None
            except Exception as e2:
                err = "cuda 失败：%s；cpu 加载也失败：%s" % (str(e)[:100], str(e2)[:200])
                obj = None
                backend = "none"
    else:
        try:
            compute = "float16" if str(device).startswith("cuda") else "int8"
            obj = _mk(device, compute)
            backend = "faster_whisper:%s@%s" % (model, device)
        except Exception as e:
            err = "加载 faster_whisper 失败：%s" % str(e)[:300]
            backend = "none"

    _WHISPER_CACHE[key] = (obj, backend, err)
    return obj, backend, err


_TO_SIMPLE = {"obj": None, "tried": False}


def _to_simplified(text):
    """whisper 中文会输出繁体（训练数据偏港台字幕），统一转简体再往下走。

    没有 opencc 时退回一张高频字表——够把小模型的回答和用户指令对齐。
    """
    if not text:
        return text
    if not _TO_SIMPLE["tried"]:
        _TO_SIMPLE["tried"] = True
        try:
            import opencc
            _TO_SIMPLE["obj"] = opencc.OpenCC("t2s")
        except Exception:
            _TO_SIMPLE["obj"] = None
    conv = _TO_SIMPLE["obj"]
    if conv is not None:
        try:
            return conv.convert(text)
        except Exception:
            pass
    table = str.maketrans({
        "鯨": "鲸", "魚": "鱼", "飯": "饭", "隻": "只", "們": "们", "來": "来",
        "這": "这", "個": "个", "麼": "么", "說": "说", "對": "对", "過": "过",
        "還": "还", "沒": "没", "會": "会", "時": "时", "間": "间", "現": "现",
        "頭": "头", "裡": "里", "開": "开", "關": "关", "點": "点", "樣": "样",
        "覺": "觉", "聽": "听", "講": "讲", "話": "话", "問": "问", "題": "题",
        "實": "实", "現": "现", "東": "东", "西": "西", "車": "车", "買": "买",
    })
    return text.translate(table)


def transcribe(wav_path, model="small", language="zh", device="auto"):
    """把 wav 转成文字。返回 (text, meta)。

    meta = {"backend": str, "duration": float, ...}
    空音频/静音 -> ("", meta)，不抛异常。缺 faster_whisper -> ("", meta) 同样不抛。
    """
    meta = {"backend": "none", "duration": 0.0, "model": model, "language": language}

    if not wav_path or not os.path.isfile(wav_path):
        meta["error"] = "wav 不存在：%s" % wav_path
        return "", meta

    duration = _wav_duration(wav_path)
    meta["duration"] = round(duration, 2)
    if duration <= 0.01:
        meta["error"] = "空音频"
        return "", meta
    if _wav_is_silent(wav_path):
        meta["error"] = "静音音频"
        return "", meta

    obj, backend, err = _load_whisper(model, device)
    meta["backend"] = backend
    if err:
        meta["error"] = err
    if obj is None:
        return "", meta

    def _run(m):
        # initial_prompt 是 whisper 的「热词表」：把常见人称、角色名塞进去，
        # 能显著减少「鲸鱼娘→鲸鱼娘/京鱼娘」这类同音错。
        hint = os.environ.get(
            "FLEET_ASR_HINT",
            "主人，鲸鱼娘，天网，天枢，北极星，米饭，本地模型，聊天。")
        segments, _info = m.transcribe(
            wav_path, language=language, beam_size=3, vad_filter=False,
            initial_prompt=hint, condition_on_previous_text=False)
        parts = []
        for seg in segments:
            text = getattr(seg, "text", "") or ""
            if text.strip():
                parts.append(text.strip())
        return " ".join(parts).strip()

    try:
        out = _run(obj)
    except Exception as e:
        # 本机没装 cuBLAS/cuDNN 时，模型在 GPU 上能建起来、推理时才炸
        # （典型报错：Library cublas64_12.dll is not found）。
        # 这里不再让整条链路死掉——重建成 CPU/int8 再来一次。
        msg = str(e)
        cuda_like = any(k in msg for k in
                        ("cublas", "cudnn", "CUDA", "cuda", "GPU", "driver"))
        if cuda_like and device == "auto":
            meta["error"] = "GPU 推理失败（%s），已回落 CPU 重试" % msg[:120]
            obj2, backend2, err2 = _load_whisper(model, "cpu")
            if obj2 is not None:
                meta["backend"] = backend2 + "(回落)"
                try:
                    out = _run(obj2)
                except Exception as e2:
                    meta["error"] = "CPU 识别也失败：%s" % str(e2)[:200]
                    return "", meta
            else:
                meta["error"] = "CPU 也加载不了：%s" % (err2 or "")[:200]
                return "", meta
        else:
            meta["error"] = "识别失败：%s" % msg[:200]
            return "", meta

    # 后处理：繁转简 + 去掉中文之间的多余空格 + 收拾 whisper 爱加的奇怪标点
    out = _to_simplified(out)
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", out)
    out = re.sub(r"[，,]{2,}", "，", out)
    out = out.replace("?", "？").replace("!", "！").strip()

    return out, meta


# 句子没说完的标志：结尾是这些连接词/语气词时，把静音当成思考停顿，继续听
_INCOMPLETE_TAILS = ("然后", "但是", "可是", "就是", "或者", "还是", "因为", "所以",
                     "而且", "并且", "那个", "这个", "另外", "不过", "要是", "如果")


def _looks_incomplete(text):
    """识别文本结尾像半句话 -> True。启发式：省掉 80% 抢答式打断。"""
    t = (text or "").strip().rstrip("，。,.!！?？…、 ")
    if not t:
        return False
    return any(t.endswith(w) for w in _INCOMPLETE_TAILS)


def listen(**kw):
    """录 + 识别 的组合。返回 (text, wav_path, info)。

    kw 可含 record() 的全部参数（录音设备用 mic_device，避免和识别的 device 撞名），
    以及 transcribe() 的参数（model / language / device，device 指 GPU/CPU）。

    说话没说完（「我想订个……然后」）会自动续听并拼接，最多再听两轮——
    这是 Pipecat/LiveKit 那套 turn-taking 里最划算的启发式，不用额外模型。
    """
    asr_kw = {}
    for k in ("model", "language", "device"):
        if k in kw:
            asr_kw[k] = kw.pop(k)
    if "mic_device" in kw:
        kw["device"] = kw.pop("mic_device")

    texts = []
    last_wav, last_info = None, {}
    for round_i in range(3):                 # 首轮 + 最多两轮续听
        kw["no_speech_timeout"] = 2.0 if round_i else kw.get("no_speech_timeout", 3.0)
        wav_path, info = record(**kw)
        last_info = info
        if wav_path is None:
            break                            # 没声音/噪声：到此为止
        last_wav = wav_path
        text, meta = transcribe(wav_path, **asr_kw)
        info["asr"] = meta
        if not text:
            break
        texts.append(text)
        if not _looks_incomplete(text):
            break                            # 说完了
        # 半句话：静音只是思考停顿，继续听（续听轮的超时收紧，快速放弃）

    merged = " ".join(t for t in texts if t).strip()
    return merged, last_wav, last_info


# ============================ 语音合成 + 播放 ============================

def _say_edge(text, voice=None, rate=0, play=True, style=None, pitch=None, volume=None):
    """edge_tts 合成 mp3 -> ffmpeg 转 wav -> winsound 播放。返回 (backend, path) 或 None。

    实测 edge-tts 7.3.1 只认 `rate` / `volume` / `pitch` 三个参数，
    **没有 style**（style 要走 SSML，库不开放）。所以 `style` 参数保留但忽略，
    免得调用方以为传了就有情绪——真正能改的只有音色 + 语速 + 音调 + 音量。
    """
    try:
        import edge_tts
    except Exception as e:
        print("[voice] edge_tts 不可用：%s" % e)
        return None

    try:
        d = _tmp_dir()
    except RuntimeError as e:
        print("[voice] edge_tts 跳过：%s" % e)
        return None
    stamp = int(time.time() * 1000)
    mp3 = os.path.join(d, "tts_%d.mp3" % stamp)
    wav = os.path.join(d, "tts_%d.wav" % stamp)

    # edge_tts 的 rate 是百分比字符串："+0%" / "+10%" / "-10%"
    r = 0
    try:
        r = int(rate)
    except (TypeError, ValueError):
        r = 0
    kwargs = {}
    if r:
        kwargs["rate"] = "%+d%%" % r
    if pitch:
        kwargs["pitch"] = str(pitch)
    if volume:
        kwargs["volume"] = str(volume)

    try:
        v = voice or DEFAULT_EDGE_VOICE
        communicate = edge_tts.Communicate(text, v, **kwargs)
        asyncio.run(communicate.save(mp3))
    except Exception as e:
        print("[voice] edge_tts 合成失败：%s" % e)
        return None

    if not os.path.isfile(mp3) or os.path.getsize(mp3) == 0:
        print("[voice] edge_tts 产出的 mp3 为空")
        return None

    # mp3 -> wav（winsound 只吃 wav）
    ff = _ffmpeg()
    if ff:
        try:
            proc = _run_hidden([ff, "-y", "-loglevel", "error", "-i", mp3,
                                "-ar", str(SAMPLE_RATE), "-ac", "1", wav])
            if proc.returncode != 0:
                print("[voice] ffmpeg 转码失败：%s"
                      % (proc.stderr or b"").decode("utf-8", "replace")[:200])
                wav = None
        except Exception as e:
            print("[voice] ffmpeg 调用异常：%s" % e)
            wav = None
    else:
        print("[voice] 未找到 ffmpeg，mp3 无法转成 wav（只保留 mp3）")
        wav = None

    target = wav or mp3
    if play:
        _play(target)
    return "edge", target


def _play(path):
    """用 winsound 播放（Windows 自带，不弹窗）。"""
    if not path or not os.path.isfile(path):
        return False
    try:
        import winsound
    except Exception as e:
        print("[voice] winsound 不可用：%s" % e)
        return False
    try:
        # SND_FILENAME 会阻塞到播放结束；加 ASYNC 不用等
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return True
    except Exception as e:
        print("[voice] 播放失败：%s" % e)
        return False


_KOKORO_PIPE = {}


def _kokoro_pipeline(lang_code="z"):
    """kokoro 流水线单例。首次初始化要下模型、建词表，实测 100 秒以上；
    缓存后同进程内再调只有推理时间，所以**必须**缓存，否则每句话都等两分钟。
    """
    if lang_code in _KOKORO_PIPE:
        return _KOKORO_PIPE[lang_code]
    from kokoro import KPipeline
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    pipe = KPipeline(lang_code=lang_code, repo_id="hexgrad/Kokoro-82M")
    _KOKORO_PIPE[lang_code] = pipe
    return pipe


def _say_kokoro(text, voice=None, rate=0, play=True):
    """kokoro 本地合成。返回 (backend, path) 或 None。"""
    if not (_have("kokoro") and _have("onnxruntime")):
        return None
    try:
        pipeline = _kokoro_pipeline("z")
    except Exception as e:
        print("[voice] kokoro 不可用：%s" % e)
        return None

    try:
        d = _tmp_dir()
    except RuntimeError as e:
        print("[voice] kokoro 跳过：%s" % e)
        return None
    wav = os.path.join(d, "kokoro_%d.wav" % int(time.time() * 1000))
    try:
        speed = 1.0 + (float(rate) / 100.0 if rate else 0.0)
        chunks = []
        for _gs, _ps, audio in pipeline(text, voice=voice or DEFAULT_KOKORO_VOICE,
                                         speed=speed):
            chunks.append(audio)
        if not chunks:
            return None
        import numpy as np
        merged = np.concatenate(chunks)
        pcm = (np.clip(merged, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        _write_wav(wav, pcm, samplerate=24000)
    except Exception as e:
        print("[voice] kokoro 合成失败：%s" % e)
        return None

    if play:
        _play(wav)
    return "kokoro", wav


_ACTION_RE = re.compile(r"[（(][^）)]{0,40}[）)]")


def strip_actions(text):
    """念之前把括号里的动作描写剥掉。

    人设卡要求「情绪用尾鳍的小动作带出来」，所以她的回答里会有
    「（尾鳍轻轻一摆）」。这些字**是给人看的，不是给人听的**：
    直接念出来会变成「尾鳍轻轻一摆 嗯 鲸鱼娘在呢」，听着像机器报菜名。
    文字界面保留，语音链路剥掉——这是「不机械」最省事的一刀。
    """
    if not text:
        return text
    out = _ACTION_RE.sub("", text)
    out = re.sub(r"[*_`#>]+", "", out)
    # 剥完可能留下连续标点/空白
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"([，。！？、])\1+", r"\1", out)
    return out.strip(" ，,、")


def say(text, voice=None, rate=0, play=True, pitch=None, volume=None,
        kokoro_voice=None):
    """把文字念出来。返回 (backend, path|None)。

    backend 依次尝试：kokoro（本地，音色更自然） -> edge（在线，音色多） -> none。
    * 本机没装 kokoro 或合成失败，自动落到 edge；
    * 两个都不可用返回 ("none", None)，只打印不抛。

    念之前会剥掉括号动作与 markdown 记号（见 `strip_actions`）。
    play=False 时只合成不播放（自测/网页取音频用）。
    """
    text = strip_actions(text or "")
    if not text:
        return "none", None

    _cleanup_tmp()

    # 本地优先：离线、隐私、音色连续（edge 走网络，断网就哑）
    res = _say_kokoro(text, voice=kokoro_voice or voice, rate=rate, play=play)
    if res:
        return res

    res = _say_edge(text, voice=voice, rate=rate, play=play,
                    pitch=pitch, volume=volume)
    if res:
        return res

    print("[voice] 没有任何可用的 TTS 后端（kokoro / edge_tts 都没装或都失败）")
    return "none", None


# ============================ 自测 ============================

def _selftest():
    """不打开真实麦克风、不依赖网络的自测。返回 True 表示全过。"""
    print("=== voice.py 自测 ===")
    ok = True

    # 1) caps() 在缺包环境下也要返回合法 dict
    try:
        c = caps()
        assert isinstance(c, dict), "caps() 必须返回 dict"
        assert set(["asr", "tts", "why"]).issubset(c.keys()), "caps() 缺字段"
        assert isinstance(c["asr"], bool), "asr 必须是 bool"
        assert c["tts"] in (None, "edge", "kokoro"), "tts 取值非法"
        assert isinstance(c["why"], dict), "why 必须是 dict"
        print("[ok] caps() ->", c)
    except Exception as e:
        ok = False
        print("[FAIL] caps():", e)

    try:
        d = _tmp_dir()
    except RuntimeError as e:
        print("[FAIL] 临时目录：", e)
        return False

    # 2) VAD：用 numpy 合成 静音0.5s + 1kHz正弦1.0s + 静音1.2s，验证能切出约 1.0s 话段
    try:
        import numpy as np
    except Exception:
        print("[skip] numpy 缺失，跳过 VAD 合成波形测试")
        np = None

    if np is not None:
        try:
            sr = SAMPLE_RATE
            n_sil = int(0.5 * sr)
            n_tone = int(1.0 * sr)
            n_tail = int(1.2 * sr)
            t = np.arange(n_tone) / sr
            tone = (0.35 * np.sin(2 * np.pi * 1000.0 * t) * 32767).astype("<i2")
            sil = np.zeros(n_sil, dtype="<i2")
            tail = np.zeros(n_tail, dtype="<i2")
            pcm = np.concatenate([sil, tone, tail]).tobytes()

            src = os.path.join(d, "selftest_tone.wav")
            _write_wav(src, pcm, samplerate=sr)

            # 走和 record() 一样的 VAD 状态机（只是喂现成的块）
            blocksize = max(1, int(sr * BLOCK_SEC))
            vad = _Vad(samplerate=sr, vad_rms=0.012, silence_end=0.8,
                       no_speech_timeout=3.0, max_s=15.0)
            raw_all = np.frombuffer(pcm, dtype="<i2")
            stopped = False
            for off in range(0, len(raw_all), blocksize):
                blk = raw_all[off:off + blocksize].tobytes()
                if vad.feed(_rms_i16(blk)):
                    stopped = True
                    break
            assert stopped, "VAD 应当在静音结束处收尾"
            assert vad.has_speech, "VAD 应当检测到说话"
            dur = vad.speech_duration()
            assert 0.85 <= dur <= 1.15, "话段时长应在 1.0s 附近，实际 %.2f" % dur
            print("[ok] VAD 话段时长 = %.2fs（期望 ~1.0s），停止原因 = %s" % (dur, vad.reason))
        except Exception as e:
            ok = False
            print("[FAIL] VAD 合成测试:", e)

    # 3) transcribe 对空/静音 wav 不抛异常、返回空串
    try:
        empty = os.path.join(d, "selftest_empty.wav")
        _write_wav(empty, b"", samplerate=SAMPLE_RATE)
        text, meta = transcribe(empty)
        assert isinstance(text, str), "transcribe 必须返回 str"
        assert text == "", "空音频应返回空串，实际 %r" % text
        print("[ok] transcribe(空音频) ->", repr(text), meta)
    except Exception as e:
        ok = False
        print("[FAIL] transcribe(空音频):", e)

    try:
        quiet = os.path.join(d, "selftest_quiet.wav")
        _write_wav(quiet, b"\x00\x00" * int(SAMPLE_RATE * 0.5), samplerate=SAMPLE_RATE)
        text, meta = transcribe(quiet)
        assert isinstance(text, str), "transcribe 必须返回 str"
        assert text == "", "静音音频应返回空串，实际 %r" % text
        print("[ok] transcribe(静音音频) ->", repr(text), meta)
    except Exception as e:
        ok = False
        print("[FAIL] transcribe(静音音频):", e)

    # 4) say(play=False) 不抛异常（backend 允许是 none）
    try:
        backend, path = say("测试", play=False)
        assert backend in ("edge", "kokoro", "none"), "backend 取值非法：%r" % backend
        print("[ok] say(play=False) -> backend=%s path=%s" % (backend, path))
    except Exception as e:
        ok = False
        print("[FAIL] say():", e)

    # 5) listen() 在没有麦克风/没有 asr 时要优雅返回，不能抛
    try:
        text, wav_path, info = listen(max_s=0.2, no_speech_timeout=0.1, silence_end=0.1)
        assert isinstance(text, str), "listen 必须返回 str"
        print("[ok] listen() -> text=%r info=%s" % (text, info))
    except Exception as e:
        ok = False
        print("[FAIL] listen():", e)

    _cleanup_tmp()
    print("=== 自测%s ===" % ("通过" if ok else "失败"))
    return ok


# ============================ 命令行入口 ============================

def _main(argv):
    args = argv[1:]
    if "--selftest" in args:
        return 0 if _selftest() else 1
    if "--caps" in args:
        c = caps()
        print("ASR 可用:", c["asr"], "|", c["why"]["asr"])
        print("TTS 后端:", c["tts"], "|", c["why"]["tts"])
        return 0
    if "--say" in args:
        idx = args.index("--say")
        text = args[idx + 1] if idx + 1 < len(args) else "你好"
        backend, path = say(text, play=True)
        print("backend=%s path=%s" % (backend, path))
        if backend != "none":
            time.sleep(1.0)   # 给播放留点时间再清理
        _cleanup_tmp(keep=path if KEEP_TMP else None)
        return 0
    if "--listen" in args:
        print("请开始说话……")
        text, wav_path, info = listen()
        print("识别结果:", repr(text))
        print("信息:", info)
        _cleanup_tmp()
        return 0
    print(__doc__.strip().splitlines()[0])
    print("用法：voice.py --selftest | --caps | --say \"文字\" | --listen")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
