"""语音链路的跨进程驱动：**跑在 polaris 的解释器里**，fleet 解释器不碰 torch。

为什么要有这个文件：跨进程隔离 torch 依赖。设计当初的前提是「fleet 自带
解释器没有 torch」（在那台机器上成立）；实测 2026-10-24 本机
G:/Python312 已自带 torch 2.14.1+cpu + transformers + soundfile，前提
已不成立——但隔离仍然保留：fleet 主进程不碰 torch，语音链崩了不牵连界面。
解释器由 `fleet/gui/voice.py` 按候选制挑（环境变量 → 历史 venv → 当前
解释器）；本文件由被选中的那个执行，结果以 JSON 打到 stdout。

两个 whisper 坑在这里被**真正执行**（不是注释）：
  1. 简体 prompt（`以下为普通话。`）→ 不给它中文默认吐繁体；
  2. `max_new_tokens = 448 - len(prompt_ids) - 8` → 不减就炸
     `combined length exceeds max_target_positions 448`。

预算公式与 prompt 常量**不在这里另写一份**：从 `fleet.gui.voice` import
（同一份真值，两处各写一份必然会走偏）。

用法（由 fleet/gui/voice.py 调用，不手工跑）：
    <polaris_python> voice_runner.py hear <音频路径>
    <polaris_python> voice_runner.py say  <文字> [--engine auto|natural|local]
                                              [--voice 名字] [--rate N] [--no-play]
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 仓库根
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 预算与 prompt 走 fleet/gui/voice.py（唯一真值）；环境变量可覆盖，但
# 默认值就是那份真值——两处各写一份必然会走偏。模块目录同理：原默认钉在
# `D:/work/_local_ai/train_15b/...`（那台机器上写的，换机即失效），
# 2026-10-24 改为与 voice.py 共用同一份候选制真值。
from fleet.gui.voice import (MAX_TARGET_POSITIONS as _MAX_POS,  # noqa: E402
                             POLARIS_DIR,
                             PROMPT_TOKEN_RESERVE as _RESERVE,
                             PROMPT_ZH as _PROMPT_ZH,
                             max_new_tokens)
PROMPT_ZH = os.environ.get("FLEET_STT_PROMPT") or _PROMPT_ZH
MAX_TARGET_POSITIONS = int(os.environ.get("FLEET_STT_MAX_POS") or _MAX_POS)
PROMPT_TOKEN_RESERVE = int(os.environ.get("FLEET_STT_RESERVE") or _RESERVE)

if POLARIS_DIR and POLARIS_DIR not in sys.path:
    sys.path.insert(0, POLARIS_DIR)


def _hear(path):
    """语音 → 文字。返回 polaris hear() 那个形状的 dict。"""
    import listen
    import torch

    model, proc = listen.load()
    audio, sr = listen.load_audio(path)
    feat = proc(audio, sampling_rate=sr, return_tensors="pt").input_features

    # 坑一：简体中文 prompt
    prompt_ids = proc.get_prompt_ids(PROMPT_ZH, return_tensors="pt")
    # 坑二：预算必须减掉 prompt 长度，再减特殊位的余量
    budget = max_new_tokens(int(prompt_ids.shape[-1]))

    t0 = time.time()
    with torch.no_grad():
        try:
            ids = model.generate(feat, language="zh", task="transcribe",
                                 prompt_ids=prompt_ids, max_new_tokens=budget,
                                 num_beams=1)
        except (TypeError, ValueError):
            # transformers 版本差异：这版 generate 不吃 language/task 关键字，
            # 只能走 generation_config。这不是「静默兜底」——两条路都不通就炸。
            gc = model.generation_config
            gc.language = "zh"
            gc.task = "transcribe"
            gc.forced_decoder_ids = None
            ids = model.generate(feat, prompt_ids=prompt_ids,
                                 max_new_tokens=budget, num_beams=1)
    el = time.time() - t0
    text = proc.batch_decode(ids, skip_special_tokens=True)[0].strip()
    audio_sec = len(audio) / float(sr or 16000)
    return {"text": text, "chars": len(text.replace(" ", "")),
            "audio_sec": round(audio_sec, 2), "seconds": round(el, 2),
            "rtf": round(el / max(audio_sec, 0.01), 2),
            "prompt": PROMPT_ZH, "prompt_tokens": int(prompt_ids.shape[-1]),
            "max_new_tokens": budget,
            "max_target_positions": MAX_TARGET_POSITIONS,
            "reserve": PROMPT_TOKEN_RESERVE}


def _say(text, engine="auto", voice=None, rate=0, playback=True):
    """文字 → 语音。返回 polaris speak() 那个形状的 dict。"""
    import speak
    return speak.speak(text, engine=engine, voice=voice, rate=rate,
                       playback=playback)


def main(argv=None):
    ap = argparse.ArgumentParser(description="语音链路跨进程驱动")
    sub = ap.add_subparsers(dest="mode", required=True)
    h = sub.add_parser("hear", help="语音 → 文字")
    h.add_argument("path")
    s = sub.add_parser("say", help="文字 → 语音")
    s.add_argument("text")
    s.add_argument("--engine", default="auto", choices=["auto", "natural", "local"])
    s.add_argument("--voice", default=None)
    s.add_argument("--rate", type=int, default=0)
    s.add_argument("--no-play", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.mode == "hear":
            out = _hear(a.path)
        else:
            out = _say(a.text, engine=a.engine, voice=a.voice, rate=a.rate,
                       playback=not a.no_play)
    except Exception as e:
        # 失败必须带原因：把类型与消息带出去，fleet 侧据此给人话
        print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
