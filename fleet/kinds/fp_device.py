# -*- coding: utf-8 -*-
"""天网 · 伪装层设备（整机汇报）
====================================
本文件只做**编排**，三块零件各在自己的文件里：

    fp_profile.py   档案 + 矛盾校验   —— 纯标准库，能离线、永远对
    fp_inject.py    CDP 原生 + JS 补丁 —— 纯标准库，JS 是字符串常量，不真跑
    fp_sandbox.py   profile + 出口绑定 —— 纯标准库，只管目录和账

    这里只负责「把参数传对、把结果翻译成人话」。

为什么这么分（跟 slider_device 同一个理由）
-------------------------------------------
可测性不是测出来的，是设计进去的。
三块零件全部不依赖浏览器，所以门禁 t69 能在**没有 playwright 的 python** 上跑完。
需要真浏览器的只有 `fp.apply` / `fp.probe` 两条，它们的失败路径也必须说人话
（「连不上浏览器，先起一个 CDP 端口」），而不是抛一个 traceback 给模型看。

一条必须记住的边界
------------------
**不打分。** 返回的永远是「矛盾清单」和「期望 vs 实际」，不是「XX 分」。
分数怎么算只有检测方知道，我们猜一个就是拿猜测冒充事实。
"""
from ..core import BaseDevice, DeviceResult as R
from .. import fp_profile as FP
from .. import fp_sandbox as SB
from ..vendored_fp import FP_CAPABILITIES

try:
    from .. import browser_reg
except Exception:                                   # 缺 playwright 也不能拖垮整包 import
    browser_reg = None

_CAPS = tuple(c.name for c in FP_CAPABILITIES)
_HANDLERS = {}

# 裸环境下这些值本身就是自曝项。fp.probe 会把它们挑出来单列。
_LEAKS = (
    ("ua", "Headless", "UA 里带 Headless —— 等于门口挂块牌子"),
    ("ua_ch", "Headless", "UA-CH 里带 Headless —— 请求头干净、JS 读到脏"),
    ("webgl_renderer", "SwiftShader", "WebGL 报软件渲染 —— 真机不会有，这条最硬"),
    ("webgl_renderer", "llvmpipe", "WebGL 报 llvmpipe（Linux 软渲染）"),
    ("webgl_vendor", "SwiftShader", "WebGL vendor 带 SwiftShader"),
)


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


def _profile_from(args):
    """从参数里拿到一份档案：直接给 / 从沙箱取 / 按机型地理生成。"""
    prof = args.get("profile")
    if isinstance(prof, dict) and prof:
        return prof, ""
    sid = str(args.get("sandbox") or args.get("id") or "")
    if sid:
        m = SB.get(sid)
        if not m:
            return None, "沙箱不存在：%s（先 fp.sandbox_list 看看有哪些）" % sid
        return m.get("profile"), ""
    return FP.make(platform=args.get("platform") or "win",
                   geo=args.get("geo") or "CN",
                   seed=args.get("seed")), ""


# ---------------------------------------------------------------- 档案

@_reg("fp.make")
def _h_make(cap, args, dev):
    p = FP.make(platform=args.get("platform") or "win",
                geo=args.get("geo") or "CN",
                seed=args.get("seed"),
                chrome_major=int(args.get("chrome_major") or 142))
    bad = FP.contradictions(p)
    return R.success(cap, dev.name, via=dev.via, data={
        "profile": p,
        "ok": not bad,
        "contradictions": bad,
        "tip": "拿 fp.audit 复核，或直接 fp.sandbox_new 建一个沙箱用",
    })


@_reg("fp.audit")
def _h_audit(cap, args, dev):
    p, err = _profile_from(args)
    if err:
        return R.error(cap, err, device=dev.name, via=dev.via)
    bad = FP.contradictions(p)
    return R.success(cap, dev.name, via=dev.via, data={
        "ok": not bad,
        "contradictions": bad,
        "count": len(bad),
        "profile": p,
        "verdict": ("自洽，能上" if not bad else
                    "有 %d 处自相矛盾，注入了也是白注 —— 先修档案" % len(bad)),
    })


# ---------------------------------------------------------------- 沙箱

@_reg("fp.sandbox_new")
def _h_sandbox_new(cap, args, dev):
    r = SB.create(platform=args.get("platform") or "win",
                  geo=args.get("geo") or "CN",
                  seed=args.get("seed"),
                  fresh=bool(args.get("fresh")),
                  persist=bool(args.get("persist")),
                  ttl_days=int(args.get("ttl_days") or 7))
    if not r.get("ok"):
        return R.error(cap, r.get("err") or "建沙箱失败", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, side_effect=True, data={
        "id": r["id"],
        "profile_dir": r["profile_dir"],
        "reused": r["reused"],
        "profile": r["profile"],
        "how": ("复用了已有沙箱（历史还在）" if r["reused"] else "新建了一个"),
        "next": "fp.apply 传 sandbox=%s 就能把它注进浏览器" % r["id"],
    })


