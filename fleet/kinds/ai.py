"""AI：终端里的免费模型层。

这一层把「免费 AI」当成一台设备来管，跟其它设备走同一条路：
注册表登记 → 闸门 → 结果信封 → 终端两栏同时显形。能力名 domain 用 `ai.*`。

为什么 pollinations 免 key 是默认：学 Cherry 把免费端点当一等公民——零配置开箱即用，
不至于「装好才知道一个模型都用不了」。本地模型（openai 兼容端点）有 FLEET_LLM_URL
才活；llm7 有 token 才活。外部 token 永远从环境变量读。

多模型同问（学 Cherry 的「多模型对比」+ 话题内 executions 并行）：`ai.chat` 默认把
问题并发丢给所有可用后端，按 [后端id] 同屏——人一眼看出谁答得更好。长任务流式回流
走 `_on_line`（跟 web.* 同一条管道），界面才有真进度。

后端清单与探测见 `ai_backends.py`（四元组：id / protocol / base_url / model）。
"""
import hashlib
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .. import ai_backends as AB
from ..core import BaseDevice, DeviceResult as R
from .mediaops import _unique

# 🔴 默认 max_tokens 为什么是 2048（2026-10-21 实测抓到的坑）：
#   minimax-m3.1-flash 这类**推理模型**会先花一大截 token 思考，再吐正文。
#   带 system prompt 时 max_tokens=300 → finish_reason=length、正文 0 字符
#   （界面看着像「模型没话说」，其实是预算被推理烧光）；
#   同样参数 1024/2048 → finish_reason=stop，正文正常。
#   原默认 512 正好落在「够不着正文」的区间里 ⇒ 默认给 2048。
DEFAULT_MAX_TOKENS = 2048
DEFAULT_NEG = "blurry, low quality, watermark, deformed, extra limbs, text"
SEED_MAX = 2 ** 31 - 1


def _num(value, label, low, high, default=None, integer=False):
    """严格参数校验：非法输入在进入昂贵的生成之前变成明确错误。"""
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
    """宽高必须是 8 的倍数：扩散模型潜在空间按 8 像素下采样。"""
    parts = str(value or "512x512").strip().lower().split("x")
    if len(parts) != 2:
        raise ValueError("size 要写成 宽x高，如 512x512")
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("size 的宽高必须是整数：%r" % value) from None
    for label, side in (("宽", width), ("高", height)):
        if not 64 <= side <= 2048:
            raise ValueError("%s 必须在 64~2048，收到 %d" % (label, side))
        if side % 8:
            raise ValueError("%s 必须是 8 的倍数，收到 %d" % (label, side))
    return "%dx%d" % (width, height)


def _sidecar(file_path, payload):
    """把这次生成的完整参数写成台账：没有它就无法复现，也无法排查旧版本。"""
    target = _unique(Path(file_path).with_suffix(".json"))
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return str(target)


