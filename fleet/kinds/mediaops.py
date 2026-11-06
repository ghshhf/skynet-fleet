"""用本机 ffmpeg/ffprobe 提供六项媒体能力，Python 包依赖仅限标准库。

边界：不做 GPU 编码、不烧字幕、不混多轨；只处理本地文件，选择首个
视频轨与首个音频轨。拼接要求输入流格式一致，使用 concat demuxer。
ffmpeg/ffprobe 是必须安装的外部可执行程序，通过 subprocess 调用。

invoke 参数：每项都必须提供 out。单文件输入使用 src（也接受 input）；
concat 使用 inputs。trim 使用 start 与 end 或 duration；extract_audio
提取首条音轨；thumb 使用 start，可选 width；gif 使用 start、duration、
fps=12、width=480；speed 使用 factor（也接受 speed）。所有时间参数
接受秒数、MM:SS 或 HH:MM:SS.mmm。已有输出会另取编号文件名。
成功 data 包含 path、size_bytes、elapsed（秒）、source_info 和 output_info；
source_info 是 ffprobe 原始 JSON，concat 时为按输入顺序排列的列表。
"""

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


if __package__:
    from ..core import BaseDevice, DeviceResult as R
else:
    # 孤立文件也需能离线自测，因此仅在没有包上下文时提供契约兼容对象。
    class BaseDevice:
        """保留最小契约，以便独立自测无需读取或安装框架文件。"""

        def __init__(self, name, via="", meta=None):
            self.name = name
            self.via = via
            self.meta = dict(meta or {})

    @dataclass
    class R:
        """独立运行仍返回带属性的结果，以便与框架契约使用同一套断言。"""

        cap: str
        device: str = ""
        ok: bool = False
        code: str = "error"
        data: dict = field(default_factory=dict)
        err: str = ""
        ms: float = 0.0
        side_effect: bool = False
        via: str = ""

        @classmethod
        def success(cls, cap, device="", data=None, ms=0.0,
                    side_effect=False, via=""):
            return cls(cap, device, True, "ok", data or {}, "", ms,
                       side_effect, via)

        @classmethod
        def error(cls, cap, reason, device="", data=None, ms=0.0, via=""):
            return cls(cap, device, False, "error", data or {},
                       str(reason) or "媒体处理失败", ms, False, via)


class _MediaError(Exception):
    """单独保存子进程诊断，避免错误被通用异常包装后丢失。"""

    def __init__(self, reason, stderr=""):
        super().__init__(reason)
        self.stderr = stderr


def _secs(value):
    """统一解析时间并拒绝非有限值，以免错误的时间进入滤镜或命令行。"""
    if isinstance(value, bool):
        raise ValueError("时间不能是布尔值")
    if isinstance(value, (int, float)):
        try:
            result = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError("时间必须是有限的非负秒数") from exc
    elif isinstance(value, str):
        parts = value.strip().split(":")
        if not 1 <= len(parts) <= 3:
            raise ValueError("时间格式应为 SS、MM:SS 或 HH:MM:SS.mmm")
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", parts[-1]):
            raise ValueError("秒数必须是非负数字")
        if any(not re.fullmatch(r"[0-9]+", part) for part in parts[:-1]):
            raise ValueError("时、分必须是非负整数")
        numbers = [float(part) for part in parts]
        if len(parts) > 1 and numbers[-1] >= 60:
            raise ValueError("带冒号的秒数必须小于 60")
        if len(parts) == 3 and numbers[1] >= 60:
            raise ValueError("HH:MM:SS 中的分钟必须小于 60")
        result = sum(number * 60 ** index
                     for index, number in enumerate(reversed(numbers)))
    else:
        raise ValueError("时间必须是数字或时间字符串")
    if not math.isfinite(result) or result < 0:
        raise ValueError("时间必须是有限的非负秒数")
    return result


