"""视频生成设备：把已写好的时间线算法接成两项真能力。

背景
----
`fleet/motion.py`（稀疏关键帧 + 几何运镜）与 `fleet/storyboard.py`（分镜成片）
早就写完、各自有门禁，但 `video.gen` / `video.story` 一直只是能力表上的两个名字，
没有任何设备认领 —— 声明了却调不到，等于占位符。本模块补上这一层：

  video.gen   prompt → 关键帧（出图后端） → 几何 warp 合成播放帧 → mp4
  video.story board  → 校验/算账 → 分镜成片（配音定镜头时长） → mp4

工程约定（照成熟生成管线学来的几条）：
  · **可复现**：seed 固定则关键帧序列固定；每次出片把完整参数写成 .json 台账，
    与 mp4 同目录同 stem。没有台账就无法复现，也无法排查「上次那版怎么来的」。
  · **参数先挡**：宽高必须是 8 的倍数、步数/CFG/种子/时长都有硬边界，
    非法值在启动昂贵的生成之前就报错。
  · **不覆盖**：输出与台账一律另取编号；合成失败清掉半截文件。
  · **诚实降级**：出图后端没配置就报 unavailable 并附上算好的计划，
    绝不返回一张空壳 mp4 假装成功。
  · **后端可注入**：runner 走构造参数，测试用打桩出图，不连手机也能验全链路。

只处理本地文件；不联网拉模型、不上传、不碰用户目录之外的东西。
"""

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..core import BaseDevice, DeviceResult as R
from .. import motion
from .. import storyboard
from .mediaops import _unique

SEED_MAX = 2 ** 31 - 1
MAX_PLAY_FRAMES = 1800          # 30 秒 × 60fps，防止参数把本机拖死
DEFAULT_NEG = "blurry, low quality, watermark, deformed, extra limbs, text"


def _num(value, label, low, high, default=None, integer=False):
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


def _size(value):
    """宽高必须是 8 的倍数：扩散模型的潜在空间按 8 像素下采样。"""
    text = str(value or "512x512").strip().lower()
    parts = text.split("x")
    if len(parts) != 2:
        raise ValueError("size 要写成 宽x高，如 512x512")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("size 的宽高必须是整数：%r" % text) from None
    for label, side in (("宽", width), ("高", height)):
        if not 64 <= side <= 2048:
            raise ValueError("%s 必须在 64~2048，收到 %d" % (label, side))
        if side % 8:
            raise ValueError("%s 必须是 8 的倍数，收到 %d" % (label, side))
    return width, height


def _dest(value, default_name):
    path = Path(value).expanduser().absolute() if value else Path(
        os.path.join(os.getcwd(), "tmp", default_name))
    if path.suffix.lower() != ".mp4":
        raise ValueError("输出必须是 .mp4：%s" % path)
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    return _unique(path)


def _ffmpeg():
    found = shutil.which("ffmpeg")
    if found:
        return found
    env = os.environ.get("FLEET_FFMPEG", "").strip()
    return env if env and os.path.isfile(env) else None


