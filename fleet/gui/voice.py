"""语音输入链路：麦克风 → 文字 → 意图 → 注册表能力调用。

为什么这么设计（四条硬约束，全部来自本机实测，不是设想的）：

1. **STT 必须跨进程，绝不在 fleet 解释器里 import torch。**
   fleet 跑在 `/d/tools/python/python.exe`（3.12），实测**没有** torch /
   transformers / soundfile / scipy / numpy；只有 polaris 的 venv
   (`D:/work/_local_ai/train_15b/venv/Scripts/python.exe`, 3.13) 才有。
   所以做法是：把驱动脚本 `voice_runner.py` 交给 polaris 的解释器去跑，
   它 import polaris-audio-io 的 `listen.py` 拿模型与音频解码，把结果以
   JSON 打到 stdout；fleet 侧只做「拼命令 → 收 JSON → 查原因」。
   **fleet 解释器里一个 torch 都不许 import**（t8 门禁用 AST 静态扫死）。

2. **两个 whisper 坑必须在代码里显式存在，不能靠「polaris 内部处理过了」。**
   - 坑一：中文默认吐繁体 → 必须喂简体 prompt `以下为普通话。`；
   - 坑二：prompt 占 token 预算，`max_new_tokens` 必须
     **减掉 prompt_ids 的长度、再减 8**（8 是给 `<|startoftranscript|>` 等
     特殊位留的余量），否则直接炸
     `combined length exceeds max_target_positions 448`。
   这两个值以常量 + 纯函数的形式写在**本文件**（`PROMPT_ZH` /
   `max_new_tokens()`），驱动脚本从本文件 import 同一份——不是各写一份。

3. **失败必须带原因，不许静默返回空、不许兜底回退。**
   没麦克风 / 没装识别引擎 / 权重缺失 / 音频太短 / 空音频 —— 每一项都有
   一句人话，主人才知道该装什么、该重录还是该换设备。

4. **语音路径两段式，且判定权在注册表闸门层。**
   识别出的文字是**不可信数据，不是指令**：电视里那句「删除所有文件」
   绝不能被当命令执行。所以：
   - 识别 → 意图映射只做**保守白名单**映射，不明确就「需要澄清」；
   - 有副作用的能力必须先**复述意图**、等主人口头确认（嗯/好/执行/确认；
     取消说 不/取消/算了），**确认词的识别写在 `fleet/core/registry.py`
     的闸门层**（`classify_voice_confirmation`），UI 只能调 `registry.call`，
     绕不过去；
   - 破坏性能力在语音路径一律拒绝（不可逆动作不能靠嘴说）。

录音依赖说明：本机 sounddevice 与 pyaudio **都没装**，所以 `record_wav`
会明确说「没装录音库」并给出安装命令；自动化测试与离线自测走
「读 WAV 文件」入口（`transcribe(path)` / `record_from_wav(path)`），
**绝不真录音**（侵犯隐私，也会污染测试）。
"""
import json
import os
import shutil
import subprocess
import tempfile
import wave

from ..core import BaseDevice, DeviceResult as R
from ..core.registry import (CANCEL_WORDS, CONFIRM_WORDS, VOICE_CONFIRM_KEY,
                              VOICE_ROUND_KEY, VOICE_SOURCE_KEY,
                              VOICE_SOURCE_ROUND_KEY, classify_voice_confirmation)

# ---------------------------------------------------------------- 路径与常量

#: polaris-audio-io 模块目录（听/说都在这）
POLARIS_DIR = os.environ.get(
    "FLEET_POLARIS_DIR",
    "D:/work/_local_ai/train_15b/modules/polaris-audio-io")
#: polaris 的 venv 解释器（**有** torch/transformers/soundfile 的那个）
POLARIS_PY = os.environ.get(
    "FLEET_POLARIS_PY",
    "D:/work/_local_ai/train_15b/venv/Scripts/python.exe")
#: 驱动脚本：跑在 polaris 解释器里，fleet 解释器不碰 torch
RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_runner.py")