@_reg("fp.sandbox_list")
def _h_sandbox_list(cap, args, dev):
    items = SB.list_pool(include_expired=bool(args.get("include_expired")))
    s = SB.summary()
    return R.success(cap, dev.name, via=dev.via, data={
        "summary": s,
        "items": [{"id": m["id"], "age_days": m.get("age_days"),
                   "uses": m.get("uses") or 0, "persist": m.get("persist"),
                   "geo": (m.get("profile") or {}).get("geo"),
                   "os": (m.get("profile") or {}).get("os"),
                   "tz": (m.get("profile") or {}).get("tz"),
                   "egress": (m.get("egress") or {}).get("country") if isinstance(m.get("egress"), dict) else None,
                   "expired": m.get("expired")} for m in items],
        "root": s["root"],
    })


@_reg("fp.sandbox_check")
def _h_sandbox_check(cap, args, dev):
    sid = str(args["id"])
    issues = SB.check(sid, live_country=args.get("live_country"),
                      live_proxy=args.get("live_proxy"),
                      auto=bool(args.get("auto")),
                      timeout=float(args.get("timeout") or 8))
    return R.success(cap, dev.name, via=dev.via, data={
        "id": sid,
        "ok": not issues,
        "issues": issues,
        "verdict": ("能用" if not issues else
                    "有 %d 个问题，其中一个可能是致命的（出口漂移最狠）" % len(issues)),
    })


@_reg("fp.egress_probe")
def _h_egress_probe(cap, args, dev):
    from .. import fp_egress as EG
    r = EG.probe(timeout=float(args.get("timeout") or 8),
                 proxy=args.get("proxy") or "auto")
    if not r.get("ok"):
        # 探不出来必须说成「不知道」，不许说成「正常」
        return R.error(cap, "探不出当前出口（**这不等于没问题，等于不知道**）：%s"
                       % str(r.get("error"))[:200], device=dev.name, via=dev.via)
    r["line_kind"] = EG.looks_residential(r.get("asn"), r.get("isp"))
    r["verdict"] = ("住宅线路" if r["line_kind"] == "residential" else
                    "机房线路 —— 不是不能过，是先天分低" if r["line_kind"] == "datacenter"
                    else "线路性质判不出来（不猜）")
    return R.success(cap, dev.name, via=dev.via, data=r)


@_reg("fp.sandbox_bind")
def _h_sandbox_bind(cap, args, dev):
    r = SB.bind_egress(str(args["id"]), proxy=args.get("proxy"),
                       country=args.get("country"), ip=args.get("ip"),
                       asn=args.get("asn"))
    if not r.get("ok"):
        return R.error(cap, r.get("err") or "绑定失败", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, side_effect=True, data=r)


@_reg("fp.sandbox_purge")
def _h_sandbox_purge(cap, args, dev):
    really = bool(args.get("really"))
    r = SB.purge(dry_run=not really)
    out = dict(r)
    out["how"] = ("真删了 %d 个" % r["removed"] if really
                  else "只是报了一下（要真删传 really=true）")
    return R.success(cap, dev.name, via=dev.via, side_effect=really, data=out)


@_reg("fp.sandbox_drop")
def _h_sandbox_drop(cap, args, dev):
    r = SB.destroy(str(args["id"]))
    if not r.get("ok"):
        return R.error(cap, r.get("err") or "删除失败", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, side_effect=True, data={
        "id": r["id"], "freed": r["freed"],
    })


# ---------------------------------------------------------------- 真浏览器

def _need_browser(cap, dev):
    if browser_reg is None:
        return R.unavailable(cap, "缺 playwright：这条能力要连真浏览器", device=dev.name, via=dev.via)
    try:
        ok, why = browser_reg.playwright_ok()
        if not ok:
            return R.unavailable(cap, "playwright 不可用：%s" % why, device=dev.name, via=dev.via)
    except Exception:
        pass
    return None


def _major_of(ua):
    """从 UA 里取 Chrome 主版本。取不到返回 0（当作「不知道」，不做判断）。"""
    import re
    m = re.search(r"Chrome/(\d+)", str(ua or ""))
    return int(m.group(1)) if m else 0


