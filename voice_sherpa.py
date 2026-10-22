"""sherpa-onnx 的那一半：离线 TTS / VAD / 说话人，一个运行时包住。

为什么单独一个文件：voice.py 里已经有 kokoro 和 edge 两条合成路，再往里塞
sherpa 会让那个 1400 行的文件更难读。更重要的是**边界不同**：
  voice.py     = 业务（谁在说、说什么、该不该念）
  voice_sherpa = 运行时（把文字变成声音、把声音切成话段）
换运行时不该动业务，所以隔开。

为什么选 sherpa-onnx 而不是别的：
30 个开源语音项目读完，跨项目的收敛是「端侧全家桶正在收敛到 sherpa-onnx」——
它一个 Apache-2.0 的库同时提供流式/离线 ASR、TTS、Silero 与 TEN 两套 VAD、
语音增强、关键词识别、说话人分离，还带 RK NPU 支持。
对我们是硬需求：Windows 主机 + 一部 Android 手机，装不了 Docker + GPU 那一套。

实测（本机 i5-12450H，纯核显）：
  piper huayan  22.05kHz  RTF 0.129  语速 5.54 字/秒  首音 211ms（首短尾长切块）
  icefall aishell3 8kHz   RTF 0.076  语速 3.70 字/秒  首音 175ms
huayan 音色与语速都好，自用首选；icefall 是「哪天要对外发布」时替换的合规备选
（双 Apache-2.0，代价是 8kHz 发闷 + 语速慢 1.5 倍）。

模型配置有个坑，踩过两次：
  · piper 模型（huayan）：传 data_dir=<dir>/espeak-ng-data，且 onnx 文件名不是 model.onnx
  · icefall 模型：传 lexicon + rule_fsts(number/date/phone.fst)，**不能传 data_dir**
    传错会报 "Error when reading tokens at Line sil 0. size: 3"（size 指符号长度，
    跟行号无关，很容易看错方向）
"""
import os
import time

_MODEL_CACHE = {}
_ENGINE_CACHE = {}

# huayan 的数据集许可未声明（上游 License: Unknown，且微调自非商用音色），
# 所以只在「自用」路径上默认启用；FLEET_TTS_MODEL 可换成 icefall 那个双 Apache-2.0 的。
DEFAULT_ORDER = ("huayan", "icefall")


def _cand_dirs():
    """模型目录候选，按优先级。

    环境变量最优先（联调时直接指到 D:/tmp/tts 就能跑），
    然后是用户可写目录（正式落盘位置），最后是几个老机器上的约定位置。
    只返回**真实存在**的目录，不硬造——上一版 whisper 目录硬写 E: 盘，
    拔盘后 ASR 全哑还看不出原因。
    """
    out = []
    env = os.environ.get("FLEET_TTS_DIR")
    if env:
        out.append(env)
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    out.append(os.path.join(local, "fleet", "tts"))
    out.append(os.path.join(os.path.expanduser("~"), ".cache", "fleet", "tts"))
    out.append("D:/models/tts")
    out.append("D:/tmp/tts")            # 联调用的临时位置，放最后
    return [d for d in out if d and os.path.isdir(d)]


def find_model(prefer=None):
    """找一个能用的 TTS 模型目录，返回 (dir, kind) 或 (None, None)。

    kind 决定配置方式：'icefall'（有 lexicon.txt）或 'piper'（有 espeak-ng-data）。
    """
    order = (prefer,) if prefer else DEFAULT_ORDER
    for base in _cand_dirs():
        for name in order:
            for sub in _match_subdirs(base, name):
                kind = _kind_of(sub)
                if kind:
                    return sub, kind
    return None, None


def _match_subdirs(base, name):
    """在 base 下找名字包含 name 的子目录；base 本身也试一次。"""
    hits = []
    try:
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d)
            if os.path.isdir(p) and name in d.lower():
                hits.append(p)
    except OSError:
        pass
    if name in os.path.basename(base).lower():
        hits.append(base)
    return hits


def _onnx_file(d):
    """模型文件。piper 的导出件名字随模型变（zh_CN-huayan-medium.onnx），
    icefall 固定叫 model.onnx——不能写死。"""
    fixed = os.path.join(d, "model.onnx")
    if os.path.isfile(fixed):
        return fixed
    best = None
    for n in os.listdir(d):
        if n.endswith(".onnx") and not n.endswith((".fst.onnx", "-vocoder.onnx")):
            p = os.path.join(d, n)
            if best is None or os.path.getsize(p) > os.path.getsize(best):
                best = p
    return best


def _kind_of(d):
    """判断这个目录该按哪种模型配置。返回 'icefall' / 'piper' / None。"""
    if not os.path.isdir(d):
        return None
    if not os.path.isfile(os.path.join(d, "tokens.txt")):
        return None
    if not _onnx_file(d):
        return None
    if os.path.isfile(os.path.join(d, "lexicon.txt")):
        return "icefall"
    if os.path.isdir(os.path.join(d, "espeak-ng-data")):
        return "piper"
    return None


def available():
    """能不能用：装了 sherpa_onnx 且找得到模型。返回 (bool, 原因)。"""
    try:
        import sherpa_onnx  # noqa: F401
    except Exception as e:
        return False, "未安装 sherpa_onnx（pip install sherpa-onnx）：%s" % e
    d, kind = find_model()
    if not d:
        return False, ("没找到 TTS 模型目录。放模型到 %s ，"
                       "或设 FLEET_TTS_DIR 指向它" %
                       os.path.join(os.environ.get("LOCALAPPDATA") or "~", "fleet", "tts"))
    return True, "可用：%s（%s 格式）" % (os.path.basename(d), kind)


