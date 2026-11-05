"""图片操作设备：看尺寸 / 缩放 / 转格式 / 压体积 / 裁剪 / 缩略图 / 水印 / 拼接 / 批处理。

为什么单独一台（主人 2026-11-03「内容生成还是太少了」）：
  内容生成原来只有「出图/出视频/出声音」，**没有一条能处理已经生成出来的图**——
  尺寸太大传不进上下文、要缩略图给模型看，全得靠外部工具。

🔴 为什么先找开源实现而不是自己写（主人同一天的原话）：
  「尽量去找开源项目，不要直接硬造，这个有比你硬造的更好的。」
  JPEG 解码/重采样自己写就是给自己挖坑（色度子采样、EXIF 旋转、渐进式）。
  所以两条腿：**Pillow**（requirements 里的既有依赖，裁/拼/水印/压体积用它）
  与 **ffmpeg**（G:\\dev\\ffmpeg\\bin，转格式用它最稳）。
  两者都不在时退回 `fleet/imgscale.py` 的纯 Python PNG 路径（至少截图那条路不断）。

能力：
  image.info       只读：宽/高/格式/字节（ffprobe 优先，PNG 用纯 Python 兜底）
  image.resize     写：等比缩到 max_w×max_h 以内（ffmpeg scale，PNG 可无 ffmpeg）
  image.convert    写：转格式（png/jpg/webp/bmp/gif/tiff）
  image.compress   写：压到目标体积（JPEG/WebP 二分 quality，PNG 走降色+优化）
  image.crop       写：按像素框裁剪
  image.thumb      写：正方形缩略图（居中 cover 裁切）
  image.watermark  写：文字水印（九宫格定位 + 透明度 + 自动字号）
  image.stitch     写：多图拼接（纵向长图 / 横向），统一宽度对齐
  image.batch      写：目录批量（转格式 / 改尺寸 / 压体积），**不覆盖源文件**

2026-11-06 新增后八项里的后七项：此前只会「看尺寸」和「缩放」，
转个格式都得开外部工具 —— 这是「别人都有、我们没有」的典型缺口。
"""
import json
import os
import shutil
import subprocess

from ..core import BaseDevice, DeviceResult as R
from .. import imgscale

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:              # 冻扁包漏收时不许整台设备消失：如实报不可用
    _HAS_PIL = False


#: 扩展名 -> Pillow 格式名。jpeg 与 jpg 归一到同一个，免得写出 `.jpeg` 又写 `.jpg`
_FMT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP",
        "bmp": "BMP", "gif": "GIF", "tif": "TIFF", "tiff": "TIFF"}
_EXT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
        "BMP": ".bmp", "GIF": ".gif", "TIFF": ".tif"}

#: 九宫格定位 -> (水平权重, 垂直权重)，0=左/上，1=中，2=右/下
_ANCHOR = {"tl": (0, 0), "tc": (1, 0), "tr": (2, 0),
           "ml": (0, 1), "mc": (1, 1), "mr": (2, 1),
           "bl": (0, 2), "bc": (1, 2), "br": (2, 2),
           "lt": (0, 0), "ct": (1, 0), "rt": (2, 0),
           "lm": (0, 1), "cm": (1, 1), "rm": (2, 1),
           "lb": (0, 2), "cb": (1, 2), "rb": (2, 2)}

#: 中文字体候选（msyh 是 Win10+ 自带的微软雅黑；缺了才退 simhei/simsun）
_FONTS = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
          r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
          r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf")


def _exe(name):
    return shutil.which(name)


def _fmt_of(word, default="JPEG"):
    """`jpg` / `.JPG` / `image/jpeg` 都收，统一成 Pillow 的格式名。"""
    w = (word or "").strip().lower().lstrip(".").split("/")[-1]
    return _FMT.get(w, "")


def _font(size):
    for p in _FONTS:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


