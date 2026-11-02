# -*- coding: utf-8 -*-
"""天网 · 注册设备（把注册这条流水接到能力总线上）
====================================================

本文件只做**编排**，两块零件各在自己的文件里：

    signup_kit.py   邮箱 / 站点画像 / 交接单   —— 纯标准库
    sms_kit.py      接码平台适配               —— 纯标准库

为什么单独成一块（2026-11-03 主人定的）
---------------------------------------
上一轮我用浏览器一次一次点，失败一次就猜一次原因（域名？IP？密码？），
三次之后还在原地。原因不是不会点，是**没有把「注册」拆成能被单独验证的段**。

拆开之后：
    邮箱能不能收码   -> signup.mail_code        能单独测
    这个站要什么     -> signup.recon            能单独测
    手机这一关       -> signup.handoff          承认只有人能过
    要不要买号       -> sms.price / sms.buy     能单独测

失败时不会再是一句「注册失败」，而是「卡在 L4，因为 Google 要扫码」。

三条边界
--------
1. 不猜规则（读不到报 unknown）。
2. 不造假成功（没 key 报 no_key）。
3. 花钱的能力必须过审批（sms.buy / sms.finish 标了 side_effect）。
"""
from ..core import BaseDevice, DeviceResult as R
from .. import signup_kit as SK
from .. import sms_kit as SMS
from ..vendored_signup import SIGNUP_CAPABILITIES

_CAPS = tuple(c.name for c in SIGNUP_CAPABILITIES)
_HANDLERS = {}


def _reg(name):
    def deco(fn):
        _HANDLERS[name] = fn
        return fn
    return deco


# --------------------------------------------------------------------------
# L1 身份
# --------------------------------------------------------------------------

@_reg("signup.mail_new")
def _mail_new(cap, args, dev):
    mb, err = SK.mailbox_new(args.get("provider") or "mailtm", args.get("prefix") or "u")
    if err:
        return R.unavailable(cap, err, device=dev.name, via=dev.via)
    return R.success(cap, device=dev.name, via=dev.via,
                     data={"mailbox": mb,
                           "warn": ("域名被判可疑，Clerk 这类站会直接拒：%s"
                                    % mb.get("disposable_reason"))
                                   if mb.get("disposable") else ""})


@_reg("signup.mail_code")
def _mail_code(cap, args, dev):
    mb = args.get("mailbox")
    if not isinstance(mb, dict) or not mb.get("provider"):
        return R.error(cap, "mailbox 必须是 signup.mail_new 返回的对象", device=dev.name, via=dev.via)
    timeout = int(args.get("timeout") or SK.DEFAULT_WAIT)
    pattern = args.get("pattern") or r"\b(\d{6})\b"
    r = SK.wait_code(mb, timeout=timeout, pattern=pattern)
    if not r.get("code"):
        # 收不到码不是「错误」，是「这一段没通」—— 用 unavailable 更准
        return R.unavailable(cap, r.get("error") or "没收到码", device=dev.name, via=dev.via,
                             data={"waited": r.get("waited"), "mails": r.get("mails")})
    return R.success(cap, device=dev.name, via=dev.via, data=r)


@_reg("signup.disposable")
def _disposable(cap, args, dev):
    d = str(args.get("domain") or "")
    bad, why = SK.looks_disposable(d)
    return R.success(cap, device=dev.name, via=dev.via,
                     data={"domain": d, "disposable": bad, "reason": why,
                           "verdict": "可疑（别用）" if bad else "看着干净"})


# --------------------------------------------------------------------------
# L2 通道 / L3 闸门
# --------------------------------------------------------------------------

@_reg("signup.profile")
def _profile(cap, args, dev):
    d = str(args.get("domain") or "").strip().lower()
    d = d.replace("https://", "").replace("http://", "").split("/")[0]
    p = SK.PROFILES.get(d)
    if not p:
        return R.success(cap, device=dev.name, via=dev.via,
                         data={"domain": d, "known": False,
                               "next": "先跑 signup.recon %s 拿规则" % d})
    return R.success(cap, device=dev.name, via=dev.via,
                     data={"domain": d, "known": True, "profile": p})


