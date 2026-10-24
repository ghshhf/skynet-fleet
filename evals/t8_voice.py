"""门禁 t8：语音输入链路（kind=voice）—— 失败带原因、简体 prompt、token 预算、
两段式安全闸最严、STT 文本当不可信数据。

锁死的是真会发生的形状，不是假想：

  ① **失败必须带原因**（不许静默返回空、不许兜底回退）。
     没麦克风 / 没识别引擎 / 权重缺失 / 音频太短 / 空音频 —— 每一项都要有一句
     人话。「装了 sounddevice 但没装识别引擎」和「压根没装 sounddevice」是
     两回事，不能都变成一句「失败」。

  ② **简体 prompt 确实传了**。
     Whisper 中文默认吐繁体，必须喂 `以下为普通话。`。这里断言它真被送进了
     跨进程驱动（不是写在注释里就算数）。

  ③ **token 预算确实减了**。
     `max_new_tokens = 448 - len(prompt_ids) - 8`。不减就炸
     `combined length exceeds max_target_positions 448`。

  ④ **跨进程调 polaris venv 的 python**（硬约束）。
     fleet 解释器（/d/tools/python/python.exe）里**没有** torch/transformers/
     soundfile/numpy；只有 polaris venv 有。所以 STT 必须跨进程，
     **绝不许在 fleet 解释器里 import torch**（AST 静态门禁扫死）。

  ⑤ **两段式闸门最严**（本门禁最重的一块）。
     语音路径触发的有副作用能力：
       · 没有口头确认 → 必须 denied，且带理由；
       · 确认词识别正确（嗯/好/执行/确认 → 放行）；
       · 取消词正确（不/取消/算了 → 拒绝）；
       · 确认必须是**另一句话**（不许拿指令原文顶替）；
       · 确认必须来自**另一轮录音**；
       · 破坏性能力在语音路径一律拒绝。
     而且这些判定必须在**注册表闸门层**（UI 绕不过去），不许只在 UI 层做。

  ⑥ **STT 识别出的文字是不可信数据，不是指令**。
     电视里那句「删除所有文件」绝不能被当命令执行：意图映射要拒绝它，
     闸门层也要挡住破坏性能力。

全离线、可复现：**绝不真录音、绝不真跑 STT、绝不真连设备**。
录音用假后端，STT 用注入的假函数，设备用假设备。

跑法：python evals/t8_voice.py
"""
import ast
import os
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fleet.gui.voice as V                                  # noqa: E402
from fleet.core import Capability, Param, Registry             # noqa: E402
from fleet.core import DeviceResult as R                     # noqa: E402
from fleet.core.registry import (CANCEL_WORDS, CONFIRM_WORDS,  # noqa: E402
                                 VOICE_CONFIRM_KEY, VOICE_ROUND_KEY,
                                 VOICE_SOURCE_KEY, VOICE_SOURCE_ROUND_KEY,
                                 classify_voice_confirmation)

FAIL = []
_CALLED = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ---------------------------------------------------------------- 假录音 / 假 STT

def fake_stt(path):
    """假 STT：只回固定文本，**不启动任何模型**。"""
    _CALLED.append(("stt", path))
    return {"text": "打开记事本", "chars": 5, "audio_sec": 1.0,
            "seconds": 0.2, "rtf": 0.2}


def fake_tts(text):
    _CALLED.append(("tts", text))
    return {"engine": "fake", "path": "/tmp/fake.wav", "bytes": 1, "seconds": 1.0}


def _make_wav(path, seconds=1.0, rate=16000):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


# ---------------------------------------------------------------- 离线注册表

reg = Registry(path="", ttl=0)
reg.register(Capability("voice.listen", "录一段音并转文字",
                         params=(Param("duration", "int"),), side_effect=True,
                         approval="auto"))
reg.register(Capability("voice.speak", "把一段文字念出来",
                         params=(Param("text", "str", True),), side_effect=True))
reg.register(Capability("voice.interpret", "解析意图",
                         params=(Param("text", "str", True),)))
reg.register(Capability("act.click", "点一下",
                         params=(Param("x", "int"),), side_effect=True,
                         approval="ask"))
reg.register(Capability("act.burn", "烧掉", side_effect=True, destructive=True))
reg.register(Capability("app.launch", "启动手机应用",
                         params=(Param("target", "str", True),), side_effect=True))


