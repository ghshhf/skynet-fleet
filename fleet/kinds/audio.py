"""音频：代码生成声音（kind=audio）。

这一层是本仓「**借天下一半、自己造一半**」里「自己造」的那一半：
声音是**算出来的**，不是采样来的。

🔴 **与 TTS 的分工（不许混）**：
  · 本层（audio.synth）：功能性声音——上线、告警、确认、倒计时、氛围床。
    它们的波形由**规则**定义（音高包络、谐波结构、时长都可参数化），
    所以「编码生成」在这里是字面意义上的成立。
  · TTS / 音色克隆（voice.speak 那条线）：把**文字**变成**像人说话**的声音。
    说话是信息，不是波形规则 ⇒ 那部分必须借别人的模型。
  两者**互补不替代**。本层也不装 ffmpeg 之外的编码器：只出 WAV。

🔴 **零第三方依赖**：只用标准库（wave/math/struct/hashlib）。
    合成 3.6 秒音频耗时约 0.03 秒（RTF ≈ 0.009），
    比本地神经 TTS 快两个数量级——因为它没有神经网络。

🔴 **probe 恒为真**：这是纯计算能力，装不装都在。
    跟卡商店设备同一个道理（见 `fleet/kinds/card_store.py`）：
    probe 的契约是「不产生副作用的前提下回答能不能用」，
    不许因为「输出目录不存在」就报不可用——目录只在真写文件时才建。
"""
import os
import tempfile

from .. import audio_synth as SY
from ..core import BaseDevice, DeviceResult as R


class AudioSynth(BaseDevice):
    kind = "audio"
    via = "local"

    def __init__(self, name="audio:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "local")

    def probe(self):
        """代码合成音频是纯计算，永远在。"""
        return True, ""

    def caps(self):
        return ("audio.presets", "audio.synth")

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "audio.presets":
                return R.success(cap, self.name, via=self.via, data={
                    "presets": SY.presets(),
                    "note": "全是代码算出来的波形，零第三方依赖",
                })
            if cap == "audio.synth":
                out = (args.get("out") or "").strip()
                spec = args.get("spec") or args.get("preset") or "chime"
                custom = args.get("custom") or None
                r = SY.synth(
                    spec,
                    seed=args.get("seed"),
                    out=out or None,
                    sample_rate=int(args.get("sample_rate") or SY.DEFAULT_SAMPLE_RATE),
                    fmt=(args.get("format") or "wav"),
                    custom=custom,
                )
                return R.success(cap, self.name, via=self.via,
                                 side_effect=bool(out),
                                 data=r)
            return super().invoke(cap, args)
        except SY.SynthError as e:
            return R.error(cap, str(e), device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)


def _default_out_dir():
    """默认输出目录。存在就复用，不存在才建（probe 不许有副作用）。"""
    d = os.environ.get("FLEET_AUDIO_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audio_out")
    return d


def synth_to_dir(spec, seed=None, name=None):
    """便捷入口：合成到默认输出目录（给 GUI 用）。"""
    d = _default_out_dir()
    if not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    fn = (name or str(spec)).replace("/", "_") + ".wav"
    return SY.synth(spec, seed=seed, out=os.path.join(d, fn))
