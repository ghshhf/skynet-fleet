# -*- coding: utf-8 -*-
"""天网 · 滑块设备（人机验证第一战场）
==========================================
本文件只做**编排**，算法在 `fleet/slider_cv.py`，浏览器在 `fleet/browser_reg.py`。
分成三层不是洁癖：

    slider_cv    ← 纯算法。两个 PIL 对象进，一个数字出。能单测、能离线、永远对
    browser_reg  ← 浏览器。起 chromium、连 CDP、找元素
    slider_device← 编排。谁挂了都不粘连对方（算法挂了还能干别的，浏览器挂了还能离线算）

**最重要的一条设计：executor 可注入。**

  真实拖动要起 chromium，而门禁不能依赖浏览器（一换环境就红，还会抢焦点）。
  所以 `slider.drag` / `slider.auto` 都收一个 `executor` 可注入参数：
  门禁注入一个记录器，就能在**完全离线**的情况下断言
  「事件序列对不对 / 该拖多远 / 不确定时有没有拒绝提交」。

  这和 protocol.register 的 identity/code 可注入是同一个套路 ——
  可测性不是测出来的，是设计进去的。
"""
import time

from ..core import BaseDevice, DeviceResult as R
from .. import slider_cv as SC
from ..vendored_slider import SLIDER_CAPABILITIES

try:
    from .. import browser_reg
except Exception:                                   # 缺 playwright 也不能拖垮整包 import
    browser_reg = None

_CAPS = tuple(c.name for c in SLIDER_CAPABILITIES)
_HANDLERS = {}

_CONF_ORDER = {"weak": 0, "medium": 1, "strong": 2}


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


# ---------------------------------------------------------------- 分发

@_reg("slider.detect")
def _h_detect(cap, args, dev):
    bg = SC.load_image(args["bg"])
    tile = SC.load_image(args["tile"])
    probe = None
    if args.get("probe"):
        try:
            y, x = [int(v) for v in str(args["probe"]).split(",")]
            probe = (y, x)
        except Exception:
            raise ValueError("probe 格式应为 'y,x'，收到 %r" % args["probe"])
    r = SC.detect(bg, tile, viewport_width=args.get("viewport_width"), probe=probe)
    return R.success(cap, dev.name, via=dev.via, data=r)


@_reg("slider.track")
def _h_track(cap, args, dev):
    pts, ts, dur = SC.gen_track(float(args["distance"]),
                                seed=args.get("seed"),
                                tile_width=float(args.get("tile_width") or 44))
    events = SC.to_cdp_events(pts, ts)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return R.success(cap, dev.name, via=dev.via, data={
        "distance": float(args["distance"]),
        "points": len(pts),
        "duration": round(dur, 3),
        "x_max": round(max(xs), 2),
        "y_span": round(max(ys) - min(ys), 2),
        "events": events,
        "sampling_ms": [round((b - a) * 1000, 1) for a, b in zip(ts, ts[1:])],
    })


# ---------------------------------------------------------------- 真发事件

def _dispatch(page, events, settle=0.05):
    """在给定 page 上逐点发送鼠标事件。**不用 page.mouse.move** —— 那是高层 API，
    会自己插值，轨迹就废了。

    坐标是绝对的（生成时已加过起点），这里直接发。
    """
    prev_t = 0.0
    cdp = page.context.new_cdp_session(page)
    sent = 0
    try:
        for e in events:
            dt = e["t"] - prev_t
            if dt > 0:
                time.sleep(min(dt, 0.25))   # 单步最多等 250ms，防一条坏轨迹把任务挂死
            prev_t = e["t"]
            cdp.send("Input.dispatchMouseEvent", {
                "type": e["type"], "x": float(e["x"]), "y": float(e["y"]),
                "button": e.get("button", "left"),
                "buttons": int(e.get("buttons", 0)),
                "clickCount": int(e.get("clickCount", 1)),
            })
            sent += 1
        time.sleep(max(0.0, settle))
    finally:
        try:
            cdp.detach()
        except Exception:
            pass
    return sent


