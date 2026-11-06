"""音频细节层：把「一个音频文件」变成能查、能改的事实。

为什么单独一台：
  已有的 audio.presets / audio.synth 只会凭空合成 WAV，media.* 只会按时间裁剪
  与变速。真实音频文件最常见的几个问题它俩都答不上来：
    · 编码 / 采样率 / 声道 / 码率 / 时长 / 标签是什么
    · 有多响、有没有削波、哪一段是静音、波形长什么样
    · 转码、响度归一化、按静音切分、多轨混音、改标签
  这些不新增设备也不新增协议，只是把已有的文件读透、改对。

边界（诚实写在前面）：
  · 只接本地真实文件，不接 URL / 管道 / 设备名；输入必须含音频流。
  · 输出一律另存，绝不覆盖源文件，也绝不覆盖任何已有文件（ffmpeg -n +
    取编号文件名）。编码失败会清掉半截输出，不发布残件。
  · 读与写分开命名：读操作永远免闸门，写操作标 side_effect。
  · 电平与波形基于解码后重采样到 8 kHz 单声道的 PCM 计算，是「用于比较的
    相对值」，不是母带级测量；响度归一化走 ffmpeg loudnorm。
  · 静音检测基于 silencedetect 的能量阈值，听感上的安静不等于能量阈值以下。
  · ffmpeg / ffprobe 是必须安装的外部可执行程序，Python 侧只用标准库。
"""

import json
import math
import os
import re
import sys
import tempfile
import wave
from array import array
from pathlib import Path

from ..core import BaseDevice, DeviceResult as R
from .mediaops import MediaOps, _unique