def _verify_image(path):
    """真打开一次：魔术字节只能认格式，不能证明文件没坏、尺寸对不对。

    Pillow 不在位时如实返回 None（「没验」），不假装验过、也不把好图挡掉。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as handle:
            handle.verify()
        with Image.open(path) as handle:
            return handle.width, handle.height, (handle.format or "").lower()
    except Exception as exc:
        raise ValueError("图片打不开或已损坏：%s" % exc) from exc


def _img_fmt(data):
    """按魔术字节认格式——Pollinations 说给 PNG，实测回 JPEG，别信扩展名。"""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


class AI(BaseDevice):
    kind = "ai"
    via = "web"

    def __init__(self, name="ai:free", **kw):
        super().__init__(name, via="web", **kw)
        self.meta.setdefault("layer", "ai")

    def probe(self):
        bs = AB.backends()
        if not bs:
            return False, ("没有任何可用后端：联网状态下 pollinations（免key）会自动在；"
                           "本地模型需设 FLEET_LLM_URL")
        return True, ""

    def caps(self):
        return ("ai.list", "ai.status", "ai.complete", "ai.chat", "image.gen",
                "image.free")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "ai.list":
            return self._list(args)
        if cap == "ai.status":
            return self._status(args)
        if cap == "ai.complete":
            return self._complete(args)
        if cap == "ai.chat":
            return self._chat(args)
        if cap == "image.gen":
            return self._image_gen(args)
        if cap == "image.free":
            return self._image_free(args)
        return super().invoke(cap, args)

    # ---------------- 出图（经手机 SdBridge）----------------

    def _image_gen(self, args):
        """生成一张图：先挡非法参数，再交给 image_gen_runner（后端可注入）。

        runner 未配置或失败一律 unavailable 并带原因，不静默交空。
        出图是写文件副作用 ⇒ side_effect=True；参数随台账落盘，便于复现。
        """
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return R.error("image.gen", "prompt 不能为空",
                           device=self.name, via=self.via)
        try:
            size = _size(args.get("size"))
            steps = _num(args.get("steps"), "steps", 1, 100, 20, integer=True)
            cfg = _num(args.get("cfg"), "cfg", 0, 20, 7.0)
            seed_raw = _num(args.get("seed"), "seed", -1, SEED_MAX, -1, integer=True)
        except ValueError as exc:
            return R.error("image.gen", str(exc), device=self.name, via=self.via)
        seed = (int(time.time() * 1000) % SEED_MAX) if seed_raw < 0 else seed_raw
        neg = str(args.get("neg") or DEFAULT_NEG)
        model = str(args.get("model") or "").strip()
        try:
            import image_gen_runner as IG
        except Exception as e:
            return R.unavailable("image.gen", "出图后端未配置：%s" % e,
                                 device=self.name, via=self.via)
        res = IG.run(prompt, neg=neg, size=size, steps=steps, seed=seed,
                     cfg=cfg, model=model or None)
        if not isinstance(res, dict) or not res.get("ok"):
            return R.unavailable("image.gen",
                                 (res or {}).get("err") or "出图失败",
                                 device=self.name, via=self.via)
        path = res.get("path") or ""
        payload = {"cap": "image.gen", "prompt": prompt, "negative": neg,
                   "size": size, "steps": steps, "cfg": cfg, "seed": seed,
                   "model": model, "backend": "android_bridge",
                   "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        manifest = ""
        if path and os.path.isfile(path):
            payload["file"] = path
            manifest = _sidecar(path, payload)
        return R.success("image.gen", self.name, side_effect=True, via=self.via,
                         data={"path": path, "backend": "android_bridge",
                               "seed": seed, "size": size, "steps": steps,
                               "cfg": cfg, "model": model, "manifest": manifest})

    # ---------------- 只读 ----------------

    def _list(self, args):
        # 每个端点附带健康/冷却状态，让调用方一眼看出谁当前可用
        out = []
        for b in AB.backends():
            cd = AB.cooldown_remaining(b["id"])
            out.append({"id": b["id"], "title": b["title"],
                        "healthy": cd <= 0, "cooldown_sec": cd})
        return R.success("ai.list", self.name, via=self.via, data={"backends": out})

    def _status(self, args):
        """逐个后端真发请求探测连通（学 Cherry 的「检测」按钮 = probe 契约）。"""
        out = []
        for b in AB.backends():
            ok, reason = AB.probe(b)
            out.append({"id": b["id"], "title": b["title"],
                        "ok": ok, "reason": reason})
        return R.success("ai.status", self.name, via=self.via,
                         data={"backends": out})

    # ---------------- 动手（流式）----------------

    def _complete(self, args):
        """向一个后端要一段完整回复（不流式）。走 failover：依次试候选端点，
        第一个产出正文的胜出，切换对调用方不可见（只在 backend 字段暴露实际用了谁）。"""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return R.error("ai.complete", "prompt 不能为空",
                           device=self.name, via=self.via)
        bs = self._pick(args.get("backends") or "")
        if not bs:
            spec = (args.get("backends") or "").strip()
            have = ", ".join(b["id"] for b in AB.backends()) or "(无)"
            why = (f"后端「{spec}」不存在或无健康端点，现有可用：{have}"
                   if spec else "没有可用（健康）后端")
            return R.unavailable("ai.complete", why, device=self.name, via=self.via)
        try:
            text, via = AB.chat_fallback(
                bs, prompt, system=args.get("system") or "",
                max_tokens=int(args.get("max_tokens") or DEFAULT_MAX_TOKENS))
        except Exception as e:
            return R.error("ai.complete", f"全部后端失败: {e}",
                           device=self.name, via=self.via)
        if not (text or "").strip():
            return R.unavailable("ai.complete",
                                 f"{via} 空回：这次流里没有正文（上游临时抽风）",
                                 device=self.name, via=self.via)
        return R.success("ai.complete", self.name, side_effect=True, via=self.via,
                         data={"backend": via, "text": text})

    def _chat(self, args):
        """流式对话。默认所有可用后端并发，按 [id] 同屏；可 --backends a,b 指定。"""
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return R.error("ai.chat", "prompt 不能为空",
                           device=self.name, via=self.via)
        bs = self._pick(args.get("backends") or "")
        if not bs:
            # 指名要某个后端却一个都没匹配上：必须把「你要的名字」和「现有名字」
            # 都打出来。只回一句「没有可用后端」会让人以为是网络问题。
            spec = (args.get("backends") or "").strip()
            have = ", ".join(b["id"] for b in AB.backends()) or "(无)"
            why = (f"后端「{spec}」不存在，现有可用：{have}" if spec
                   else "没有可用后端")
            return R.unavailable("ai.chat", why,
                                 device=self.name, via=self.via)
        on_line = args.get("_on_line")
        mt = int(args.get("max_tokens") or DEFAULT_MAX_TOKENS)
        system = args.get("system") or ""
        results = {}

        if len(bs) == 1:
            try:
                results[bs[0]["id"]] = self._one(bs[0], prompt, system, mt, on_line)
            except Exception as e:
                results[bs[0]["id"]] = f"[错误: {e}]"
        else:
            # 多模型同问：并发跑，谁先回谁先显形（学 Cherry 话题内 executions 并行）
            lock = threading.Lock()

            def work(b):
                try:
                    t = self._one(b, prompt, system, mt, on_line)
                    with lock:
                        results[b["id"]] = t
                except Exception as e:
                    with lock:
                        results[b["id"]] = f"[错误: {e}]"
            ths = [threading.Thread(target=work, args=(b,), daemon=True) for b in bs]
            for t in ths:
                t.start()
            for t in ths:
                t.join()

        return R.success("ai.chat", self.name, side_effect=True, via=self.via,
                         data={"prompt": prompt,
                               "backends": list(results.keys()),
                               "answers": results})

    def _one(self, b, prompt, system, mt, on_line):
        """单个后端：流式拿 token，攒成行再经 _on_line 回流（界面才是一行行长出来）。

        SSE 给的是 token 不是行；这里按换行切，每段完整行回流一次，末尾残行也吐掉。
        """
        buf = {"t": ""}

        def on_token(delta):
            buf["t"] += delta
            while "\n" in buf["t"]:
                head, buf["t"] = buf["t"].split("\n", 1)
                if on_line:
                    on_line(f"[{b['id']}] {head}")

        text = AB.chat(b, prompt, system=system, on_token=on_token, max_tokens=mt)
        if buf["t"].strip() and on_line:
            on_line(f"[{b['id']}] {buf['t']}")
        if not (text or "").strip():
            # 空回是最阴的那种失败：界面上看着像「模型没话说」，实则是流里根本没有
            # 正文——上游临时抽风（实测 15 次里偶发 1 次，curl 直连也复现不了规律）。
            # 静默失败不许过：失败必须给原因，否则人只能盯着一块空白猜。
            note = ("[空回] 这个后端这次流里没有正文（上游临时抽风，实测偶发）；"
                    "换个后端或再问一次")
            if on_line:
                on_line(f"[{b['id']}] {note}")
            return note
        return text

    # ---------------- 出图（免 key 云端：Pollinations）----------------

    def _image_free(self, args):
        """免 key 出图：Pollinations 的 image 端点，零配置、不要 token。

        和 `image.gen`（走手机 SdBridge）分工：
          image.gen  —— 手边有手机、要它本地算（可控、不排队）
          image.free —— 手机不在场时的兜底：公网免费端点，一条 URL 就是一次生成
        为什么要它：内容生成这块只有一条腿（手机），手机一断就整块瘫——
        多一条免 key 的公网腿，才能「谁在谁上」。

        出图不是只读：写文件 ⇒ side_effect=True。参数先校验、seed 进 URL、
        完整参数写同 stem 的 .json 台账，输出另取编号绝不覆盖旧图。
        选路不写死端口：天网自有 7891 → 系统代理 → 直连，逐个试。
        """
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return R.error("image.free", "缺 prompt", device=self.name, via=self.via)
        try:
            w = _num(args.get("width"), "width", 64, 2048, 1024, integer=True)
            h = _num(args.get("height"), "height", 64, 2048, 1024, integer=True)
            seed_raw = _num(args.get("seed"), "seed", -1, SEED_MAX, -1, integer=True)
        except ValueError as exc:
            return R.error("image.free", str(exc), device=self.name, via=self.via)
        seed = (int(time.time() * 1000) % SEED_MAX) if seed_raw < 0 else seed_raw
        model = str(args.get("model") or "flux").strip() or "flux"
        given = str(args.get("out") or "").strip()
        url = ("https://image.pollinations.ai/prompt/%s?width=%d&height=%d"
               "&nologo=true&model=%s&seed=%d"
               % (urllib.parse.quote(prompt), w, h,
                  urllib.parse.quote(model), seed))

        data = None
        last = None
        for proxy in self._proxy_chain():
            try:
                if proxy:
                    op = urllib.request.build_opener(
                        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
                else:
                    op = urllib.request.build_opener()
                req = urllib.request.Request(url, headers={"User-Agent": "fleet-image/1.0"})
                with op.open(req, timeout=90) as r:
                    data = r.read()
                if data:
                    break
            except Exception as e:
                last = e
        if not data:
            return R.unavailable("image.free", "出图失败（三条路都不通）: %s" % last,
                                 device=self.name, via=self.via)

        fmt = _img_fmt(data)
        if not fmt:
            return R.error("image.free", "回来的不是图片（%d 字节，头 %r）"
                           % (len(data), data[:8]), device=self.name, via=self.via)
        if given:
            target = Path(given).expanduser().absolute()
            root, ext = os.path.splitext(str(target))
            if ext.lower().lstrip(".") != fmt:  # 要 .png 回来 jpg：以真实格式为准
                target = Path(root + "." + fmt)
        else:
            tag = hashlib.sha256(url.encode()).hexdigest()[:10]
            target = Path(os.getcwd()) / "tmp" / ("img_%s.%s" % (tag, fmt))
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _unique(target)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(str(tmp), str(target))
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

        try:
            checked = _verify_image(target)
        except ValueError as exc:
            target.unlink(missing_ok=True)
            return R.error("image.free", str(exc), device=self.name, via=self.via)
        real_w, real_h, real_fmt = checked or (None, None, None)
        checked_ok = checked is not None
        payload = {"cap": "image.free", "prompt": prompt, "width": w, "height": h,
                   "model": model, "seed": seed, "format": fmt, "bytes": len(data),
                   "real_width": real_w, "real_height": real_h,
                   "real_format": real_fmt, "verified": checked_ok,
                   "source": "pollinations（免 key）", "file": str(target),
                   "url_digest": hashlib.sha256(url.encode()).hexdigest()[:16],
                   "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        manifest = _sidecar(str(target), payload)
        return R.success("image.free", self.name, side_effect=True, via=self.via, data={
            "prompt": prompt, "model": model, "width": w, "height": h,
            "file": str(target), "path": str(target), "bytes": len(data),
            "format": fmt, "seed": seed, "manifest": manifest,
            "real_width": real_w, "real_height": real_h, "verified": checked_ok,
            "source": "pollinations（免 key）",
        })

    def _proxy_chain(self):
        """候选链：天网 7891 → 系统代理 → 直连（None）。取不到就只留直连。"""
        cands = []
        try:
            from .. import net as N
            url, _ = N.resolve_proxy()
            if url:
                cands.append(url)
            surl, _ = N.system_proxy_url()
            if surl and surl not in cands:
                cands.append(surl)
        except Exception:
            pass
        return cands + [None]

    def _pick(self, spec):
        # 「all」/空 → 只返回健康端点（冷却中的不打扰）；显式指定 → 按名过滤（尊重用户选择）
        if not spec or spec.strip().lower() in ("all", ""):
            return AB.healthy_backends()
        want = {s.strip() for s in spec.split(",") if s.strip()}
        return [b for b in AB.backends() if b["id"] in want]


# ---------------------------------------------------------------- 出图自测

def _selftest():
    """全离线：网络与出图后端都打桩，只验参数契约、台账与不覆盖。"""
    import shutil
    import sys
    import tempfile
    import types

    total = 0
    fails = []
    folder = tempfile.mkdtemp(prefix="ai-image-test-")
    # 打桩图必须是真能打开的图片：加了真校验后，假的魔术字节会被正确地挡掉。
    has_pil = True
    try:
        import io
        from PIL import Image as _PIL
        _buf = io.BytesIO()
        _PIL.new("RGB", (8, 8), (201, 33, 33)).save(_buf, format="JPEG")
        jpeg = _buf.getvalue()
    except ImportError:
        has_pil = False
        jpeg = b"\xff\xd8" + b"fakejpeg" * 20
    seen_urls = []
    orig_opener = urllib.request.build_opener
    orig_module = sys.modules.get("image_gen_runner")

    def check(cond, label):
        nonlocal total
        total += 1
        if not cond:
            fails.append(label)
            print("失败：%s" % label)

    class _Resp:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            if isinstance(self.data, Exception):
                raise self.data
            return self.data

    class _Opener:
        def __init__(self, data):
            self.data = data

        def open(self, req, timeout=None):
            seen_urls.append(req.full_url)
            return _Resp(self.data)

    def fake_opener(*_a, **_kw):
        return _Opener(state["payload"])

    state = {"payload": jpeg}

    try:
        dev = AI()
        check("image.gen" in dev.caps() and "image.free" in dev.caps(), "两项出图能力在位")
        check(_size("512x512") == "512x512" and _size("512X512") == "512x512", "尺寸：标准写法")
        for value in ("512", "0x512", "500x512", "512x2049", "abc"):
            try:
                _size(value)
                check(False, "尺寸：拒绝 %r" % value)
            except ValueError:
                check(True, "尺寸：拒绝 %r" % value)
        check(_img_fmt(jpeg) == "jpg" and _img_fmt(b"\x89PNG\r\n\x1a\n") == "png",
              "格式按魔术字节认，不信扩展名")
        check(_img_fmt(b"not an image") is None, "格式：非图片返回 None")

        # image.gen：参数先挡、后端打桩避免碰真手机
        fake_module = types.ModuleType("image_gen_runner")
        captured = {}

        def fake_run(prompt, neg="", size="512x512", steps=20, seed=-1,
                     cfg=7.0, model=None, **kw):
            captured.update({"prompt": prompt, "neg": neg, "size": size,
                             "steps": steps, "seed": seed, "cfg": cfg})
            return {"ok": False, "err": "打桩：没接手机"}

        fake_module.run = fake_run
        sys.modules["image_gen_runner"] = fake_module
        r = dev.invoke("image.gen", {"prompt": ""})
        check(not r.ok, "gen：空 prompt 被拒")
        for label, patch in (("非法尺寸", {"size": "500x512"}),
                             ("步数越界", {"steps": 0}),
                             ("CFG 越界", {"cfg": 99}),
                             ("种子越界", {"seed": -5})):
            args = {"prompt": "一只猫"}
            args.update(patch)
            r = dev.invoke("image.gen", args)
            check(not r.ok and r.code == "error", "gen：%s 明确报错" % label)
        r = dev.invoke("image.gen", {"prompt": "一只猫", "seed": 7, "steps": 12})
        check(not r.ok and r.code == "unavailable", "gen：后端未配置诚实降级")
        check(captured.get("seed") == 7 and captured.get("size") == "512x512"
              and captured.get("steps") == 12, "gen：参数被规整后传给后端")
        check("blurry" in captured.get("neg", ""), "gen：带默认负向提示词")

        # image.free：打桩网络，真落文件 + 台账
        urllib.request.build_opener = fake_opener
        root = Path(folder)
        out = root / "cat.png"           # 故意给 .png，回来是 jpg，应自动改后缀
        r = dev.invoke("image.free", {"prompt": "一只猫", "seed": 123,
                                      "width": 512, "height": 512, "out": str(out)})
        check(r.ok and r.code == "ok", "free：成功：%s" % r.err)
        file1 = None
        first_bytes = b""
        if r.ok:
            d = r.data
            check(r.side_effect is True, "free：结果信封标副作用")
            check(d["format"] == "jpg" and d["path"].endswith(".jpg"),
                  "free：以魔术字节为准改后缀")
            check(d["path"] == d["file"], "free：path 与 file 指向同一文件")
            check(d["seed"] == 123, "free：seed 回显")
            check(any("seed=123" in u for u in seen_urls), "free：seed 进了请求 URL")
            check(d["verified"] is has_pil, "free：是否真验过如实回显")
            if has_pil:
                check(d["real_width"] == 8 and d["real_height"] == 8,
                      "free：真打开读出实际尺寸")
            file1 = Path(d["path"])
            sidecar = Path(d["manifest"])
            check(file1.is_file() and file1.read_bytes() == jpeg, "free：文件真落盘")
            check(sidecar.is_file() and json.loads(sidecar.read_text(encoding="utf-8"))["seed"] == 123,
                  "free：台账落盘且记下完整参数")
            first_bytes = file1.read_bytes()
        r2 = dev.invoke("image.free", {"prompt": "一只猫", "seed": 123,
                                       "width": 512, "height": 512, "out": str(out)})
        check(file1 is not None and r2.ok and r2.data["path"] != str(file1),
              "free：同名再跑另取编号")
        check(file1 is not None and first_bytes == file1.read_bytes(),
              "free：旧图没被覆盖")

        r = dev.invoke("image.free", {"prompt": ""})
        check(not r.ok, "free：空 prompt 被拒")
        for label, patch in (("宽度非数字", {"width": "abc"}),
                             ("宽度越界", {"width": 9999}),
                             ("高度为零", {"height": 0}),
                             ("种子越界", {"seed": -9})):
            args = {"prompt": "一只猫"}
            args.update(patch)
            rr = dev.invoke("image.free", args)
            check(not rr.ok and rr.code == "error", "free：%s 明确报错" % label)
        state["payload"] = b"this is not an image"
        r = dev.invoke("image.free", {"prompt": "一只猫", "out": str(root / "bad.jpg")})
        check(not r.ok and r.code == "error", "free：回来的不是图片被拒")
        if has_pil:
            state["payload"] = b"\xff\xd8" + b"fakejpeg" * 20
            r = dev.invoke("image.free", {"prompt": "一只猫",
                                          "out": str(root / "broken.jpg")})
            check(not r.ok and r.code == "error" and not (root / "broken.jpg").exists(),
                  "free：伪 JPEG 被真校验挡掉且不留下坏文件")
        state["payload"] = OSError("三条路都不通")
        r = dev.invoke("image.free", {"prompt": "一只猫"})
        check(not r.ok and r.code == "unavailable", "free：三条路全断报 unavailable")
    finally:
        urllib.request.build_opener = orig_opener
        if orig_module is None:
            sys.modules.pop("image_gen_runner", None)
        else:
            sys.modules["image_gen_runner"] = orig_module
        shutil.rmtree(folder, ignore_errors=True)

    got = total - len(fails)
    print("\nai image selftest: %d/%d%s"
          % (got, total, "" if not fails else "（失败 %d）" % len(fails)))
    for item in fails:
        print("   失败:", item)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv:
        _sys.exit(_selftest())
    print("用法: python -m fleet.kinds.ai --selftest")
