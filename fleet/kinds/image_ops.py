"""图片操作设备：看尺寸 / 等比缩放，接进总线。

为什么单独一台（主人 2026-11-03「内容生成还是太少了」）：
  内容生成原来只有「出图/出视频/出声音」，**没有一条能处理已经生成出来的图**——
  尺寸太大传不进上下文、要缩略图给模型看，全得靠外部工具。

🔴 为什么先找开源实现而不是自己写（主人同一天的原话）：
  「尽量去找开源项目，不要直接硬造，这个有比你硬造的更好的。」
  JPEG 解码/重采样自己写就是给自己挖坑（色度子采样、EXIF 旋转、渐进式）。
  本机已有 **ffmpeg**（G:\\dev\\ffmpeg\\bin）——它是这件事的事实标准，直接调。
  没有 ffmpeg 时退回 `fleet/imgscale.py` 的纯 Python PNG 路径（至少截图那条路不断）。

能力：
  image.info    只读：宽/高/格式/字节（ffprobe 优先，PNG 用纯 Python 兜底）
  image.resize  写文件：等比缩到 max_w×max_h 以内（ffmpeg scale，PNG 可无 ffmpeg）
"""
import json
import os
import shutil
import subprocess

from ..core import BaseDevice, DeviceResult as R
from .. import imgscale


def _exe(name):
    return shutil.which(name)


class ImageOps(BaseDevice):
    kind = "imageops"
    via = "local"

    def __init__(self, name="image:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "creative")

    def probe(self):
        return True, ""          # ffmpeg 不在也能干 PNG，只是格式少一点

    def caps(self):
        return ("image.info", "image.resize")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "image.info":
            return self._info(args)
        if cap == "image.resize":
            return self._resize(args)
        return super().invoke(cap, args)

    # ---------------- 内部 ----------------

    def _path(self, args, cap):
        path = (args.get("path") or "").strip().strip('"')
        if not path:
            return None, R.error(cap, "缺 path", device=self.name, via=self.via)
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(p):
            return None, R.error(cap, "文件不存在: %s" % p, device=self.name, via=self.via)
        return p, None

    def _probe_meta(self, p):
        """ffprobe 读宽高/格式（覆盖 jpg/png/webp/gif）；没有 ffprobe 就只认 PNG。"""
        fp = _exe("ffprobe")
        if fp:
            try:
                out = subprocess.run(
                    [fp, "-v", "quiet", "-print_format", "json",
                     "-show_streams", "-select_streams", "v:0", p],
                    capture_output=True, timeout=20)
                data = json.loads(out.stdout.decode("utf-8", "ignore") or "{}")
                st = (data.get("streams") or [{}])[0]
                if st.get("width"):
                    return {"format": st.get("codec_name") or "?",
                            "width": int(st["width"]), "height": int(st["height"])}
            except Exception:
                pass
        w, h, ch, _px = imgscale.read_png(p)      # 非 PNG 会在这抛，由调用方接住
        return {"format": "png", "width": w, "height": h, "channels": ch}

    # ---------------- 只读 ----------------

    def _info(self, args):
        p, err = self._path(args, "image.info")
        if err:
            return err
        try:
            meta = self._probe_meta(p)
        except Exception as e:
            return R.error("image.info", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        data = dict(meta)
        data.update({"path": p, "bytes": os.path.getsize(p),
                     "pixels": meta["width"] * meta["height"]})
        return R.success("image.info", self.name, via=self.via, data=data)

    # ---------------- 写文件（副作用） ----------------

    def _resize(self, args):
        p, err = self._path(args, "image.resize")
        if err:
            return err
        max_w = max(16, min(int(args.get("max_w") or 720), 8192))
        max_h = max(16, min(int(args.get("max_h") or 1600), 8192))
        out = (args.get("out") or "").strip()
        if not out:
            root, ext = os.path.splitext(p)
            out = "%s_%dx%d%s" % (root, max_w, max_h, ext or ".png")
        out = os.path.abspath(os.path.expanduser(out))
        os.makedirs(os.path.dirname(out), exist_ok=True)

        ff = _exe("ffmpeg")
        if ff:
            # 只缩不放（force_original_aspect_ratio=decrease + min），并强制偶数边长
            vf = ("scale='min(iw,%d)':'min(ih,%d)':force_original_aspect_ratio=decrease,"
                  "scale=trunc(iw/2)*2:trunc(ih/2)*2" % (max_w, max_h))
            try:
                r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", p,
                                    "-vf", vf, out],
                                   capture_output=True, timeout=120)
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.decode("utf-8", "ignore")[:300])
                meta = self._probe_meta(out)
                return R.success("image.resize", self.name, via=self.via, data={
                    "src": p, "out": out, "width": meta["width"], "height": meta["height"],
                    "bytes": os.path.getsize(out), "engine": "ffmpeg",
                })
            except Exception as e:
                # ffmpeg 失败（极少）：PNG 还能走纯 Python，其它格式如实报错
                if not p.lower().endswith(".png"):
                    return R.error("image.resize", "ffmpeg 缩放失败: %s" % e,
                                   device=self.name, via=self.via)

        try:
            nw, nh, size = imgscale.downscale(p, out, max_w=max_w, max_h=max_h)
        except Exception as e:
            return R.error("image.resize", "无 ffmpeg 且不是可解的 PNG: %s" % e,
                           device=self.name, via=self.via)
        return R.success("image.resize", self.name, via=self.via, data={
            "src": p, "out": out, "width": nw, "height": nh, "bytes": size,
            "engine": "imgscale（纯 Python，最近邻）",
        })
