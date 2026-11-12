"""格式自动转换设备：一个入口吃任意文件 → 目标格式，引擎按媒体类型自动分派。

为什么要有这一台（主人 2026-11-06「格式转化，自动格式转化之类的」）：
  仓库里原来只有三处零散的转换 —— `image.convert`（图片）、`doc.convert`（文档抽成
  文本）、`code.convert`（代码语言互译）。**音视频一条都没有**：手上有 mp4 想转
  mp3、想压成 webm、想让视频变 GIF，全都得自己去敲 ffmpeg 命令行。
  真实需求从来不是「图片转格式」，是「我有个文件，帮我转成 X」。

架构照抄开源界已经跑通的做法（ConvertX / HandBrake / Shutter Encoder 全是这个形状）：
  **单一调度前端 + 按媒体类型分派的引擎**。
  没有任何一个工具能同时吃图片/音频/视频/文档/压缩包，硬做就是重造一个四不像；
  正确姿势是承认「每种媒体都有它的事实标准」，然后把它们编排起来：

      image  → Pillow（精确控制画质/尺寸）  兜底 ffmpeg
      audio  → ffmpeg
      video  → ffmpeg
      doc    → MarkItDown（只出不进的抽取）  pandoc/LibreOffice（互转，本机没装）
      archive→ 7-Zip

🔴 「自动」落在三处，不是噱头：
  1. **按内容判类型，不信扩展名**。`_sniff()` 读魔数：从微信/download 下来的
     `xxx.dat`、被改名过的 `.mp4`（其实是 webm）都能认对。扩展名只当提示。
  2. **按类型选引擎**。调用方只说 `to="mp3"`，不用知道背后是 ffmpeg。
  3. **按目标推参数**。容器与编码器有硬绑定（webm 装不了 h264、mp4 装不了 vp9 的
     常规组合），选错就是 ffmpeg 一句天书报错。这里按容器给默认编码器。

不覆盖源文件、不产出半截文件（见 _atomic 的注释），这两条是硬边界：
转格式是**最容易毁原件的操作**，一次误写就没了。

本地引擎实测（2026-11-06）：ffmpeg 9.0.2、Pillow 12.3.0、markitdown 0.1.9、7-Zip 26.01。
没有 pandoc / LibreOffice / ImageMagick —— 缺的时候如实说缺什么、怎么装，不假装成功。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from ..core import BaseDevice, DeviceResult as R
from .. import locate as _locate

#: Windows 下不给子进程开控制台窗口（硬约束：后台脚本不许弹黑框）
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

#: 各媒体类型的合法目标扩展名。写死白名单而不是「让 ffmpeg 自己决定」：
#: 传错一个字母时，白名单能立刻回「你要的 mp4x 不认识，像是想写 mp4」，
#: 而 ffmpeg 只会回一句 "Unable to find a suitable output format"。
IMAGE_FMT = ("png", "jpg", "webp", "bmp", "gif", "tiff", "ico")
AUDIO_FMT = ("mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma", "ac3")
VIDEO_FMT = ("mp4", "mkv", "webm", "avi", "mov", "flv", "ts", "wmv", "m4v", "mpg")
DOC_FMT = ("md", "txt")

#: 容器 -> 编码器。新手最容易栽的地方：`-c copy` 把 h264 塞进 webm，或者
#: 让 ffmpeg 自己挑，结果产出在浏览器/手机上放不出来。这里给确定性答案。
_VIDEO_ARGS = {
    "mp4": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k"],
    "mov": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"],
    "mkv": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"],
    "webm": ["-c:v", "libvpx-vp9", "-b:v", "0", "-c:a", "libopus", "-b:a", "96k"],
    "avi": ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-b:a", "160k"],
    "flv": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"],
    "ts": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"],
    "wmv": ["-c:v", "wmv2", "-c:a", "wmav2"],
    "m4v": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"],
    "mpg": ["-c:v", "mpeg2video", "-c:a", "mp2"],
}

_AUDIO_ARGS = {
    "mp3": ["-c:a", "libmp3lame"], "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"], "ogg": ["-c:a", "libvorbis"],
    "m4a": ["-c:a", "aac"], "aac": ["-c:a", "aac"],
    "opus": ["-c:a", "libopus"], "wma": ["-c:a", "wmav2"], "ac3": ["-c:a", "ac3"],
}

#: 别名归一。`jpeg`/`jpg` 是同一个东西，但目录里会出现两种写法
_ALIAS = {"jpeg": "jpg", "tif": "tiff", "mpeg": "mpg", "m4v": "m4v",
          "htm": "html", "text": "txt", "markdown": "md"}

#: 目标格式在 A/V 之间跨类时的意图：视频转 mp3 就是「把音轨抽出来」
_AUDIO_LIKE = frozenset(AUDIO_FMT)


# ---------------------------------------------------------------- 工具定位

_TOOLDIRS_CACHE = None


def _tool_dirs():
    """找可执行文件的额外目录。

    为什么不能只靠 PATH：这台机器上 ffmpeg 在 `G:\\dev\\ffmpeg\\bin`，
    而常驻服务（计划任务/开机自启）的 PATH 往往和交互式终端不一样 —— 终端里
    `which ffmpeg` 有，服务里就没有。这类「交互能用、服务里不能」的 bug 排查起来
    极其费时，索性把已知安装点都列进来。
    """
    global _TOOLDIRS_CACHE
    if _TOOLDIRS_CACHE is not None:
        return _TOOLDIRS_CACHE
    dirs = []
    if os.environ.get("FLEET_TOOLBIN"):
        dirs.append(os.environ["FLEET_TOOLBIN"])
    # 盘符不写死：hints 只写「相对怎么摆」，根目录运行时枚举（fleet.locate）。
    for cand in _locate._hint_candidates(
            "ffmpeg.exe",
            ("dev/ffmpeg/bin", "tools/ffmpeg/bin", "ffmpeg/bin"),
            _locate.roots()):
        d = os.path.dirname(cand)
        if os.path.isdir(d):
            dirs.append(d)
    for env in ("USERPROFILE", "LOCALAPPDATA", "ProgramData"):
        base = os.environ.get(env)
        if not base:
            continue
        dirs += [os.path.join(base, "scoop", "shims"),
                 os.path.join(base, "Microsoft", "WinGet", "Links"),
                 os.path.join(base, "chocolatey", "bin")]
    _TOOLDIRS_CACHE = [d for d in dirs if os.path.isdir(d)]
    return _TOOLDIRS_CACHE


def _tool(name):
    """解析一个外部工具的真实路径；不存在返回 None。

    ⚠️ `convert` 必须特殊处理：Windows 的 `C:\\Windows\\system32\\convert.exe` 是
    **FAT 转 NTFS 的磁盘命令**，和 ImageMagick 的 `convert` 同名不同物。
    真按 ImageMagick 调它，轻则报参数错，重则对着一个卷动文件系统。所以直接不认。
    """
    if name.lower() == "convert":
        return None
    env = os.environ.get("FLEET_" + name.upper().replace("-", "_"))
    if env and os.path.isfile(env):
        return env
    hit = shutil.which(name)
    if hit:
        return hit
    for d in _tool_dirs():
        for cand in (name + ".exe", name + ".cmd", name + ".bat"):
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def _run(cmd, timeout=600):
    """跑一个外部命令。返回 (rc, stdout, stderr)，永不抛。"""
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return -9, "", "超时（%ds）：%s" % (timeout, os.path.basename(cmd[0]))
    except FileNotFoundError:
        return -2, "", "找不到可执行文件：%s" % cmd[0]
    except Exception as e:                      # 权限、杀软拦截等等
        return -1, "", "%s: %s" % (type(e).__name__, e)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out, err


def _tail(text, n=3):
    """错误信息只留最后几行：ffmpeg 的完整输出几百行，中间全是无关的流信息。"""
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return " | ".join(lines[-n:]) if lines else ""


# ---------------------------------------------------------------- 内容嗅探

def _head(path, n=64):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def sniff(path):
    """按魔数判类型，返回 dict(kind, fmt, ext, name)。

    kind ∈ image/audio/video/doc/archive/text/unknown，fmt 是真实格式。
    为什么要这个：用户手里的文件扩展名经常是错的（下载工具加 `.dat`、
    手机拍的视频叫 `.mp4` 其实是 HEVC 封装、微信语音 `.amr` 改 `.m4a`）。
    按扩展名分派引擎，第一步就派错了，后面全是白费。
    """
    h = _head(path, 512)
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    kind, fmt = "unknown", (ext or "?")

    def hit(k, f):
        return k, f

    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, fmt = hit("image", "png")
    elif h.startswith(b"\xff\xd8\xff"):
        kind, fmt = hit("image", "jpg")
    elif h.startswith(b"GIF87a") or h.startswith(b"GIF89a"):
        kind, fmt = hit("image", "gif")
    elif h.startswith(b"BM") and len(h) > 14:
        kind, fmt = hit("image", "bmp")
    elif h[:4] in (b"II*\x00", b"MM\x00*"):
        kind, fmt = hit("image", "tiff")
    elif h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        kind, fmt = hit("image", "webp")
    elif h[:4] == b"RIFF" and h[8:12] == b"WAVE":
        kind, fmt = hit("audio", "wav")
    elif h[:4] == b"RIFF" and h[8:12] == b"AVI ":
        kind, fmt = hit("video", "avi")
    elif len(h) > 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand[:3] == b"M4A" or brand in (b"M4B ", b"M4P "):
            kind, fmt = hit("audio", "m4a")
        elif brand[:3] == b"qt ":
            kind, fmt = hit("video", "mov")
        else:
            kind, fmt = hit("video", "mp4")
    elif h.startswith(b"\x1aE\xdf\xa3"):
        # Matroska 家族：webm 与 mkv 魔数完全一样，只能靠 DocType 区分
        kind, fmt = hit("video", "webm" if b"webm" in h[:64] else "mkv")
    elif h.startswith(b"OggS"):
        kind, fmt = hit("audio", "opus" if b"OpusHead" in h[:64] else "ogg")
    elif h.startswith(b"fLaC"):
        kind, fmt = hit("audio", "flac")
    elif h.startswith(b"ID3") or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        kind, fmt = hit("audio", "mp3")
    elif h[:2] in (b"\xff\xf1", b"\xff\xf9"):
        kind, fmt = hit("audio", "aac")
    elif h.startswith(b"0&\xb2u\x8e\x66\xcf\x11"):
        kind, fmt = hit("audio" if "wma" in ext else "video", "wma" if "wma" in ext else "wmv")
    elif h.startswith(b"FLV\x01"):
        kind, fmt = hit("video", "flv")
    elif h.startswith(b"%PDF"):
        kind, fmt = hit("doc", "pdf")
    elif h.startswith(b"{\\rtf"):
        kind, fmt = hit("doc", "rtf")
    elif h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # OLE 复合文档：doc/xls/ppt 老格式共用一个魔数，只能借扩展名细分
        kind, fmt = hit("doc", ext if ext in ("doc", "xls", "ppt") else "ole")
    elif h.startswith(b"7z\xbc\xaf\x27\x1c"):
        kind, fmt = hit("archive", "7z")
    elif h.startswith(b"PK\x03\x04"):
        if ext in ("docx", "xlsx", "pptx", "epub", "odt", "ods"):
            kind, fmt = hit("doc", ext)
        else:
            kind, fmt = hit("archive", "zip")
    elif h.startswith(b"\x1f\x8b"):
        kind, fmt = hit("archive", "gz")
    elif h.startswith(b"BZh"):
        kind, fmt = hit("archive", "bz2")
    elif h.startswith(b"\xfd7zXZ"):
        kind, fmt = hit("archive", "xz")
    elif h.startswith(b"Rar!"):
        kind, fmt = hit("archive", "rar")
    elif len(h) > 262 and h[257:262] == b"ustar":
        kind, fmt = hit("archive", "tar")
    else:
        # 最后一步：能按 UTF-8 解出大半且控制字符少 → 当文本
        probe = h[:4096]
        if probe:
            ctrl = sum(1 for b in probe if b < 9 or (13 < b < 32))
            if ctrl / float(len(probe)) < 0.02:
                try:
                    probe.decode("utf-8")
                    kind, fmt = hit("text", ext or "txt")
                except UnicodeDecodeError:
                    pass
    return {"kind": kind, "fmt": fmt, "ext": ext, "size": os.path.getsize(path)}


def _ffprobe(path, ffprobe):
    """读媒体真相：时长/分辨率/编码/码率。给 convert.probe 用，失败不影响转换。"""
    rc, out, _err = _run([
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path], timeout=30)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
    except Exception:
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    info = {
        "duration_s": round(float(fmt.get("duration") or 0), 3),
        "bitrate_kbps": int((float(fmt.get("bit_rate") or 0)) // 1000),
        "container": fmt.get("format_name", ""),
    }
    if v:
        info["video"] = {"codec": v.get("codec_name"), "width": v.get("width"),
                         "height": v.get("height"), "fps": v.get("r_frame_rate")}
    if a:
        info["audio"] = {"codec": a.get("codec_name"), "rate": a.get("sample_rate"),
                         "channels": a.get("channels")}
    return info


# ---------------------------------------------------------------- 输出路径

def _norm_ext(word):
    w = (word or "").strip().lower().lstrip(".").split("/")[-1].strip()
    return _ALIAS.get(w, w)


def _available_engines():
    """本机此刻真能用的引擎名。不给「理论上支持」——只报实际探测到的。"""
    out = []
    if _tool("ffmpeg"):
        out.append("ffmpeg")
    for mod, alias in (("PIL", "pillow"), ("markitdown", "markitdown")):
        try:
            __import__(mod)
            out.append(alias)
        except Exception:
            pass
    if _tool("7z"):
        out.append("7z")
    return out


def _unique(path):
    """同名就加 _1/_2，绝不覆盖。转格式最怕的就是「输出路径正好等于输入路径」。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = "%s_%d%s" % (stem, i, ext)
        if not os.path.exists(cand):
            return cand
    raise RuntimeError("同名文件超过 1000 个：%s" % path)


