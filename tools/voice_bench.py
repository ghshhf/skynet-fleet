# 语音编排实测：整段念 vs 边合边播，以及打断的停止延迟。
#
# 前提（必须在报告里说清）：**这台开发机没有音频输出设备**——
#   winsound.PlaySound -> RuntimeError: Failed to play sound
#   sounddevice.query_devices(kind="output") -> Error querying device -1
# 所以这里测的是「从开始到第一块音频就绪」和「打断后还会不会继续往下走」，
# **不包括**「声音真的从音箱里出来」和「用户耳朵听到的延迟」。
# 那 10 厘米只能在有音箱的机器上验，不能在这台机器上假装测过。
#
# 测法：注入一个假播放器（每块"播" 0.25 秒的等待），
# 这样时序和真实播放一致，但没有声卡也能跑完。
import os
import sys
import threading
import time

sys.path.insert(0, r"D:/code/device-fleet")
os.environ.setdefault("FLEET_TTS_DIR", r"D:/tmp/tts")

import voice
import voice_sherpa
import voice_stream

TEXT = ("现在是下午两点十一分，屋里温度二十六度，湿度百分之四十五。"
        "再过三天就是立冬了，记得把厚被子拿出来晒一晒。")


class FakePlayer:
    """假装播一块要 0.25 秒。只实现 Speech 需要的三件事：available / play_blocking / interrupt。"""

    available = True

    def __init__(self, per_chunk=0.25):
        self.per_chunk = per_chunk
        self.played = []          # 播完的块
        self.stop_latency_ms = None
        self._int = False

    def play_blocking(self, path):
        if self._int:
            return False
        t0 = time.time()
        step = 0.01
        n = int(self.per_chunk / step)
        for _ in range(n):
            if self._int:
                self.stop_latency_ms = round((time.time() - t0) * 1000, 1)
                return False
            time.sleep(step)
        self.played.append(path)
        return True

    def interrupt(self):
        self._int = True

    @property
    def interrupted(self):
        return self._int

    def reset(self):
        self._int = False


def warm():
    t0 = time.time()
    voice_sherpa.engine()
    return round((time.time() - t0) * 1000, 1)


def bench_whole():
    """旧路径：say() 整段合成。首音 = 整段合成耗时（因为没有流式，合成完才有声）。"""
    t0 = time.time()
    backend, path = voice.say(TEXT, play=False)
    el = (time.time() - t0) * 1000
    dur = 0.0
    try:
        dur = voice._wav_duration(path) if path else 0.0
    except Exception:
        pass
    return {"backend": backend, "first_ms": round(el, 1), "audio_s": round(dur, 2)}


def bench_stream(interrupt_after=None):
    """新路径：voice_stream.Speech 边合边播。首音 = 第一块就绪并被送去播放的时间。

    interrupt_after 给的是「第一块开始播之后多久叫停」——
    不能从 run() 开始计时：第一块合成本身要 300ms 以上，
    从 run 计时会在合成期间就打断，测到的是「合成期取消」，不是「播放期打断」。
    """
    pl = FakePlayer(per_chunk=0.25)

    def synth(t):
        b, p = voice.say(t, play=False, cleanup=False)
        return p

    sp = voice_stream.Speech(synth=synth, player=pl, mic=None)

    if interrupt_after:
        def on_first(_ms):
            def killer():
                time.sleep(interrupt_after)
                pl.interrupt()
            threading.Thread(target=killer, daemon=True).start()
        r = sp.run(TEXT, interruptible=True, cleanup=False, on_first_audio=on_first)
    else:
        r = sp.run(TEXT, interruptible=False, cleanup=False)

    r["played_chunks"] = len(pl.played)
    r["stop_latency_ms"] = pl.stop_latency_ms
    return r


if __name__ == "__main__":
    print("=== 0 前置事实 ===")
    try:
        import winsound
        probe = os.path.join(voice.TMP_DIR, "probe.wav")
        voice._write_wav(probe, b"\x00\x00" * 1600, samplerate=16000)
        winsound.PlaySound(probe, winsound.SND_FILENAME)
        print("  本机有声卡：是")
        has_audio = True
    except Exception as e:
        print("  本机有声卡：**否**（%s）——下面所有「首音」都是"
              "「音频就绪」时间，不是「耳朵听到」时间" % str(e)[:60])
        has_audio = False

    print("\n=== 1 预热 ===")
    print("  sherpa 引擎加载 %s ms | 采样率 %s | 音色 %s 个" % (
        warm(), voice_sherpa.model_info().get("sample_rate"), voice_sherpa.voices()))

    print("\n=== 2 文本规范化在不在 ===")
    raw = "湿度 45%，2026年10月22日 14:30，¥1,200"
    print("  原文：%s" % raw)
    print("  送 TTS：%s" % voice._norm_zh(raw))

    print("\n=== 3 A 整段念（旧路径）===")
    a = bench_whole()
    print("  backend=%s | 首音 %s ms | 音频 %s s | 文本 %d 字" % (
        a["backend"], a["first_ms"], a["audio_s"], len(TEXT)))

    print("\n=== 4 B 边合边播（新路径）===")
    b = bench_stream()
    print("  块数 %d | 播完 %d 块 | 首音 %s ms | 总耗时 %s ms" % (
        b["chunks"], b["played_chunks"], b["first_audio_ms"], b["total_ms"]))
    for i, c in enumerate(b["texts"], 1):
        print("     块%d %2d 字：%s" % (i, len(c), c))
    if b.get("errors"):
        print("  错误：%s" % b["errors"])
    if a["first_ms"] and b["first_audio_ms"]:
        print("  首音提速：**%.1f 倍**（%.0f -> %.0f ms）" % (
            a["first_ms"] / b["first_audio_ms"], a["first_ms"], b["first_audio_ms"]))

    print("\n=== 5 C 打断 ===")
    c = bench_stream(interrupt_after=0.12)
    print("  块数 %d | 打断前播完 %d 块 | 停止延迟 %s ms | interrupted=%s" % (
        c["chunks"], c["played_chunks"], c["stop_latency_ms"], c["interrupted"]))
    ok = c["interrupted"] and c["played_chunks"] < c["chunks"]
    print("  判定：%s" % ("通过——后续块没有再播" if ok else "失败——打断后还在继续"))

    print("\n=== 6 D 麦克风 ===")
    try:
        import sounddevice as sd
        ins = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        print("  输入设备 %d 个%s" % (len(ins), "" if ins else "（没有麦克风，打断只能靠假播放器验编排）"))
    except Exception as e:
        print("  sounddevice 查不到设备：%s" % str(e)[:70])

    print("\n=== 结论 ===")
    print("  首音：%s ms（整段 %s ms）" % (b["first_audio_ms"], a["first_ms"]))
    print("  打断：%s" % ("生效" if ok else "未生效"))
    print("  真出声：%s" % ("未验证（本机无声卡）" if not has_audio else "已试播"))