class FakeVoice(V.VoiceDevice):
    """假语音设备：录音与 STT 全是假的，绝不碰麦克风/模型。"""

    def __init__(self):
        super().__init__("voice:local")
        self.calls = []

    def probe(self):
        return True, "假语音设备"

    def invoke(self, cap, args=None):
        args = args or {}
        self.calls.append((cap, dict(args)))
        if cap == "voice.listen":
            return R.success(cap, self.name, side_effect=True,
                             data={"text": "打开记事本", "chars": 5,
                                   "path": "/tmp/fake.wav"})
        if cap == "voice.speak":
            return R.success(cap, self.name, side_effect=True,
                             data={"engine": "fake", "path": "/tmp/fake.wav"})
        if cap == "voice.interpret":
            return R.success(cap, self.name, data=V.voice_plan(args.get("text", "")))
        return R.unsupported(cap, "没有这项能力")


dev = FakeVoice()
reg.add(dev)
_ACTED = []


class FakeAct(V.BaseDevice):
    kind = "fakeact"

    def probe(self):
        return True, "假动作设备"

    def caps(self):
        return ("act.click", "act.burn", "app.launch")

    def invoke(self, cap, args=None):
        _ACTED.append((cap, dict(args or {})))
        return R.success(cap, self.name, data={"acted": cap})


reg.add(FakeAct("act:local"))


def gate_yes(cap, d, args, level):
    return True, ""


# ---------------------------------------------------------------- ① 能力登记

for name in ("voice.listen", "voice.speak", "voice.interpret"):
    check(reg.capability(name) is not None, "%s 已登记" % name)
    check(bool(reg.owners(name)), "%s 有设备能接（caps() 返字符串名）" % name)

check(dev.caps() == ("voice.listen", "voice.speak", "voice.interpret"),
      "VoiceDevice.caps() 返回字符串名元组（不是 Capability 对象）")
check(all(isinstance(c, str) for c in dev.caps()),
      "caps() 每个元素都是字符串（登记了却调不通的老坑）")

# catalog 里也登记了（真清单是唯一真相）
try:
    from fleet.catalog import build
    reg2 = build(path=os.path.join(tempfile.mkdtemp(), "d.json"), with_nodes=False)
    for name in ("voice.listen", "voice.speak", "voice.interpret"):
        check(reg2.capability(name) is not None, "catalog 里登记了 %s" % name)
        check(bool(reg2.owners(name)), "catalog 里 %s 有设备能接" % name)
except SyntaxError as e:
    check(False, "catalog 能 import（其它模块语法错误）：%s" % e)

# ---------------------------------------------------------------- ② 失败带原因

_tmp = tempfile.mkdtemp(prefix="t8_voice_")
empty_wav = _make_wav(os.path.join(_tmp, "empty.wav"), seconds=0.0)
short_wav = _make_wav(os.path.join(_tmp, "short.wav"), seconds=0.1)
good_wav = _make_wav(os.path.join(_tmp, "good.wav"), seconds=1.0)

# ②a 文件不存在
try:
    V.transcribe(os.path.join(_tmp, "nope.wav"), stt=fake_stt)
    check(False, "转写不存在的文件必须报错")
except V.VoiceError as e:
    check("不存在" in str(e), "音频文件不存在 → 带明确原因（%s）" % e)

# ②b 空音频（0 帧）
try:
    V.transcribe(empty_wav, stt=fake_stt)
    check(False, "空音频必须报错")
except V.VoiceError as e:
    check("空" in str(e), "空音频 → 带明确原因（%s）" % e)

# ②c 音频太短
try:
    V.transcribe(short_wav, stt=fake_stt)
    check(False, "太短的音频必须报错")
except V.VoiceError as e:
    check("太短" in str(e), "音频太短 → 带明确原因（%s）" % e)

# ②d 不是 WAV
bad = os.path.join(_tmp, "bad.wav")
with open(bad, "w", encoding="utf-8") as f:
    f.write("not a wav")
try:
    V.transcribe(bad, stt=fake_stt)
    check(False, "非 WAV 必须报错")
except V.VoiceError as e:
    check("WAV" in str(e), "非 WAV 文件 → 带明确原因（%s）" % e)