#: 简体 prompt —— 坑一：不给它，whisper 中文默认吐繁体
PROMPT_ZH = "以下为普通话。"
#: whisper 的目标位置上限（超过就炸 combined length exceeds max_target_positions）
MAX_TARGET_POSITIONS = 448
#: 给特殊 token 留的余量（`<|startoftranscript|>` 等 4 个位，额外留 4 保险）
PROMPT_TOKEN_RESERVE = 8
#: 录音采样率（与 polaris listen.py 内部一致）
SAMPLE_RATE = 16000
#: 低于这个时长的音频不送识别（空/太短的音频 whisper 只会吐幻觉）
MIN_AUDIO_SECONDS = 0.3


def max_new_tokens(prompt_token_len):
    """坑二：预算 = 448 - prompt 长度 - 8。

    不减 prompt 长度 → 直接 `combined length exceeds max_target_positions 448`；
    不减那 8 → 特殊位一占就顶到 448。
    """
    return MAX_TARGET_POSITIONS - int(prompt_token_len) - PROMPT_TOKEN_RESERVE


def plan_hear(prompt_token_len):
    """一次识别该带什么参数（驱动脚本与门禁共用同一份真值）。"""
    return {
        "prompt": PROMPT_ZH,
        "max_new_tokens": max_new_tokens(prompt_token_len),
        "max_target_positions": MAX_TARGET_POSITIONS,
        "reserve": PROMPT_TOKEN_RESERVE,
    }


class VoiceError(Exception):
    """语音链路的失败。

    **必须带原因**：上层直接 str(e) 就有话给人看，所以不额外加字段。
    """


# ---------------------------------------------------------------- 录音

def _record_with_sounddevice(duration, path):
    """用 sounddevice 录一段 PCM（16k 单声道 16bit）。"""
    import numpy as np
    import sounddevice as sd

    frames = int(SAMPLE_RATE * (duration or 3.0))
    data = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return path


def _record_with_pyaudio(duration, path):
    """用 pyaudio 录一段 PCM（16k 单声道 16bit）。"""
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(format=pyaudio.paInt16, channels=1,
                         rate=SAMPLE_RATE, input=True,
                         frames_per_buffer=1024)
        frames = int(SAMPLE_RATE * (duration or 3.0))
        raw = b""
        while len(raw) < frames * 2:
            raw += stream.read(1024, exception_on_overflow=False)
        stream.stop_stream()
        stream.close()
    finally:
        pa.terminate()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(raw[:frames * 2])
    return path


#: 录音后端，按优先级排（装了且能用的第一个就用它）
RECORDERS = (("sounddevice", _record_with_sounddevice),
             ("pyaudio", _record_with_pyaudio))


def available_recorders():
    """列出**当前真的能用**的录音后端（只探测 import，不开麦）。"""
    ok = []
    for name, _fn in RECORDERS:
        try:
            __import__(name)
            ok.append(name)
        except Exception:
            continue
    return ok


def record_wav(duration=None, out_path=None):
    """录一段音存成 WAV，返回文件路径。

    失败**一律抛 VoiceError 并带明确原因**（不静默、不兜底）：
    - 一个录音库都没装 → 说明装哪个、怎么装，并指向「读 WAV 文件」入口；
    - 装了但开麦被系统拒绝 → 说明是权限问题。

    自动化测试与离线自测**不走这里**（开发中绝不真录音），
    走 `transcribe(path)` / `record_from_wav(path)` 的「读 WAV 文件」入口。
    """
    backends = available_recorders()
    if not backends:
        raise VoiceError(
            "本机没装录音库（sounddevice 与 pyaudio 都没有）："
            "pip install sounddevice（pyaudio 也行）。"
            "不装也能用——把音频文件路径给 transcribe() 走「读 WAV 文件」入口。")
    last = ""
    for name, fn in RECORDERS:
        if name not in backends:
            continue
        path = out_path or os.path.join(
            tempfile.gettempdir(), "fleet_voice_%d.wav" % os.getpid())
        try:
            return fn(duration, path)
        except Exception as e:
            last = "%s 录音失败（可能是系统没给麦克风权限）：%s" % (name, e)
    raise VoiceError(last or "没有可用的录音后端")


