# -*- coding: utf-8 -*-
"""天网 · 注册设备：承载 register.* 能力（伪装登录第二级「会话层」）。

为什么不把浏览器操作塞进本进程？
    Playwright 的 sync API 绑线程、绑事件循环，而天网设备会被控制面任意线程调。
    一旦在 asyncio 线程里 launch 就会炸。所以「启停」在本进程（纯 subprocess），
    「页面动作」全部子进程隔离（browser_reg.run_verb）——单步超时、崩了不牵连 host。

人机分工写死在能力切分里：
    fill/submit/click/await_pass 是四个独立动词，不是一个大函数。
    这样机器能自己走完前三个，卡在验证码时「停在那里等人」，人点完第四个接着跑——
    断点天然存在，不用专门设计状态机。
"""
from ..core import BaseDevice, DeviceResult as R
from ..vendored_register import REGISTER_CAPABILITIES

try:
    from .. import browser_reg as B
except Exception:
    B = None

_CAPS = tuple(c.name for c in REGISTER_CAPABILITIES)
_HANDLERS = {}

_FAIL_CODES = ("unavailable", "error", "timeout")


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


def _port(args):
    try:
        return int(args.get("port") or 9222)
    except Exception:
        return 9222


def _wrap(cap, args, dev, r, ok_code=None):
    """把引擎返回的 dict 翻译成天网结果信封。"""
    if r is None:
        return R.unavailable(cap, "浏览器注册引擎不可用", device=dev.name, via=dev.via)
    if r.get("ok"):
        return R.success(cap, dev.name, via=dev.via, data=r)
    why = r.get("err") or "动作失败"
    if r.get("timeout") or r.get("passed") is False:
        return R.timeout(cap, why, device=dev.name, via=dev.via)
    return R.error(cap, why, device=dev.name, via=dev.via)


def _verb(cap, args, dev, name, payload, timeout=180):
    r = B.run_verb(name, payload, timeout=timeout)
    return _wrap(cap, args, dev, r)


# ------------------------------------------------------------------ 启停

@_reg("register.open")
def _h_open(cap, args, dev):
    if B is None:
        return R.unavailable(cap, "浏览器注册引擎不可用", device=dev.name, via=dev.via)
    r = B.launch(port=_port(args), proxy=args.get("proxy") or None,
                 profile=args.get("profile") or None, exe=args.get("exe") or None,
                 width=int(args.get("width") or 1360), height=int(args.get("height") or 940),
                 headless=bool(args.get("headless")))
    return _wrap(cap, args, dev, r)


@_reg("register.close")
def _h_close(cap, args, dev):
    if B is None:
        return R.unavailable(cap, "浏览器注册引擎不可用", device=dev.name, via=dev.via)
    return _wrap(cap, args, dev, B.close(port=_port(args)))


@_reg("register.list")
def _h_list(cap, args, dev):
    if B is None:
        return R.unavailable(cap, "浏览器注册引擎不可用", device=dev.name, via=dev.via)
    rows = B.list_open()
    return R.success(cap, dev.name, via=dev.via,
                     data=dict(browsers=rows, count=len(rows),
                               alive=sum(1 for x in rows if x.get("alive"))))


# ------------------------------------------------------------------ 页面动作

@_reg("register.goto")
def _h_goto(cap, args, dev):
    url = args.get("url")
    if not url:
        return R.error(cap, "url 必填", device=dev.name, via=dev.via)
    return _verb(cap, args, dev, "goto", dict(
        port=_port(args), url=url,
        timeout=int(args.get("timeout") or 60000),
        wait_until=args.get("wait_until") or "domcontentloaded",
        settle=args.get("settle") or 0))


@_reg("register.inspect")
def _h_inspect(cap, args, dev):
    return _verb(cap, args, dev, "inspect", dict(port=_port(args)))


@_reg("register.fill")
def _h_fill(cap, args, dev):
    fields = args.get("fields")
    if not isinstance(fields, dict) or not fields:
        return R.error(cap, "fields 必填，且必须是 {字段名: 值} 的对象", device=dev.name, via=dev.via)
    return _verb(cap, args, dev, "fill", dict(
        port=_port(args), fields=fields,
        only_empty=bool(args.get("only_empty")),
        then_submit=bool(args.get("then_submit")),
        submit_text=args.get("submit_text"),
        press_enter=bool(args.get("press_enter"))))


@_reg("register.submit")
def _h_submit(cap, args, dev):
    return _verb(cap, args, dev, "submit", dict(
        port=_port(args), text=args.get("text"),
        press_enter=bool(args.get("press_enter"))))


@_reg("register.click")
def _h_click(cap, args, dev):
    if not args.get("text") and not args.get("selector"):
        return R.error(cap, "text 和 selector 至少给一个", device=dev.name, via=dev.via)
    return _verb(cap, args, dev, "click", dict(
        port=_port(args), text=args.get("text"), selector=args.get("selector")))


@_reg("register.await_pass")
def _h_await(cap, args, dev):
    timeout = int(args.get("timeout") or 180)
    return _verb(cap, args, dev, "wait_pass", dict(
        port=_port(args), timeout=timeout,
        poll=float(args.get("poll") or 2.5)), timeout=timeout + 40)


@_reg("register.save_session")
def _h_save(cap, args, dev):
    return _verb(cap, args, dev, "save", dict(
        port=_port(args), host=args.get("host"), extra=args.get("extra")))


class RegisterDevice(BaseDevice):
    kind = "register"
    via = "local"

    def __init__(self, name="register:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "注册能力（伪装登录第二级·会话层）；有头 chromium + CDP，人机分工 7:1")

    def probe(self):
        if B is None:
            return False, "浏览器注册引擎不可用"
        ok, why = B.playwright_ok()
        if not ok:
            return False, why
        exe = B.find_chromium()
        if not exe:
            return False, "找不到完整 chromium/chrome（headless_shell 过不了 Cloudflare）"
        n = sum(1 for x in B.list_open() if x.get("alive"))
        return True, "引擎在线%s" % ("（%d 个浏览器开着）" % n if n else "")

    def caps(self):
        return _CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in _CAPS:
            return R.unsupported(cap, "register 设备没有这个能力", device=self.name, via=self.via)
        h = _HANDLERS.get(cap)
        if h is None:
            return R.unavailable(cap, "能力已声明，handler 待接入", device=self.name, via=self.via)
        try:
            return h(cap, args, self)
        except Exception as e:
            return R.error(cap, "handler 执行异常：%s" % e, device=self.name, via=self.via)
