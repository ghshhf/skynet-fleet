# -*- coding: utf-8 -*-
"""音色银行：把「音色名 → 引擎/参考音色」固化，并支持批量预合成 + 按 id 取用。

为什么有这一层
--------------
`tts_runner` 管的是「一段文本怎么合成」（引擎、CLI、批量往返）。但漫剧真正要的是：
  · 同一个角色反复说不同的话 —— 每次都写 engine/ref/speed 太啰嗦；
  · 主人要的正是「语音可以一次性合成很多，然后直接调用相关的」—— 而不是每帧实时算。

voicebank 把常用音色登记成名字（旁白-女 / 新闻女 / 雷军…），并给每对
`(text, voice)` 一个稳定的缓存文件。合成过 → 命中缓存秒回；没合成过 → 现场补
（或显式 `prewarm` 整批预合成）。

缓存路径：`~/.fleet/voicebank/<voice>/<sha1>.wav`（sha1 只吃 text+voice+speed，
所以同一句同一音色永远命中同一文件，改一句台词就只重算那一句）。

用法
----
    from fleet import voicebank as vb
    vb.list_voices()                       # 看有哪些音色
    vb.prewarm("新闻女", [line1, line2, ...])   # 一次性把整批台词合成进缓存
    vb.take("新闻女", line1)              # 之后合成影片时直接按 id 取，秒回
    vb.register("老板", engine="zipv", ref="老板", ref_text="...", ...)  # 自己录音色
"""
import hashlib
import os

from fleet import tts_runner as tr

CACHE = os.path.join(os.path.expanduser("~/.fleet"), "voicebank")

# 内置音色：名字 → 引擎参数。克隆类（zipv）用 ref 指向 zipv 内置的参考音色。
# ref_text 必须和参考音频内容一致，zipv 靠它对齐韵律。
ZIPV_REFS = tr.ENGINES["zipv"]["refs"]
VOICE_REGISTRY = {
    "旁白-女": dict(engine="matcha", sid=0, speed=1.0),
    "男-中":   dict(engine="melo", sid=0, speed=1.0),
    "新闻女":  dict(engine="zipv", ref="news-female",
                  ref_text=ZIPV_REFS["news-female"], speed=1.0),
    "新闻女2": dict(engine="zipv", ref="news-female-2",
                  ref_text=ZIPV_REFS["news-female-2"], speed=1.0),
    "雷军":    dict(engine="zipv", ref="leijun-1",
                  ref_text=ZIPV_REFS["leijun-1"], speed=1.0),
    # 精品档：IndexTTS2 1.5B，手机 proot + torch CPU。单条 RTF~33（141s 出 4.3s），
    # 所以**只适合先 prewarm 整批、再 take 秒回**；直接 take 未命中的句子要等两分多钟。
    "精品-女":  dict(engine="indextts2", speed=1.0),
}


def register(name, engine="zipv", ref=None, ref_text=None, speed=1.0, sid=0):
    """登记一个自定义音色（最有用的是「自己录 3 秒人声当角色」）。

    zipv 必须给 ref + ref_text（ref 是手机上参考音频的基名，要先进 zipv 目录；
    或者先 record 一段、推到 /data/local/tmp/zipv/test_wavs/）。
    """
    if engine == "zipv" and (not ref or not ref_text):
        raise ValueError("zipv 音色必须有 ref（参考音频基名）+ ref_text（音频里念的文字）")
    VOICE_REGISTRY[name] = dict(engine=engine, ref=ref, ref_text=ref_text,
                                speed=speed, sid=sid)
    return VOICE_REGISTRY[name]


def list_voices():
    """返回 {名字: {engine, ref, speed}}。"""
    return {k: {kk: vv for kk, vv in v.items()} for k, v in VOICE_REGISTRY.items()}


def _cache_path(voice, text, speed):
    h = hashlib.sha1(("%s|%s|%s" % (voice, text, speed)).encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE, voice, h + ".wav")