def _number(value, label, integer=False):
    """滤镜值先检查再格式化，因为直接插入任意字符串会改变滤镜语义。"""
    if isinstance(value, bool):
        raise ValueError("%s 必须是正数" % label)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("%s 必须是正数" % label) from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("%s 必须是有限的正数" % label)
    if integer and not result.is_integer():
        raise ValueError("%s 必须是正整数" % label)
    return int(result) if integer else result


def _unique(out):
    """保留用户指定扩展名，因为 ffmpeg 依靠它选择输出容器。"""
    if not isinstance(out, (str, os.PathLike)) or not os.fspath(out):
        raise ValueError("必须提供非空的 out 输出路径")
    path = Path(out).expanduser().absolute()
    if not path.suffix:
        raise ValueError("out 必须带有媒体文件扩展名")
    if not path.parent.is_dir():
        raise ValueError("输出目录不存在：%s" % path.parent)
    candidate = path
    index = 1
    # lexists 同时避开断开的符号链接，避免把已有目录项当作空闲路径。
    while os.path.lexists(candidate):
        candidate = path.with_name("%s_%d%s" % (path.stem, index, path.suffix))
        index += 1
    return candidate


def _atempo(factor):
    """将倍率分解到 0.5–2.0，保证每一级 atempo 都能保持音调。"""
    value = _number(factor, "factor")
    stages = []
    while value > 2.0:
        stages.append(2.0)
        value /= 2.0
    while value < 0.5:
        stages.append(0.5)
        value *= 2.0
    stages.append(value)
    return ",".join("atempo=%.17g" % stage for stage in stages)


