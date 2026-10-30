"""安卓/节点视频生成 runner：把 video.gen 能力落到「视频后端」。

与 image_gen_runner 同构——把「怎么出视频」和「协议/参数」解耦。

2026-10-30 更新：**手机本地已经能出视频了**，原来的结论（见下）被实测推翻。
新后端 `localdream_backend`：手机 QNN NPU 走 t2i 首帧 → img2img 链式推进，
512×512 / 12 步实测 2.7s/帧、10 分钟约 200 帧（≈17 秒 12fps 素材）。

原结论（保留作对照）：手机本地算力（Mali-G925 / 11GB）跑**扩散视频模型**
（animatediff 一类）交互速度撑不住，所以早先只用 ffmpeg 出占位片。
区别在于：帧链不是跑视频扩散模型，是拿 SD1.5 做逐帧 img2img —— 算力需求
和单张出图同量级，这才是手机能扛的路线。

后端优先级（_default_backend）：local-dream（手机 NPU，真画面）> CLI/ffmpeg（占位）。
"""
import os
import shutil
import subprocess


# ---------------------------------------------------------------------------
# local-dream（手机 QNN NPU）帧链后端
# ---------------------------------------------------------------------------

# unet.bin 按分辨率切过 patch（1024.patch / 768.patch / 1024x768.patch …），
# 落到表外尺寸手机侧会静默回退 512，出来的全是彩色噪声。这里先挡住。
FRAME_SIZES = {
    (512, 512), (768, 768), (1024, 1024),
    (768, 512), (512, 768), (1024, 768), (768, 1024),
}


def _parse_wh(size):
    s = str(size or "512x512").lower().replace("×", "x")
    if "x" not in s:
        raise RuntimeError(f"size 要写成 WxH（如 512x512），收到 {size!r}")
    try:
        w, h = (int(v) for v in s.split("x", 1))
    except ValueError:
        raise RuntimeError(f"size 解析失败：{size!r}")
    if (w, h) not in FRAME_SIZES:
        allowed = "、".join("%dx%d" % p for p in sorted(FRAME_SIZES))
        raise RuntimeError(
            f"手机帧链不支持 {w}x{h}（没有对应 patch，会出噪声）。可选：{allowed}")
    return w, h


def _need_pil():
    try:
        from PIL import Image  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            f"视频后端需要 pillow（帧稳定与编码都用它）：pip install pillow（{e}）")


def _color_match(im, ref):
    import numpy as np
    from PIL import Image
    a = np.asarray(im, dtype=np.float32)
    r = np.asarray(ref, dtype=np.float32)
    ma, sa = a.mean(axis=(0, 1)), a.std(axis=(0, 1)) + 1e-6
    mr, sr = r.mean(axis=(0, 1)), r.std(axis=(0, 1)) + 1e-6
    out = (a - ma) / sa * sr + mr
    return Image.fromarray(np.clip(out, 0, 255).astype("uint8"))


def _edge(im):
    from PIL import ImageFilter, ImageStat
    return ImageStat.Stat(im.convert("L").filter(ImageFilter.FIND_EDGES)).mean[0]


def _sharpen_to(im, target, cap=180):
    from PIL import ImageFilter
    cur = _edge(im)
    if cur >= target or target <= 0:
        return im
    percent = min(cap, int((target / max(cur, 1e-3) - 1.0) * 130))
    if percent <= 0:
        return im
    return im.filter(ImageFilter.UnsharpMask(radius=1.4, percent=percent, threshold=3))


def _blend(ref, cur, alpha):
    import numpy as np
    from PIL import Image
    a = np.asarray(ref, dtype=np.float32)
    b = np.asarray(cur, dtype=np.float32)
    return Image.fromarray(np.clip(a * alpha + b * (1 - alpha), 0, 255).astype("uint8"))