def _send_via_cdp(events, port=9222, handle=None, settle=0.05, page=None):
    """发事件。page 给了就用它（调用方已持有会话），否则自己起一个 Session。"""
    if page is not None:                       # 复用调用方会话，不再另起一个
        return {"ok": True, "sent": _dispatch(page, events, settle=settle),
                "handle": handle, "start_box": None, "reused_page": True}
    if browser_reg is None:
        raise RuntimeError("browser_reg 不可用（缺 playwright）")
    with browser_reg.Session(port) as s:
        box = None
        if handle:
            el = s.page.query_selector(handle)
            if el is None:
                raise RuntimeError("找不到滑块元素：%s" % handle)
            box = el.bounding_box()
            if not box:
                raise RuntimeError("滑块元素不可见（bounding_box 为空）：%s" % handle)
        sent = _dispatch(s.page, events, settle=settle)
    return {"ok": True, "sent": sent, "handle": handle, "start_box": box}


def _handle_origin(port, handle):
    """拿滑块元素的中心点，作为拖动的起点。没给 handle 就用 (0,0) 相对坐标。"""
    if not handle:
        return 0.0, 0.0, None
    if browser_reg is None:
        raise RuntimeError("browser_reg 不可用（缺 playwright）")
    with browser_reg.Session(port) as s:
        el = s.page.query_selector(handle)
        if el is None:
            raise RuntimeError("找不到滑块元素：%s" % handle)
        box = el.bounding_box()
        if not box:
            raise RuntimeError("滑块元素不可见：%s" % handle)
        return (box["x"] + box["width"] / 2.0, box["y"] + box["height"] / 2.0, box)


def _do_drag(distance, handle, port, seed, dry_run, executor, tile_width=44.0,
             origin=None, page=None):
    """轨迹 → 事件 → 发送。返回 (events, meta, sent_info)。

    origin 显式给了就用它（元素在 iframe 里时，主文档的 query_selector 找不到，
    必须由调用方把已经算好的中心点传进来）。
    """
    x0, y0, box = 0.0, 0.0, None
    if origin:
        x0, y0 = float(origin[0]), float(origin[1])
    elif not dry_run and not callable(executor):
        x0, y0, box = _handle_origin(port, handle)
    pts, ts, dur = SC.gen_track(distance, seed=seed, tile_width=tile_width)
    events = SC.to_cdp_events(pts, ts, x0=x0, y0=y0)
    meta = {
        "distance": round(float(distance), 2),
        "points": len(pts),
        "duration": round(dur, 3),
        "origin": [round(x0, 2), round(y0, 2)],
        "handle_box": box,
        "seed": seed,
        "tile_width": float(tile_width),
    }
    if callable(executor):
        info = executor(events, meta)
        return events, meta, {"ok": True, "mode": "executor",
                              "result": info if isinstance(info, dict) else str(info)}
    if dry_run:
        return events, meta, {"ok": True, "mode": "dry_run", "sent": 0}
    info = _send_via_cdp(events, port=port, handle=handle, page=page)
    info["mode"] = "cdp"
    return events, meta, info


@_reg("slider.drag")
def _h_drag(cap, args, dev):
    _, meta, info = _do_drag(
        distance=float(args["distance"]),
        handle=args.get("handle"),
        port=int(args.get("port") or 9222),
        seed=args.get("seed"),
        dry_run=bool(args.get("dry_run")),
        executor=args.get("executor"),
    )
    return R.success(cap, dev.name, via=dev.via, data={"drag": meta, "send": info})


# ---------------------------------------------------------------- 一条龙

@_reg("slider.auto")
def _h_auto(cap, args, dev):
    """detect → 判把握 → track → drag。

    中间那道「把握不够就不提交」是本能力最值钱的地方：
      · 滑块输一次通常只是换张图
      · 乱提交一次可能触发风控降级（后面要过就更难）
    所以 weak 一律不提交，除非调用方显式把 min_confidence 降到 weak。
    """
    min_conf = str(args.get("min_confidence") or "medium").lower()
    if min_conf not in _CONF_ORDER:
        raise ValueError("min_confidence 只能是 weak / medium / strong")

    bg = SC.load_image(args["bg"])
    tile = SC.load_image(args["tile"])
    det = SC.detect(bg, tile, viewport_width=args.get("viewport_width"))

    got = _CONF_ORDER.get(det["confidence"], 0)
    need = _CONF_ORDER[min_conf]
    base = {"detect": det, "min_confidence": min_conf}
    if got < need:
        # 不确定 -> 明确回报不确定，不拖。这不是失败，是纪律。
        base["submitted"] = False
        base["why"] = ("判断把握 %s 低于要求 %s，未提交（换张图重试比硬拖更安全）"
                       % (det["confidence"], min_conf))
        return R.success(cap, dev.name, via=dev.via, data=base)

    distance = det["x_display"] if args.get("viewport_width") else det["x"]
    _, meta, info = _do_drag(
        distance=float(distance),
        handle=args.get("handle"),
        port=int(args.get("port") or 9222),
        seed=args.get("seed"),
        dry_run=bool(args.get("dry_run")),
        executor=args.get("executor"),
        tile_width=float((det.get("tile_size") or [44])[0]),
    )
    base.update({"submitted": info.get("mode") != "dry_run",
                 "distance": round(float(distance), 2),
                 "drag": meta, "send": info})
    return R.success(cap, dev.name, via=dev.via, data=base)


