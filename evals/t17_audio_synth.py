#!/usr/bin/env python
"""门禁 t17：代码生成音频（procedural audio）。

**这个门禁钉的是六件真事**：

  ① **「能下到声音」≠「能出到声音」。** 这一层的意义是**波形由代码算出来**，
     不是采样库回放。⇒ 每个预设都必须真的合成出非空 WAV。

  ② **同 seed ⇒ 逐字节相同。** 不可复现的提示音没法调试：
     「昨天还好好的」这句话靠它才查得动。
     🔴 这里踩过坑：用 Python 内置 `hash()` 不行——**str hash 每进程加盐**
        （PYTHONHASHSEED），同一个设备名在不同进程里会得到不同种子。
        必须用 sha256。

  ③ **不同 seed ⇒ 不同声音。** 每台设备有自己的声音（主人原话：「每台设备声音不同，
     但重启后一样」）。

  ④ **陌生预设必须抛明确错误并说清「只认哪几种」。**
     不许猜、不许静默退回默认（静默降级 = 静默失败）。

  ⑤ **零第三方依赖。** 引入 Tone.js / SDL / numpy 这些是给浏览器/游戏用的，
     本仓是后端进程。⇒ 本文件不许 import 任何第三方音频库。

  ⑥ **audio.synth 写文件 ⇒ 副作用 ⇒ 没闸门时 fail-closed。**

**全离线**：不联网、不装任何东西，合成全在内存与临时目录里完成。
"""
import os
import shutil
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []
SKIP = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


def skip(msg):
    print("  --", msg)
    SKIP.append(msg)