def _ffmpeg():
    ff = os.environ.get("FLEET_FFMPEG") or shutil.which("ffmpeg") or ""
    if ff and os.path.isfile(ff):
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _encode_mp4(frame_dir, total, fps, out_path):
    ff = _ffmpeg()
    if not ff:
        raise RuntimeError("编 mp4 需要 ffmpeg：设 FLEET_FFMPEG 或 pip install imageio-ffmpeg")
    cmd = [ff, "-y", "-framerate", str(int(fps)),
           "-i", os.path.join(frame_dir, "f_%04d.png"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        raise RuntimeError(f"ffmpeg 编码失败（rc={proc.returncode}）：{tail}")
    return out_path


def localdream_backend(prompt, duration=3, fps=12, size="512x512", seed=-1, model=None,
                       out_dir=None, denoise=0.35, steps=12, stabilize=True,
                       anchor=0.12, on_frame=None, timeout=900):
    """手机 NPU 帧链出视频：t2i 首帧 → img2img 逐帧推进 → mp4。

    为什么不是逐帧 t2i：每帧独立采样就是「另一张画」，拼起来是幻灯片。
    img2img 把上一帧当输入、只重噪 denoise 那么多，帧间才连得上。

    为什么必须 stabilize：纯链式有两个必然的累积病（实测 12 帧）——
    VAE roundtrip 每帧编解码一次，边缘密度 21.6→9.95（糊）；
    CFG 采样偏好把暗部越压越暗，亮度 88→33（黑）。
    稳定器做三件事：颜色/亮度对齐首帧、锐度补回首帧水平、输入与首帧少量混合。
    开了之后同一组参数下边缘回到 20.5、亮度回到 87.7，不再是「越走越糊的黑洞」。

    返回 (mp4 路径, 帧目录)。未配置/手机不可达一律抛 RuntimeError 说清原因。
    """
    import base64 as _b
    import io as _io
    import time as _t

    import image_gen_runner as ig
    from PIL import Image

    _need_pil()
    host, ctl, gen = ig._ld_endpoints()
    opener = ig._ld_opener()
    try:
        info = ig._ld_json(opener, f"http://{host}:{ctl}/info", 10)
    except Exception as e:
        raise RuntimeError(
            f"连不上手机控制口 {host}:{ctl}（{type(e).__name__}: {e}）——"
            "多半是 host mode 没开，或手机不在同一网段")
    if info.get("app") != "localdream":
        raise RuntimeError(f"{host}:{ctl} 不是 local-dream 的控制口（app={info.get('app')!r}）")

    w, h = _parse_wh(size)
    try:
        fps = max(1, min(60, int(fps)))
    except (TypeError, ValueError):
        fps = 12
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        duration = 3.0
    total = max(2, int(round(duration * fps)))
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        seed = -1
    base_seed = seed if seed >= 0 else int(_t.time()) % 1000000

    ig._ld_ensure_model(opener, host, ctl, model, w, h)
    ig._ld_wait_health(opener, host, gen)

    out_dir = out_dir or os.environ.get("FLEET_VIDEO_OUT") or os.path.join(
        os.path.expanduser("~"), ".fleet", "video_out")
    tag = "ld_%d" % (int(_t.time() * 1000))
    frame_dir = os.path.join(out_dir, tag)
    os.makedirs(frame_dir, exist_ok=True)

    neg = ("lowres, bad anatomy, worst face, extra fingers, missing fingers, "
           "blurry, watermark, text, signature")
    ref = None
    ref_edge = 0.0
    made = 0
    for i in range(total):
        path = os.path.join(frame_dir, "f_%04d.png" % i)
        payload = {
            "prompt": prompt,
            "negative_prompt": neg,
            "steps": int(steps),
            "cfg": 7.0,
            "width": w,
            "height": h,
            "output_format": "png",
            "show_diffusion_process": False,
            "seed": base_seed + i,
        }
        if i > 0:
            prev = Image.open(os.path.join(frame_dir, "f_%04d.png" % (i - 1))).convert("RGB")
            src = _blend(ref, prev, anchor) if (stabilize and ref is not None and anchor > 0) else prev
            buf = _io.BytesIO()
            src.save(buf, format="PNG")
            payload["image"] = _b.b64encode(buf.getvalue()).decode()
            payload["denoise_strength"] = float(denoise)

        img_b64, meta = ig._ld_stream_generate(
            opener, f"http://{host}:{gen}/generate", payload, timeout)
        im = Image.open(_io.BytesIO(_b.b64decode(img_b64))).convert("RGB")
        if stabilize:
            if ref is None:
                ref = im
                ref_edge = _edge(ref)
            else:
                im = _color_match(im, ref)
                im = _sharpen_to(im, ref_edge)
        im.save(path)
        made += 1
        if on_frame:
            try:
                on_frame(i, total, meta)
            except Exception:
                pass

    if made < 2:
        raise RuntimeError(f"只出了 {made} 帧，凑不成视频")

    mp4 = os.path.join(out_dir, tag + ".mp4")
    _encode_mp4(frame_dir, made, fps, mp4)
    return mp4, frame_dir


def _localdream_ready():
    return bool((os.environ.get("FLEET_LOCAL_DREAM") or "").strip())


# 默认后端：没配环境变量就视为「这个节点没接视频后端」。
# 与 image_gen_runner 同一纪律——未配置时 run() 明确报错，不静默交空。
def pc_video_backend(prompt, duration=3, fps=24, size="512x512", seed=-1, model=None,
                      out_dir=None, timeout=300):
    """PC 侧视频生成：优先用 FLEET_VIDEO_CLI，否则用 ffmpeg 生成占位短视频。"""
    import os as _os, subprocess as _sp, shutil as _sh, time as _t
    cli = os.environ.get("FLEET_VIDEO_CLI") or ""
    out_dir = out_dir or os.environ.get("FLEET_VIDEO_OUT") or _os.path.join(
        _os.path.expanduser("~/.fleet"), "video_out")
    _os.makedirs(out_dir, exist_ok=True)
    out_path = _os.path.join(out_dir, f"video_{int(_t.time()*1000)}.mp4")
    w, h = (size or "512x512").lower().split("x") if "x" in (size or "") else ("512", "512")

    if cli and _os.path.isfile(cli):
        cmd = [cli, "--prompt", prompt, "--duration", str(duration), "--fps", str(fps),
               "--width", str(w), "--height", str(h), "--out", out_path]
        if seed is not None and int(seed) >= 0:
            cmd += ["--seed", str(seed)]
        if model:
            cmd += ["--model", model]
        try:
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f"视频 CLI 执行失败：{type(e).__name__}: {e}")
        if proc.returncode != 0 or not _os.path.isfile(out_path):
            raise RuntimeError(f"视频 CLI 失败（rc={proc.returncode}）：{(proc.stderr or proc.stdout or '').strip()[:300]}")
        return out_path

    ff = _sh.which("ffmpeg") or os.environ.get("FLEET_FFMPEG") or ""
    if not ff or not _os.path.isfile(ff):
        raise RuntimeError("视频后端未配置：设 FLEET_VIDEO_CLI 或安装 ffmpeg（FLEET_FFMPEG）")
    cmd = [ff, "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={duration}",
           "-vf", f"drawtext=text='{prompt[:40]}':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h-text_h)/2",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path]
    try:
        proc = _sp.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"ffmpeg 执行失败：{type(e).__name__}: {e}")
    if proc.returncode != 0 or not _os.path.isfile(out_path):
        raise RuntimeError(f"ffmpeg 生成失败（rc={proc.returncode}）：{(proc.stderr or '').strip()[:300]}")
    return out_path