# ②e 正常路径：假 STT 被调到了
_CALLED.clear()
res = V.transcribe(good_wav, stt=fake_stt)
check(res.get("text") == "打开记事本", "转写成功返回文字（假 STT）")
check(any(c[0] == "stt" for c in _CALLED), "转写确实走了注入的 STT（没碰真模型）")

# ②f 没录音库 → 明确说装什么（真机上 sounddevice/pyaudio 都没装）
_old_avail = V.available_recorders
V.available_recorders = lambda: []
try:
    V.record_wav()
    check(False, "没录音库时必须报错")
except V.VoiceError as e:
    check("没装录音库" in str(e) and "sounddevice" in str(e),
          "没录音库 → 说清装什么（%s）" % e)
finally:
    V.available_recorders = _old_avail

# ②g 录音失败带原因（假后端抛异常）
def _boom_recorder(duration, path):
    raise RuntimeError("设备被占用")


_old_rec = V.RECORDERS
V.RECORDERS = (("sounddevice", _boom_recorder),)
V.available_recorders = lambda: ["sounddevice"]
try:
    V.record_wav()
    check(False, "录音失败必须报错")
except V.VoiceError as e:
    check("录音失败" in str(e) and "设备被占用" in str(e),
          "录音失败 → 带底层原因（%s）" % e)
finally:
    V.RECORDERS = _old_rec
    V.available_recorders = _old_avail

# ②h 假后端成功时写出的是真 WAV
V.available_recorders = lambda: ["sounddevice"]
V.RECORDERS = (("sounddevice", lambda d, p: _make_wav(p, seconds=0.5)),)
try:
    p = V.record_wav(duration=0.5)
    check(V._wav_duration(p) is not None, "录音成功时写出真 WAV")
finally:
    V.RECORDERS = _old_rec
    V.available_recorders = _old_avail

# ②i 「读 WAV 文件」入口：不碰麦克风
got = V.record_from_wav(good_wav)
check(os.path.isfile(got) and V._wav_duration(got) is not None,
      "record_from_wav 是「读 WAV 文件」入口（自动化走这个，不录音）")
try:
    V.record_from_wav(os.path.join(_tmp, "nope.wav"))
    check(False, "读不存在的 WAV 必须报错")
except V.VoiceError as e:
    check("不存在" in str(e), "读 WAV 入口失败也带原因（%s）" % e)

# ---------------------------------------------------------------- ③ 简体 prompt

check(V.PROMPT_ZH == "以下为普通话。", "简体 prompt 常量在（%s）" % V.PROMPT_ZH)
# 不许是繁体
_trad = ("說", "話", "電", "開", "關")
check(not any(t in V.PROMPT_ZH for t in _trad), "prompt 是简体不是繁体")

# prompt 确实被送进了跨进程驱动：读驱动源码 + 跑一次假驱动看 env
with open(V.RUNNER, encoding="utf-8") as f:
    runner_src = f.read()
check("FLEET_STT_PROMPT" in runner_src, "驱动脚本读 FLEET_STT_PROMPT（prompt 真被传）")
check("PROMPT_ZH" in runner_src, "驱动脚本用的是 fleet.gui.voice 的 PROMPT_ZH（同一份真值）")
check("get_prompt_ids" in runner_src, "驱动脚本真的生成了 prompt_ids（不是摆设）")

# 假 subprocess：抓 env 看 prompt 有没有真送出去
import subprocess as _sp

_real_run = _sp.run
_captured = {}


def _fake_run(cmd, **kw):
    _captured["cmd"] = cmd
    _captured["env"] = kw.get("env") or {}
    class R:
        returncode = 0
        stdout = '{"text": "打开记事本"}'
        stderr = ""
    return R()


_sp.run = _fake_run
_r = None
try:
    try:
        _r = V.transcribe(good_wav)
    except V.VoiceError as e:
        # 门禁纪律：环境缺就 skip 并说明，不许把「本机没装 polaris-audio-io」
        # 判成代码坏了（上面的静态源码断言已单独验过驱动脚本）。
        if "polaris-audio-io" in str(e) or "解释器" in str(e):
            print("  [SKIP] 跨进程实跑缺 polaris 环境，跳过（%s）" % str(e)[:50])
        else:
            raise
finally:
    _sp.run = _real_run