class MediaOps(BaseDevice):
    """集中处理进程与文件生命周期，保证六项能力使用相同的错误契约。"""

    kind = "mediaops"
    name = "media:ffmpeg"
    layer = "media"
    via = "local"

    def __init__(self, name="media:ffmpeg", timeout=120.0, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "media")
        self.timeout = _number(timeout, "timeout")
        self._probe_cache = None
        self._ffmpeg_path = None
        self._ffprobe_path = None

    def caps(self):
        return ("media.trim", "media.concat", "media.extract_audio",
                "media.thumb", "media.gif", "media.speed")

    def _run(self, command, label):
        """communicate 排空两条管道；超时也回收进程，防止僵进程或死锁。"""
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False)
        except OSError as exc:
            raise _MediaError("无法启动 %s：%s" % (label, exc)) from exc
        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            raise _MediaError("%s 执行超时（%g 秒）" % (label, self.timeout),
                              stderr.decode("utf-8", errors="replace")) from exc
        except BaseException:
            process.kill()
            process.communicate()
            raise
        diagnostic = stderr.decode("utf-8", errors="replace")
        if process.returncode:
            raise _MediaError("%s 执行失败，退出码 %s" % (label, process.returncode),
                              diagnostic)
        return stdout, diagnostic

    @staticmethod
    def _reason(exc):
        """即使参数错误没有子进程输出，也明确诊断来源而不伪造 stderr。"""
        tail = getattr(exc, "stderr", "")[-400:].strip() or "（无输出）"
        return "%s；ffmpeg/ffprobe stderr 最后 400 字符：%s" % (exc, tail)

    def probe(self):
        """缓存可执行程序检查，避免每次小操作都额外启动版本查询进程。"""
        if self._probe_cache is not None:
            return self._probe_cache
        try:
            self._ffmpeg_path = shutil.which("ffmpeg")
            self._ffprobe_path = shutil.which("ffprobe")
            if os.name == "nt":
                search = r"G:\dev\ffmpeg\bin"
                self._ffmpeg_path = self._ffmpeg_path or shutil.which("ffmpeg", path=search)
                self._ffprobe_path = self._ffprobe_path or shutil.which("ffprobe", path=search)
            if not self._ffmpeg_path:
                raise _MediaError("找不到 ffmpeg，请将本机 ffmpeg 的 bin 目录加入 PATH")
            self._run([self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
                       "-version"], "ffmpeg")
            if not self._ffprobe_path:
                raise _MediaError("找不到 ffprobe，请将本机 ffmpeg 的 bin 目录加入 PATH")
            self._probe_cache = (True, "")
        except Exception as exc:
            self._probe_cache = (False, self._reason(exc))
        return self._probe_cache

    def _ffmpeg(self, arguments):
        """禁用交互并禁止临时输出覆盖，让异常也无法卡在确认提示上。"""
        return self._run([self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
                          "-nostdin", "-n"] + arguments, "ffmpeg")

    def _info(self, path):
        """保留原始 ffprobe JSON，因为容器、时长和流信息应来自实际媒体。"""
        stdout, stderr = self._run(
            [self._ffprobe_path, "-hide_banner", "-loglevel", "error",
             "-print_format", "json", "-show_format", "-show_streams", str(path)],
            "ffprobe")
        try:
            result = json.loads(stdout.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise _MediaError("ffprobe 返回了无法解析的 JSON", stderr) from exc
        if not isinstance(result, dict) or not result.get("streams"):
            raise _MediaError("输入中没有可用的媒体流", stderr)
        return result

    def _source(self, value):
        """只接受真实本地文件，避免文件名被 ffmpeg 解释成输入协议。"""
        if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
            raise ValueError("必须提供 src 输入文件路径")
        path = Path(value).expanduser().absolute()
        if not path.is_file():
            raise ValueError("输入文件不存在或不是普通文件：%s" % path)
        return path, self._info(path)

    def _input(self, args):
        return self._source(args.get("src", args.get("input")))

    @staticmethod
    def _stream(info, kind):
        return next((stream for stream in info["streams"]
                     if stream.get("codec_type") == kind), None)

    @classmethod
    def _maps(cls, info):
        """显式选择首条音视频轨，因为自动映射可能选择多轨中的其他轨道。"""
        result = []
        for kind in ("video", "audio"):
            stream = cls._stream(info, kind)
            if stream is not None:
                result.extend(["-map", "0:%s" % stream["index"]])
        if not result:
            raise ValueError("输入中没有音频或视频流")
        return result + ["-sn", "-dn"]

    @staticmethod
    def _duration(info):
        """部分容器只在流上记录时长，因此容器时长缺失时再使用流时长。"""
        values = [info.get("format", {}).get("duration")]
        values.extend(stream.get("duration") for stream in info.get("streams", []))
        for value in values:
            try:
                duration = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                return duration
        return None

    def _segment(self, args, info, trim=False):
        """在启动编码前检查空区间，避免生成看似成功但无法播放的空文件。"""
        start = _secs(args.get("start", 0))
        has_end = "end" in args
        has_duration = "duration" in args
        if has_end and has_duration:
            raise ValueError("end 与 duration 只能提供一个")
        if trim and not (has_end or has_duration):
            raise ValueError("trim 必须提供 end 或 duration")
        if has_end:
            end = _secs(args["end"])
            if end <= start:
                raise ValueError("end 必须大于 start")
            duration = end - start
        elif has_duration:
            duration = _secs(args["duration"])
            if duration <= 0:
                raise ValueError("duration 必须大于 0")
        else:
            duration = None
        total = self._duration(info)
        if total is not None and start >= total:
            raise ValueError("start 已到达或超出源文件时长")
        return start, duration

    @staticmethod
    def _publish(temporary, requested):
        """排他创建最终文件，堵住检查名称后被其他进程抢占的覆盖竞态。"""
        while True:
            destination = _unique(requested)
            try:
                # 同目录临时文件通常可硬链接，发布时不会暴露只写了一半的媒体。
                os.link(temporary, destination)
                return destination
            except FileExistsError:
                continue
            except OSError:
                # 不支持硬链接的文件系统使用排他复制，仍绝不覆盖已有目录项。
                try:
                    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o666)
                except FileExistsError:
                    continue
                try:
                    with os.fdopen(descriptor, "wb") as target:
                        with open(temporary, "rb") as source:
                            shutil.copyfileobj(source, target)
                except BaseException:
                    destination.unlink()
                    raise
                return destination

    def _render(self, args, source_info, render):
        """编码和探测先在临时目录完成，只发布通过检查的输出并清理中间文件。"""
        requested = _unique(args["out"])
        directory = tempfile.mkdtemp(prefix=".mediaops-", dir=str(requested.parent))
        try:
            temporary = Path(directory) / ("output" + requested.suffix)
            render(Path(directory), temporary)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise _MediaError("ffmpeg 未生成有效的非空输出文件")
            output_info = self._info(temporary)
            size = temporary.stat().st_size
            destination = self._publish(temporary, args["out"])
            # ffprobe 先验证临时输出；返回时将文件名改为实际发布路径。
            output_info.get("format", {})["filename"] = str(destination)
            return {"path": str(destination), "size_bytes": size,
                    "source_info": source_info, "output_info": output_info}
        finally:
            shutil.rmtree(directory)

    def invoke(self, cap, args=None):
        """所有失败都走 R.error，让调用方只需处理一种失败结果。"""
        started = time.perf_counter()
        functions = {"media.trim": self._trim, "media.concat": self._concat,
                     "media.extract_audio": self._extract_audio,
                     "media.thumb": self._thumb, "media.gif": self._gif,
                     "media.speed": self._speed}
        try:
            function = functions.get(cap)
            if function is None:
                raise ValueError("未知能力 %r" % cap)
            if args is None:
                args = {}
            if not isinstance(args, Mapping):
                raise ValueError("参数必须是字典")
            if "out" not in args:
                raise ValueError("每项能力都必须提供 out 输出路径")
            _unique(args["out"])
            ready, reason = self.probe()
            if not ready:
                return R.error(cap, reason, device=self.name, via=self.via,
                               ms=(time.perf_counter() - started) * 1000)
            data = function(args)
            elapsed = time.perf_counter() - started
            data["elapsed"] = elapsed
            return R.success(cap, device=self.name, data=data, ms=elapsed * 1000,
                             side_effect=True, via=self.via)
        except Exception as exc:
            return R.error(cap, self._reason(exc), device=self.name, via=self.via,
                           ms=(time.perf_counter() - started) * 1000)

    def _trim(self, args):
        """重新编码以获得准确切点，因为流复制只能可靠地从关键帧开始。"""
        source, info = self._input(args)
        start, duration = self._segment(args, info, trim=True)
        command = ["-ss", "%.17g" % start, "-i", str(source),
                   "-t", "%.17g" % duration] + self._maps(info)
        if self._stream(info, "video"):
            command += ["-pix_fmt", "yuv420p"]
        return self._render(args, info, lambda work, out:
                            self._ffmpeg(command + [str(out)]))

    def _concat(self, args):
        """使用 demuxer 流复制避免重复编码，故先拒绝不兼容的流布局。"""
        inputs = args.get("inputs", args.get("sources"))
        if not isinstance(inputs, (list, tuple)) or len(inputs) < 2:
            raise ValueError("concat 的 inputs 必须包含至少两个输入文件")
        sources = [self._source(value) for value in inputs]
        for source, info in sources:
            if "\n" in str(source) or "\r" in str(source):
                raise ValueError("concat 输入路径不能含换行符")
        def signature(info):
            keys = ("codec_type", "codec_name", "codec_tag_string", "profile",
                    "width", "height", "pix_fmt", "sample_rate", "channels",
                    "channel_layout", "time_base")
            return [tuple(stream.get(key) for key in keys)
                    for stream in info["streams"]
                    if stream.get("codec_type") in ("video", "audio")]
        expected = signature(sources[0][1])
        if any(signature(info) != expected for source, info in sources[1:]):
            raise ValueError("concat 要求输入的音视频流顺序、编码和格式一致")
        def render(work, out):
            listing = work / "concat.txt"
            with open(listing, "x", encoding="utf-8", newline="\n") as handle:
                handle.write("ffconcat version 1.0\n")
                for source, info in sources:
                    # concat 自有引号语法；这里没有 shell，单引号仍需正确转义。
                    escaped = str(source).replace("\\", "/").replace("'", "'\\''")
                    handle.write("file '%s'\n" % escaped)
            self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing)]
                         + self._maps(sources[0][1]) + ["-c", "copy", str(out)])
        return self._render(args, [info for source, info in sources], render)

    def _extract_audio(self, args):
        """由输出容器选择音频编码，因此既可导出 WAV，也可导出 AAC 等格式。"""
        source, info = self._input(args)
        audio = self._stream(info, "audio")
        if audio is None:
            raise ValueError("源文件没有音频流，无法提取音频")
        command = ["-i", str(source), "-map", "0:%s" % audio["index"],
                   "-vn", "-sn", "-dn"]
        return self._render(args, info, lambda work, out:
                            self._ffmpeg(command + [str(out)]))

    def _thumb(self, args):
        """解码到指定时间再取一帧，以免缩略图跳到邻近关键帧。"""
        source, info = self._input(args)
        video = self._stream(info, "video")
        if video is None:
            raise ValueError("源文件没有视频流，无法生成缩略图")
        start, duration = self._segment(args, info)
        command = ["-ss", "%.17g" % start, "-i", str(source),
                   "-map", "0:%s" % video["index"], "-frames:v", "1", "-an"]
        if "width" in args:
            width = _number(args["width"], "width", integer=True)
            command += ["-vf", "scale=%d:-2" % width]
        return self._render(args, info, lambda work, out:
                            self._ffmpeg(command + [str(out)]))

    def _gif(self, args):
        """先分析完整片段的调色板再用它编码，减少逐帧量化造成的色彩闪烁。"""
        source, info = self._input(args)
        video = self._stream(info, "video")
        if video is None:
            raise ValueError("源文件没有视频流，无法生成 GIF")
        if Path(args["out"]).suffix.lower() != ".gif":
            raise ValueError("gif 的 out 必须使用 .gif 扩展名")
        start, duration = self._segment(args, info)
        fps = _number(args.get("fps", 12), "fps")
        width = _number(args.get("width", 480), "width", integer=True)
        selection = ["-ss", "%.17g" % start]
        if duration is not None:
            selection += ["-t", "%.17g" % duration]
        selection += ["-i", str(source)]
        filters = "fps=%.17g,scale=%d:-1:flags=lanczos" % (fps, width)
        def render(work, out):
            palette = work / "palette.png"
            self._ffmpeg(selection + ["-map", "0:%s" % video["index"], "-an",
                         "-vf", filters + ",palettegen", "-frames:v", "1",
                         str(palette)])
            graph = ("[0:%s]%s[frames];[frames][1:v:0]"
                     "paletteuse=dither=sierra2_4a[gif]" % (video["index"], filters))
            self._ffmpeg(selection + ["-i", str(palette), "-filter_complex", graph,
                         "-map", "[gif]", "-an", "-loop", "0", str(out)])
        return self._render(args, info, render)

    def _speed(self, args):
        """视频改时间戳、音频用 atempo，而不改采样率，以保持原有音调。"""
        source, info = self._input(args)
        factor = _number(args.get("factor", args.get("speed")), "factor")
        command = ["-i", str(source)] + self._maps(info)
        if self._stream(info, "video"):
            command += ["-vf", "setpts=(PTS-STARTPTS)/%.17g" % factor,
                        "-fps_mode", "vfr", "-pix_fmt", "yuv420p"]
        if self._stream(info, "audio"):
            command += ["-af", "asetpts=PTS-STARTPTS," + _atempo(factor)]
        return self._render(args, info, lambda work, out:
                            self._ffmpeg(command + [str(out)]))