def _default_backend(prompt, duration, fps, size, seed, model, **kw):
    # 手机 NPU 帧链是真画面，占位片是黑底打字，先给真画面。
    if _localdream_ready():
        if os.environ.get("FLEET_VIDEO_BACKEND") and \
                os.environ.get("FLEET_VIDEO_BACKEND") != "localdream":
            raise RuntimeError(
                "FLEET_VIDEO_BACKEND 指向的远端桥未落地；要真画面就去掉这个变量，"
                "让 local-dream（手机 NPU）接")
        path, _frames = localdream_backend(
            prompt, duration, fps, size, seed, model, **kw)
        return path
    if os.environ.get("FLEET_VIDEO_BACKEND"):
        raise RuntimeError("video.gen 远端桥接待实现：FLEET_VIDEO_BACKEND 已设但桥代码未落地")
    if os.environ.get("FLEET_VIDEO_CLI") or os.environ.get("FLEET_FFMPEG") or __import__("shutil").which("ffmpeg"):
        return pc_video_backend(prompt, duration, fps, size, seed, model)
    raise RuntimeError(
        "video.gen 后端未配置：设 FLEET_LOCAL_DREAM=<手机IP> 走手机 NPU 帧链"
        "（真画面），或设 FLEET_VIDEO_CLI / 装 ffmpeg（只能出占位片）")