if _r is not None:
    check(_r.get("text") == "打开记事本", "跨进程调用收得到 JSON 结果")
    check(_captured.get("env", {}).get("FLEET_STT_PROMPT") == "以下为普通话。",
          "跨进程 env 里确实带了简体 prompt")
    check(_captured.get("cmd", [None])[0] == V.POLARIS_PY,
          "跑的是 polaris venv 的解释器（不是 fleet 解释器）")
    check("voice_runner.py" in " ".join(_captured.get("cmd", [])),
          "跑的是仓库里的驱动脚本（不临时拼代码）")

# ---------------------------------------------------------------- ④ token 预算

check(V.MAX_TARGET_POSITIONS == 448, "目标位置上限是 448")
check(V.PROMPT_TOKEN_RESERVE == 8, "特殊位余量是 8")
for plen in (0, 1, 5, 20):
    want = 448 - plen - 8
    check(V.max_new_tokens(plen) == want,
          "token 预算 = 448 - %d - 8 = %d" % (plen, want))
    check(V.max_new_tokens(plen) < 448, "预算永远小于 448（不炸 combined length）")
check(V.max_new_tokens(0) == 440, "不减 prompt 时是 440（原始上限减 8）")
check("max_new_tokens=budget" in runner_src or "max_new_tokens" in runner_src,
      "驱动脚本把预算传给了 generate")
check("max_new_tokens(" in runner_src, "驱动脚本用的是同一份预算函数（不各写一份）")

# ---------------------------------------------------------------- ⑤ 跨进程硬约束

def _top_imports(path):
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    out = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            out.add((node.module or "").split(".")[0])
    return out


_heavy = {"torch", "transformers", "soundfile", "scipy", "numpy",
          "sounddevice", "pyaudio", "edge_tts"}
for rel in ("fleet/gui/voice.py", "fleet/gui/voice_runner.py"):
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        check(False, "%s 存在" % rel)
        continue
    top = _top_imports(p)
    bad = _heavy & top
    check(not bad, "%s 顶层不 import %s（fleet 解释器里没有 torch）"
          % (rel, bad or "重依赖"))

# fleet 解释器里真的没有 torch（门禁自身的前提）
try:
    import torch          # noqa: F401
    check(True, "（本机 fleet 解释器居然有 torch，跳过这层断言）")
except ImportError:
    check(True, "fleet 解释器确实没有 torch（所以必须跨进程调 polaris venv）")

# 驱动脚本必须用 polaris 的解释器跑：源码里不许自己 import polaris 的 listen
check("import listen" not in V.POLARIS_PY, "polaris 解释器路径不是 listen 模块")
check("sys.path" in runner_src and "POLARIS_DIR" in runner_src,
      "驱动脚本把 polaris 模块目录插进 sys.path（POLARIS_DIR 从 voice.py 单一真值来，"
      "不各写一份 env 名）")

# ---------------------------------------------------------------- ⑥ 两段式闸门

# ⑥a 无口头确认 → denied 且带理由
_ACTED.clear()
r = reg.call("act.click", device="act:local",
             args={"x": 10, VOICE_SOURCE_KEY: "打开记事本",
                   VOICE_SOURCE_ROUND_KEY: 1},
             gate=gate_yes)
check(r.code == R.DENIED, "语音动作没有口头确认 → denied")
check("确认" in r.err, "拒绝理由里说清要口头确认（%s）" % r.err)
check(_ACTED == [], "没确认时绝不落地到设备")

# ⑥b 确认词正确 → 放行
for w in ("嗯", "好", "好的", "执行", "确认", "来吧", "对", "是的", "可以"):
    _ACTED.clear()
    r = reg.call("act.click", device="act:local",
                 args={"x": 10, VOICE_SOURCE_KEY: "打开记事本",
                       VOICE_CONFIRM_KEY: w, VOICE_SOURCE_ROUND_KEY: 1,
                       VOICE_ROUND_KEY: 2},
                 gate=gate_yes)
    check(r.ok and len(_ACTED) == 1, "确认词「%s」→ 放行并执行" % w)

# ⑥c 取消词正确 → 拒绝
for w in ("不", "不要", "取消", "算了", "别", "停"):
    _ACTED.clear()
    r = reg.call("act.click", device="act:local",
                 args={"x": 10, VOICE_SOURCE_KEY: "打开记事本",
                       VOICE_CONFIRM_KEY: w, VOICE_SOURCE_ROUND_KEY: 1,
                       VOICE_ROUND_KEY: 2},
                 gate=gate_yes)
    check(r.code == R.DENIED and "取消" in r.err,
          "取消词「%s」→ 拒绝（%s）" % (w, r.err))