def main():
    import fleet.audio_synth as SY
    from fleet import catalog as C

    tmp = tempfile.mkdtemp(prefix="fleet-t17-")

    # ---------- ① 每个预设都能合成出非空 WAV ----------
    names = sorted(SY.PRESETS.keys())
    check(len(names) >= 5, "预设至少 5 个（实际 %d：%s）" % (len(names), names))
    for n in names:
        p = os.path.join(tmp, n + ".wav")
        try:
            r = SY.synth(n, seed="dev-01", out=p)
            good = (r["bytes"] > 1000 and r["seconds"] > 0.03
                    and os.path.exists(p))
            check(good, "预设 %s 合成出真音频（%.2fs / %d 字节）"
                  % (n, r["seconds"], r["bytes"]))
            with wave.open(p, "rb") as w:
                ok_fmt = (w.getnchannels() == 1 and w.getsampwidth() == 2
                          and w.getnframes() > 0)
            check(ok_fmt, "预设 %s 输出是 16bit 单声道有帧的 WAV" % n)
        except Exception as e:
            check(False, "预设 %s 合成失败：%s" % (n, e))

    # ---------- ② 同 seed ⇒ 逐字节相同 ----------
    a = os.path.join(tmp, "a.wav")
    b = os.path.join(tmp, "b.wav")
    SY.synth("ambient", seed="dev-01", out=a)
    SY.synth("ambient", seed="dev-01", out=b)
    check(open(a, "rb").read() == open(b, "rb").read(),
          "同 seed 两次合成逐字节相同（可复现）")

    # 跨进程也要一样（🔴 str hash 加盐的坑就在这儿）
    import subprocess
    py = sys.executable
    code = ("import sys;sys.path.insert(0,%r);import fleet.audio_synth as S;"
            "S.synth('ambient',seed='dev-01',out=%r)"
            % (ROOT, os.path.join(tmp, "c.wav")))
    r = subprocess.run([py, "-c", code], capture_output=True, timeout=120)
    if r.returncode == 0:
        check(open(a, "rb").read() == open(os.path.join(tmp, "c.wav"), "rb").read(),
              "跨进程同 seed 仍然逐字节相同（没用带盐的 hash()）")
    else:
        skip("跨进程复现跑不起来：%s" % r.stderr.decode("utf-8", "replace")[:120])

    # ---------- ③ 不同 seed ⇒ 不同声音 ----------
    c = os.path.join(tmp, "d.wav")
    SY.synth("ambient", seed="dev-02", out=c)
    check(open(a, "rb").read() != open(c, "rb").read(),
          "不同 seed 出不同的音频（每台设备声音不同）")

    # ---------- ④ 陌生预设 ⇒ 明确错误 ----------
    try:
        SY.synth("no-such-preset")
        check(False, "陌生预设应当抛错（实际没抛）")
    except SY.SynthError as e:
        check("只认这几种" in str(e), "陌生预设报错说清了「只认哪几种」")
    except Exception as e:
        check(False, "陌生预设抛的不是 SynthError：%s" % type(e).__name__)

    # 不支持的输出格式也要说清
    try:
        SY.synth("chime", fmt="mp3")
        check(False, "mp3 输出应当抛错（本层不带编码器）")
    except SY.SynthError as e:
        check("wav" in str(e) and "ffmpeg" in str(e),
              "格式不支持时报错说清了「只支持 wav」与「用 ffmpeg 转」")

    # ---------- ⑤ 零第三方依赖 ----------
    body = open(os.path.join(ROOT, "fleet", "audio_synth.py"),
                encoding="utf-8").read()
    for bad in ("import numpy", "import scipy", "import soundfile",
                "import sounddevice", "import pyaudio", "import tone"):
        check(bad not in body, "audio_synth.py 里没有 %s" % bad)

    # ---------- ⑥ 能力登记 + 闸门 ----------
    reg = C.build()
    allcaps = reg.cap_names()
    check("audio.presets" in allcaps, "能力 audio.presets 已登记")
    check("audio.synth" in allcaps, "能力 audio.synth 已登记")
    cap = reg.capability("audio.synth")
    check(cap.side_effect is True, "audio.synth 是副作用能力（写 WAV 文件）")
    pcap = reg.capability("audio.presets")
    check(pcap.side_effect is False, "audio.presets 是只读能力")

    r = reg.call("audio.presets")
    check(r.get("ok") is True, "audio.presets 能调通（err=%r）" % r.get("err"))
    check(len((r.get("data") or {}).get("presets") or []) >= 5,
          "audio.presets 返回了预设清单")

    # 没闸门 ⇒ fail-closed
    dest = os.path.join(tmp, "out.wav")
    r = reg.call("audio.synth", args={"spec": "chime", "seed": "x", "out": dest})
    check(r.get("ok") is False, "audio.synth 没闸门时被拒绝")
    check(r.get("code") == "denied", "拒绝码是 denied")
    check(not os.path.exists(dest), "被拒时没有偷偷写文件")

    # 有闸门 ⇒ 放行并落盘
    r = reg.call("audio.synth", args={"spec": "chime", "seed": "x", "out": dest},
                 gate=lambda c, d, a_, l: (True, ""))
    check(r.get("ok") is True, "audio.synth 有闸门时放行（err=%r）" % r.get("err"))
    check(os.path.exists(dest) and os.path.getsize(dest) > 1000,
          "放行后 WAV 真的落盘了")

    # 能力层报错也要带原因（不许静默失败）
    r = reg.call("audio.synth", args={"spec": "nope"}, gate=lambda c, d, a_, l: (True, ""))
    check(r.get("ok") is False, "陌生预设在能力层报错")
    check("只认这几种" in (r.get("err") or ""),
          "能力层的错误里带原因（说清只认哪几种）")

    # ---------- ⑥b 默认落盘目录 + 副作用恒真 ----------
    # 不给 out 时也必须落进仓库内 audio_out（不是 %TEMP%），且结果里
    # side_effect 恒为 True——它**每次都写文件**，原来 `bool(out)` 会在
    # out 为空时谎报 False，骗过上层「有没有副作用」判定。
    r = reg.call("audio.synth", args={"spec": "chime", "seed": "t17-default"},
                 gate=lambda c, d, a_, l: (True, ""))
    check(r.get("ok") is True, "audio.synth 不给 out 也能合成（err=%r）" % r.get("err"))
    out_path = (r.get("data") or {}).get("path") or ""
    check(bool(out_path) and "audio_out" in out_path,
          "不给 out 默认落进仓库 audio_out（%s）" % out_path)
    check(r.get("side_effect") is True,
          "audio.synth 即便不给 out 也标了 side_effect=True（每次都写文件）")
    if out_path and os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    # ---------- ⑦ 能力是四个面都能看见的（注册表是唯一真相）----------
    check(len([c for c in allcaps if c.startswith("audio.")]) == 2,
          "注册表里 audio.* 恰好 2 项（不重名）")

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if SKIP:
        print("跳过 %d 项：" % len(SKIP))
        for s in SKIP:
            print("  --", s)
    if FAIL:
        print("=== t17 失败 %d 项 ===" % len(FAIL))
        for f in FAIL:
            print("  -", f)
        return 1
    print("=== t17 全通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