# ---------------------------------------------------------------------------
# 远端路由钩子（预留）：把任务经指定 relay/桥派到能跑视频的节点。
# 这里只定义契约，实际落地依赖 android_bridge / relay 通道，先留空不静默。
# ---------------------------------------------------------------------------

def _accepts_kw(fn):
    """后端是否吃得下帧链参数。别用「试调用再 catch TypeError」——
    后端内部真抛 TypeError 时会被误当成签名不匹配，悄悄吞掉一个真错误。"""
    import inspect
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    for p in sig.parameters.values():
        if p.kind is p.VAR_KEYWORD:
            return True
    return "denoise" in sig.parameters


def run(prompt, duration=3, fps=12, size="512x512", seed=-1, model=None,
        backend=None, denoise=0.35, steps=12, stabilize=True, anchor=0.12):
    """出一段视频。

    返回 {"ok": bool, "path": str, "err": str}。

    backend: 可注入（测试用）；默认 _default_backend：
      手机 NPU 帧链（FLEET_LOCAL_DREAM）> PC CLI/ffmpeg 占位片。
    任何异常都不外抛——能力未接好就如实说「未配置」，不交空。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "path": "", "err": "prompt 不能为空"}
    try:
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 3
        try:
            fps = int(fps)
        except (TypeError, ValueError):
            fps = 12
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            seed = -1
        fn = backend or _default_backend
        if _accepts_kw(fn):
            path = fn(prompt, duration, fps, size, seed, model,
                      denoise=denoise, steps=steps,
                      stabilize=stabilize, anchor=anchor)
        else:
            path = fn(prompt, duration, fps, size, seed, model)
        return {"ok": True, "path": path, "err": ""}
    except Exception as e:
        return {"ok": False, "path": "", "err": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 自测：python video_gen_runner.py --selftest
#
# 不起真机。用两个本地 HTTP 服务冒充手机的 8808 / 8081，把
# /info -> /status -> /select -> /generate(SSE) -> 帧链 -> mp4 整条链路真跑一遍。
# 假服务端故意让每帧都更暗，用来锁死稳定器的行为：开稳定器亮度要回来，
# 关掉就必须看得出在变黑。这样「稳定器到底有没有接上」不靠肉眼判断。
# ---------------------------------------------------------------------------

def _selftest():
    import base64 as _b
    import io
    import json as _j
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    tally = {"ok": 0, "bad": 0}

    def check(cond, msg):
        if cond:
            tally["ok"] += 1
            print("  [ok]", msg)
        else:
            tally["bad"] += 1
            print("  [!!]", msg)

    def section(t):
        print("\n[" + t + "]")

    try:
        import numpy as np
        from PIL import Image, ImageStat
    except Exception as e:
        print(f"  [!!] 缺 pillow/numpy，帧链无法自测：{e}")
        print("\nFAILED  0 passed, 1 failed")
        return 1

    st = {"serving": None, "state": "idle", "width": None, "height": None,
          "selects": 0, "calls": 0, "seen_images": 0, "payload": None}

    def frame_png(idx):
        """第 idx 帧：整幅变暗 + 细噪声（保证边缘密度不为 0，稳定器才有得算）。"""
        lum = max(24, 240 - 24 * idx)
        base = np.full((512, 512, 3), lum, dtype="int16")
        noise = np.random.RandomState(idx).randint(-18, 19, size=(512, 512, 3))
        arr = np.clip(base + noise, 0, 255).astype("uint8")
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        return _b.b64encode(buf.getvalue()).decode()

    class Ctl(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            body = _j.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/info":
                return self._send({"app": "localdream", "protocol": 1,
                                   "version": "fake", "device": "FakePhone"})
            if self.path == "/status":
                return self._send({"serving_model_id": st["serving"],
                                   "state": st["state"], "message": None,
                                   "error_model_id": None,
                                   "width": st["width"], "height": st["height"]})
            return self._send({"error": "not found"}, 404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = _j.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = {}
            if self.path == "/select":
                st["selects"] += 1
                st["serving"] = body.get("model_id")
                st["width"] = body.get("width")
                st["height"] = body.get("height")
                st["state"] = "running"
                return self._send({"ok": True})
            return self._send({"error": "not found"}, 404)

    class Gen(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ok = self.path == "/health"
            body = b"{}"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                st["payload"] = _j.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                st["payload"] = {}
            if st["payload"].get("image"):
                st["seen_images"] += 1
            idx = st["calls"]
            st["calls"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(("event: complete\ndata: %s\n\n" % _j.dumps(
                {"type": "complete", "image": frame_png(idx), "format": "png",
                 "seed": st["payload"].get("seed", 0)})).encode("utf-8"))

    ctl = HTTPServer(("127.0.0.1", 0), Ctl)
    gen = HTTPServer(("127.0.0.1", 0), Gen)
    for s in (ctl, gen):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    ctl_port, gen_port = ctl.server_address[1], gen.server_address[1]

    keys = ("FLEET_LOCAL_DREAM", "FLEET_LOCAL_DREAM_CONTROL", "FLEET_VIDEO_OUT",
            "FLEET_VIDEO_CLI", "FLEET_VIDEO_BACKEND", "FLEET_FFMPEG",
            "HTTP_PROXY", "HTTPS_PROXY")
    saved = {k: os.environ.get(k) for k in keys}
    tmp = tempfile.mkdtemp(prefix="vg_selftest_")
    try:
        section("未配置时如实报错")
        for k in keys:
            os.environ.pop(k, None)
        r = run("a cat", backend=None)
        check(r["ok"] is False and ("未配置" in r["err"] or "pillow" in r["err"]),
              "没配后端 -> ok:false 且说未配置：%s" % r["err"][:70])
        check(r["path"] == "", "失败时 path 为空，不交假路径")

        section("空 prompt")
        r = run("   ")
        check(r["ok"] is False and "prompt" in r["err"], "空 prompt 被拦")

        section("分辨率白名单（表外尺寸手机会静默出噪声）")
        for wh in ("512x512", "768x1024", "1024x768"):
            w, h = _parse_wh(wh)
            check((w, h) in FRAME_SIZES, "%s 在白名单里" % wh)
        try:
            _parse_wh("600x600")
            check(False, "600x600 应该被拒")
        except RuntimeError as e:
            check("patch" in str(e), "600x600 被拒且说明没 patch")
        try:
            _parse_wh("512")
            check(False, "非 WxH 应该被拒")
        except RuntimeError:
            check(True, "非 WxH 格式被拒")

        section("backend 异常透出")
        def boom(*a, **kw):
            raise RuntimeError("后端炸了")
        r = run("x", backend=boom)
        check(r["ok"] is False and "后端炸了" in r["err"],
              "backend 异常 -> ok:false 带原因")

        section("帧链 /info -> /select -> /generate(SSE) -> mp4")
        os.environ["FLEET_LOCAL_DREAM"] = "127.0.0.1:%d" % gen_port
        os.environ["FLEET_LOCAL_DREAM_CONTROL"] = str(ctl_port)
        os.environ["FLEET_VIDEO_OUT"] = tmp
        # 代理指向死端口：局域网地址若被塞进代理，这条必然失败
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"

        st.update({"calls": 0, "seen_images": 0, "selects": 0, "state": "idle",
                   "serving": None, "width": None, "height": None})
        mp4, fdir = localdream_backend(
            "a rainy street", duration=1, fps=4, size="512x512", seed=7,
            model="fake-model", out_dir=tmp, steps=4, stabilize=True, anchor=0.0)
        check(os.path.isfile(mp4) and os.path.getsize(mp4) > 1024, "mp4 落盘且非空")
        frames = sorted(f for f in os.listdir(fdir) if f.startswith("f_"))
        check(len(frames) == 4, "帧数 = duration*fps = 4（实际 %d）" % len(frames))
        with Image.open(os.path.join(fdir, frames[0])) as im:
            check(im.size == (512, 512), "帧尺寸 512x512（实际 %s）" % (im.size,))
        check(st["selects"] == 1, "/select 被调用一次（模型切换）")
        check(st["seen_images"] == 3,
              "第 2 帧起才带 image 走 img2img（实际 %d）" % st["seen_images"])
        p = st["payload"] or {}
        check(p.get("output_format") == "png", "显式要 png")
        check(isinstance(p.get("denoise_strength"), float), "img2img 带 denoise_strength")

        section("稳定器：假后端每帧变暗，开稳定器亮度要回来")
        def lum_of(path):
            with Image.open(path) as im:
                return sum(ImageStat.Stat(im.convert("RGB")).mean) / 3.0

        first = lum_of(os.path.join(fdir, frames[0]))
        last = lum_of(os.path.join(fdir, frames[-1]))
        check(abs(last - first) < 12,
              "开稳定器：末帧亮度贴住首帧（%.1f vs %.1f）" % (last, first))

        st.update({"calls": 0, "seen_images": 0, "selects": 0, "state": "idle",
                   "serving": None, "width": None, "height": None})
        _mp4b, fdirb = localdream_backend(
            "a rainy street", duration=1, fps=4, size="512x512", seed=7,
            model="fake-model", out_dir=os.path.join(tmp, "raw"), steps=4,
            stabilize=False, anchor=0.0)
        framesb = sorted(f for f in os.listdir(fdirb) if f.startswith("f_"))
        firstb = lum_of(os.path.join(fdirb, framesb[0]))
        lastb = lum_of(os.path.join(fdirb, framesb[-1]))
        check(firstb - lastb > 30,
              "关稳定器：末帧明显更暗（%.1f -> %.1f），这就是帧链的真病"
              % (firstb, lastb))

        section("run() 默认后端")
        st.update({"calls": 0, "seen_images": 0, "selects": 0, "state": "idle",
                   "serving": None, "width": None, "height": None})
        r = run("a rainy street", duration=1, fps=3, size="512x512", steps=4,
                model="fake-model")
        check(r["ok"] is True, "run() 成功：%s" % (r["err"] or "ok"))
        check(bool(r["path"]) and os.path.isfile(r["path"]),
              "run() 返回的 mp4 真实存在")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for s in (ctl, gen):
            try:
                s.shutdown()
            except Exception:
                pass

    print("\n%s  %d passed, %d failed"
          % ("ALL PASS" if not tally["bad"] else "FAILED",
             tally["ok"], tally["bad"]))
    return 0 if not tally["bad"] else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("用法：python video_gen_runner.py --selftest")