def _suggest(word, pool):
    """给拼错的目标格式找一个最像的，错误信息才有用。"""
    import difflib
    near = difflib.get_close_matches(word, list(pool), n=3, cutoff=0.5)
    if near:
        return "，是不是想写 %s？" % " / ".join(near)
    return ""


class ConvertDevice(BaseDevice):
    kind = "converter"
    via = "local"

    def __init__(self, name="media:convert", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "media")

    # ---------------- 设备契约 ----------------

    def probe(self):
        if not (_tool("ffmpeg") or _tool("ffprobe")):
            try:
                import PIL  # noqa: F401
            except Exception:
                return False, "ffmpeg 与 Pillow 都没有：装 ffmpeg（音视频）或 pip install Pillow"
            return True, ""          # 只有 Pillow：图片能转，音视频不行
        return True, ""

    def caps(self):
        return ("convert.engines", "convert.probe", "convert.file",
                "convert.audio", "convert.video", "convert.gif",
                "convert.frames", "convert.batch")

    def invoke(self, cap, args=None):
        args = args or {}
        try:
            if cap == "convert.engines":
                return self._engines()
            if cap == "convert.probe":
                return self._probe(args)
            if cap == "convert.file":
                return self._file(args)
            if cap == "convert.audio":
                return self._audio(args)
            if cap == "convert.video":
                return self._video(args)
            if cap == "convert.gif":
                return self._gif(args)
            if cap == "convert.frames":
                return self._frames(args)
            if cap == "convert.batch":
                return self._batch(args)
        except Exception as e:                  # 兜底：任何内部异常都变成可读错误
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return super().invoke(cap, args)

    # ---------------- 内部工具 ----------------

    def _src(self, args, cap, allow_dir=False):
        raw = (args.get("path") or args.get("src") or "").strip().strip('"')
        if not raw:
            return None, R.error(cap, "缺 path（要转的文件）", device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(raw))
        if allow_dir and os.path.isdir(p):
            return p, None
        if not os.path.isfile(p):
            return None, R.error(cap, "文件不存在: %s" % p, device=self.name, via=self.via)
        return p, None

    def _out(self, src, args, ext):
        """定输出路径。返回 (路径, 错误)。

        用户给了 out 就用它；没给就放源文件旁边、同名换扩展名。
        **输出等于输入直接拒绝** —— 覆盖源文件是不可逆的，宁可多一步。
        """
        raw = (args.get("out") or args.get("dst") or "").strip().strip('"')
        if raw:
            out = os.path.abspath(os.path.expanduser(raw))
            if os.path.isdir(out):                       # 传了个目录：按原名+新扩展名放进去
                out = os.path.join(out, os.path.splitext(os.path.basename(src))[0] + "." + ext)
            elif not os.path.splitext(out)[1]:           # 像 "结果" 这样的裸名：补扩展名
                out += "." + ext
        else:
            out = os.path.splitext(src)[0] + "." + ext
        if os.path.normcase(out) == os.path.normcase(src):
            if raw:
                # 用户自己把输出指到了源文件：这是明确要覆盖，必须拦住
                return None, ("输出路径与源文件相同（%s）：拒绝覆盖原件，"
                              "请换 out 或换目标格式" % out)
            # 同容器重编码（「把这个 mp4 压小点」）：自动让到 rec_1.mp4，
            # 既不覆盖原件，也不用逼调用方凭空想一个输出名
            out = _unique(out)
        if not bool(args.get("overwrite") or False):
            out = _unique(out)
        parent = os.path.dirname(out)
        if parent:
            os.makedirs(parent, exist_ok=True)
        return out, None

    def _atomic(self, out, produce):
        """先写临时文件再原子改名。

        为什么：ffmpeg 中途失败会在目标路径上留一个几百字节的坏文件。
        调用方看到「文件存在」就当成功了，实际放不出来 —— 这种假成功最难查。
        临时文件仍带目标扩展名（ffmpeg 靠扩展名决定封装格式），成功后 os.replace。
        """
        stem, ext = os.path.splitext(out)
        tmp = "%s.part%d%s" % (stem, int(time.time() * 1000) % 100000, ext)
        try:
            rc, stdout, stderr = produce(tmp)
            if rc != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
                if os.path.isfile(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                return rc, stdout, (stderr or "").strip() or "产出为空"
            os.replace(tmp, out)
            return 0, stdout, stderr
        finally:
            for p in (tmp,):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # ---------------- convert.engines ----------------

    def _engines(self):
        rows = {}
        ff = _tool("ffmpeg")
        if ff:
            rc, out, _e = _run([ff, "-version"], timeout=15)
            ver = (out.splitlines() or ["?"])[0] if rc == 0 else "?"
            rows["ffmpeg"] = {"path": ff, "version": ver.strip()[:90],
                              "kinds": ["audio", "video", "image(兜底)"],
                              "note": "音视频转码主力；容器与编码器的搭配由本设备给默认值"}
        try:
            import PIL
            rows["pillow"] = {"version": getattr(PIL, "__version__", "?"),
                              "kinds": ["image"], "formats": list(IMAGE_FMT),
                              "note": "图片转换首选：画质/尺寸可控，不依赖外部二进制"}
        except Exception:
            pass
        try:
            import markitdown
            rows["markitdown"] = {"version": getattr(markitdown, "__version__", "?"),
                                  "kinds": ["doc→md/txt"], "formats": list(DOC_FMT),
                                  "note": "只出不进：抽文本，不能反向生成 docx"}
        except Exception:
            pass
        if _tool("7z"):
            rows["7z"] = {"path": _tool("7z"), "kinds": ["archive"],
                          "note": "压缩包打包/解包，见 archive.* 能力"}
        missing = {}
        if not _tool("pandoc"):
            missing["pandoc"] = "文档互转（md/html/docx/rst/LaTeX）：scoop install pandoc"
        if not _tool("soffice"):
            missing["libreoffice"] = "Office 格式互转 + 转 PDF：装 LibreOffice 后 soffice --headless"
        if not _tool("magick"):
            missing["imagemagick"] = "245 种图片格式互转（含 heic/psd/eps）：scoop install imagemagick"
        return R.success("convert.engines", self.name, via=self.via, data={
            "engines": rows,
            "missing": missing,
            "targets": {"image": list(IMAGE_FMT), "audio": list(AUDIO_FMT),
                        "video": list(VIDEO_FMT), "doc": list(DOC_FMT),
                        "archive": ["zip", "7z", "tar", "gz"]},
            "note": "按内容判类型，不信扩展名；调用方只说目标格式，引擎自动选",
        })

    # ---------------- convert.probe ----------------

    def _probe(self, args):
        p, err = self._src(args, "convert.probe")
        if err is not None:
            return err
        info = sniff(p)
        kind = info["kind"]
        targets = {"image": list(IMAGE_FMT), "audio": list(AUDIO_FMT),
                   "video": list(VIDEO_FMT), "doc": list(DOC_FMT),
                   "archive": [], "text": list(DOC_FMT)}.get(kind, [])
        # 视频额外能转成音频（抽音轨），这是最常用的一步
        if kind == "video":
            targets = list(VIDEO_FMT) + list(AUDIO_FMT)
        detail = {}
        fp = _tool("ffprobe")
        if fp and kind in ("audio", "video"):
            detail = _ffprobe(p, fp)
        ext_conflict = info["ext"] and info["fmt"] != info["ext"] and info["kind"] != "text"
        return R.success("convert.probe", self.name, via=self.via, data={
            "path": p, "kind": kind, "format": info["fmt"],
            "ext": info["ext"], "bytes": info["size"],
            "ext_mismatch": bool(ext_conflict),
            "note": ("扩展名是 .%s，内容其实是 %s（已按内容判定）" % (info["ext"], info["fmt"]))
                    if ext_conflict else "",
            "targets": targets,
            "detail": detail,
            "engines": _available_engines(),
        })

    # ---------------- convert.file（总入口） ----------------

    def _file(self, args):
        p, err = self._src(args, "convert.file")
        if err is not None:
            return err
        info = sniff(p)
        kind = info["kind"]
        want = _norm_ext(args.get("to") or args.get("format") or args.get("ext") or "")
        if not want or want == "auto":
            want = {"image": "jpg", "audio": "mp3", "video": "mp4",
                    "doc": "md", "text": "md", "archive": "zip"}.get(kind)
        if not want:
            return R.error("convert.file",
                           "认不出这是什么文件（%s）：请显式给 to=目标格式" % info["fmt"],
                           device=self.name, via=self.via)

        # 同格式且没给输出路径：要么是 auto 落到自家格式，要么是用户想重编码。
        # 两种都不该覆盖原件，但错误话必须说清是哪种，否则「拒绝覆盖」看起来像 bug。
        if want == info["fmt"] and not (args.get("out") or args.get("dst")):
            return R.error("convert.file",
                           "源文件已经是 %s，无需转换（没给 out 时不会覆盖原件）；"
                           "要重编码请给 out=输出路径，或换一个目标格式" % want,
                           device=self.name, via=self.via)

        # 视频/音频 → 音频扩展名：抽音轨，走音频通道（-vn）
        if kind in ("video", "audio") and want in _AUDIO_LIKE:
            merged = dict(args)
            merged.setdefault("to", want)
            return self._audio(merged, cap="convert.file")

        if kind == "image":
            return self._image(p, want, args)
        if kind in ("audio", "video"):
            if want == "gif":
                merged = dict(args)
                merged.setdefault("to", "gif")
                return self._gif(merged, cap="convert.file")
            if want not in VIDEO_FMT:
                suggestions = _suggest(want, VIDEO_FMT)
                return R.error("convert.file", "视频不能转成 %s%s（支持 %s）"
                               % (want, suggestions, " / ".join(VIDEO_FMT)),
                               device=self.name, via=self.via)
            merged = dict(args)
            merged.setdefault("to", want)
            return self._video(merged, cap="convert.file")
        if kind in ("doc", "text"):
            return self._doc(p, want, args)
        if kind == "archive":
            return R.error("convert.file",
                           "压缩包不走格式转换，请用 archive.extract / archive.pack",
                           device=self.name, via=self.via)
        return R.error("convert.file", "不支持的类型：%s" % kind,
                       device=self.name, via=self.via)

    # ---------------- 图片 ----------------

    def _image(self, p, want, args):
        try:
            from PIL import Image
        except Exception:
            return self._ff_simple(p, want, args, "convert.file", vf=None)
        if want not in IMAGE_FMT:
            return R.error("convert.file", "图片不能转成 %s%s（支持 %s）"
                           % (want, _suggest(want, IMAGE_FMT), " / ".join(IMAGE_FMT)),
                           device=self.name, via=self.via)
        out, err = self._out(p, args, want)
        if err is not None:
            return R.error("convert.file", err, device=self.name, via=self.via)
        quality = int(args.get("quality") or 90)
        try:
            im = Image.open(p)
            im.load()
            src_size = list(im.size)
            fmt = {"jpg": "JPEG", "tiff": "TIFF"}.get(want, want.upper())
            # JPEG 不认 RGBA/调色板：不转 RGB 会直接抛 "cannot write mode RGBA as JPEG"
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            # ICO 有尺寸上限（256），超了 Pillow 直接报错，先夹一下
            if fmt == "ICO" and max(im.size) > 256:
                im.thumbnail((256, 256), Image.LANCZOS)
            save_kw = {}
            if fmt in ("JPEG", "WEBP"):
                save_kw["quality"] = max(1, min(quality, 100))
            if fmt == "JPEG":
                save_kw["optimize"] = True
            if fmt == "GIF":
                save_kw["optimize"] = True
            im.save(out, fmt, **save_kw)
            im.close()
        except Exception as e:
            return R.error("convert.file", "图片转换失败：%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return R.success("convert.file", self.name, via=self.via, data={
            "src": p, "out": out, "from": "image", "to": want,
            "src_bytes": os.path.getsize(p), "bytes": os.path.getsize(out),
            "size": src_size, "engine": "Pillow",
        })

    def _ff_simple(self, p, want, args, cap, vf=None):
        """没有 Pillow 时的图片兜底：交给 ffmpeg。"""
        ff = _tool("ffmpeg")
        if not ff:
            return R.error(cap, "既没有 Pillow 也没有 ffmpeg，转不了图",
                           device=self.name, via=self.via)
        out, err = self._out(p, args, want)
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", p, "-frames:v", "1"]
        if vf:
            cmd += ["-vf", vf]
        cmd += [out]
        rc, _o, e = self._atomic(out, lambda tmp: _run(cmd[:-1] + [tmp], timeout=int(args.get("timeout") or 120)))
        if rc != 0:
            return R.error(cap, "ffmpeg 转图失败：%s" % _tail(e),
                           device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": out, "to": want, "bytes": os.path.getsize(out),
            "engine": "ffmpeg",
        })

    # ---------------- 音频 ----------------

    def _audio(self, args, cap="convert.audio"):
        p, err = self._src(args, cap)
        if err is not None:
            return err
        ff = _tool("ffmpeg")
        if not ff:
            return R.error(cap, "没装 ffmpeg（音视频转码必需）",
                           device=self.name, via=self.via)
        want = _norm_ext(args.get("to") or args.get("format") or "mp3")
        if want not in AUDIO_FMT:
            return R.error(cap, "音频不能转成 %s%s（支持 %s）"
                           % (want, _suggest(want, AUDIO_FMT), " / ".join(AUDIO_FMT)),
                           device=self.name, via=self.via)
        out, err = self._out(p, args, want)
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        # 无声视频转音频：ffmpeg 只会回一句 "Output file does not contain any stream"，
        # 用户完全不知道是「没音轨」还是「参数错了」。先自己判一次，把话说清楚。
        fp = _tool("ffprobe")
        if fp and sniff(p)["kind"] == "video":
            info = _ffprobe(p, fp)
            if info and "audio" not in info:
                return R.error(cap, "源视频没有音轨（只有画面），抽不出音频；"
                                    "要图请用 convert.frames", device=self.name, via=self.via)

        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", p, "-vn"]                     # -vn：源是视频时把画面丢掉
        cmd += _AUDIO_ARGS.get(want, [])
        br = int(args.get("bitrate_kbps") or 0)
        if br and want not in ("wav", "flac"):
            cmd += ["-b:a", "%dk" % br]
        elif want in ("mp3", "aac", "m4a") and not br:
            cmd += ["-b:a", "192k"]                # 默认值：128k 听起来发闷，320k 对语音浪费
        rate = int(args.get("rate") or 0)
        if rate:
            cmd += ["-ar", str(rate)]
        ch = int(args.get("channels") or 0)
        if ch:
            cmd += ["-ac", str(ch)]
        if args.get("normalize"):
            # 单遍 loudnorm：响度拉到 -16 LUFS（播客/视频平台通用），够用且不要两遍
            cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        if args.get("start") or args.get("duration"):
            if args.get("start"):
                cmd += ["-ss", str(args["start"])]
            if args.get("duration"):
                cmd += ["-t", str(args["duration"])]

        t0 = time.time()
        rc, _o, e = self._atomic(out, lambda tmp: _run(
            cmd + [tmp], timeout=int(args.get("timeout") or 1800)))
        if rc != 0:
            return R.error(cap, "ffmpeg 音频转换失败：%s" % _tail(e),
                           device=self.name, via=self.via)
        data = {"src": p, "out": out, "to": want, "bytes": os.path.getsize(out),
                "src_bytes": os.path.getsize(p), "engine": "ffmpeg",
                "ms": int((time.time() - t0) * 1000)}
        fp = _tool("ffprobe")
        if fp:
            info = _ffprobe(out, fp)
            if info.get("duration_s"):
                data["duration_s"] = info["duration_s"]
        return R.success(cap, self.name, via=self.via, data=data)

    # ---------------- 视频 ----------------

    def _video(self, args, cap="convert.video"):
        p, err = self._src(args, cap)
        if err is not None:
            return err
        ff = _tool("ffmpeg")
        if not ff:
            return R.error(cap, "没装 ffmpeg（视频转码必需）",
                           device=self.name, via=self.via)
        want = _norm_ext(args.get("to") or args.get("format") or "mp4")
        if want not in VIDEO_FMT:
            return R.error(cap, "视频不能转成 %s%s（支持 %s）"
                           % (want, _suggest(want, VIDEO_FMT), " / ".join(VIDEO_FMT)),
                           device=self.name, via=self.via)
        out, err = self._out(p, args, want)
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)

        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", p]
        if args.get("start"):
            cmd += ["-ss", str(args["start"])]
        if args.get("duration"):
            cmd += ["-t", str(args["duration"])]

        copy_mode = bool(args.get("copy") or False)
        if copy_mode:
            # 只换封装不重编码：秒级完成、无损，但容器必须真能装下原编码。
            # 装不下 ffmpeg 会失败，那时应退回重编码而不是硬塞。
            cmd += ["-c", "copy"]
            if want in ("mp4", "mov"):
                cmd += ["-movflags", "+faststart"]
        else:
            crf = int(args.get("crf") or 23)
            preset = (args.get("preset") or "medium").strip()
            codec_args = list(_VIDEO_ARGS.get(want, _VIDEO_ARGS["mp4"]))
            if want == "webm":
                codec_args = ["-c:v", "libvpx-vp9", "-crf", str(max(0, min(crf, 63))),
                              "-b:v", "0", "-c:a", "libopus", "-b:a", "96k"]
            else:
                codec_args += ["-preset", preset, "-crf", str(max(0, min(crf, 51)))]
            cmd += codec_args
            vf = []
            scale = str(args.get("scale") or "").strip()
            if scale:
                if re.fullmatch(r"\d+", scale):
                    # 只给高度：宽度写 -2 而不是 -1。
                    # yuv420p 要求宽高都是偶数，-1 会算出奇数宽，编码直接失败。
                    vf.append("scale=-2:%s" % scale)
                elif re.fullmatch(r"\d+x\d+", scale.lower()):
                    vf.append("scale=%s" % scale.lower())
                else:
                    return R.error(cap, "scale 只认 `720` 或 `1280x720`，收到 %r" % scale,
                                   device=self.name, via=self.via)
            fps = args.get("fps")
            if fps:
                vf.append("fps=%s" % fps)
            if vf:
                cmd += ["-vf", ",".join(vf)]
            if args.get("no_audio"):
                cmd += ["-an"]
            else:
                br = int(args.get("bitrate_kbps") or 0)
                if br:
                    cmd += ["-b:a", "%dk" % br]

        t0 = time.time()
        rc, _o, e = self._atomic(out, lambda tmp: _run(
            cmd + [tmp], timeout=int(args.get("timeout") or 3600)))
        if rc != 0:
            hint = ""
            if copy_mode:
                hint = "（copy 模式只换封装，装不下原编码时去掉 copy 重编码）"
            return R.error(cap, "ffmpeg 视频转换失败%s：%s" % (hint, _tail(e)),
                           device=self.name, via=self.via)
        data = {"src": p, "out": out, "to": want, "bytes": os.path.getsize(out),
                "src_bytes": os.path.getsize(p), "engine": "ffmpeg",
                "copy": copy_mode, "ms": int((time.time() - t0) * 1000)}
        fp = _tool("ffprobe")
        if fp:
            info = _ffprobe(out, fp)
            data.update({k: v for k, v in info.items() if k in ("duration_s", "video")})
        return R.success(cap, self.name, via=self.via, data=data)

    # ---------------- GIF ----------------

    def _gif(self, args, cap="convert.gif"):
        """视频 → GIF。必须走调色板两遍，否则灰蒙蒙一层噪点。

        单靠 `-vf fps=10,scale=480:-1` 出来的 GIF 只有 256 色且是固定调色板，
        暗部全是色带。业界做法是 palettegen 统计这一部片子的实际用色，
        再用 paletteuse 套上去 —— ffmpeg 能在一个 filtergraph 里做完。
        """
        p, err = self._src(args, cap)
        if err is not None:
            return err
        ff = _tool("ffmpeg")
        if not ff:
            return R.error(cap, "没装 ffmpeg", device=self.name, via=self.via)
        out, err = self._out(p, args, "gif")
        if err is not None:
            return R.error(cap, err, device=self.name, via=self.via)
        fps = int(args.get("fps") or 10)
        width = int(args.get("width") or 480)
        width = max(64, min(width, 1280))
        dither = (args.get("dither") or "sierra2_4a").strip()
        vf = ("fps=%d,scale=%d:-1:flags=lanczos,split[s0][s1];"
              "[s0]palettegen=max_colors=256:stats_mode=diff[p];"
              "[s1][p]paletteuse=dither=%s:diff_mode=rectangle" % (fps, width, dither))
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if args.get("start"):
            cmd += ["-ss", str(args["start"])]
        cmd += ["-i", p]
        if args.get("duration"):
            cmd += ["-t", str(args["duration"])]
        cmd += ["-vf", vf, "-loop", "0" if args.get("loop", True) else "-1"]

        t0 = time.time()
        rc, _o, e = self._atomic(out, lambda tmp: _run(
            cmd + [tmp], timeout=int(args.get("timeout") or 1800)))
        if rc != 0:
            return R.error(cap, "转 GIF 失败：%s" % _tail(e), device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out": out, "to": "gif", "fps": fps, "width": width,
            "bytes": os.path.getsize(out), "src_bytes": os.path.getsize(p),
            "ratio": round(os.path.getsize(out) / max(1, os.path.getsize(p)), 3),
            "engine": "ffmpeg palettegen/paletteuse",
            "ms": int((time.time() - t0) * 1000),
        })

    # ---------------- 抽帧 ----------------

    def _frames(self, args, cap="convert.frames"):
        """视频 → 均匀抽 N 帧图。做封面、故事板、给模型看内容都用它。

        均匀抽的算法：先读时长，再让 fps = N/时长。用 `-vf fps=` 而不是
        `select=not(mod(n,k))`，因为后者要事先知道总帧数，而 VFR 视频的总帧数
        不可靠（手机上拍的视频大量是 VFR）。
        """
        p, err = self._src(args, cap)
        if err is not None:
            return err
        ff, fp = _tool("ffmpeg"), _tool("ffprobe")
        if not ff:
            return R.error(cap, "没装 ffmpeg", device=self.name, via=self.via)
        count = max(1, min(int(args.get("count") or 6), 60))
        fmt = _norm_ext(args.get("fmt") or "jpg")
        if fmt not in ("jpg", "png", "webp"):
            return R.error(cap, "抽帧只出 jpg/png/webp，收到 %s" % fmt,
                           device=self.name, via=self.via)
        dur = 0.0
        if fp:
            info = _ffprobe(p, fp)
            dur = float(info.get("duration_s") or 0)
        raw_dir = (args.get("out_dir") or "").strip().strip('"')
        if raw_dir:
            out_dir = os.path.abspath(os.path.expanduser(raw_dir))
        else:
            out_dir = os.path.splitext(p)[0] + "_frames"
        os.makedirs(out_dir, exist_ok=True)

        vf = []
        if args.get("start"):
            vf.append("trim=start=%s" % args["start"])
        if dur > 0:
            vf.append("fps=%f" % (count / dur))
        vf.append("scale=-2:%d" % int(args.get("height") or 720))
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-i", p, "-vf", ",".join(vf),
               "-frames:v", str(count), "-q:v", "3",
               os.path.join(out_dir, "f%03d." + fmt)]
        t0 = time.time()
        rc, _o, e = _run(cmd, timeout=int(args.get("timeout") or 900))
        if rc != 0:
            return R.error(cap, "抽帧失败：%s" % _tail(e), device=self.name, via=self.via)
        files = sorted(os.path.join(out_dir, n) for n in os.listdir(out_dir)
                       if n.startswith("f") and n.lower().endswith("." + fmt))
        if not files:
            return R.error(cap, "ffmpeg 说成功但一帧都没产出（时长解析成 0？）",
                           device=self.name, via=self.via)
        return R.success(cap, self.name, via=self.via, data={
            "src": p, "out_dir": out_dir, "count": len(files),
            "files": files[:12], "duration_s": dur, "fmt": fmt,
            "ms": int((time.time() - t0) * 1000),
        })

    # ---------------- 文档 ----------------

    def _doc(self, p, want, args):
        """文档：MarkItDown 抽文本（只出不进）。

        为什么不能「转成 docx/pdf」：那需要 pandoc 或 LibreOffice，本机都没有。
        能装但我不擅自装 —— 缺就明说缺什么、一条命令怎么补，比假装支持好。
        """
        if want not in DOC_FMT:
            msg = ("文档方向只能出 md/txt（MarkItDown 是单向抽取）。"
                   "要出 %s 得装 pandoc（md/html/docx 互转）或 LibreOffice（Office/PDF）"
                   % want)
            if not _tool("pandoc"):
                msg += "；当前 pandoc 未安装：scoop install pandoc"
            return R.error("convert.file", msg, device=self.name, via=self.via)
        try:
            from markitdown import MarkItDown
        except Exception:
            return R.error("convert.file",
                           "没装 markitdown：pip install 'markitdown[pdf,docx,pptx,xlsx]'",
                           device=self.name, via=self.via)
        out, err = self._out(p, args, want)
        if err is not None:
            return R.error("convert.file", err, device=self.name, via=self.via)
        try:
            res = MarkItDown().convert(p)
            text = res.text_content or ""
        except Exception as e:
            return R.error("convert.file", "抽取失败：%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        if not text.strip():
            return R.error("convert.file",
                           "抽不到文字：可能是扫描版 PDF（无文字层），需要 OCR",
                           device=self.name, via=self.via)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return R.success("convert.file", self.name, via=self.via, data={
            "src": p, "out": out, "from": "doc", "to": want,
            "chars": len(text), "bytes": os.path.getsize(out),
            "engine": "MarkItDown",
        })

    # ---------------- 批量 ----------------

    def _batch(self, args):
        """目录批量转换。默认预演，`dry_run=False` 才真干。

        为什么要预演默认开：批量转 500 张图，参数错一次就是 500 个坏文件
        （虽然不覆盖原件，但白占空间、还得挨个删）。先看计划再做，成本一分钟。
        """
        import fnmatch
        # 批量入口的源是目录，参数名叫 dir；_src 只认 path/src，这里显式接一下
        merged = dict(args)
        if merged.get("dir") and not merged.get("path"):
            merged["path"] = merged["dir"]
        root, err = self._src(merged, "convert.batch", allow_dir=True)
        if err is not None:
            return err
        if not os.path.isdir(root):
            return R.error("convert.batch", "不是目录: %s" % root,
                           device=self.name, via=self.via)
        pattern = (args.get("pattern") or "*").strip()
        want = _norm_ext(args.get("to") or "")
        if not want:
            return R.error("convert.batch", "缺 to（目标格式）",
                           device=self.name, via=self.via)
        recursive = bool(args.get("recursive"))
        limit = max(1, min(int(args.get("limit") or 200), 5000))
        dry = args.get("dry_run")
        dry = True if dry is None else bool(dry)
        raw_out = (args.get("out_dir") or "").strip().strip('"')
        out_dir = os.path.abspath(os.path.expanduser(raw_out)) if raw_out \
            else os.path.join(root, "_" + want)

        files = []
        walker = os.walk(root) if recursive else [(root, [], os.listdir(root))]
        for dirpath, _dirs, names in walker:
            for n in sorted(names):
                full = os.path.join(dirpath, n)
                if os.path.isfile(full) and fnmatch.fnmatch(n.lower(), pattern.lower()):
                    files.append(full)
        files.sort()
        truncated = len(files) > limit
        files = files[:limit]
        if not files:
            return R.unavailable("convert.batch",
                                 "目录里没有匹配 %s 的文件" % pattern,
                                 device=self.name, via=self.via)

        plan, done, skipped, failed = [], 0, [], []
        for f in files:
            info = sniff(f)
            if info["kind"] not in ("image", "audio", "video", "text"):
                skipped.append({"src": f, "why": "%s 不支持批转" % info["kind"]})
                continue
            if info["fmt"] == want:
                skipped.append({"src": f, "why": "已经是 %s" % want})
                continue
            dest = os.path.join(out_dir, os.path.splitext(os.path.basename(f))[0] + "." + want)
            plan.append({"src": f, "out": _unique(dest) if not dry else dest})
            if dry:
                continue
            r = self._file({"path": f, "to": want, "out": dest,
                            "overwrite": args.get("overwrite"),
                            "quality": args.get("quality"),
                            "crf": args.get("crf"), "scale": args.get("scale"),
                            "bitrate_kbps": args.get("bitrate_kbps")})
            if r.ok:
                done += 1
            else:
                failed.append({"src": f, "err": r.err})
        return R.success("convert.batch", self.name, via=self.via, data={
            "dir": root, "to": want, "out_dir": out_dir,
            "matched": len(files), "done": done, "dry_run": dry,
            "truncated": truncated,
            "skipped": skipped[:10], "skipped_count": len(skipped),
            "failed": failed[:10], "failed_count": len(failed),
            "plan": plan[:20],
            "note": "dry_run=True：只列计划，没动文件" if dry else "",
        })


# ---------------------------------------------------------------- 自测

def _selftest():
    """离线自测：用 ffmpeg 自己生成素材，不依赖仓库里任何样例文件。"""
    fails = []
    counted = [0]

    def ck(cond, msg):
        counted[0] += 1
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    ff = _tool("ffmpeg")
    if not ff:
        print("跳过：没装 ffmpeg")
        return 0

    dev = ConvertDevice()
    td = tempfile.mkdtemp(prefix="convert_st_")
    try:
        # ---- 素材：1 秒 320x240 视频（带音轨）+ 1 秒正弦波 + 两张 png ----
        src_mp4 = os.path.join(td, "v.mp4")
        rc, _o, e = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                          "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1",
                          "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                          "-t", "1", "-shortest", src_mp4], 120)
        ck(rc == 0 and os.path.getsize(src_mp4) > 0, "素材：造出 1 秒测试视频（带音轨）")

        # 无声视频：用来验证「抽不出音频」时报的是人话而不是 ffmpeg 天书
        silent_mp4 = os.path.join(td, "silent.mp4")
        rc, _o, _e = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1", silent_mp4], 120)
        ck(rc == 0, "素材：造出无声视频")

        src_wav = os.path.join(td, "a.wav")
        rc, _o, _e = _run([ff, "-hide_banner", "-loglevel", "error", "-y",
                           "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                           "-c:a", "pcm_s16le", src_wav], 120)
        ck(rc == 0, "素材：造出 1 秒正弦波 wav")

        try:
            from PIL import Image
            png = os.path.join(td, "p.png")
            Image.new("RGB", (120, 80), (200, 30, 60)).save(png)
            png2 = os.path.join(td, "p2.png")
            Image.new("RGB", (60, 40), (10, 200, 90)).save(png2)
            has_pil = True
        except Exception:
            has_pil, png, png2 = False, None, None
        ck(has_pil, "素材：Pillow 可用（图片路径才测得了）")

        # ---- 嗅探：按内容而不是扩展名 ----
        if has_pil:
            disguised = os.path.join(td, "mystery.dat")
            shutil.copyfile(png, disguised)
            info = sniff(disguised)
            ck(info["kind"] == "image" and info["fmt"] == "png",
               "嗅探：.dat 后缀的 PNG 认成 image/png")
            r = dev.invoke("convert.probe", {"path": disguised})
            ck(r.ok and r.data["ext_mismatch"] is True, "probe：报出扩展名与内容不符")
        ck(sniff(src_mp4)["kind"] == "video", "嗅探：mp4 认成 video")
        ck(sniff(src_wav)["fmt"] == "wav", "嗅探：wav 认成 wav")

        # ---- 图片转换 ----
        if has_pil:
            r = dev.invoke("convert.file", {"path": png, "to": "jpg"})
            ck(r.ok and os.path.getsize(r.data["out"]) > 0, "图片：png → jpg")
            if r.ok:
                im = Image.open(r.data["out"])
                ck(im.size == (120, 80) and im.format == "JPEG", "图片：尺寸不变、格式真是 JPEG")
                im.close()
            # 不覆盖：再转一次同一个源，应产出另一个文件
            r2 = dev.invoke("convert.file", {"path": png, "to": "jpg"})
            ck(r2.ok and r2.data["out"] != r.data["out"], "图片：同名自动加序号，不覆盖")
            # 拒绝覆盖原件
            bad = dev.invoke("convert.file", {"path": png, "to": "png"})
            ck((not bad.ok) and ("已经是 png" in (bad.err or "")),
               "图片：同格式不覆盖原件，并说明原因")

        # ---- 音频 ----
        r = dev.invoke("convert.audio", {"path": src_wav, "to": "mp3"})
        ck(r.ok and r.data["to"] == "mp3" and os.path.getsize(r.data["out"]) > 0,
           "音频：wav → mp3")
        if r.ok and _tool("ffprobe"):
            ck(r.data.get("duration_s", 0) > 0.5, "音频：产物时长接近 1 秒")

        # ---- 视频 → 音频（抽音轨） ----
        r = dev.invoke("convert.file", {"path": src_mp4, "to": "mp3"})
        ck(r.ok and r.data["to"] == "mp3", "跨类：视频直接转 mp3（自动抽音轨）")
        r = dev.invoke("convert.file", {"path": silent_mp4, "to": "mp3"})
        ck((not r.ok) and "没有音轨" in (r.err or ""),
           "跨类：无声视频转音频时说明「没有音轨」")

        # ---- 视频转换（copy 模式，秒级） ----
        r = dev.invoke("convert.video", {"path": src_mp4, "to": "mkv", "copy": True})
        ck(r.ok and r.data["to"] == "mkv", "视频：mp4 → mkv（copy 只换封装）")

        # ---- 视频重编码 + 缩放（偶数宽度那条坑） ----
        r = dev.invoke("convert.video", {"path": src_mp4, "to": "mkv",
                                        "scale": "120", "crf": 35, "preset": "ultrafast"})
        ck(r.ok, "视频：重编码 + scale=120（-2 保证偶数宽）")
        if r.ok:
            ck((r.data.get("video") or {}).get("height") == 120, "视频：产物高度真是 120")

        # ---- 同容器重编码：自动改名，不覆盖原件 ----
        r = dev.invoke("convert.video", {"path": src_mp4, "to": "mp4",
                                        "crf": 35, "preset": "ultrafast"})
        ck(r.ok and r.data["out"] != src_mp4, "视频：同容器重编码自动让到 _1，不覆盖原件")

        # ---- GIF ----
        r = dev.invoke("convert.gif", {"path": src_mp4, "fps": 5, "width": 160})
        ck(r.ok and os.path.getsize(r.data["out"]) > 0, "GIF：视频 → gif（调色板两遍）")

        # ---- 抽帧 ----
        r = dev.invoke("convert.frames", {"path": src_mp4, "count": 3, "fmt": "jpg",
                                          "height": 120})
        ck(r.ok and r.data["count"] == 3, "抽帧：1 秒视频均匀抽 3 帧")

        # ---- 错误信息有没有用 ----
        bad = dev.invoke("convert.video", {"path": src_mp4, "to": "mp5"})
        ck((not bad.ok) and "mp4" in (bad.err or ""), "报错：写错目标格式会提示正确的那个")
        # 回归：DeviceResult 的 __bool__ 返回 ok，`if err:` 对失败结果恒为假，
        # 代码会带着 p=None 继续跑。这类反向条件只有错误路径才暴露，必须钉住。
        bad = dev.invoke("convert.file", {"path": os.path.join(td, "nope.png"), "to": "jpg"})
        ck((not bad.ok) and "文件不存在" in (bad.err or ""),
           "报错：文件不存在时明说，不带着 None 往下跑")

        # ---- 批量（默认预演，不落盘） ----
        if has_pil:
            r = dev.invoke("convert.batch", {"dir": td, "pattern": "*.png", "to": "webp"})
            ck(r.ok and r.data["dry_run"] is True and r.data["done"] == 0,
               "批量：默认 dry_run 只列计划")
        r = dev.invoke("convert.batch", {"dir": td, "pattern": "*.wav", "to": "mp3",
                                         "dry_run": False})
        ck(r.ok and r.data["done"] == 1, "批量：dry_run=False 真转了一个")

        # ---- 引擎清单 ----
        r = dev.invoke("convert.engines", {})
        ck(r.ok and "ffmpeg" in r.data["engines"], "引擎：清单里如实列出 ffmpeg")
        ck(r.ok and isinstance(r.data["missing"], dict), "引擎：缺什么也一并列出")
    finally:
        shutil.rmtree(td, ignore_errors=True)

    total = counted[0]
    if fails:
        print("convert selftest: %d/%d（失败 %d）" % (total - len(fails), total, len(fails)))
        return 1
    print("convert selftest: %d/%d" % (total, total))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python -m fleet.kinds.convert --selftest")