# 输出容器 -> 音频编码器。无损格式忽略码率参数。
CODEC_BY_SUFFIX = {
    ".wav": "pcm_s16le",
    ".mp3": "libmp3lame",
    ".flac": "flac",
    ".ogg": "libvorbis",
    ".m4a": "aac",
    ".aac": "aac",
    ".opus": "libopus",
}
LOSSLESS = {".wav", ".flac"}
PCM_RATE = 8000
MAX_PARTS = 200
TAG_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def _num(value, label, low, high, default=None, integer=False):
    """严格数值校验：bool / nan / inf / 越界一律拒绝，不悄悄截断。"""
    if value is None or value == "":
        if default is None:
            raise ValueError("缺 %s" % label)
        value = default
    if isinstance(value, bool):
        raise ValueError("%s 不接受 bool" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是数字，收到 %r" % (label, value)) from None
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError("%s 必须在 %g ~ %g 之间，收到 %r" % (label, low, high, value))
    if integer:
        if number != int(number):
            raise ValueError("%s 必须是整数，收到 %r" % (label, value))
        return int(number)
    return number


def _dbfs(value):
    """线性幅度转 dBFS；零幅度不返回 -inf，用可比较的下限表示。"""
    if value <= 0:
        return -120.0
    return 20.0 * math.log10(value)


def _as_path(value, label, must_exist=True):
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise ValueError("必须提供 %s 文件路径" % label)
    path = Path(value).expanduser().absolute()
    if must_exist and not path.is_file():
        raise ValueError("%s 不存在或不是普通文件：%s" % (label, path))
    return path


class AudioOps(BaseDevice):
    kind = "audioops"
    via = "local"

    def __init__(self, name="audio:file", timeout=120.0, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "media")
        # 复用媒体层的可执行程序探测、子进程回收与 ffprobe 封装，不复制一份。
        self._m = MediaOps(timeout=timeout)
        self._probe_cache = None

    def probe(self):
        if self._probe_cache is None:
            self._probe_cache = self._m.probe()
        return self._probe_cache

    def caps(self):
        return ("audio.info", "audio.level", "audio.silence", "audio.wave",
                "audio.tags", "audio.convert", "audio.normalize",
                "audio.segment", "audio.mix", "audio.retag")

    def invoke(self, cap, args=None):
        a = args or {}
        table = {
            "audio.info": self._info,
            "audio.level": self._level,
            "audio.silence": self._silence,
            "audio.wave": self._wave,
            "audio.tags": self._tags,
            "audio.convert": self._convert,
            "audio.normalize": self._normalize,
            "audio.segment": self._segment,
            "audio.mix": self._mix,
            "audio.retag": self._retag,
        }
        fn = table.get(cap)
        if fn is None:
            return R.error(cap, "未知能力 %r" % cap, device=self.name)
        ok, why = self.probe()
        if not ok:
            return R.unavailable(cap, "ffmpeg/ffprobe 不可用：%s" % why,
                                 device=self.name, via=self.via)
        try:
            data, effect = fn(a)
        except ValueError as exc:
            return R.error(cap, str(exc), device=self.name, via=self.via)
        except Exception as exc:                          # noqa: BLE001
            extra = getattr(exc, "stderr", "")
            tail = ("；外部命令 stderr 末尾：%s" % extra[-200:].strip()) if extra else ""
            return R.error(cap, "%s: %s%s" % (type(exc).__name__, exc, tail),
                           device=self.name, via=self.via)
        return R.success(cap, device=self.name, data=data,
                         side_effect=effect, via=self.via)

    # ------------------------------------------------------------ 公共输入

    def _source(self, a):
        raw = a.get("src", a.get("input"))
        path = _as_path(raw, "src")
        info = self._m._info(path)
        streams = info.get("streams") or []
        if not any(s.get("codec_type") == "audio" for s in streams):
            raise ValueError("文件里没有音频流：%s" % path.name)
        return path, info

    @staticmethod
    def _stream(info):
        return next(s for s in info["streams"] if s.get("codec_type") == "audio")

    def _duration(self, info):
        return self._m._duration(info)

    def _dest(self, a, src, suffix, label, same_suffix=False):
        given = a.get("out") or a.get("dst")
        if given:
            target = _as_path(given, "out", must_exist=False)
            if not target.suffix:
                raise ValueError("out 必须带扩展名：%s" % target)
        else:
            target = src.with_name("%s_%s%s" % (src.stem, label, suffix))
        if same_suffix and target.suffix.lower() != src.suffix.lower():
            raise ValueError("流拷贝要求输出与源同容器，后缀须为 %s" % src.suffix)
        if os.path.abspath(target) == os.path.abspath(src):
            raise ValueError("输出不能等于源文件（不覆盖源文件）：%s" % target)
        if not target.parent.is_dir():
            raise ValueError("输出目录不存在：%s" % target.parent)
        return _unique(target)

    def _emit(self, arguments, out):
        """跑一次编码；失败清掉半截文件，成功再校验非空。"""
        try:
            self._m._ffmpeg(arguments)
        except Exception:
            try:
                if out.exists():
                    out.unlink()
            except OSError:
                pass
            raise
        if not out.is_file() or out.stat().st_size == 0:
            raise ValueError("编码结束但没有有效输出：%s" % out)
        return out

    def _pcm(self, path):
        """解码成 8 kHz 单声道 s16le PCM，供电平/波形计算。"""
        stdout, _ = self._m._run(
            [self._m._ffmpeg_path, "-hide_banner", "-loglevel", "error",
             "-nostdin", "-i", str(path), "-vn", "-map", "0:a:0",
             "-ac", "1", "-ar", str(PCM_RATE), "-f", "s16le", "-c:a", "pcm_s16le", "-"],
            "PCM 解码")
        usable = stdout[: len(stdout) // 2 * 2]
        samples = array("h")
        samples.frombytes(usable)
        if sys.byteorder == "big":
            samples.byteswap()
        return samples

    @staticmethod
    def _stats(samples):
        count = len(samples)
        if not count:
            return {"peak": 0.0, "rms": 0.0, "peak_dbfs": -120.0,
                    "rms_dbfs": -120.0, "clipped": 0, "samples": 0}
        peak = 0
        total = 0
        clipped = 0
        for value in samples:
            mag = abs(value)
            if mag > peak:
                peak = mag
            total += value * value
            if mag >= 32767:
                clipped += 1
        peak_linear = peak / 32768.0
        rms_linear = math.sqrt(total / count) / 32768.0
        return {"peak": round(peak_linear, 6), "rms": round(rms_linear, 6),
                "peak_dbfs": round(_dbfs(peak_linear), 2),
                "rms_dbfs": round(_dbfs(rms_linear), 2),
                "clipped": clipped, "samples": count}

    def _outcome(self, src, out, label):
        info = self._m._info(out)
        return {"path": str(out), "size_bytes": out.stat().st_size,
                "operation": label, "source": str(src),
                "output": {"format": info.get("format", {}).get("format_name"),
                           "duration": self._duration(info),
                           "codec": self._stream(info).get("codec_name")}}

    # ------------------------------------------------------------ 只读五项

    def _info(self, a):
        path, info = self._source(a)
        stream = self._stream(info)
        fmt = info.get("format", {})
        try:
            rate = int(stream.get("sample_rate") or 0)
        except (TypeError, ValueError):
            rate = 0
        try:
            bitrate = int(fmt.get("bit_rate") or stream.get("bit_rate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        data = {"path": str(path), "format": fmt.get("format_name"),
                "duration": self._duration(info), "size_bytes": fmt.get("size"),
                "codec": stream.get("codec_name"), "sample_rate": rate,
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
                "bits_per_sample": stream.get("bits_per_sample"),
                "bit_rate": bitrate, "tags": dict(fmt.get("tags") or {})}
        return data, False

    def _level(self, a):
        path, _info = self._source(a)
        stats = self._stats(self._pcm(path))
        stats["path"] = str(path)
        stats["sample_rate"] = PCM_RATE
        stats["duration"] = round(stats["samples"] / PCM_RATE, 3)
        stats["silent"] = stats["peak"] < 1e-4
        return stats, False

    def _silence_raw(self, path, noise, minimum, duration):
        _, diagnostic = self._m._run(
            [self._m._ffmpeg_path, "-hide_banner", "-nostdin", "-loglevel", "info",
             "-i", str(path), "-vn", "-map", "0:a:0",
             "-af", "silencedetect=noise=%.1fdB:d=%.3f" % (noise, minimum),
             "-f", "null", "-"], "静音检测")
        starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", diagnostic)]
        ends = [(float(s), float(d)) for s, d in re.findall(
            r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", diagnostic)]
        segments = []
        for index, start in enumerate(starts):
            if index < len(ends):
                end, reported = ends[index]
            else:
                # 末尾静音没有 silence_end 行，用文件时长补齐。
                end, reported = (duration or start), (duration or start) - start
            end = min(end, duration or end)
            segments.append({"start": round(start, 3), "end": round(end, 3),
                             "duration": round(max(0.0, min(reported, end - start)), 3)})
        return segments

    def _silence(self, a):
        path, info = self._source(a)
        duration = self._duration(info)
        noise = _num(a.get("noise_db", a.get("threshold")), "noise_db", -90, 0, -35)
        minimum = _num(a.get("min_s", a.get("min_silence")), "min_s", 0.01, 60, 0.2)
        segments = self._silence_raw(path, noise, minimum, duration)
        return {"path": str(path), "threshold_db": noise, "min_s": minimum,
                "duration": duration, "count": len(segments),
                "total_silence": round(sum(s["duration"] for s in segments), 3),
                "segments": segments}, False

    def _wave(self, a):
        path, _info = self._source(a)
        buckets = _num(a.get("buckets"), "buckets", 10, 2000, 100, integer=True)
        samples = self._pcm(path)
        peaks, rms = [], []
        if samples:
            size = max(1, math.ceil(len(samples) / buckets))
            for start in range(0, len(samples), size):
                part = samples[start:start + size]
                part_stats = self._stats(part)
                peaks.append(part_stats["peak"])
                rms.append(part_stats["rms"])
        return {"path": str(path), "sample_rate": PCM_RATE,
                "duration": round(len(samples) / PCM_RATE, 3),
                "buckets": len(peaks),
                "bucket_seconds": round(len(samples) / PCM_RATE / len(peaks), 4) if peaks else 0,
                "peaks": peaks, "rms": rms}, False

    def _tags(self, a):
        path, info = self._source(a)
        tags = dict(info.get("format", {}).get("tags") or {})
        return {"path": str(path), "count": len(tags), "tags": tags}, False

    # ------------------------------------------------------------ 写入五项

    def _convert(self, a):
        src, info = self._source(a)
        out = self._dest(a, src, ".wav", "conv")
        suffix = out.suffix.lower()
        if suffix not in CODEC_BY_SUFFIX:
            raise ValueError("不支持的输出格式 %s（可用：%s）"
                             % (suffix, ", ".join(sorted(CODEC_BY_SUFFIX))))
        rate = _num(a.get("rate", a.get("sample_rate")), "rate", 8000, 192000,
                    self._stream(info).get("sample_rate") or 44100, integer=True)
        channels = _num(a.get("channels"), "channels", 1, 2,
                        self._stream(info).get("channels") or 1, integer=True)
        arguments = ["-i", str(src), "-vn", "-map", "0:a:0",
                     "-c:a", CODEC_BY_SUFFIX[suffix], "-ar", str(rate), "-ac", str(channels)]
        if suffix not in LOSSLESS:
            bitrate = _num(a.get("bitrate_k"), "bitrate_k", 32, 512, 192, integer=True)
            arguments += ["-b:a", "%dk" % bitrate]
        arguments.append(str(out))
        self._emit(arguments, out)
        return self._outcome(src, out, "convert"), True

    def _normalize(self, a):
        src, info = self._source(a)
        out = self._dest(a, src, ".wav", "norm")
        target = _num(a.get("target_lufs", a.get("i")), "target_lufs", -70, -5, -16)
        true_peak = _num(a.get("true_peak", a.get("tp")), "true_peak", -9, 0, -1.5)
        lra = _num(a.get("lra"), "lra", 1, 50, 11)
        rate = _num(a.get("rate"), "rate", 8000, 192000,
                    self._stream(info).get("sample_rate") or 44100, integer=True)
        before = self._stats(self._pcm(src))
        arguments = ["-i", str(src), "-vn", "-map", "0:a:0",
                     "-af", "loudnorm=I=%g:TP=%g:LRA=%g" % (target, true_peak, lra),
                     "-ar", str(rate), "-c:a", "pcm_s16le", str(out)]
        self._emit(arguments, out)
        after = self._stats(self._pcm(out))
        result = self._outcome(src, out, "normalize")
        result.update({"target_lufs": target, "true_peak": true_peak, "lra": lra,
                       "rms_dbfs_before": before["rms_dbfs"],
                       "rms_dbfs_after": after["rms_dbfs"]})
        return result, True

    def _segment(self, a):
        src, info = self._source(a)
        duration = self._duration(info)
        if not duration or duration <= 0:
            raise ValueError("源文件读不到正时长，无法切分")
        mode = str(a.get("mode") or "silence").lower()
        ranges = []
        if mode == "silence":
            noise = _num(a.get("noise_db"), "noise_db", -90, 0, -35)
            minimum = _num(a.get("min_s"), "min_s", 0.01, 60, 0.2)
            quiet = self._silence_raw(src, noise, minimum, duration)
            cursor = 0.0
            for seg in quiet:
                if seg["start"] - cursor >= 0.1:
                    ranges.append((cursor, seg["start"]))
                cursor = max(cursor, seg["end"])
            if duration - cursor >= 0.1:
                ranges.append((cursor, duration))
        elif mode == "fixed":
            chunk = _num(a.get("chunk_s"), "chunk_s", 0.1, 3600, 1.0)
            cursor = 0.0
            while cursor < duration - 0.01:
                ranges.append((cursor, min(cursor + chunk, duration)))
                cursor += chunk
        else:
            raise ValueError("mode 只能是 silence 或 fixed")
        if not ranges:
            raise ValueError("整段都判为静音，没有可切出的片段")
        if len(ranges) > MAX_PARTS:
            raise ValueError("切出 %d 段，超过上限 %d；调高 noise_db 或用 fixed"
                             % (len(ranges), MAX_PARTS))
        given = a.get("out_dir", a.get("out"))
        if given:
            folder = Path(given).expanduser().absolute()
        else:
            folder = src.with_name("%s_parts" % src.stem)
        folder.mkdir(parents=True, exist_ok=True)
        parts = []
        for index, (start, end) in enumerate(ranges):
            part = folder / ("part_%03d.wav" % index)
            if os.path.lexists(part):
                raise ValueError("分片文件已存在，换一个 out_dir：%s" % part)
            length = end - start
            self._emit(["-i", str(src), "-vn", "-map", "0:a:0",
                        "-ss", "%.3f" % start, "-t", "%.3f" % length,
                        "-c:a", "pcm_s16le", str(part)], part)
            parts.append({"path": str(part), "start": round(start, 3),
                          "end": round(end, 3), "duration": round(length, 3),
                          "size_bytes": part.stat().st_size})
        return {"dir": str(folder), "mode": mode, "count": len(parts),
                "source_duration": duration, "parts": parts}, True

    def _mix(self, a):
        raw_inputs = a.get("inputs")
        if raw_inputs is None:
            raw_inputs = [a.get("src", a.get("input")), a.get("other")]
        if not isinstance(raw_inputs, (list, tuple)) or len(raw_inputs) < 2:
            raise ValueError("mix 需要 inputs 至少两个文件")
        if len(raw_inputs) > 8:
            raise ValueError("一次最多混 8 条音轨")
        paths = [_as_path(item, "inputs[%d]" % i) for i, item in enumerate(raw_inputs)]
        gains = a.get("gains")
        if gains is None:
            gains = [1.0] * len(paths)
        if not isinstance(gains, (list, tuple)) or len(gains) != len(paths):
            raise ValueError("gains 必须与 inputs 等长")
        gains = [_num(g, "gains[%d]" % i, 0, 4, 1.0) for i, g in enumerate(gains)]
        src = paths[0]
        out = self._dest(a, src, ".wav", "mix")
        filters = []
        labels = []
        for i, gain in enumerate(gains):
            filters.append("[%d:a]volume=%.4f[a%d]" % (i, gain, i))
            labels.append("[a%d]" % i)
        filters.append("%samix=inputs=%d:duration=longest:normalize=0[out]"
                       % ("".join(labels), len(paths)))
        arguments = []
        for path in paths:
            arguments += ["-i", str(path)]
        arguments += ["-filter_complex", ";".join(filters), "-map", "[out]",
                      "-c:a", "pcm_s16le", "-ar", "44100", str(out)]
        self._emit(arguments, out)
        result = self._outcome(src, out, "mix")
        result["inputs"] = [str(p) for p in paths]
        result["gains"] = gains
        return result, True

    def _retag(self, a):
        src, _info = self._source(a)
        tags = a.get("tags")
        if not isinstance(tags, dict) or not tags:
            raise ValueError("缺 tags：给一个 {键: 值} 字典")
        clean = {}
        for key, value in tags.items():
            if not isinstance(key, str) or not TAG_KEY_RE.match(key):
                raise ValueError("标签键只许字母数字下划线点连字符：%r" % key)
            clean[key] = str(value)
        out = self._dest(a, src, src.suffix, "tagged", same_suffix=True)
        arguments = ["-i", str(src), "-map", "0", "-c", "copy", "-map_metadata", "0"]
        for key, value in clean.items():
            arguments += ["-metadata", "%s=%s" % (key, value)]
        arguments.append(str(out))
        self._emit(arguments, out)
        read_back = dict(self._m._info(out).get("format", {}).get("tags") or {})
        result = self._outcome(src, out, "retag")
        result["written"] = clean
        result["read_back"] = read_back
        return result, True


# ---------------------------------------------------------------- 自测

def _make_wav(path, segments, rate=16000):
    """合成固定测试素材：[(频率 Hz, 时长 s, 幅度 0~1), ...]，freq=0 即静音。"""
    frames = array("h")
    for freq, seconds, amp in segments:
        count = int(rate * seconds)
        for i in range(count):
            value = (int(amp * 32000 * math.sin(2 * math.pi * freq * i / rate))
                     if freq else 0)
            frames.append(value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.tobytes())


def _selftest():
    total = 0
    fails = []
    folder = tempfile.mkdtemp(prefix="audioops-")

    def check(condition, label):
        nonlocal total
        total += 1
        if not condition:
            fails.append(label)
            print("失败：%s" % label)

    def rejects(fn, *args):
        try:
            fn(*args)
        except (ValueError, TypeError):
            return True
        return False

    try:
        root = Path(folder)
        dev = AudioOps()
        src = root / "tone.wav"
        other = root / "other.wav"
        quiet = root / "quiet.wav"
        not_audio = root / "plain.txt"
        _make_wav(src, [(440, 1.0, 0.7), (0, 0.5, 0), (880, 0.6, 0.5)])
        _make_wav(other, [(220, 1.4, 0.4)])
        _make_wav(quiet, [(0, 0.8, 0)])
        not_audio.write_text("这不是音频", encoding="utf-8")
        src_bytes = src.read_bytes()

        check(dev.kind == "audioops" and dev.name == "audio:file", "设备元信息")
        check(len(dev.caps()) == 10 and len(set(dev.caps())) == 10, "十项能力且无重名")
        check(all(c.startswith("audio.") for c in dev.caps()), "能力名同域")

        # 纯函数：数值边界，不依赖 ffmpeg
        check(_num("16", "x", 1, 20, integer=True) == 16, "数值：字符串整数")
        for bad in (-1, 21, "nan", float("inf"), True, "abc", None):
            check(rejects(_num, bad, "x", 1, 20), "数值：拒绝非法 %r" % (bad,))
        check(_dbfs(1.0) == 0.0 and _dbfs(0) == -120.0, "dBFS：满幅与静音")
        stats = AudioOps._stats(array("h", [0, 16000, -16000, 32767]))
        check(stats["peak"] > 0.9 and stats["clipped"] == 1, "PCM 统计：峰值与削波")
        check(AudioOps._stats(array("h"))["samples"] == 0, "PCM 统计：空样本")

        ready, why = dev.probe()
        check(ready, "ffmpeg/ffprobe 可用：%s" % why)
        if not ready:
            raise RuntimeError("缺 ffmpeg/ffprobe，自测无法继续")

        # ---- 只读五项 ----
        r = dev.invoke("audio.info", {"src": str(src)})
        check(r.ok and not r.side_effect, "info：成功且只读：%s" % r.err)
        if r.ok:
            d = r.data
            check(abs(d["duration"] - 2.1) < 0.05, "info：时长 2.1s")
            check(d["sample_rate"] == 16000 and d["channels"] == 1, "info：采样率/声道")
            check(d["codec"] == "pcm_s16le", "info：编码来自真实探针")
            check(isinstance(d["tags"], dict), "info：标签字段")

        r = dev.invoke("audio.level", {"src": str(src)})
        check(r.ok and not r.side_effect, "level：成功且只读")
        if r.ok:
            d = r.data
            check(0.4 < d["peak"] < 0.9, "level：峰值落在素材幅度内")
            check(d["rms"] <= d["peak"] and d["rms_dbfs"] <= d["peak_dbfs"],
                  "level：RMS 不超过峰值")
            check(d["clipped"] == 0 and d["samples"] > 15000, "level：无削波、样本量对")
            check(d["silent"] is False, "level：有声文件不判静音")
        r = dev.invoke("audio.level", {"src": str(quiet)})
        check(r.ok and r.data["silent"] is True and r.data["peak"] == 0,
              "level：全静音文件判静音")

        r = dev.invoke("audio.silence", {"src": str(src), "noise_db": -40})
        check(r.ok and not r.side_effect, "silence：成功且只读")
        if r.ok:
            d = r.data
            check(d["count"] >= 1, "silence：至少抓到一段静音")
            check(any(0.8 < s["start"] < 1.3 for s in d["segments"]),
                  "silence：静音起点落在素材静音区")
            check(d["total_silence"] > 0.2, "silence：累计时长")
            check(d["threshold_db"] == -40, "silence：阈值回显")
        # 数字零的能量是 -inf，任何阈值下都算静音；要验「零段」得靠最短时长。
        r = dev.invoke("audio.silence", {"src": str(src), "noise_db": -90, "min_s": 5})
        check(r.ok and r.data["count"] == 0, "silence：最短静音超过文件时长时零段")

        r = dev.invoke("audio.wave", {"src": str(src), "buckets": 21})
        check(r.ok and not r.side_effect, "wave：成功且只读")
        if r.ok:
            d = r.data
            check(d["buckets"] == 21 and len(d["peaks"]) == 21 and len(d["rms"]) == 21,
                  "wave：桶数与序列长度")
            check(max(d["peaks"]) > 0.4 and min(d["peaks"]) < 0.05,
                  "wave：波形能看出有声段与静音段")
            check(all(0 <= v <= 1 for v in d["peaks"]), "wave：幅度归一到 0~1")

        r = dev.invoke("audio.tags", {"src": str(src)})
        check(r.ok and not r.side_effect and isinstance(r.data["tags"], dict),
              "tags：读标签且只读")

        # ---- 写入五项 ----
        r = dev.invoke("audio.convert", {"src": str(src), "rate": 8000})
        check(r.ok and r.side_effect, "convert：成功且标副作用：%s" % r.err)
        conv = Path(r.data["path"]) if r.ok else None
        if conv:
            check(conv.is_file() and conv.stat().st_size > 1000, "convert：输出非空中")
            back = dev.invoke("audio.info", {"src": str(conv)})
            check(back.ok and back.data["sample_rate"] == 8000, "convert：降采样生效")
            check(abs(back.data["duration"] - 2.1) < 0.08, "convert：时长不变")
        r = dev.invoke("audio.convert", {"src": str(src), "out": str(root / "a.mp3")})
        check(r.ok and (root / "a.mp3").is_file(), "convert：转 mp3 真出文件")
        r = dev.invoke("audio.convert", {"src": str(src), "out": str(root / "x.xyz")})
        check(not r.ok and not (root / "x.xyz").exists(), "convert：不支持的格式被拒且不留残件")

        r = dev.invoke("audio.normalize", {"src": str(src), "target_lufs": -16})
        check(r.ok and r.side_effect, "normalize：成功且标副作用：%s" % r.err)
        if r.ok:
            norm = Path(r.data["path"])
            check(norm.is_file() and norm.stat().st_size > 1000, "normalize：输出存在")
            check("rms_dbfs_before" in r.data and "rms_dbfs_after" in r.data,
                  "normalize：给出前后电平")
            check(r.data["rms_dbfs_before"] != r.data["rms_dbfs_after"],
                  "normalize：电平确实变了")

        r = dev.invoke("audio.segment", {"src": str(src), "noise_db": -40})
        check(r.ok and r.side_effect, "segment：成功且标副作用：%s" % r.err)
        if r.ok:
            check(r.data["count"] >= 2, "segment：至少切出两段")
            check(all(Path(p["path"]).is_file() for p in r.data["parts"]),
                  "segment：每段都真落盘")
            # 按静音切分会丢掉静音段：源 2.1s，静音 0.5s，留下约 1.6s。
            kept = sum(p["duration"] for p in r.data["parts"])
            check(1.3 <= kept <= 1.9,
                  "segment：切掉静音后保留约 1.6s（实测 %.2fs）" % kept)
        r = dev.invoke("audio.segment", {"src": str(src), "mode": "fixed", "chunk_s": 0.7,
                                        "out_dir": str(root / "fixed")})
        check(r.ok and r.data["count"] == 3, "segment：固定时长切三段")

        r = dev.invoke("audio.mix", {"inputs": [str(src), str(other)]})
        check(r.ok and r.side_effect, "mix：成功且标副作用：%s" % r.err)
        if r.ok:
            mix = Path(r.data["path"])
            check(mix.is_file() and mix.stat().st_size > 1000, "mix：输出存在")
            info = dev.invoke("audio.info", {"src": str(mix)})
            check(info.ok and abs(info.data["duration"] - 2.1) < 0.1,
                  "mix：时长取最长轨")
        r = dev.invoke("audio.mix", {"inputs": [str(src), str(other)], "gains": [0.5, 2]})
        check(r.ok and r.data["gains"] == [0.5, 2.0], "mix：各轨音量回显")
        r = dev.invoke("audio.mix", {"inputs": [str(src)]})
        check(not r.ok, "mix：少于两条输入被拒")

        r = dev.invoke("audio.retag", {"src": str(src),
                                       "tags": {"title": "测试", "artist": "audioops"}})
        check(r.ok and r.side_effect, "retag：成功且标副作用：%s" % r.err)
        if r.ok:
            check(r.data["read_back"].get("title") == "测试", "retag：写后读回一致")
            check(r.data["path"] != str(src), "retag：另存不覆盖源")
        r = dev.invoke("audio.tags", {"src": str(r.data["path"])} if r.ok else {})
        check(r.ok and r.data["tags"].get("artist") == "audioops", "tags：能读到写入的标签")
        r = dev.invoke("audio.retag", {"src": str(src), "tags": {"bad key": "x"}})
        check(not r.ok, "retag：非法标签键被拒")

        # ---- 反例与不变量 ----
        for label, args in (
            ("缺 src", {}),
            ("src 不存在", {"src": str(root / "nope.wav")}),
            ("src 不是音频", {"src": str(not_audio)}),
            ("src 是目录", {"src": str(root)}),
        ):
            r = dev.invoke("audio.info", args)
            check(not r.ok, "info：%s 要报错" % label)
        r = dev.invoke("audio.info", {"src": str(src), "unknown": 1})
        check(r.ok, "info：多余参数不影响只读")
        r = dev.invoke("audio.wave", {"src": str(src), "buckets": 5})
        check(not r.ok, "wave：桶数低于下限被拒")
        r = dev.invoke("audio.silence", {"src": str(src), "noise_db": 10})
        check(not r.ok, "silence：正阈值被拒")
        r = dev.invoke("audio.convert", {"src": str(src), "out": str(src)})
        check(not r.ok, "convert：输出等于源被拒")
        r = dev.invoke("audio.retag", {"src": str(src),
                                       "tags": {"title": "x"}, "out": str(root / "x.mp3")})
        check(not r.ok, "retag：流拷贝换容器被拒")
        r = dev.invoke("audio.unknown")
        check(not r.ok, "未知能力要报错")
        r = dev.invoke("audio.info", {"src": str(src)})
        check(r.ok and r.code == "ok", "结果信封：code=ok")
        check(src.read_bytes() == src_bytes, "全程未改动源文件")
        # 输出不覆盖：同参数再跑一次，应换编号而不是覆盖
        first = dev.invoke("audio.convert", {"src": str(src),
                                             "out": str(root / "dup.wav")})
        second = dev.invoke("audio.convert", {"src": str(src),
                                              "out": str(root / "dup.wav")})
        check(first.ok and second.ok and first.data["path"] != second.data["path"],
              "同输出名再跑：另取编号不覆盖")
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)

    got = total - len(fails)
    print("\naudioops selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for item in fails:
        print("   失败:", item)
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.audioops --selftest")