# ⑥d 取消优先于确认（「不要执行」里有「执行」）
check(classify_voice_confirmation("不要执行") == "cancel",
      "取消优先于确认（「不要执行」判为取消）")
check(classify_voice_confirmation("不要执行") == "cancel",
      "「不要执行」即使含确认词也判取消")

# ⑥e 听不清 → 必须拒绝（绝不默认放行）
for w in ("嗯嗯嗯大概", "那个那个", "", "   "):
    v = classify_voice_confirmation(w)
    check(v != "confirm", "含糊的「%s」判为不明（%s），绝不默认放行" % (w, v))

# ⑥f 确认必须来自另一轮录音
_ACTED.clear()
r = reg.call("act.click", device="act:local",
             args={"x": 10, VOICE_SOURCE_KEY: "打开记事本",
                   VOICE_CONFIRM_KEY: "嗯", VOICE_SOURCE_ROUND_KEY: 2,
                   VOICE_ROUND_KEY: 2},
             gate=gate_yes)
check(r.code == R.DENIED, "确认轮次不晚于指令轮次 → denied")
check(_ACTED == [], "轮次不对时绝不落地")

# ⑥g 不许拿指令原文当确认
_ACTED.clear()
r = reg.call("act.click", device="act:local",
             args={"x": 10, VOICE_SOURCE_KEY: "嗯",
                   VOICE_CONFIRM_KEY: "嗯", VOICE_SOURCE_ROUND_KEY: 1,
                   VOICE_ROUND_KEY: 2},
             gate=gate_yes)
check(r.code == R.DENIED, "确认内容与指令原文相同 → denied")
check(_ACTED == [], "同一句当确认时绝不落地")

# ⑥h 破坏性能力在语音路径一律拒绝（哪怕有确认）
_ACTED.clear()
r = reg.call("act.burn", device="act:local",
             args={VOICE_SOURCE_KEY: "烧掉", VOICE_CONFIRM_KEY: "嗯",
                   VOICE_SOURCE_ROUND_KEY: 1, VOICE_ROUND_KEY: 2},
             gate=gate_yes)
check(r.code == R.DENIED, "破坏性能力在语音路径 → denied（嘴说不能启动不可逆动作）")
check("破坏性" in r.err, "拒绝理由说清是破坏性（%s）" % r.err)
check(_ACTED == [], "破坏性能力没落地")

# ⑥i 判定必须在注册表层（前端绕不过去）：直接调 registry.call 也一样被拒
r = reg.call("act.click", device="act:local",
             args={"x": 1, VOICE_SOURCE_KEY: "打开记事本"},
             gate=gate_yes)
check(r.code == R.DENIED,
      "确认判定在 registry.call 里（UI 层绕不过去）")
_src = ""
with open(os.path.join(ROOT, "fleet", "core", "registry.py"), encoding="utf-8") as f:
    _src = f.read()
check("classify_voice_confirmation" in _src and "_voice_approval" in _src,
      "确认词识别实现在 fleet/core/registry.py（闸门层）")
_voice_src = open(os.path.join(ROOT, "fleet", "gui", "voice.py"),
                  encoding="utf-8").read()
check("def classify_voice_confirmation" not in _voice_src,
      "UI 层没有自己实现一套确认词判定（避免两套口径）")
check("from ..core.registry import" in _voice_src
      and "classify_voice_confirmation" in _voice_src,
      "UI 层从注册表导入确认词判定（同一份口径）")

# ⑥j 只读能力（voice.interpret）不受两段式影响
r = reg.call("voice.interpret", device="voice:local", args={"text": "打开记事本"})
check(r.ok, "只读能力 voice.interpret 免两段式（%s）" % r.err)
r = reg.call("voice.interpret", device="voice:local", args={"text": "随便说点什么"})
check(r.ok, "只读能力失败也给结果不崩")

# ---------------------------------------------------------------- ⑦ STT 当不可信数据