class ImageOps(BaseDevice):
    kind = "imageops"
    via = "local"

    def __init__(self, name="image:ops", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "creative")

    def probe(self):
        if not _HAS_PIL and not _exe("ffmpeg"):
            return False, "Pillow 与 ffmpeg 都没有：装 Pillow，或把 ffmpeg 放进 PATH"
        return True, ""          # ffmpeg 不在也能干 PNG，只是格式少一点

    def caps(self):
        return ("image.info", "image.resize", "image.convert", "image.compress",
                "image.crop", "image.thumb", "image.watermark", "image.stitch",
                "image.batch")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "image.info":
            return self._info(args)
        if cap == "image.resize":
            return self._resize(args)
        if cap == "image.convert":
            return self._convert(args)
        if cap == "image.compress":
            return self._compress(args)
        if cap == "image.crop":
            return self._crop(args)
        if cap == "image.thumb":
            return self._thumb(args)
        if cap == "image.watermark":
            return self._watermark(args)
        if cap == "image.stitch":
            return self._stitch(args)
        if cap == "image.batch":
            return self._batch(args)
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

    def _need_pil(self, cap):
        if _HAS_PIL:
            return None
        return R.unavailable(cap, "没装 Pillow：pip install Pillow",
                             device=self.name, via=self.via)

    def _out_path(self, src, out, fmt, suffix=""):
        """定输出路径。**任何情况下都不覆盖源文件**——写图写坏原图是不可逆的。"""
        fmt = fmt or "JPEG"
        ext = (out or "").strip()
        if ext:
            p = os.path.abspath(os.path.expanduser(ext))
            if not os.path.splitext(p)[1]:
                p += _EXT.get(fmt, ".png")
        else:
            root, _old = os.path.splitext(src)
            p = root + (suffix or "_out") + _EXT.get(fmt, ".png")
        if os.path.normcase(p) == os.path.normcase(src):
            root, _old = os.path.splitext(src)
            p = root + (suffix or "_out") + _EXT.get(fmt, ".png")
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        return p

    def _save(self, im, out, fmt, quality=None):
        """统一保存：JPEG 必须 RGB（RGBA 存 JPEG 会直接抛），其余保留透明。"""
        fmt = fmt or "PNG"
        if fmt == "JPEG":
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            kw = {"quality": int(quality or 88), "optimize": True,
                  "progressive": True, "subsampling": 1}
        elif fmt == "WEBP":
            kw = {"quality": int(quality or 82), "method": 5}
        elif fmt == "PNG":
            kw = {"optimize": True}
        else:
            kw = {}
        im.save(out, fmt, **kw)

    def _open_any(self, p):
        """打开任意格式并摆正方向（EXIF Orientation 不处理会把手机竖拍图转 90°）。"""
        im = Image.open(p)
        try:
            from PIL import ImageOps as _PIO
            im = _PIO.exif_transpose(im)
        except Exception:
            pass
        return im

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

    # ---------------- 转格式 ----------------

    def _convert(self, args):
        p, err = self._path(args, "image.convert")
        if err:
            return err
        err = self._need_pil("image.convert")
        if err:
            return err
        fmt = _fmt_of(args.get("fmt") or args.get("format") or "jpg")
        if not fmt:
            return R.error("image.convert",
                           "不认这个格式：%s（认 jpg/png/webp/bmp/gif/tif）" % (args.get("fmt"),),
                           device=self.name, via=self.via)
        out = self._out_path(p, args.get("out"), fmt, "_" + fmt.lower())
        try:
            im = self._open_any(p)
            self._save(im, out, fmt, quality=args.get("quality"))
            im.close()
        except Exception as e:
            return R.error("image.convert", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return R.success("image.convert", self.name, via=self.via, data={
            "src": p, "out": out, "format": fmt,
            "bytes": os.path.getsize(out), "src_bytes": os.path.getsize(p),
            "engine": "Pillow",
        })

    # ---------------- 压体积 ----------------

    def _compress(self, args):
        """压到目标体积。JPEG/WebP 二分 quality，PNG 走降色 + optimize。

        为什么不用「拍一个 quality 就交差」：同一个 quality 在不同图上体积差十几倍。
        用户要的是「压到 200KB 以内」，不是「quality=60」——所以按体积反解质量。
        """
        p, err = self._path(args, "image.compress")
        if err:
            return err
        err = self._need_pil("image.compress")
        if err:
            return err
        target = max(8, int(args.get("target_kb") or 200)) * 1024
        src_bytes = os.path.getsize(p)
        try:
            im = self._open_any(p)
            fmt = _fmt_of(args.get("fmt") or "")
            if not fmt:
                # 有透明通道就留 PNG，否则转 JPEG（截图/照片绝大多数是后者）
                fmt = "PNG" if im.mode in ("RGBA", "LA", "P") else "JPEG"
            out = self._out_path(p, args.get("out"), fmt, "_small")

            if fmt == "PNG":
                # PNG 没有 quality 旋钮：降色位。降到底还超就如实说不达标，
                # 不偷偷改成 JPEG —— 透明背景丢了是不可逆的。
                size, colors = 0, 256
                for colors in (256, 128, 96, 64, 32):
                    im.convert("RGBA").quantize(colors=colors, method=2).save(
                        out, "PNG", optimize=True)
                    size = os.path.getsize(out)
                    if size <= target:
                        break
                im.close()
                ok = size <= target
                return R.success("image.compress", self.name, via=self.via, data={
                    "src": p, "out": out, "format": "PNG", "bytes": size,
                    "src_bytes": src_bytes, "colors": colors, "hit_target": ok,
                    "note": "" if ok else
                            "降到 %d 色还超目标：PNG 无 quality 旋钮，"
                            "要么 image.resize 缩尺寸，要么显式 fmt=jpg 舍弃透明" % colors,
                    "engine": "Pillow",
                })

            # JPEG / WebP：二分 quality，留「不超目标里最大的那个」
            lo, hi, best_q, best_size = 20, 95, 0, 0
            for _ in range(7):
                mid = (lo + hi) // 2
                self._save(im, out, fmt, quality=mid)
                size = os.path.getsize(out)
                if size <= target:
                    best_q, best_size = mid, size
                    lo = mid + 1
                else:
                    hi = mid - 1
                if lo > hi:
                    break
            if not best_size:
                self._save(im, out, fmt, quality=20)
                best_q, best_size = 20, os.path.getsize(out)
            else:
                self._save(im, out, fmt, quality=best_q)
            im.close()
            return R.success("image.compress", self.name, via=self.via, data={
                "src": p, "out": out, "format": fmt, "quality": best_q,
                "bytes": best_size, "src_bytes": src_bytes,
                "ratio": round(best_size / max(1, src_bytes), 3),
                "hit_target": best_size <= target,
                "note": "" if best_size <= target else
                        "压到 q=%d 仍超 %dKB：这图原生就大，先缩尺寸再压"
                        % (best_q, target // 1024),
                "engine": "Pillow",
            })
        except Exception as e:
            return R.error("image.compress", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 裁剪 ----------------

    def _crop(self, args):
        p, err = self._path(args, "image.crop")
        if err:
            return err
        err = self._need_pil("image.crop")
        if err:
            return err
        fmt = _fmt_of(args.get("fmt") or "png") or "PNG"
        out = self._out_path(p, args.get("out"), fmt, "_crop")
        try:
            im = self._open_any(p)
            W, H = im.size
            x = int(args.get("x") or 0)
            y = int(args.get("y") or 0)
            w = int(args.get("w") or 0) or (W - x)
            h = int(args.get("h") or 0) or (H - y)
            # 越界不报错，直接夹到图内：截图坐标常带小数误差，
            # 差 1 像素就抛异常会让整条链路断在一个不值得断的地方。
            x = max(0, min(x, W - 1))
            y = max(0, min(y, H - 1))
            w = max(1, min(w, W - x))
            h = max(1, min(h, H - y))
            box = (x, y, x + w, y + h)
            im2 = im.crop(box)
            self._save(im2, out, fmt, quality=args.get("quality"))
            im.close()
            im2.close()
        except Exception as e:
            return R.error("image.crop", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return R.success("image.crop", self.name, via=self.via, data={
            "src": p, "out": out, "box": list(box),
            "width": w, "height": h, "bytes": os.path.getsize(out),
            "src_size": [W, H], "engine": "Pillow",
        })

    # ---------------- 缩略图 ----------------

    def _thumb(self, args):
        """正方形缩略图：先按短边 cover 裁成正方形，再缩。

        为什么不直接用 resize：缩完会变形（长图压成方块）。缩略图是用来
        判断「这是哪张」的，变形比丢边更糟。
        """
        p, err = self._path(args, "image.thumb")
        if err:
            return err
        err = self._need_pil("image.thumb")
        if err:
            return err
        size = max(16, min(int(args.get("size") or 256), 2048))
        anchor = (args.get("anchor") or "mc").strip().lower()
        fmt = _fmt_of(args.get("fmt") or "png") or "PNG"
        out = self._out_path(p, args.get("out"), fmt, "_thumb%d" % size)
        try:
            im = self._open_any(p)
            W, H = im.size
            side = min(W, H)
            hw, vw = _ANCHOR.get(anchor, (1, 1))
            left = 0 if hw == 0 else (W - side if hw == 2 else (W - side) // 2)
            top = 0 if vw == 0 else (H - side if vw == 2 else (H - side) // 2)
            im2 = im.crop((left, top, left + side, top + side)).resize(
                (size, size), Image.LANCZOS)
            self._save(im2, out, fmt, quality=args.get("quality"))
            im.close()
            im2.close()
        except Exception as e:
            return R.error("image.thumb", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return R.success("image.thumb", self.name, via=self.via, data={
            "src": p, "out": out, "size": size, "bytes": os.path.getsize(out),
            "src_size": [W, H], "anchor": anchor, "engine": "Pillow",
        })

    # ---------------- 水印 ----------------

    def _watermark(self, args):
        """文字水印：九宫格定位，字号不给就按图宽自动定（图宽/28），带半透明。"""
        p, err = self._path(args, "image.watermark")
        if err:
            return err
        err = self._need_pil("image.watermark")
        if err:
            return err
        text = (args.get("text") or "").strip()
        if not text:
            return R.error("image.watermark", "缺 text", device=self.name, via=self.via)
        pos = (args.get("pos") or "br").strip().lower()
        hw, vw = _ANCHOR.get(pos, (2, 2))
        opacity = max(0.05, min(float(args.get("opacity") or 0.35), 1.0))
        try:
            im = self._open_any(p).convert("RGBA")
            W, H = im.size
            fsize = int(args.get("size") or 0) or max(14, W // 28)
            font = _font(fsize)
            layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
            dr = ImageDraw.Draw(layer)
            try:
                box = dr.textbbox((0, 0), text, font=font)
                tw, th = box[2] - box[0], box[3] - box[1]
            except Exception:
                tw, th = fsize * len(text), fsize
            pad = max(8, fsize // 3)
            x = pad if hw == 0 else ((W - tw) // 2 if hw == 1 else W - tw - pad)
            y = pad if vw == 0 else ((H - th) // 2 if vw == 1 else H - th - pad)
            a = int(255 * opacity)
            # 白字加一道暗描边：白底上纯白水印等于没加
            dr.text((x + 1, y + 1), text, font=font, fill=(0, 0, 0, a // 2))
            dr.text((x, y), text, font=font, fill=(255, 255, 255, a))
            im = Image.alpha_composite(im, layer)
            fmt = _fmt_of(args.get("fmt") or "") or (
                "PNG" if p.lower().endswith(".png") else "JPEG")
            out = self._out_path(p, args.get("out"), fmt, "_wm")
            self._save(im, out, fmt, quality=args.get("quality") or 90)
            im.close()
            layer.close()
        except Exception as e:
            return R.error("image.watermark", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        return R.success("image.watermark", self.name, via=self.via, data={
            "src": p, "out": out, "text": text, "pos": pos,
            "font_size": fsize, "bytes": os.path.getsize(out), "engine": "Pillow",
        })

    # ---------------- 拼接 ----------------

    def _stitch(self, args):
        """多图拼接：纵向 = 长截图，横向 = 对比图。

        统一宽度必须做：手机截图 1080 宽、电脑截图 2560 宽，不统一就直接
        paste 会按第一张的画布裁掉后面那张的一半，而且**不报错**。
        """
        err = self._need_pil("image.stitch")
        if err:
            return err
        raw = args.get("paths") or args.get("sources") or []
        if isinstance(raw, str):
            raw = [x for x in raw.replace(";", "\n").replace(",", "\n").split("\n") if x.strip()]
        paths = []
        for one in raw:
            one = str(one).strip().strip('"')
            if not one:
                continue
            q = os.path.abspath(os.path.expanduser(one))
            if not os.path.isfile(q):
                return R.error("image.stitch", "文件不存在: %s" % q,
                               device=self.name, via=self.via)
            paths.append(q)
        if not paths:
            return R.error("image.stitch", "至少给一张图（paths）",
                           device=self.name, via=self.via)
        direction = (args.get("direction") or "v").strip().lower()
        vertical = direction[0] not in ("h", "l", "r")
        gap = max(0, int(args.get("gap") or 0))
        bg = (args.get("bg") or "ffffff").strip().lstrip("#")
        try:
            bgc = tuple(int(bg[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
        except Exception:
            bgc = (255, 255, 255, 255)

        ims = []
        try:
            for q in paths:
                ims.append(self._open_any(q).convert("RGBA"))
            width = int(args.get("width") or 0) or (
                max(i.size[0] for i in ims) if vertical else ims[0].size[0])
            width = max(32, min(width, 8192))
            if vertical:
                scaled = []
                for im in ims:
                    W, H = im.size
                    scaled.append(im.resize(
                        (width, max(1, int(round(H * width / float(W))))), Image.LANCZOS))
                total = sum(i.size[1] for i in scaled) + gap * (len(scaled) - 1)
                canvas = Image.new("RGBA", (width, total), bgc)
                at = 0
                for i in scaled:
                    canvas.paste(i, (0, at), i)
                    at += i.size[1] + gap
            else:
                hgt = int(args.get("height") or 0) or ims[0].size[1]
                height = max(32, min(hgt, 8192))
                scaled = []
                for im in ims:
                    W, H = im.size
                    scaled.append(im.resize(
                        (max(1, int(round(W * height / float(H)))), height), Image.LANCZOS))
                total = sum(i.size[0] for i in scaled) + gap * (len(scaled) - 1)
                canvas = Image.new("RGBA", (total, height), bgc)
                at = 0
                for i in scaled:
                    canvas.paste(i, (at, 0), i)
                    at += i.size[0] + gap
            fmt = _fmt_of(args.get("fmt") or "") or "JPEG"
            out = self._out_path(paths[0], args.get("out"), fmt, "_stitch")
            self._save(canvas, out, fmt, quality=args.get("quality") or 90)
            size = os.path.getsize(out)
            geometry = [canvas.size[0], canvas.size[1]]
            canvas.close()
        except Exception as e:
            return R.error("image.stitch", "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)
        finally:
            for i in ims:
                try:
                    i.close()
                except Exception:
                    pass
        return R.success("image.stitch", self.name, via=self.via, data={
            "out": out, "count": len(paths),
            "direction": "v" if vertical else "h",
            "width": geometry[0], "height": geometry[1],
            "bytes": size, "gap": gap, "engine": "Pillow",
        })

    # ---------------- 目录批处理 ----------------

    def _batch(self, args):
        """目录批处理。**永不覆盖源文件**：输出默认落 `out_dir` 或 `<目录>/_<op>/`。"""
        err = self._need_pil("image.batch")
        if err:
            return err
        raw_dir = (args.get("dir") or args.get("path") or "").strip().strip('"')
        if not raw_dir:
            return R.error("image.batch", "缺 dir", device=self.name, via=self.via)
        root = os.path.abspath(os.path.expanduser(raw_dir))
        if not os.path.isdir(root):
            return R.error("image.batch", "不是目录: %s" % root,
                           device=self.name, via=self.via)
        recursive = bool(args.get("recursive") or False)
        pattern = (args.get("pattern") or "*").strip()
        limit = max(1, min(int(args.get("limit") or 500), 5000))
        op = (args.get("op") or "convert").strip().lower()
        fmt = _fmt_of(args.get("fmt") or "jpg") or "JPEG"
        max_w = int(args.get("max_w") or 0)
        max_h = int(args.get("max_h") or 0)
        target_kb = int(args.get("target_kb") or 0)
        dry_run = bool(args.get("dry_run") or False)
        out_dir = (args.get("out_dir") or "").strip()
        out_dir = (os.path.abspath(os.path.expanduser(out_dir)) if out_dir
                   else os.path.join(root, "_" + op))

        import fnmatch

        found = []
        if recursive:
            for dirpath, _dn, names in os.walk(root):
                for n in names:
                    if fnmatch.fnmatch(n.lower(), pattern.lower()):
                        found.append(os.path.join(dirpath, n))
        else:
            for n in sorted(os.listdir(root)):
                q = os.path.join(root, n)
                if os.path.isfile(q) and fnmatch.fnmatch(n.lower(), pattern.lower()):
                    found.append(q)
        exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")
        found = sorted(f for f in found if os.path.splitext(f)[1].lower() in exts)
        if not found:
            return R.unavailable(
                "image.batch", "目录里没有可处理的图片（pattern=%s）" % pattern,
                device=self.name, via=self.via)
        truncated = len(found) > limit
        found = found[:limit]

        plan, done, failed = [], 0, []
        for f in found:
            stem = os.path.splitext(os.path.basename(f))[0]
            dest = os.path.join(out_dir, stem + _EXT.get(fmt, ".jpg"))
            plan.append({"src": f, "out": dest})
            if dry_run:
                continue
            try:
                im = self._open_any(f)
                if max_w or max_h:
                    w0, h0 = im.size
                    k = min(1.0, (max_w or w0) / float(w0), (max_h or h0) / float(h0))
                    if k < 1.0:
                        im = im.resize((max(1, int(w0 * k)), max(1, int(h0 * k))),
                                       Image.LANCZOS)
                os.makedirs(out_dir, exist_ok=True)
                self._save(im, dest, fmt, quality=args.get("quality") or 88)
                if target_kb:
                    # 批量场景要的是稳定，不做极致二分：逐档试到达标就停
                    for q in (75, 60, 45, 30):
                        if os.path.getsize(dest) <= target_kb * 1024:
                            break
                        self._save(im, dest, fmt, quality=q)
                im.close()
                done += 1
            except Exception as e:
                failed.append({"src": f, "err": "%s: %s" % (type(e).__name__, e)})
        return R.success("image.batch", self.name, via=self.via, data={
            "dir": root, "op": op, "out_dir": out_dir, "matched": len(found),
            "done": done, "failed": failed[:10], "failed_count": len(failed),
            "dry_run": dry_run, "truncated": truncated,
            "plan": plan[:20] if dry_run else plan[:5],
            "note": "dry_run=True 只列计划没动手" if dry_run else "",
            "engine": "Pillow",
        })


# ---------------------------------------------------------------- 自测
#
# `python -m fleet.kinds.image_ops --selftest`
#
# 全离线：自己在临时目录里造图，不读用户任何文件、不联网。
# 每条都断言**可验证的事实**（尺寸/格式/字节关系），不断言「没抛异常」——
# 「没抛异常但输出是错的」正是这类模块最容易出的问题。
def _selftest():
    import tempfile

    fails = []
    checks = [0]

    def ck(cond, msg):
        checks[0] += 1
        print(("  ✓ " if cond else "  ✗ ") + msg)
        if not cond:
            fails.append(msg)

    if not _HAS_PIL:
        print("没装 Pillow —— 跳过（probe 会如实报不可用）")
        return 0

    dev = ImageOps()
    td = tempfile.mkdtemp(prefix="imgops_")
    try:
        # 造三张尺寸/比例/模式都不同的图：只有尺寸不同才测得出「统一宽度」这件事
        specs = [("a", (400, 300), (200, 60, 60)),
                 ("b", (200, 600), (60, 160, 60)),
                 ("c", (300, 300), (60, 60, 200))]
        srcs = []
        for name, size, color in specs:
            p = os.path.join(td, name + ".png")
            Image.new("RGB", size, color).save(p)
            srcs.append(p)

        p0 = srcs[0]
        r = dev.invoke("image.info", {"path": p0})
        ck(r.ok and r.data["width"] == 400 and r.data["height"] == 300, "info 读到真实宽高")

        r = dev.invoke("image.resize", {"path": p0, "max_w": 200, "max_h": 200})
        ck(r.ok and r.data["width"] == 200 and r.data["height"] == 150,
           "resize 等比缩到 200×150（不是硬拉成 200×200）")

        r = dev.invoke("image.convert", {"path": p0, "fmt": "jpg"})
        ck(r.ok and r.data["out"].lower().endswith(".jpg")
           and os.path.isfile(r.data["out"]), "convert 真的产出 .jpg")
        jpg = r.data["out"]

        r = dev.invoke("image.compress", {"path": p0, "target_kb": 8})
        ck(r.ok and r.data["bytes"] <= 8 * 1024, "compress 压进 8KB 目标")
        ck(r.ok and os.path.getsize(p0) > 0, "compress 没动源文件")

        r = dev.invoke("image.crop", {"path": p0, "x": 50, "y": 40, "w": 120, "h": 90})
        ck(r.ok and r.data["width"] == 120 and r.data["height"] == 90,
           "crop 产出 120×90")
        r2 = dev.invoke("image.crop", {"path": p0, "x": 9999, "y": 9999, "w": 50, "h": 50})
        ck(r2.ok, "crop 越界不报错（坐标误差不该断链路）")
        ck(r2.ok and r2.data["width"] == 1 and r2.data["height"] == 1,
           "crop 越界被夹到 1×1")

        r = dev.invoke("image.thumb", {"path": srcs[1], "size": 64})
        ck(r.ok, "thumb 成功")
        th = Image.open(r.data["out"])
        ck(th.size == (64, 64), "thumb 是正方形 64×64（竖图不被压变形）")
        th.close()

        r = dev.invoke("image.watermark", {"path": p0, "text": "天网 2026", "pos": "br"})
        ck(r.ok and os.path.getsize(r.data["out"]) > 0, "watermark 出图（中文不乱码）")
        wm = Image.open(r.data["out"])
        ck(wm.size == (400, 300), "watermark 不改变原尺寸")
        wm.close()

        r = dev.invoke("image.stitch", {"paths": srcs, "direction": "v"})
        ck(r.ok and r.data["count"] == 3, "stitch 拼了 3 张")
        ck(r.ok and r.data["width"] == 400, "stitch 纵向统一到最宽 400")
        # 统一到 400 宽后逐张等比：300 / 600*2=1200 / 300*4/3=400 ⇒ 合计 1900
        ck(r.ok and r.data["height"] == 1900,
           "stitch 高度按等比累加（300+1200+400=1900，不是原高相加）")
        im = Image.open(r.data["out"])
        ck(im.size == (400, 1900), "stitch 落盘尺寸与回报一致")
        im.close()

        r = dev.invoke("image.stitch", {"paths": srcs[:2], "direction": "h"})
        ck(r.ok and r.data["height"] == 300, "stitch 横向以第一张高度为准")
        ck(r.ok and r.data["width"] == 400 + 100, "stitch 横向宽度按等比累加（400+100）")

        sub = os.path.join(td, "many")
        os.makedirs(sub, exist_ok=True)
        for i in range(3):
            Image.new("RGB", (500, 400), (10 * i, 100, 200)).save(
                os.path.join(sub, "p%d.png" % i))
        r = dev.invoke("image.batch", {"dir": sub, "dry_run": True})
        ck(r.ok and r.data["matched"] == 3 and r.data["done"] == 0,
           "batch dry_run 只列计划不动手")
        ck(r.ok and not os.path.isdir(r.data["out_dir"]), "batch dry_run 没建输出目录")
        r = dev.invoke("image.batch", {"dir": sub, "fmt": "jpg", "max_w": 250})
        ck(r.ok and r.data["done"] == 3, "batch 真跑处理 3 张")
        outs = [f for f in os.listdir(r.data["out_dir"]) if f.endswith(".jpg")]
        ck(len(outs) == 3, "batch 产出 3 个 .jpg")
        if outs:
            one = Image.open(os.path.join(r.data["out_dir"], outs[0]))
            ck(one.size[0] == 250, "batch 按 max_w 缩到 250 宽")
            one.close()
        ck(all(os.path.isfile(f) for f in srcs), "batch 没动源文件")
    finally:
        import shutil as _sh
        _sh.rmtree(td, ignore_errors=True)

    if fails:
        print("image_ops selftest: %d/%d（失败 %d）"
              % (checks[0] - len(fails), checks[0], len(fails)))
        return 1
    print("image_ops selftest: %d/%d" % (checks[0], checks[0]))
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python -m fleet.kinds.image_ops --selftest")