def _backend_for(spec):
    """挑后端：手机 proot > 手机 sherpa > PC。

    proot 引擎（indextts2）和 sherpa 是两条完全不同的路，不能混着 fallback：
    手机没部署时降到 PC 也没用（PC 上没这套 torch 环境），所以单独标一个
    backend 值，调用方按它分流，别让它悄悄掉进 `_pc_synth` 报一个看不懂的错。
    """
    e = tr.ENGINES.get(spec["engine"], {})
    ok, _ = tr.phone_ready(spec["engine"])
    if e.get("kind") == "proot":
        return "phone_proot" if ok else "unavailable"
    return "phone_sherpa" if ok else "pc_sherpa"


def _need_phone(backend, voice):
    if backend == "unavailable":
        raise RuntimeError(
            "音色 %r 用的引擎在手机上没就绪（proot 环境不全），"
            "先跑 tts_runner.deploy / 检查 _contrib/proot/setup_tts.sh" % voice)


def take(voice, text, speed=None, backend=None):
    """按 id 取一条语音：命中缓存秒回，没命中就现场补一条。返回 wav 路径。"""
    spec = VOICE_REGISTRY.get(voice)
    if not spec:
        raise KeyError("未知音色 %r（可选: %s）" % (voice, ", ".join(VOICE_REGISTRY)))
    speed = speed or spec.get("speed", 1.0)
    cp = _cache_path(voice, text, speed)
    if os.path.isfile(cp) and os.path.getsize(cp) >= 1024:
        return cp
    backend = backend or _backend_for(spec)
    _need_phone(backend, voice)
    if backend in ("phone_sherpa", "phone_proot"):
        p, _, _ = tr.phone_synth(text, cp, engine=spec["engine"], sid=spec.get("sid", 0),
                                 speed=speed, ref=spec.get("ref"), ref_text=spec.get("ref_text"))
    else:
        p, _, _ = tr._pc_synth(text, cp, engine=spec["engine"], sid=spec.get("sid", 0),
                               speed=speed, ref=spec.get("ref"), ref_text=spec.get("ref_text"))
    return p


def prewarm(voice, texts, speed=None, backend=None):
    """一次性把整批台词预合成进缓存。返回 {text: wav路径 或 None（失败）}。

    手机端走 `phone_batch`：N 条台词一次推、一次跑、一次拉回，把 adb 往返压到 1 次；
    PC 端顺序合成（调试用）。缓存命中过的台词直接跳过，所以可反复调用、只补新增。
    """
    spec = VOICE_REGISTRY.get(voice)
    if not spec:
        raise KeyError("未知音色 %r（可选: %s）" % (voice, ", ".join(VOICE_REGISTRY)))
    speed = speed or spec.get("speed", 1.0)
    backend = backend or _backend_for(spec)
    _need_phone(backend, voice)

    pending = []  # (text, cache_path)
    result = {}
    for t in texts:
        cp = _cache_path(voice, t, speed)
        if os.path.isfile(cp) and os.path.getsize(cp) >= 1024:
            result[t] = cp  # 已缓存，跳过
        else:
            pending.append((t, cp))

    if not pending:
        return result

    if backend in ("phone_sherpa", "phone_proot"):
        items = [(t, cp, spec.get("sid"), speed, spec.get("ref"), spec.get("ref_text"))
                 for t, cp in pending]
        res = tr.phone_batch(items, engine=spec["engine"], threads=4)
        for (t, cp), (ok, path, _, err) in zip(pending, res):
            if ok and path and os.path.isfile(path):
                result[t] = path
                if err:
                    pass  # 兼容旧返回结构
            else:
                result[t] = path if path else None
    else:
        for t, cp in pending:
            try:
                p, _, _ = tr._pc_synth(t, cp, engine=spec["engine"], sid=spec.get("sid", 0),
                                       speed=speed, ref=spec.get("ref"),
                                       ref_text=spec.get("ref_text"))
                result[t] = p
            except Exception:
                result[t] = None
    return result


def cache_report(voice=None):
    """缓存占用速览。方便确认「一次性合成很多」到底落了多少。"""
    import glob
    if voice:
        d = os.path.join(CACHE, voice)
        fs = glob.glob(os.path.join(d, "*.wav"))
        return {"voice": voice, "count": len(fs),
                "size_mb": round(sum(os.path.getsize(f) for f in fs) / 1e6, 2)}
    return {v: cache_report(v) for v in VOICE_REGISTRY}


if __name__ == "__main__":
    import json
    print(json.dumps(list_voices(), ensure_ascii=False, indent=2))