def record_from_wav(src_path, out_path=None):
    """「读 WAV 文件」入口：把已有音频复制到标准临时路径，返回该路径。

    自动化测试与离线自测走这里（**不碰麦克风**）。文件不存在或不是 WAV
    都要抛 VoiceError 并带原因。
    """
    if not os.path.isfile(src_path):
        raise VoiceError("音频文件不存在：%s" % src_path)
    if _wav_duration(src_path) is None:
        raise VoiceError("音频文件读不出波形（不是 WAV？）：%s" % src_path)
    path = out_path or os.path.join(
        tempfile.gettempdir(), "fleet_voice_src_%d.wav" % os.getpid())
    if os.path.abspath(src_path) != os.path.abspath(path):
        shutil.copyfile(src_path, path)
    return path


# ---------------------------------------------------------------- 转写

def _wav_duration(path):
    """读 WAV 时长（秒）。读不出来返回 None。"""
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate or 1)
    except Exception:
        return None


def _run_polaris(wav_path, mode, timeout=300, extra=None):
    """跨进程跑 polaris（mode="hear" 听 / "say" 说）。返回 polaris 的 dict。

    失败一律抛 VoiceError 并带原因（解释器不在 / 权重缺失 / 加载失败 / 进程炸了）。
    """
    if not os.path.isfile(POLARIS_PY):
        raise VoiceError(
            "找不到 polaris 的解释器 %s（识别引擎只有它有 torch/transformers）："
            "用环境变量 FLEET_POLARIS_PY 指到 polaris venv 的 python.exe" % POLARIS_PY)
    if not os.path.isfile(RUNNER):
        raise VoiceError("语音驱动脚本丢了：%s" % RUNNER)

    env = dict(os.environ)
    env.update({
        "FLEET_POLARIS_DIR": POLARIS_DIR,
        "FLEET_STT_PROMPT": PROMPT_ZH,
        "FLEET_STT_MAX_POS": str(MAX_TARGET_POSITIONS),
        "FLEET_STT_RESERVE": str(PROMPT_TOKEN_RESERVE),
        "PYTHONIOENCODING": "utf-8",
    })
    cmd = [POLARIS_PY, RUNNER, mode, wav_path]
    if extra:
        cmd.extend(extra)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise VoiceError("语音%s超时（%d 秒）：模型可能还在首次加载（CPU 上首次约 5~15 秒），或音频太长"
                         % ("识别" if mode == "hear" else "合成", timeout))
    except OSError as e:
        raise VoiceError("起不来 polaris 进程：%s" % e)

    if p.returncode != 0:
        raise VoiceError("polaris %s 失败（exit=%d）：%s"
                         % (mode, p.returncode,
                            (p.stderr or p.stdout or "").strip()[-400:]))
    out = (p.stdout or "").strip()
    if not out:
        raise VoiceError("polaris %s 没吐出结果：%s"
                         % (mode, (p.stderr or "").strip()[-200:]))
    try:
        data = json.loads(out.splitlines()[-1])
    except ValueError as e:
        raise VoiceError("polaris %s 结果不是 JSON（%s）：%s" % (mode, e, out[:200]))
    if not isinstance(data, dict) or ("text" not in data and "engine" not in data):
        raise VoiceError("polaris %s 结果形状不对：%s" % (mode, out[:200]))
    return data