@_reg("signup.recon")
def _recon(cap, args, dev):
    d = str(args.get("domain") or "").strip()
    if not d:
        return R.error(cap, "缺 domain", device=dev.name, via=dev.via)
    out = SK.recon(d, timeout=int(args.get("timeout") or 12))
    if not out.get("ok"):
        return R.unavailable(cap, out.get("error") or "三个端点都没读到",
                             device=dev.name, via=dev.via, data=out)
    return R.success(cap, device=dev.name, via=dev.via, data=out)


@_reg("signup.handoff")
def _handoff(cap, args, dev):
    d = str(args.get("domain") or "").strip()
    if not d:
        return R.error(cap, "缺 domain", device=dev.name, via=dev.via)
    return R.success(cap, device=dev.name, via=dev.via, data=SK.handoff(d))


# --------------------------------------------------------------------------
# L4 兜底：接码
# --------------------------------------------------------------------------

def _plat(args):
    return str(args.get("platform") or "sms_activate")


@_reg("sms.balance")
def _sms_balance(cap, args, dev):
    b, err = SMS.balance(_plat(args))
    if err:
        # no_key 是「没配」，不是「坏了」
        code_fn = R.unavailable
        return code_fn(cap, err, device=dev.name, via=dev.via)
    return R.success(cap, device=dev.name, via=dev.via, data=b)


@_reg("sms.price")
def _sms_price(cap, args, dev):
    p, err = SMS.price(_plat(args), args.get("service") or "google",
                       str(args.get("country") or ""))
    if err:
        return R.unavailable(cap, err, device=dev.name, via=dev.via)
    return R.success(cap, device=dev.name, via=dev.via, data=p)


@_reg("sms.buy")
def _sms_buy(cap, args, dev):
    p, err = SMS.buy(_plat(args), args.get("service") or "google",
                     str(args.get("country") or ""))
    if err:
        return R.unavailable(cap, err, device=dev.name, via=dev.via)
    return R.success(cap, device=dev.name, via=dev.via, data=p)


@_reg("sms.wait")
def _sms_wait(cap, args, dev):
    aid = args.get("activation_id")
    if aid in (None, ""):
        return R.error(cap, "缺 activation_id", device=dev.name, via=dev.via)
    r = SMS.wait_code(_plat(args), aid, timeout=int(args.get("timeout") or 240))
    if not r.get("code"):
        return R.unavailable(cap, r.get("error") or "没收到码",
                             device=dev.name, via=dev.via, data=r)
    return R.success(cap, device=dev.name, via=dev.via, data=r)


@_reg("sms.finish")
def _sms_finish(cap, args, dev):
    aid = args.get("activation_id")
    if aid in (None, ""):
        return R.error(cap, "缺 activation_id", device=dev.name, via=dev.via)
    ok, msg = SMS.finish(_plat(args), aid)
    if not ok:
        return R.unavailable(cap, msg, device=dev.name, via=dev.via)
    return R.success(cap, device=dev.name, via=dev.via, data={"closed": True, "id": aid})


# --------------------------------------------------------------------------

class SignupDevice(BaseDevice):
    kind = "signup"
    via = "local"

    def __init__(self, name="signup:bench", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault(
            "note", "注册流水线：邮箱 -> 站点画像 -> 人机交接 -> 接码兜底。"
                    "不做浏览器点击，不猜站点规则，花钱的能力要过审批。")

    def probe(self):
        # 两块零件都是纯标准库，设备永远可用；只有真正联网的动作才会失败
        return True, ""

    def caps(self):
        return _CAPS

    def invoke(self, cap, args=None):
        args = args or {}
        if cap not in _CAPS:
            return R.unsupported(cap, "signup 设备没有这个能力",
                                 device=self.name, via=self.via)
        h = _HANDLERS.get(cap)
        if h is None:
            return R.unavailable(cap, "能力已声明，handler 待接入",
                                 device=self.name, via=self.via)
        try:
            return h(cap, args, self)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, str(e)[:200]),
                           device=self.name, via=self.via)