class VideoGen(BaseDevice):
    kind = "videogen"
    via = "local"

    def __init__(self, name="video:gen", runner=None, timeout=300.0, **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("layer", "media")
        self.runner = runner
        self.timeout = float(timeout)
        self._probe_cache = None

    def probe(self):
        if self._probe_cache is None:
            missing = []
            if not _ffmpeg():
                missing.append("ffmpeg")
            try:
                import PIL  # noqa: F401
            except ImportError:
                missing.append("Pillow")
            self._probe_cache = (
                (True, "") if not missing else
                (False, "缺依赖：%s" % "、".join(missing)))
        return self._probe_cache

    def caps(self):
        return ("video.gen", "video.story")

    def invoke(self, cap, args=None):
        a = args or {}
        if cap not in self.caps():
            return R.error(cap, "未知能力 %r" % cap, device=self.name)
        fn = self._generate if cap == "video.gen" else self._story
        try:
            data = fn(a)
        except ValueError as exc:
            return R.error(cap, str(exc), device=self.name, via=self.via)
        except Exception as exc:                          # noqa: BLE001
            return R.error(cap, "%s: %s" % (type(exc).__name__, exc),
                           device=self.name, via=self.via)
        if data.get("unavailable"):
            reason = data.pop("unavailable")
            return R.unavailable(cap, reason, device=self.name, data=data, via=self.via)
        return R.success(cap, device=self.name, data=data,
                         side_effect=not data.get("plan_only"), via=self.via)

    # ------------------------------------------------------------ video.gen

    def _generate(self, a):
        prompt = str(a.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("缺 prompt：给一句画面描述")
        duration = _num(a.get("duration"), "duration", 0.5, 30, 3)
        play_fps = _num(a.get("fps"), "fps", 1, 60, 24, integer=True)
        gen_fps = _num(a.get("gen_fps"), "gen_fps", 0.5, 24, 6)
        width, height = _size(a.get("size"))
        preset = str(a.get("motion") or "push_in").strip().lower()
        if preset not in motion.PRESETS:
            raise ValueError("未知运镜 %r，可选：%s"
                             % (preset, ", ".join(sorted(motion.PRESETS))))
        seed_raw = _num(a.get("seed"), "seed", -1, SEED_MAX, -1, integer=True)
        seed = (int(time.time() * 1000) % SEED_MAX) if seed_raw < 0 else seed_raw
        steps = _num(a.get("steps"), "steps", 1, 100, 12, integer=True)
        cfg = _num(a.get("cfg"), "cfg", 0, 20, 7.0)
        denoise = _num(a.get("denoise"), "denoise", 0, 1, 0.28)
        model = str(a.get("model") or "").strip()
        neg = str(a.get("neg") or DEFAULT_NEG)
        plan_only = bool(a.get("plan_only"))

        ktimes, ptimes = motion.plan(duration, gen_fps, play_fps)
        if len(ptimes) > MAX_PLAY_FRAMES:
            raise ValueError("播放帧 %d 超过上限 %d，调低 fps 或 duration"
                             % (len(ptimes), MAX_PLAY_FRAMES))
        track = motion.Track(preset)
        plan = {
            "prompt": prompt, "negative": neg, "seed": seed, "size": "%dx%d" % (width, height),
            "duration": duration, "fps": play_fps, "gen_fps": gen_fps,
            "motion": preset, "steps": steps, "cfg": cfg, "denoise": denoise,
            "model": model, "keyframes": len(ktimes), "play_frames": len(ptimes),
            "key_times": [round(t, 4) for t in ktimes],
            "camera": {"pad_ratio": round(motion.pad_for(track, duration), 4)},
            "backend": "injected" if self.runner is not None else "image_gen_runner",
        }
        if plan_only:
            return dict(plan, plan_only=True, estimated_keyframe_calls=len(ktimes))

        ok, why = self.probe()
        if not ok:
            return dict(plan, unavailable="本机跑不了合成：%s" % why)
        if self.runner is None:
            try:
                import image_gen_runner as IG
            except Exception as exc:
                return dict(plan, unavailable="出图后端未配置：%s" % exc)
            runner = IG.run
        else:
            runner = self.runner

        tag = hashlib.sha1(json.dumps(plan, sort_keys=True, ensure_ascii=False)
                           .encode("utf-8")).hexdigest()[:10]
        out = _dest(a.get("out"), "video_%s.mp4" % tag)
        work = tempfile.mkdtemp(prefix="videogen-")
        started = time.time()
        try:
            from PIL import Image
            images = []
            for index, moment in enumerate(ktimes):
                # 首帧用完整步数，后续关键帧按 65% 步数补细节，省算力。
                frame_steps = steps if index == 0 else max(1, int(steps * 0.65))
                frame_seed = (seed + index) % (SEED_MAX + 1)
                res = runner("%s, frame at %.2fs" % (prompt, moment), neg,
                             "%dx%d" % (width, height), frame_steps, frame_seed,
                             cfg, model or None)
                if not isinstance(res, dict) or not res.get("ok") or not res.get("path"):
                    return dict(plan, unavailable="关键帧 %d 出图失败：%s"
                                % (index + 1, (res or {}).get("err") or "后端无返回"))
                path = Path(res["path"])
                if not path.is_file():
                    return dict(plan, unavailable="关键帧 %d 给了不存在的路径：%s"
                                % (index + 1, path))
                with Image.open(path) as handle:
                    images.append(handle.convert("RGB"))
            frames, pad_ratio = motion.render_playback(
                images, ktimes, track, duration, play_fps,
                out_dir=work, prefix="p", ref_size=(width, height))
            storyboard.encode_frames(list(frames), play_fps, str(out),
                                     size=(width, height))
            if not out.is_file() or out.stat().st_size == 0:
                raise ValueError("编码结束但没有有效 mp4：%s" % out)
        except Exception:
            try:
                if out.exists():
                    out.unlink()
            except OSError:
                pass
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

        manifest = dict(plan)
        manifest.update({"file": str(out), "bytes": out.stat().st_size,
                         "pad_ratio": round(pad_ratio, 4),
                         "elapsed": round(time.time() - started, 2),
                         "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        sidecar = _unique(out.with_suffix(".json"))
        sidecar.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        return dict(manifest, manifest=str(sidecar))

    # ------------------------------------------------------------ video.story

    def _story(self, a):
        board = a.get("board")
        if not isinstance(board, dict):
            raise ValueError("缺 board：给一份分镜数据（含 shots 数组）")
        clean = storyboard.validate(board)
        estimate = storyboard.estimate(clean)
        dry_run = bool(a.get("dry_run"))
        model = str(a.get("model") or clean.get("model") or "").strip()
        result = {"shots": len(clean.get("shots") or []),
                  "canvas": clean.get("canvas"), "fps": clean.get("fps"),
                  "gen_fps": clean.get("gen_fps"), "size": clean.get("size"),
                  "estimate": estimate, "model": model, "dry_run": dry_run}
        if dry_run:
            return dict(result, plan_only=True)
        ok, why = self.probe()
        if not ok:
            return dict(result, unavailable="本机跑不了成片：%s" % why)
        out_dir = a.get("out_dir")
        if out_dir:
            folder = Path(out_dir).expanduser().absolute()
            folder.mkdir(parents=True, exist_ok=True)
        else:
            folder = Path(tempfile.mkdtemp(prefix="videostory-"))
        started = time.time()
        try:
            produced = storyboard.run_board(clean, out_dir=str(folder),
                                           model=model or None, dry_run=False)
        except Exception as exc:
            return dict(result, unavailable="成片失败：%s: %s"
                        % (type(exc).__name__, exc))
        if not produced:
            return dict(result, unavailable="成片后端没有返回产物，不假装成功")
        return dict(result, out_dir=str(folder), produced=produced,
                    elapsed=round(time.time() - started, 2))


# ---------------------------------------------------------------- 自测

def _selftest():
    total = 0
    fails = []
    folder = tempfile.mkdtemp(prefix="videogen-test-")

    def check(cond, label):
        nonlocal total
        total += 1
        if not cond:
            fails.append(label)
            print("失败：%s" % label)

    def bad(fn, *args):
        try:
            fn(*args)
        except (ValueError, TypeError):
            return True
        return False

    try:
        root = Path(folder)
        check(_size("512x512") == (512, 512), "尺寸：标准写法")
        check(_size("512X512") == (512, 512), "尺寸：大写 X 也认")
        for value in ("512", "0x512", "500x512", "512x500", "abc", "512x2049"):
            check(bad(_size, value), "尺寸：拒绝 %r" % value)
        check(_num("24", "x", 1, 60, integer=True) == 24, "数值：字符串整数")
        for value in (True, "nan", float("inf"), 61, 0):
            check(bad(_num, value, "x", 1, 60), "数值：拒绝 %r" % value)

        # 打桩出图后端：不连手机，真生成纯色 PNG
        calls = []

        def fake_runner(prompt, neg, size, steps, seed, cfg, model):
            from PIL import Image
            width, height = (int(x) for x in size.split("x"))
            index = len(calls)
            calls.append({"prompt": prompt, "seed": seed, "steps": steps, "size": size})
            path = root / ("key_%03d.png" % index)
            Image.new("RGB", (width, height), (30 + index * 20, 80, 160)).save(path)
            return {"ok": True, "path": str(path), "url": "", "err": ""}

        dev = VideoGen(runner=fake_runner)
        check(dev.kind == "videogen" and len(dev.caps()) == 2, "设备元信息")
        ok, why = dev.probe()
        check(ok, "ffmpeg + Pillow 可用：%s" % why)

        # 只算计划：真调 motion.plan / Track，不烧生成
        r = dev.invoke("video.gen", {"prompt": "海浪", "plan_only": True,
                                     "duration": 1, "gen_fps": 2, "fps": 4,
                                     "size": "64x64", "seed": 42, "motion": "push_in"})
        check(r.ok and not r.side_effect, "plan_only：成功且无副作用：%s" % r.err)
        if r.ok:
            d = r.data
            check(d["seed"] == 42 and d["keyframes"] >= 2 and d["play_frames"] >= 4,
                  "plan_only：种子固定、关键帧/播放帧数对")
            check(d["camera"]["pad_ratio"] >= 0, "plan_only：相机轨道算出来了")
        r2 = dev.invoke("video.gen", {"prompt": "海浪", "plan_only": True,
                                      "duration": 1, "gen_fps": 2, "fps": 4,
                                      "size": "64x64", "seed": 42})
        check(r.ok and r2.ok and r.data["key_times"] == r2.data["key_times"],
              "plan_only：同参数可复现")
        r = dev.invoke("video.gen", {"prompt": "", "plan_only": True})
        check(not r.ok, "缺 prompt 被拒")
        for label, patch in (
            ("非法尺寸", {"size": "500x512"}),
            ("未知运镜", {"motion": "spin_me"}),
            ("超长时长", {"duration": 999}),
            ("帧率越界", {"fps": 120}),
            ("步数越界", {"steps": 0}),
            ("种子越界", {"seed": -5}),
            ("CFG 越界", {"cfg": 99}),
        ):
            args = {"prompt": "海浪", "plan_only": True, "size": "64x64"}
            args.update(patch)
            check(not dev.invoke("video.gen", args).ok, "参数：%s 被拒" % label)

        # 真跑整条链路：关键帧 → warp 播放帧 → mp4
        out = root / "clip.mp4"
        r = dev.invoke("video.gen", {"prompt": "海浪", "out": str(out),
                                     "duration": 1, "gen_fps": 2, "fps": 4,
                                     "size": "64x64", "seed": 7, "steps": 2,
                                     "motion": "pan_left"})
        check(r.ok and r.side_effect, "真生成：成功且标副作用：%s" % r.err)
        if r.ok:
            mp4 = Path(r.data["file"])
            sidecar = Path(r.data["manifest"])
            check(mp4.is_file() and mp4.stat().st_size > 500, "真生成：mp4 非空中")
            check(sidecar.is_file() and json.loads(sidecar.read_text(encoding="utf-8"))["seed"] == 7,
                  "真生成：台账落盘且记下 seed")
            check(len(calls) == r.data["keyframes"], "真生成：关键帧调用次数等于计划")
            ffprobe = shutil.which("ffprobe") or "ffprobe"
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_frames,width,height", "-of", "json", str(mp4)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            check(probe.returncode == 0 and '"nb_frames"' in probe.stdout,
                  "真生成：ffprobe 能读出帧")
            r_again = dev.invoke("video.gen", {"prompt": "海浪", "out": str(out),
                                               "duration": 1, "gen_fps": 2, "fps": 4,
                                               "size": "64x64", "seed": 7, "steps": 2,
                                               "motion": "pan_left"})
            check(r_again.ok and r_again.data["file"] != r.data["file"],
                  "真生成：同名再跑另取编号不覆盖")

        # 后端缺失 = 诚实降级，附计划不假装成功
        broken = VideoGen(runner=lambda *a: {"ok": False, "err": "没接手机"})
        r = broken.invoke("video.gen", {"prompt": "海浪", "duration": 1,
                                        "gen_fps": 2, "fps": 4, "size": "64x64"})
        check(not r.ok and r.code == "unavailable" and r.data.get("keyframes"),
              "后端不可用：unavailable 且附计划")

        # video.story：校验 + 算账 + dry_run，不碰配音与手机
        board = {"shots": [{"prompt": "夜色里的便利店", "line": "灯还亮着"}]}
        r = dev.invoke("video.story", {"board": board, "dry_run": True})
        check(r.ok and not r.side_effect and r.data["shots"] == 1,
              "story dry_run：校验通过且无副作用：%s" % r.err)
        r = dev.invoke("video.story", {"board": {"shots": []}, "dry_run": True})
        check(not r.ok, "story：空分镜被拒")
        r = dev.invoke("video.story", {"board": {"shots": [{"line": "没画面"}]},
                                       "dry_run": True})
        check(not r.ok, "story：镜头缺 prompt 被拒")
        r = dev.invoke("video.unknown")
        check(not r.ok, "未知能力要报错")
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    got = total - len(fails)
    print("\nvideogen selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for item in fails:
        print("   失败:", item)
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.kinds.videogen --selftest")