# ---------------------------------------------------------------- 打真实页面

def _page_ctx(args, port):
    """拿 page，返回 (page, close_fn)。注入的 page 不关（生命周期归调用方），自起的一定关。"""
    page = args.get("page")
    if page is not None:
        return page, (lambda: None)
    if browser_reg is None:
        raise RuntimeError("browser_reg 不可用（缺 playwright）")
    sess = browser_reg.Session(port)
    page = sess.__enter__()

    def _close():
        try:
            sess.__exit__(None, None, None)
        except Exception:
            pass
    return page, _close


@_reg("slider.solve_page")
def _h_solve_page(cap, args, dev):
    """认墙 → 定位 → 截图 → 算缺口 → 拖 → **再认墙**。

    最后那一步是整个能力的灵魂：不做它，就等于把「我拖完了」当成「我进去了」。
    滑块拖完只是提交了一次，过没过要看墙还在不在 —— 这是两件完全不同的事，
    混起来会让人在风控已经降级的情况下还以为自己很顺。
    """
    min_conf = str(args.get("min_confidence") or "medium").lower()
    if min_conf not in _CONF_ORDER:
        raise ValueError("min_confidence 只能是 weak / medium / strong")
    port = int(args.get("port") or 9222)
    settle = float(args.get("settle") if args.get("settle") is not None else 2.5)
    dry_run = bool(args.get("dry_run"))
    executor = args.get("executor")
    steps = []
    out = {"steps": steps, "min_confidence": min_conf}   # 同一个 dict 引用，后面 append 就同步

    def done(_unused=None):
        return R.success(cap, dev.name, via=dev.via, data=out)

    page, close = _page_ctx(args, port)
    try:
        # ① 认墙：先看清面对的是什么，别急着动手
        wall = browser_reg.wall_state(page)
        v = wall.get("verify") or {}
        steps.append({"step": "wall", "blocked": wall.get("blocked"),
                      "vendor": v.get("vendor"), "kind": v.get("kind")})
        out["wall_before"] = wall

        if not v.get("found"):
            out.update({"ok": False, "passed": wall.get("blocked") is False,
                        "why": "页面上没认到人机验证（verify.found=False）"})
            return done(out)
        if v.get("kind") != "slider":
            out.update({"ok": False, "passed": None,
                        "why": "不是滑块（%s / %s）。%s"
                               % (v.get("vendor"), v.get("kind"), v.get("advice"))})
            return done(out)

        # ② 定位（含跨域 iframe）
        loc = browser_reg.locate_slider(
            page, vendor=args.get("vendor") or v.get("vendor"), want_png=True)
        steps.append({"step": "locate", "ok": loc.get("ok"),
                      "in_iframe": loc.get("in_iframe"),
                      "frame": (loc.get("frame_url") or "")[:80]})
        out["locate"] = loc
        if not loc.get("ok"):
            out.update({"ok": False, "passed": None, "why": loc.get("err")})
            return done(out)
        if not (loc.get("bg") or {}).get("png_b64"):
            out.update({"ok": False, "passed": None, "why": "没截到背景图"})
            return done(out)
        if not (loc.get("slice") or {}).get("png_b64"):
            # 诚实的边界：腾讯/阿里的位移式滑块只给一张整图，没有单独的缺口块，
            # 我们现在的算法要两张图。整图分析还没做，就说还没做。
            out.update({"ok": False, "passed": None,
                        "why": "%s 没有单独的缺口图，当前算法需要「背景 + 缺口」两张；"
                               "整图位移分析尚未实现" % v.get("vendor")})
            return done(out)

        # ③ 算缺口（viewport_width 用元素的显示宽度，自动处理 2x 截图）
        bg_img = SC.load_image(loc["bg"]["png_b64"])
        t_img = SC.load_image(loc["slice"]["png_b64"])
        det = SC.detect(bg_img, t_img,
                        viewport_width=(loc["bg"].get("box") or {}).get("w"))
        steps.append({"step": "detect", "x": det["x"], "confidence": det["confidence"],
                      "basis": det["basis"], "scale": det.get("scale")})
        out["detect"] = det

        # ④ 把握不够就不提交（滑块输一次只是换图，乱提交可能触发风控降级）
        if _CONF_ORDER.get(det["confidence"], 0) < _CONF_ORDER[min_conf]:
            out.update({"ok": False, "submitted": False,
                        "why": "把握 %s 低于要求 %s，未提交（换张图重试比硬拖安全）"
                               % (det["confidence"], min_conf)})
            steps.append({"step": "abort", "why": "low_confidence"})
            return done(out)

        # ⑤ 拖（起点用把手中心，元素在 iframe 里也成立 —— box 已换算到主视口）
        hb = (loc.get("handle") or {}).get("box")
        origin = (hb["x"] + hb["w"] / 2.0, hb["y"] + hb["h"] / 2.0) if hb else None
        distance = det.get("x_display")
        if distance is None:
            distance = det["x"]
        _, meta, info = _do_drag(
            distance=float(distance), handle=None, port=port,
            seed=args.get("seed"), dry_run=dry_run, executor=executor,
            origin=origin, tile_width=float((det.get("tile_size") or [44])[0]),
            page=page)
        steps.append({"step": "drag", "distance": meta["distance"],
                      "mode": info.get("mode")})
        out["drag"] = meta
        out["send"] = info
        out["submitted"] = info.get("mode") != "dry_run"

        # ⑥ 再认墙 —— 拖完 ≠ 过了
        if dry_run or callable(executor):
            out.update({"ok": True, "passed": None,
                        "why": "dry_run/executor 模式没有真提交，无从判断过没过"})
            return done(out)
        if settle > 0:
            time.sleep(settle)
        wall2 = browser_reg.wall_state(page)
        v2 = wall2.get("verify") or {}
        out["wall_after"] = wall2
        if wall2.get("error"):
            out.update({"ok": True, "passed": None,
                        "why": "拖动后页面报错（%s），判断不了" % wall2.get("error")})
        elif not wall2.get("blocked") and not v2.get("found"):
            out.update({"ok": True, "passed": True,
                        "why": "墙没了（verify.found=False），这一轮过了"})
        else:
            out.update({"ok": True, "passed": False,
                        "why": "还挡着（%s / %s）—— 拖完了但没进去"
                               % (v2.get("vendor"), v2.get("kind"))})
        steps.append({"step": "verify", "passed": out["passed"]})
        return done(out)
    finally:
        close()


# ---------------------------------------------------------------- 设备

class SliderDevice(BaseDevice):
    kind = "slider"
    via = "local"

    def __init__(self, name="slider:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "滑块验证（人机验证第一战场）；视觉找缺口 + 造像人轨迹 + CDP 逐点拖动，"
                    "判断不确定时明确拒提交")

    def probe(self):
        try:
            import numpy  # noqa: F401
            from PIL import Image  # noqa: F401
        except Exception as e:
            return False, "缺 numpy/Pillow：%s" % e
        return True, ""

    def caps(self):
        return _CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in _CAPS:
            return R.unsupported(cap, "slider 设备没有这个能力", device=self.name, via=self.via)
        h = _HANDLERS.get(cap)
        if h is None:
            return R.unavailable(cap, "能力已声明，handler 待接入",
                                 device=self.name, via=self.via)
        try:
            return h(cap, args, self)
        except KeyError as e:
            return R.error(cap, "缺必填参数：%s" % e, device=self.name, via=self.via)
        except Exception as e:
            return R.error(cap, "handler 执行异常：%s" % e, device=self.name, via=self.via)