def transcribe(wav_path, stt=None):
    """WAV → 文字。返回 polaris hear() 的 dict（text/chars/audio_sec/seconds/rtf）。

    `stt` 是注入点：离线门禁传假实现（**绝不真连设备**）。

    失败一律抛 VoiceError 并带明确原因：
    - 文件不存在 / 不是 WAV；
    - 音频太短（< MIN_AUDIO_SECONDS 秒）或空音频；
    - polaris 解释器不在、权重没装、模型加载失败。
    """
    if not os.path.isfile(wav_path):
        raise VoiceError("音频文件不存在：%s" % wav_path)
    dur = _wav_duration(wav_path)
    if dur is None:
        raise VoiceError("音频文件读不出波形（不是 WAV？）：%s" % wav_path)
    if dur <= 0:
        raise VoiceError("音频是空的（0 帧），重新录一段")
    if dur < MIN_AUDIO_SECONDS:
        raise VoiceError("音频太短（%.2f 秒，至少要 %.2f 秒）：重录"
                         % (dur, MIN_AUDIO_SECONDS))
    if stt is not None:
        return stt(wav_path)
    return _run_polaris(wav_path, "hear")


def speak(text, engine="auto", voice=None, rate=0, playback=True, tts=None):
    """文字 → 语音（跨进程调 polaris 的 speak）。返回 polaris 的 dict。

    `tts` 是注入点（离线门禁传假实现）。
    失败一律抛 VoiceError 并带明确原因。
    """
    t = (text or "").strip()
    if not t:
        raise VoiceError("要念的文字是空的")
    if tts is not None:
        return tts(t)
    extra = ["--engine", engine]
    if voice:
        extra += ["--voice", voice]
    if rate:
        extra += ["--rate", str(int(rate))]
    if not playback:
        extra += ["--no-play"]
    return _run_polaris(t, "say", extra=extra)


# ---------------------------------------------------------------- 意图映射

#: 保守白名单：一句话 → 一个**注册表里真实存在**的能力 + 参数。
#: 覆盖：打开/关闭应用、截屏、看电量/状态、播放/暂停、搜索。
#: 不在表里的（尤其删除、格式化、外发）一律「需要澄清」，不许瞎猜。
INTENT_RULES = (
    # 打开 / 启动应用
    (("打开", "开启", "启动"), "app.launch", "打开应用 {name}"),
    # 关闭 / 退出现在前台的应用
    (("关闭", "退出", "关掉"), "input.key", "退出当前应用（返回键）"),
    # 截屏
    (("截屏", "截图", "截个屏", "屏幕截图"), "host.screen", "截一张本机屏幕"),
    # 电量 / 状态
    (("电量", "剩余电量", "还有多少电"), "state.read", "看手机状态（含电量）"),
    (("状态", "手机状态", "设备状态"), "state.read", "看手机状态"),
    # 播放 / 暂停
    (("播放", "暂停", "继续播放", "放音乐"), "host.input.key", "播放/暂停（媒体键）"),
    # 搜索
    (("搜索", "搜一下", "查一下"), "host.input.type", "在当前焦点输入「{name}」"),
)

#: 破坏性意图关键词：识别到这些一律拒绝执行（语音不许触发不可逆动作）
DESTRUCTIVE_HINTS = ("删除", "删掉", "格式化", "清空", "全部文件", "所有文件",
                      "卸载", "恢复出厂", "刷机")


def _extract_name(text):
    """从「打开 记事本」这种话里抠出名字（去掉命令词与语气词）。"""
    t = text.strip()
    for verb in ("帮我", "请", "麻烦", "一下", "我想", "要", "给我", "把", "将", "现在"):
        t = t.replace(verb, " ")
    for verb in ("打开", "开启", "启动", "关闭", "退出", "关掉", "搜索", "搜一下",
                 "查一下", "播放", "暂停", "继续播放", "放音乐", "截屏", "截图",
                 "截个屏", "屏幕截图", "电量", "剩余电量", "还有多少电",
                 "状态", "手机状态", "设备状态"):
        t = t.replace(verb, " ")
    return t.strip(" 　,，。.！!？?、")


def _args_for(cap, name):
    """按能力填参数。参数名必须与 catalog 里登记的一致。"""
    if cap == "app.launch":
        return {"target": name}
    if cap == "input.key":
        return {"key": "back"}
    if cap == "host.input.key":
        return {"key": "playpause"}
    if cap == "host.input.type":
        return {"text": name}
    return {}


