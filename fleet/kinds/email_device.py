# -*- coding: utf-8 -*-
"""天网 · 邮箱设备：承载 email.* 能力（伪装登录第一级「邮箱层」）。

借免费临时邮箱服务（guerrillamail 主力 / mail.tm 后备）跑通「生成邮箱 → 收验证码 →
注册账号」链路。零付费依赖、不实名；后续「临时变永久」再自建 catch-all 域名。
"""
from ..core import BaseDevice, DeviceResult as R
from ..vendored_email import EMAIL_CAPABILITIES

try:
    from .. import email_pool as EP
except Exception:
    EP = None

_EMAIL_CAPS = tuple(c.name for c in EMAIL_CAPABILITIES)
_HANDLERS = {}


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


@_reg("email.new_mailbox")
def _h_new_mailbox(cap, args, dev):
    if EP is None:
        return R.unavailable(cap, "邮箱池模块不可用", device=dev.name, via=dev.via)
    provider = args.get("provider") or "guerrillamail"
    r = EP.new_mailbox(provider)
    if not r.get("ok"):
        return R.unavailable(cap, r.get("err") or "生成邮箱失败", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data=r)


@_reg("email.read")
def _h_read(cap, args, dev):
    if EP is None:
        return R.unavailable(cap, "邮箱池模块不可用", device=dev.name, via=dev.via)
    mbox = args.get("mailbox")
    if not mbox:
        return R.error(cap, "mailbox 必填（new_mailbox 的返回）", device=dev.name, via=dev.via)
    r = EP.read_mailbox(mbox)
    if not r.get("ok"):
        return R.unavailable(cap, r.get("err") or "读信失败", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data=r)


@_reg("email.wait_code")
def _h_wait_code(cap, args, dev):
    if EP is None:
        return R.unavailable(cap, "邮箱池模块不可用", device=dev.name, via=dev.via)
    mbox = args.get("mailbox")
    if not mbox:
        return R.error(cap, "mailbox 必填", device=dev.name, via=dev.via)
    r = EP.wait_code(mbox,
                     timeout=float(args.get("timeout") or 90),
                     length=int(args.get("length") or 0),
                     interval=float(args.get("interval") or 5))
    if not r.get("ok"):
        return R.timeout(cap, r.get("err") or "等待验证码超时", device=dev.name, via=dev.via)
    return R.success(cap, dev.name, via=dev.via, data=r)


@_reg("email.extract_code")
def _h_extract_code(cap, args, dev):
    if EP is None:
        return R.unavailable(cap, "邮箱池模块不可用", device=dev.name, via=dev.via)
    text = args.get("text")
    if not text:
        return R.error(cap, "text 必填", device=dev.name, via=dev.via)
    code = EP.extract_code(text, length=int(args.get("length") or 0))
    return R.success(cap, dev.name, via=dev.via,
                     data={"code": code, "found": code is not None})


class EmailDevice(BaseDevice):
    kind = "email"
    via = "local"

    def __init__(self, name="email:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("note", "邮箱能力（伪装登录第一级·邮箱层）；借免费临时邮箱，零成本不实名")

    def probe(self):
        if EP is None:
            return False, "邮箱池模块不可用"
        return True, ""

    def caps(self):
        return _EMAIL_CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in _EMAIL_CAPS:
            return R.unsupported(cap, "email 设备没有这个能力", device=self.name, via=self.via)
        h = _HANDLERS.get(cap)
        if h is None:
            return R.unavailable(cap, "能力已声明，handler 待接入", device=self.name, via=self.via)
        try:
            return h(cap, args, self)
        except Exception as e:
            return R.error(cap, "handler 执行异常：%s" % e, device=self.name, via=self.via)
