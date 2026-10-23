"""语音链路端到端自测：说一句 → 听回来 → 答一句 → 念出来。

不用麦克风也能跑（--file 指定现成 wav）；有麦克风时 --mic 真录。

## 为什么要有这个

语音链路的坑几乎都不在单点上，而在**接缝**：

* 合成的音频能不能被自己识别回来（TTS→ASR 闭环）——验音色是否可懂；
* 识别出来的文本繁简、标点、错字能不能喂给模型（脏输入验证）；
* 念之前有没有把「（尾鳍一摆）」这类动作描写剥掉（否则像机器报菜名）；
* 每段的耗时能不能接受（人说话不该等 30 秒）。

跑一遍就知道整条线通没通、慢在哪。

用法：
    python voice_selftest.py                      # 合成->识别->回答->合成
    python voice_selftest.py --mic                # 真录一段再说
    python voice_selftest.py --card whale_girl --tts kokoro
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice                       # noqa: E402

try:
    import dialogue                # noqa: E402
except Exception:
    dialogue = None


DEMO_LINE = "（尾鳍轻轻一摆）嗯——鲸鱼娘在呢，主人。今天想吃米饭。"


def step(name, fn):
    t0 = time.time()
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, "%s: %s" % (type(e).__name__, e)
    dt = time.time() - t0
    print("  [%s] %-14s %5.1fs  %s" % ("PASS" if ok else "FAIL", name, dt, detail))
    return ok, dt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="whale_girl")
    ap.add_argument("--mic", action="store_true", help="用麦克风真录")
    ap.add_argument("--file", default="", help="用现成 wav")
    ap.add_argument("--tts", default="", choices=["", "kokoro", "edge"])
    ap.add_argument("--say-only", action="store_true", help="只测合成，不跑识别")
    args = ap.parse_args()

    print("=" * 64)
    print("语音链路端到端自测")
    caps = voice.caps()
    print("  能力：asr=%s  tts=%s" % (caps["asr"], caps["tts"]))
    for k, v in caps.get("why", {}).items():
        print("    %s: %s" % (k, v))
    print("=" * 64)

    results = []
    line = DEMO_LINE
    wav = None

    # ---- 1) 合成（TTS）----
    if args.tts == "edge":
        def _tts():
            r = voice._say_edge(line, rate=4, play=False, pitch="+4Hz")
            return bool(r), (r[0] + " " + r[1]) if r else "无输出"
    elif args.tts == "kokoro":
        def _tts():
            r = voice._say_kokoro(line, voice="zf_xiaobei", play=False)
            return bool(r), (r[0] + " " + r[1]) if r else "无输出"
    else:
        def _tts():
            text = voice.strip_actions(line)
            r = voice.say(text, play=False)
            return bool(r[1]), (r[0] + " -> " + str(r[1] or "")) if r else "无输出"

    ok, _dt = step("合成(说)", _tts)
    results.append(ok)
    if ok:
        # 拿最新产物做识别输入
        d = voice.TMP_DIR
        try:
            cands = [os.path.join(d, f) for f in os.listdir(d)
                     if f.lower().endswith((".wav", ".mp3"))]
            wav = max(cands, key=os.path.getmtime) if cands else None
        except Exception:
            wav = None

    # ---- 2) 动作剥离（念之前必须做，否则像机器报菜名）----
    def _strip():
        out = voice.strip_actions(line)
        same = "尾鳍" not in out
        return same, "剥后：%s" % out
    ok, _dt = step("动作剥离", _strip)
    results.append(ok)

    if args.say_only:
        _summary(results)
        return

    # ---- 3) 识别（ASR）----
    src = args.file or wav
    if args.mic:
        def _rec():
            path, info = voice.record(max_s=10)
            return path is not None, "录音 %s 秒（%s）" % (
                info.get("duration"), info.get("reason"))
        ok, _dt = step("录音", _rec)
        results.append(ok)
        if ok:
            d = voice.TMP_DIR
            cands = [os.path.join(d, f) for f in os.listdir(d)
                     if f.lower().endswith(".wav")]
            src = max(cands, key=os.path.getmtime) if cands else None

    heard = ""
    if src and os.path.exists(src):
        def _asr():
            nonlocal heard
            text, meta = voice.transcribe(src, language="zh")
            heard = text
            if not text:
                return False, "空结果 %s" % (meta.get("error") or "")[:80]
            return True, "听到：%s" % text
        ok, _dt = step("识别(听)", _asr)
        results.append(ok)
    else:
        print("  [SKIP] 识别         没有可用的音频输入")
        results.append(False)

    # ---- 4) 回答（脑）----
    if dialogue is not None and heard:
        def _reply():
            res = dialogue.reply(heard, card_id=args.card, speak=False)
            say = res.get("say") or ""
            return bool(say), "[%s] %s" % (res.get("backend"), say[:90])
        ok, _dt = step("回答(脑)", _reply)
        results.append(ok)

    _summary(results)


def _summary(results):
    n, t = sum(1 for x in results if x), len(results)
    print("=" * 64)
    print("结果：%d/%d 项通过" % (n, t))
    if n < t:
        print("  没过的项看上面的 FAIL 行；语音链路的坑通常在依赖或网络。")
    print("=" * 64)


if __name__ == "__main__":
    main()