def engine(prefer=None, num_threads=4):
    """懒加载 OfflineTts，进程内单例。

    为什么必须缓存：加载实测 3 秒（30MB 模型）。每句话都重加载，
    首音延迟就从 211ms 变成 3 秒，等于白做。
    """
    import sherpa_onnx

    d, kind = find_model(prefer)
    if not d:
        raise RuntimeError("没找到可用的 TTS 模型目录")
    key = (d, num_threads)
    if key in _ENGINE_CACHE:
        return _ENGINE_CACHE[key]

    onnx = _onnx_file(d)
    t0 = time.time()
    if kind == "icefall":
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=onnx,
            lexicon=os.path.join(d, "lexicon.txt"),
            tokens=os.path.join(d, "tokens.txt"))
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=vits, num_threads=num_threads, provider="cpu"),
            rule_fsts=",".join(
                os.path.join(d, f) for f in ("date.fst", "number.fst", "phone.fst")
                if os.path.isfile(os.path.join(d, f))),
            max_num_sentences=1)
    else:
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=onnx,
            tokens=os.path.join(d, "tokens.txt"),
            data_dir=os.path.join(d, "espeak-ng-data"))
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=vits, num_threads=num_threads, provider="cpu"),
            max_num_sentences=1)

    tts = sherpa_onnx.OfflineTts(cfg)
    _ENGINE_CACHE[key] = tts
    _MODEL_CACHE["load_ms"] = round((time.time() - t0) * 1000, 1)
    _MODEL_CACHE["dir"] = d
    _MODEL_CACHE["kind"] = kind
    _MODEL_CACHE["sample_rate"] = tts.sample_rate
    return tts


def synth(text, speed=1.0, sid=0, prefer=None):
    """合成一段文字，返回 (int16 bytes, sample_rate)。

    这里返回 bytes 而不是写文件，是为了让上层决定落哪、什么时候删——
    流式播放时每块 wav 的生命周期由播放器管，合成层不该操心。
    """
    import array

    tts = engine(prefer)
    a = tts.generate(text, sid=sid, speed=float(speed or 1.0))
    sr = tts.sample_rate
    if not getattr(a, "samples", None):
        return b"", sr
    pcm = array.array("h", [
        int(max(-32768, min(32767, x * 32767))) for x in a.samples
    ])
    return pcm.tobytes(), sr


def synth_to_wav(text, path, speed=1.0, sid=0, prefer=None):
    """合成并写成 16-bit 单声道 wav。返回 (path, sample_rate) 或 (None, 0)。"""
    import wave

    pcm, sr = synth(text, speed=speed, sid=sid, prefer=prefer)
    if not pcm:
        return None, sr
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return path, sr


def model_info():
    """报告当前用的模型（给 caps() / 诊断用）。"""
    d, kind = find_model()
    if not d:
        return {"found": False}
    got = dict(_MODEL_CACHE) if _MODEL_CACHE.get("dir") == d else {}
    return {
        "found": True, "dir": d, "kind": kind,
        "onnx": os.path.basename(_onnx_file(d) or ""),
        "loaded": bool(got), "sample_rate": got.get("sample_rate"),
        "load_ms": got.get("load_ms"),
    }


def voices(sid_probe=(0,)):
    """模型支持多少音色。icefall-aishell3 有 174 个，huayan 只有 1 个。

    判据是 speakers.txt 的行数；没有这个文件就是单音色。
    """
    d, _kind = find_model()
    if not d:
        return 0
    p = os.path.join(d, "speakers.txt")
    if not os.path.isfile(p):
        return 1
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 1


def _selftest():
    """跑一遍：找模型 → 合成两句 → 报首音与 RTF。"""
    ok, why = available()
    print("可用性：%s" % why)
    if not ok:
        return False

    tts = engine()
    info = model_info()
    print("模型：%s | %s | 采样率 %s | 加载 %sms | 音色 %d 个" % (
        info["onnx"], info["kind"], info["sample_rate"], info["load_ms"], voices()))

    short = "好的"
    long = "现在是下午两点十一分，屋里温度二十六度，湿度百分之四十五。再过三天就是立冬了。"
    bad = 0
    for label, txt in (("短句", short), ("长句", long)):
        t0 = time.time()
        pcm, sr = synth(txt)
        el = (time.time() - t0) * 1000
        dur = len(pcm) / 2.0 / sr if sr else 0
        rtf = el / (dur * 1000) if dur else 0
        print("  %s %2d 字：合成 %5.0fms | 音频 %.2fs | RTF %.3f | 语速 %.2f 字/秒" % (
            label, len(txt), el, dur, rtf, len(txt) / dur if dur else 0))
        if dur <= 0:
            bad += 1

    # 数字念没念对：靠音频时长能粗判（数字被吞会明显更短），
    # 但要真判对错得 ASR 回环，这里只报数字，不假装验证过。
    pcm_num, sr = synth("百分之四十五")
    pcm_dig, sr = synth("45%")
    print("  数字对照：中文 %d 字节 / 阿拉伯数字 %d 字节 （差 %d）" % (
        len(pcm_num), len(pcm_dig), abs(len(pcm_num) - len(pcm_dig))))
    return bad == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if _selftest() else 1)
    print(model_info())