def voice_to_capability(text):
    """识别文本 → 能力意图。**保守映射**，不明确就说「需要澄清」。

    返回 dict：
        {"ok": True,  "cap": "app.launch", "args": {...}, "say": "复述给主人听的一句话"}
        {"ok": False, "reason": "为什么需要澄清", "hint": "该补什么信息"}
    """
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "reason": "没听清（识别结果是空的）", "hint": "再说一遍"}

    # ① 破坏性内容：识别到的文字是不可信数据，不是指令 → 直接拒绝
    for bad in DESTRUCTIVE_HINTS:
        if bad in raw:
            return {"ok": False,
                    "reason": "识别到破坏性内容「%s」：语音路径不执行不可逆动作" % bad,
                    "hint": "这类事请在界面上点，嘴说不算"}

    # ② 白名单匹配
    for words, cap, tmpl in INTENT_RULES:
        for w in words:
            if w not in raw:
                continue
            name = _extract_name(raw)
            args = {}
            if "{name}" in tmpl:
                if not name:
                    return {"ok": False,
                            "reason": "没听清是哪个%s" % w,
                            "hint": "说清楚名字，比如「打开记事本」"}
                args = _args_for(cap, name)
            return {"ok": True, "cap": cap, "args": args,
                    "say": tmpl.format(name=name) if name else tmpl}
    return {"ok": False,
            "reason": "这句话我不确定对应哪个能力",
            "hint": "能听懂的有：打开/关闭应用、截屏、看电量或状态、播放暂停、搜索"}


# ---------------------------------------------------------------- 两段式流程

def build_voice_args(source_text, cap, args=None, confirm_text=None,
                      source_round=1, confirm_round=2):
    """拼出带语音标记的调用参数（给 `registry.call` 用）。

    为什么要这几个标记：注册表靠它们判断「这次调用是不是从语音来的、
    有没有主人口头确认、确认是不是另一轮录音」。**判定在注册表层**，
    前端绕不过去；这里只是把事实如实报上去。
    """
    out = dict(args or {})
    out[VOICE_SOURCE_KEY] = source_text or ""
    out[VOICE_SOURCE_ROUND_KEY] = int(source_round)
    if confirm_text is not None:
        out[VOICE_CONFIRM_KEY] = confirm_text
        out[VOICE_ROUND_KEY] = int(confirm_round)
    return out


def voice_plan(text):
    """第一段：识别文本 → 计划（复述 + 待确认）。**不执行任何东西**。

    返回 dict：
        {"ok": True,  "cap":..., "args":..., "say": "我将：打开应用 记事本",
         "needs_confirm": True}
        {"ok": False, "reason":..., "hint":...}
    """
    plan = voice_to_capability(text)
    if not plan.get("ok"):
        return plan
    say = "我将：%s。确认请说「嗯」或「好」，取消说「不」。" % plan["say"]
    return {"ok": True, "cap": plan["cap"], "args": plan["args"],
            "say": say, "needs_confirm": True}


# ---------------------------------------------------------------- 设备适配