def _leaks_of(actual):
    """把当前环境里「自曝」的项挑出来。有就说明还没伪装。"""
    out = []
    for field, token, why in _LEAKS:
        v = actual.get(field)
        s = ""
        if isinstance(v, dict):                     # ua_ch 是个 dict
            s = str(v.get("brands") or v)
        else:
            s = str(v or "")
        if token.lower() in s.lower():
            out.append({"field": field, "value": s[:120], "why": why})
    if actual.get("webdriver") is True:
        out.append({"field": "webdriver", "value": True,
                    "why": "navigator.webdriver=true —— 自动化特征的直球自曝"})
    outer = actual.get("outer") or []
    if not outer or 0 in list(outer):
        out.append({"field": "outer", "value": outer,
                    "why": "outerWidth/Height=0 —— headless 的老破绽，一直没修干净"})
    return out


@_reg("fp.probe")
def _h_probe(cap, args, dev):
    bad = _need_browser(cap, dev)
    if bad:
        return bad
    port = int(args.get("port") or 9222)
    from .. import fp_inject as FI
    try:
        with browser_reg.Session(port) as s:
            actual = FI.readback(s.page)
    except Exception as e:
        return R.error(cap, "连不上 %d 端口的浏览器：%s" % (port, e),
                       device=dev.name, via=dev.via)
    leaks = _leaks_of(actual)
    return R.success(cap, dev.name, via=dev.via, data={
        "actual": actual,
        "leaks": leaks,
        "clean": not leaks,
        "real_major": _major_of(actual.get("ua")),
        "verdict": ("干净" if not leaks else "有 %d 项自曝，这就是会被抓的样子" % len(leaks)),
    })


@_reg("fp.apply")
def _h_apply(cap, args, dev):
    from .. import fp_inject as FI
    prof, err = _profile_from(args)
    if err:
        return R.error(cap, err, device=dev.name, via=dev.via)
    if not prof:
        return R.error(cap, "要么给 sandbox，要么给 profile", device=dev.name, via=dev.via)
    # 自相矛盾的档案注入了也是白注 —— 先在门口拦住，别让人以为"跑过了就好了"
    bad = FP.contradictions(prof)
    if bad:
        return R.error(cap, "档案本身有 %d 处自相矛盾，注入了也白注：%s"
                       % (len(bad), "；".join(b["rule"] for b in bad[:5])),
                       device=dev.name, via=dev.via)
    bad2 = _need_browser(cap, dev)
    if bad2:
        return bad2
    port = int(args.get("port") or 9222)
    res = {}
    try:
        with browser_reg.Session(port) as s:
            # 注入前先读一次真实 UA：拿真实引擎版本。
            # 规则是单向的 —— **声称的版本高于真实引擎**才危险（新版才有的 API
            # 我们报不出来，一问就露）；声称旧版本很常见（用户不更新浏览器），不算破绽。
            raw = FI.readback(s.page)
            real_major = _major_of(raw.get("ua"))
            res = FI.apply_to_page(s.page, s.ctx, prof)
            if not res.get("ok"):
                return R.error(cap, "注入失败：%s" % res.get("err"),
                               device=dev.name, via=dev.via)
            if args.get("reload", True):
                try:
                    s.page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
            actual = FI.readback(s.page)
            d = FI.diff(prof, actual)
            want_major = int(prof.get("chrome_major") or 0)
            if real_major and want_major > real_major:
                d.append({"field": "ua_version",
                          "want": "≤ %d（真实引擎版本）" % real_major,
                          "got": want_major,
                          "note": "声称的版本比真实引擎新 —— 新版才有的 API 我们报不出来，一问就露"})
    except Exception as e:
        return R.error(cap, "连不上 %d 端口的浏览器：%s。先起一个 CDP 端口，或换 port。"
                       % (port, e), device=dev.name, via=dev.via)
    leaks = _leaks_of(actual)
    return R.success(cap, dev.name, via=dev.via, side_effect=True, data={
        "ok": not d,
        "diff": d,
        "diff_count": len(d),
        "leaks": leaks,
        "cdp": res.get("cdp") or [],
        "init": bool(res.get("init")),
        "real_major": real_major,
        "actual": actual,
        "verdict": ("注入生效：档案里写的，页面报的就是它" if not d else
                    "还有 %d 项对不上，看 diff" % len(d)),
        "reminder": "注入不落盘 —— 沙箱重开必须重新 apply",
    })


# ---------------------------------------------------------------- 设备

class FpDevice(BaseDevice):
    kind = "fp"
    via = "local"

    def __init__(self, name="fp:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "指纹层（整机汇报）：档案自洽校验 + CDP/JS 双层注入 + 沙箱（profile 目录 + 出口绑定）。"
                    "只报矛盾不打分")

    def probe(self):
        # 三块零件都是纯标准库，所以设备永远可用；
        # 只有 fp.apply / fp.probe 需要 playwright，它们在 handler 里自己说清楚。
        return True, ""

    def caps(self):
        return _CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in _CAPS:
            return R.unsupported(cap, "fp 设备没有这个能力", device=self.name, via=self.via)
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