def _selftest():
    """合成固定测试素材并检查实际媒体，保证测试离线且不依赖用户文件。"""
    total = 0
    fails = []
    directory = tempfile.mkdtemp()

    def check(condition, label):
        nonlocal total
        total += 1
        try:
            assert condition, label
        except AssertionError:
            fails.append(label)
            print("失败：%s" % label)

    def rejects(function, value):
        try:
            function(value)
        except (ValueError, TypeError):
            return True
        return False

    try:
        root = Path(directory)
        device = MediaOps()
        check(device.kind == "mediaops" and device.name == "media:ffmpeg"
              and device.meta["layer"] == "media", "设备元信息")
        check(len(device.caps()) == 6, "六项能力")
        check(_secs("12.5") == 12.5, "SS")
        check(_secs("02:03") == 123, "MM:SS")
        check(_secs("01:02:03.125") == 3723.125, "HH:MM:SS.mmm")
        check(_secs(0) == 0, "零秒")
        for value in ("-1", "1:60", "1:60:00", "1:2:3:4", "abc", "nan",
                      float("inf"), -1, True):
            check(rejects(_secs, value), "拒绝非法时间 %r" % value)
        for factor in (0.125, 0.25, 0.5, 1, 2, 4, 8):
            stages = [float(part.split("=")[1]) for part in _atempo(factor).split(",")]
            check(all(0.5 <= stage <= 2 for stage in stages)
                  and math.isclose(math.prod(stages), factor), "atempo 分解 %g" % factor)
        ready, why = device.probe()
        check(ready, "ffmpeg/ffprobe 可用：%s" % why)
        if not ready:
            raise RuntimeError(why)
        cached = device._probe_cache
        check(device.probe() is cached, "probe 缓存结果")

        video = root / "testsrc.mp4"
        audio = root / "sine.wav"
        silent = root / "silent.mp4"
        # 使用标准软件编码；lavfi 内部生成素材，因此没有网络或素材下载。
        device._ffmpeg(["-f", "lavfi", "-i", "testsrc=size=160x120:rate=24",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(video)])
        device._ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                        "-t", "3", "-c:a", "pcm_s16le", str(audio)])
        device._ffmpeg(["-i", str(video), "-map", "0:v:0", "-c", "copy", str(silent)])
        check(abs(device._duration(device._info(video)) - 3) < 0.1, "合成视频时长")
        check(abs(device._duration(device._info(audio)) - 3) < 0.1, "合成音频时长")

        def invoke(cap, arguments):
            result = device.invoke(cap, arguments)
            check(result.ok, "%s 成功：%s" % (cap, result.err))
            if not result.ok:
                raise RuntimeError(result.err)
            data = result.data
            check(result.code == "ok" and result.side_effect, "%s 结果契约" % cap)
            check(Path(data["path"]).is_file() and data["size_bytes"] > 0
                  and Path(data["path"]).stat().st_size == data["size_bytes"],
                  "%s 输出存在且大小匹配" % cap)
            check(data["elapsed"] > 0 and math.isclose(result.ms, data["elapsed"] * 1000),
                  "%s 耗时" % cap)
            sources = data["source_info"]
            check(all(item.get("streams") and item.get("format") for item in sources)
                  if isinstance(sources, list) else bool(sources.get("streams")
                  and sources.get("format")), "%s 源探测信息" % cap)
            return data

        trimmed = invoke("media.trim", {"src": video, "out": root / "trim.mp4",
                                        "start": "00:00:00.5", "end": "00:02"})
        check(abs(device._duration(trimmed["output_info"]) - 1.5) < 0.15, "trim end 时长")
        short = invoke("media.trim", {"src": audio, "out": root / "trim.wav",
                                      "start": "00:01", "duration": "1.25"})
        check(abs(device._duration(short["output_info"]) - 1.25) < 0.05, "trim duration 时长")
        joined = invoke("media.concat", {"inputs": [video, video], "out": root / "joined.mp4"})
        check(abs(device._duration(joined["output_info"]) - 6) < 0.2, "concat 时长")
        check(len(joined["source_info"]) == 2, "concat 保存所有源信息")
        extracted = invoke("media.extract_audio", {"src": video, "out": root / "extracted.m4a"})
        check(abs(device._duration(extracted["output_info"]) - 3) < 0.15, "音频提取时长")
        check(device._stream(extracted["output_info"], "video") is None, "提取结果只有音轨")
        thumbnail = invoke("media.thumb", {"src": video, "out": root / "thumb.png",
                                          "start": "00:01", "width": 96})
        check(device._stream(thumbnail["output_info"], "video")["width"] == 96, "缩略图宽度")
        animated = invoke("media.gif", {"src": video, "out": root / "clip.gif",
                                        "start": 0.5, "duration": 1})
        check(device._stream(animated["output_info"], "video")["width"] == 480, "GIF 默认宽度")
        check(abs(device._duration(animated["output_info"]) - 1) < 0.15, "GIF 时长")
        check(Path(animated["path"]).read_bytes()[:6] in (b"GIF87a", b"GIF89a"), "GIF 文件签名")
        fast = invoke("media.speed", {"src": video, "out": root / "fast.mp4", "factor": 4})
        check(abs(device._duration(fast["output_info"]) - 0.75) < 0.2, "视频四倍速度时长")
        check(device._stream(fast["output_info"], "video") is not None
              and device._stream(fast["output_info"], "audio") is not None, "变速保留音视频")
        slow = invoke("media.speed", {"src": audio, "out": root / "slow.wav", "factor": 0.25})
        check(abs(device._duration(slow["output_info"]) - 12) < 0.25, "音频四分之一速度时长")
        # 解码后以正向过零点估计音调，直接确认变速没有把 440 Hz 改成 110 Hz。
        pcm, stderr = device._ffmpeg(["-i", slow["path"], "-map", "0:a:0", "-f", "s16le",
                                     "-acodec", "pcm_s16le", "-ac", "1", "-ar", "48000", "pipe:1"])
        import array
        import sys
        samples = array.array("h")
        samples.frombytes(pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        middle = samples[48000:96000]
        crossings = sum(left <= 0 < right for left, right in zip(middle, middle[1:]))
        check(430 <= crossings <= 450, "atempo 保持 440 Hz 音调")

        original = video.read_bytes()
        preserved = invoke("media.trim", {"src": video, "out": video,
                                          "start": 0, "duration": 1})
        check(Path(preserved["path"]) != video and video.read_bytes() == original,
              "输出与源路径相同时绝不覆盖源")
        sentinel = root / "occupied.wav"
        sentinel.write_bytes(b"existing output must survive")
        first = invoke("media.extract_audio", {"src": video, "out": sentinel})
        first_bytes = Path(first["path"]).read_bytes()
        second = invoke("media.extract_audio", {"src": video, "out": sentinel})
        check(sentinel.read_bytes() == b"existing output must survive", "已有输出内容不变")
        check(first["path"] != second["path"] and Path(first["path"]).read_bytes() == first_bytes,
              "重复输出使用不同编号且不覆盖")

        # 单引号、空格、中文路径必须按列表文件语法处理，而非依赖 shell 引号。
        quoted = root / "中文 clip's.mp4"
        shutil.copyfile(video, quoted)
        escaped = invoke("media.concat", {"inputs": [quoted, video], "out": root / "quoted.mp4"})
        check(abs(device._duration(escaped["output_info"]) - 6) < 0.2, "concat 特殊路径")

        for cap in device.caps():
            bad = device.invoke(cap, {"src": video, "inputs": [video, video]})
            check(not bad.ok and bad.code == "error" and "out" in bad.err,
                  "%s 必须传 out" % cap)
        invalid = [
            ("media.trim", {"start": 1, "end": 1}),
            ("media.trim", {"start": 2, "end": 1}),
            ("media.trim", {"start": 0, "duration": 0}),
            ("media.trim", {"start": "bad", "duration": 1}),
            ("media.trim", {"start": 0}),
            ("media.trim", {"end": 1, "duration": 1}),
            ("media.trim", {"src": root / "missing.mp4", "duration": 1}),
            ("media.concat", {"inputs": [video]}),
            ("media.concat", {"inputs": [video, audio]}),
            ("media.speed", {"factor": 0}),
            ("media.speed", {"factor": -1}),
            ("media.speed", {"factor": "nan"}),
            ("media.speed", {"factor": True}),
            ("media.thumb", {"src": audio}),
            ("media.thumb", {"start": 4}),
            ("media.thumb", {"width": 1.5}),
            ("media.extract_audio", {"src": silent}),
            ("media.gif", {"src": audio}),
            ("media.gif", {"fps": 0}),
            ("media.gif", {"width": 0}),
            ("media.gif", {"duration": 0}),
        ]
        for index, (cap, extra) in enumerate(invalid):
            arguments = {"src": video, "out": root / ("invalid_%d.gif" % index)}
            arguments.update(extra)
            bad = device.invoke(cap, arguments)
            check(not bad.ok and bad.code == "error" and bool(bad.err)
                  and "stderr" in bad.err, "错误输入 %d 返回 R.error" % index)
            check(not Path(arguments["out"]).exists(), "错误输入 %d 不留下输出" % index)
        broken = root / "broken.mp4"
        broken.write_bytes(b"this is not media")
        bad = device.invoke("media.trim", {"src": broken, "out": root / "broken_out.mp4",
                                            "duration": 1})
        check(not bad.ok and "ffprobe" in bad.err and "无输出" not in bad.err,
              "保留真实 ffprobe stderr")
        bad = device.invoke("media.trim", {"src": video, "duration": 1,
                                            "out": root / "unknown.not_a_container"})
        check(not bad.ok and "ffmpeg" in bad.err and "无输出" not in bad.err,
              "保留真实 ffmpeg stderr")
        check(not (root / "unknown.not_a_container").exists(), "编码失败不发布输出")
        check(not device.invoke("media.unknown", {}).ok, "未知能力返回错误")
        check(not device.invoke("media.trim", []).ok, "非字典参数返回错误")
        check(not device.invoke("media.trim", {"out": ""}).ok, "空 out 返回错误")
        check(not any(path.name.startswith(".mediaops-") for path in root.iterdir()),
              "中间调色板和 concat 列表全部清理")
        diagnostic = device._reason(_MediaError("失败", "x" * 1000 + "结尾"))
        check(diagnostic.split("字符：", 1)[1] == ("x" * 1000 + "结尾")[-400:],
              "stderr 仅保留最后 400 字符")
        # 模拟找不到程序只替换定位函数，不触碰系统 PATH 或安装状态。
        from unittest.mock import patch
        with patch.object(shutil, "which", return_value=None):
            missing = MediaOps()
            available, reason = missing.probe()
            check(not available and "找不到 ffmpeg" in reason, "缺少程序的中文原因")
            bad = missing.invoke("media.trim", {"src": video, "out": root / "missing_out.mp4",
                                                 "duration": 1})
            check(not bad.ok and bad.code == "error", "缺少程序也返回 R.error")
    except Exception as exc:
        check(False, "自测执行异常：%s" % exc)
    finally:
        try:
            shutil.rmtree(directory)
            check(not os.path.exists(directory), "自测临时目录清理")
        except Exception as exc:
            check(False, "自测临时目录清理失败：%s" % exc)
    print("mediaops selftest: %d/%d" % (total - len(fails), total))
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.mediaops --selftest")
    sys.exit(0)