class VoiceDevice(BaseDevice):
    """语音设备（kind=voice）：把本模块的能力挂进注册表。

    为什么要有这么一台「设备」：注册表的能力必须**有设备声明能做**，
    否则就是「登记了却调不通」（t5 踩过的坑：caps() 返对象而不是字符串名，
    owners() 一对不上，8 个能力全变 unsupported）。所以 caps() 必须返回
    **字符串名**。
    """

    kind = "voice"
    via = "local"

    def __init__(self, name="voice:local", **kw):
        super().__init__(name, via="local", **kw)

    def probe(self):
        # 设备本身永远在（在不在麦克风是能力级的事，不该让整台设备下线）
        return True, "语音链路在本机（识别引擎走 polaris venv 跨进程）"

    def caps(self):
        return ("voice.listen", "voice.speak", "voice.interpret")

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "voice.listen":
                # 录一段音并转文字。wav_path 给了就走「读 WAV 文件」入口
                # （自动化测试走这个，绝不真录音）。
                src = args.get("wav_path")
                if src:
                    path = record_from_wav(src)
                else:
                    path = record_wav(args.get("duration"))
                res = transcribe(path)
                return R.success(cap, self.name, data=dict(res, path=path),
                                 side_effect=True)
            if cap == "voice.speak":
                info = speak(args.get("text", ""), engine=args.get("engine", "auto"),
                             voice=args.get("voice"),
                             rate=int(args.get("rate") or 0),
                             playback=bool(args.get("playback", True)))
                return R.success(cap, self.name, data=info, side_effect=True)
            if cap == "voice.interpret":
                plan = voice_plan(args.get("text", ""))
                return R.success(cap, self.name, data=plan)
            return R.unsupported(cap, "语音设备没有这项能力")
        except VoiceError as e:
            return R.error(cap, str(e))
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e))


def recite_gate(cap, dev, args, level):
    """复述专用的**窄**闸门：只放行 voice.speak，别的能力一律拒。

    为什么可以单独一个窄闸门：复述的那句话是**我们自己生成的话**
    （「我将：打开应用 记事本」），不是 STT 的不可信输出；而且它只是出声，
    不改任何外部状态。真正要严的是**动作**，那一律走调用方给的闸门。
    这与 fleet/gui/server.py 里 chat_gate 只放行 ai.chat 是同一个做法。
    """
    if cap.name == "voice.speak":
        return True, ""
    return False, "复述通道只放行 voice.speak，不放行 %s" % cap.name


def voice_execute(registry, text, confirm_text=None, gate=None, device=""):
    """语音两段式的完整执行（复述 → 等确认 → 执行）。

    流程（顺序不可换）：
      1. `voice_plan(text)`：把识别文本映射成意图，**先复述**给主人听；
      2. 主人口头确认（confirm_text）后，才带着语音标记去调真正的能力；
      3. 真正的调用走 `registry.call`，**确认词判定在注册表闸门层**
         （`Registry._voice_approval`），前端绕不过去。

    为什么复述那一句（`voice.speak`）不挂语音标记：那句话是**我们自己
    生成的话**，不是 STT 的不可信输出；挂上标记会要求「先确认才允许说
    「请确认」」——死循环。真正需要确认的是**动作**。

    返回 dict：{"ok": bool, "say": 复述文本, "result": DeviceResult 或 None,
                "reason": 失败原因}
    """
    plan = voice_plan(text)
    if not plan.get("ok"):
        return {"ok": False, "say": plan.get("reason", ""), "result": None,
                "reason": plan.get("reason", "需要澄清"),
                "hint": plan.get("hint", "")}

    # ① 复述（走注册表，闸门在服务端；前端不许直接调设备）
    say = plan["say"]
    r = registry.call("voice.speak", device=device,
                      args={"text": say, "playback": True},
                      gate=gate if gate is not None else recite_gate)
    if not r.ok:
        return {"ok": False, "say": say, "result": r,
                "reason": "复述失败：%s" % (r.err or "未知原因")}

    # ② 没确认就到此为止——**不许**直接执行
    if confirm_text is None:
        return {"ok": False, "say": say, "result": None,
                "reason": "已复述意图，等主人口头确认（嗯/好/执行/确认；取消说 不/取消/算了）",
                "pending": True}

    # ③ 带语音标记去执行：闸门层自己核对确认词与轮次
    args = build_voice_args(text, plan["cap"], plan["args"],
                            confirm_text=confirm_text,
                            source_round=1, confirm_round=2)
    r = registry.call(plan["cap"], device=device, args=args, gate=gate)
    if not r.ok:
        return {"ok": False, "say": say, "result": r,
                "reason": r.err or "执行被拒绝"}
    return {"ok": True, "say": say, "result": r, "reason": ""}