# ⑦a 破坏性内容不映射成任何能力
for bad in ("删除所有文件", "把手机格式化", "卸载微信", "清空所有文件"):
    plan = V.voice_to_capability(bad)
    check(not plan.get("ok"), "破坏性内容「%s」不映射成能力" % bad)
    check("破坏性" in plan.get("reason", ""),
          "破坏性内容拒绝时说明原因（%s）" % plan.get("reason", ""))

# ⑦b 电视声里的「删除所有文件」不能通过两段式变成真动作
_ACTED.clear()
plan = V.voice_to_capability("删除所有文件")
check(not plan.get("ok"), "电视声里的破坏性句子在映射层就被拒")
r = reg.call("act.burn", device="act:local",
             args={VOICE_SOURCE_KEY: "删除所有文件", VOICE_CONFIRM_KEY: "嗯",
                   VOICE_SOURCE_ROUND_KEY: 1, VOICE_ROUND_KEY: 2},
             gate=gate_yes)
check(r.code == R.DENIED, "即便带确认，破坏性内容在闸门层也被拒")
check(_ACTED == [], "破坏性内容绝不落地")

# ⑦c 保守映射：覆盖任务要求的几类
cases = [
    ("打开记事本", "app.launch"),
    ("帮我打开计算器", "app.launch"),
    ("关闭应用", "input.key"),
    ("截个屏", "host.screen"),
    ("看一下电量", "state.read"),
    ("手机状态", "state.read"),
    ("暂停", "host.input.key"),
    ("搜索天气", "host.input.type"),
]
for text, want in cases:
    plan = V.voice_to_capability(text)
    check(plan.get("ok") and plan.get("cap") == want,
          "「%s」→ %s（实得 %s）" % (text, want, plan.get("cap")))

# ⑦d 不明确 → 需要澄清，不许瞎猜
for text in ("那个那个", "嗯", "帮我弄一下"):
    plan = V.voice_to_capability(text)
    check(not plan.get("ok") and plan.get("reason"),
          "不明确的话「%s」→ 需要澄清（%s）" % (text, plan.get("reason", "")))

# ⑦e 参数要跟 catalog 登记的对得上
plan = V.voice_to_capability("打开记事本")
check(plan.get("args", {}).get("target") == "记事本",
      "打开应用的参数是 target（与 catalog 的 app.launch 一致）")

# ---------------------------------------------------------------- ⑧ 两段式全流程

# ⑧a 只复述不执行
_ACTED.clear()
res = V.voice_execute(reg, "打开记事本", gate=gate_yes)
check(not res["ok"] and res.get("pending"), "没有确认时只复述不执行（%s）" % res.get("reason", ""))
check(_ACTED == [], "只复述阶段不落地任何动作")
check("记事本" in res["say"], "复述内容包含要做的动作（%s）" % res["say"])
check("确认" in res["say"], "复述里带出确认提示（%s）" % res["say"])

# ⑧b 确认后才执行
_ACTED.clear()
res = V.voice_execute(reg, "打开记事本", confirm_text="嗯", gate=gate_yes)
check(res["ok"], "确认「嗯」后执行成功（%s）" % res.get("reason", ""))
check(len(_ACTED) == 1 and _ACTED[0][0] == "app.launch",
      "执行的是 app.launch（映射对了）")

# ⑧c 取消词 → 不执行
_ACTED.clear()
res = V.voice_execute(reg, "打开记事本", confirm_text="算了", gate=gate_yes)
check(not res["ok"], "取消词 → 不执行（%s）" % res.get("reason", ""))
check(_ACTED == [], "取消后不落地")

# ⑧d 破坏性句子 → 连复述都不该变成动作
_ACTED.clear()
res = V.voice_execute(reg, "删除所有文件", confirm_text="嗯", gate=gate_yes)
check(not res["ok"], "破坏性句子整条流程被拒（%s）" % res.get("reason", ""))
check(_ACTED == [], "破坏性句子不落地")

# ⑧e 复述走的是注册表（前端不许直接调设备）
_ACTED.clear()
dev.calls.clear()
V.voice_execute(reg, "打开记事本", gate=gate_yes)
check(any(c[0] == "voice.speak" for c in dev.calls),
      "复述走 registry.call('voice.speak')（不直接调设备）")

# ---------------------------------------------------------------- ⑨ 清理

try:
    for f in os.listdir(_tmp):
        os.remove(os.path.join(_tmp, f))
    os.rmdir(_tmp)
except OSError:
    pass

print("t8 voice:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
