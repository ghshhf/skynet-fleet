# -*- coding: utf-8 -*-
"""天网 · 注册能力（把「注册一个站」做成可调用的零件）
=========================================================

主人 2026-11-03 定的方向：**别再拿浏览器一次一次试**。
把注册抽象成函数，人只负责那一段机器过不去的（手机扫码），其余全部可调用。

四段流水（每一段单独一条能力，失败原因不会互相掩盖）
----------------------------------------------------
    L1 身份   signup.mail_new / signup.disposable
    L2 通道   signup.profile / signup.recon
    L3 闸门   （复用 slider.* / protocol.*，不在这里重复造）
    L4 兜底   sms.balance / sms.price / sms.buy / sms.wait / sms.finish

外加一条正式出口
----------------
    signup.handoff —— 吐「地址 + 该干什么 + 为什么只有人能过」。
    **这不是失败路径**，是人工段的正确形态。上一轮我把这一段当失败硬撑，
    结果在扫码那一关原地打转。

三条边界
--------
1. 不猜站点规则，读不到就报 unknown。
2. 不产生假成功：没 key 报 no_key，没余额报 no_balance。
3. 花钱的能力（sms.buy / sms.finish）必须 `side_effect=True` 过审批闸门。
"""
from .core import Capability, Param


def _check_signup():
    def _chk(reg, dev):
        try:
            from . import signup_kit  # noqa: F401
            return True, "注册工具箱在线（%d 个已实测站点画像）" % len(signup_kit.PROFILES)
        except Exception as e:
            return False, "注册工具箱不可用：%s" % e
    return _chk


def _check_sms():
    def _chk(reg, dev):
        try:
            from . import sms_kit  # noqa: F401
            return True, "接码适配层在线（%d 个平台）" % len(sms_kit.PLATFORMS)
        except Exception as e:
            return False, "接码适配层不可用：%s" % e
    return _chk


SIGNUP_CAPABILITIES = [
    # ---------------- L1 身份 ----------------
    Capability(
        "signup.mail_new",
        "建一个临时信箱（返回地址+凭证），作为注册的收码口",
        params=(Param("provider", "str", False, "mailtm（默认）/ tempmail_lol"),
                Param("prefix", "str", False, "地址前缀，默认 u")),
        side_effect=False, check=_check_signup(),
        note="建信箱本身不花钱。返回里带 disposable 判定 —— "
             "一次性域名会被 Clerk 这类站直接拒，先看这个再往下走。",
    ),
    Capability(
        "signup.mail_code",
        "等一封带验证码的邮件并提取（超时也返回结构体，说明收了几封）",
        params=(Param("mailbox", "json", True, "signup.mail_new 返回的信箱对象"),
                Param("timeout", "int", False, "等待秒数，默认 180（短了就是白等）"),
                Param("pattern", "str", False, r"提取用的正则，默认 \b(\d{6})\b")),
        side_effect=False, check=_check_signup(),
        note="超时不返回 None —— 会写清「等了多久、收到几封、最后一条错误」，"
             "否则上游只看得见一句失败。",
    ),
    Capability(
        "signup.disposable",
        "判一个邮箱域名会不会被当成一次性邮箱（判不出来一律报可疑）",
        params=(Param("domain", "str", True, "邮箱域名，如 fldubai.com")),
        side_effect=False, check=_check_signup(),
        note="宁可误报可疑也不误报干净 —— 反了会白烧一个账号。"
             "三级域名（x.y.com）是典型形状。",
    ),
    # ---------------- L2 通道 / L3 闸门 ----------------
    Capability(
        "signup.profile",
        "读一个站的注册画像（已实测过的直接给，带检测日期）",
        params=(Param("domain", "str", True, "站点域名，如 openrouter.ai")),
        side_effect=False, check=_check_signup(),
        note="没实测过的站会返回 known=false 并提示先跑 signup.recon。"
             "画像带 checked 日期 —— 过期事实要有日期，不许当永久真相。",
    ),
    Capability(
        "signup.recon",
        "现场探一个站的注册规则（面板类型/登录方式/邮箱白名单/人机验证）",
        params=(Param("domain", "str", True, "站点域名"),
                Param("timeout", "int", False, "单次请求超时秒数，默认 12")),
        side_effect=False, check=_check_signup(),
        note="按信息密度探：先是 Sub2API 的 public settings（一次给全规则），"
             "再是 Clerk 的 environment（一次给全部登录方式），最后才落到首页 HTML。"
             "全程只读 GET。",
    ),
    Capability(
        "signup.handoff",
        "出人工交接单：准确的地址 + 每一步干什么 + 为什么这一步只有人能过",
        params=(Param("domain", "str", True, "站点域名")),
        side_effect=False, check=_check_signup(),
        note="这是**正式出口不是失败路径**。机器过不去的段（手机扫码）就该交人，"
             "硬撑只会原地打转。",
    ),
    # ---------------- L4 兜底：接码 ----------------
    Capability(
        "sms.balance",
        "查接码平台余额（没配 key 会明确报 no_key）",
        params=(Param("platform", "str", False, "sms_activate / five_sim，默认 sms_activate")),
        side_effect=False, check=_check_sms(),
        note="只读。没 key 不静默降级，直接报 no_key 并告诉你去哪配。",
    ),
    Capability(
        "sms.price",
        "询价（只读，不买）",
        params=(Param("platform", "str", False, "sms_activate / five_sim"),
                Param("service", "str", False, "google（默认）/ openai / telegram"),
                Param("country", "str", False, "国家参数，不传按出口 IP 推")),
        side_effect=False, check=_check_sms(),
        note="价格随时变，只当量级参考。真价必须现查。",
    ),
    Capability(
        "sms.buy",
        "买一个号码用于收码（**花钱**，必须过审批）",
        params=(Param("platform", "str", False, "sms_activate / five_sim"),
                Param("service", "str", False, "google（默认）"),
                Param("country", "str", False, "国家参数，不传按出口 IP 推")),
        side_effect=True, check=_check_sms(),
        note="这一步真花钱。号码归属地必须与出口 IP 同国 —— 不同国时平台侧一关联就露馅，"
             "这是「买了号也过不去」最常见的原因，不是号的问题。",
    ),
    Capability(
        "sms.wait",
        "轮询取码（返回结构体，超时也说明最后状态）",
        params=(Param("platform", "str", False, "sms_activate / five_sim"),
                Param("activation_id", "str", True, "sms.buy 返回的 id"),
                Param("timeout", "int", False, "等待秒数，默认 240")),
        side_effect=False, check=_check_sms(),
        note="轮询间隔 5 秒（平台文档建议 3–5 秒，太快会被限流）。",
    ),
    Capability(
        "sms.finish",
        "结单（成功用；取消请调 sms.cancel 语义的 status）",
        params=(Param("platform", "str", False, "sms_activate / five_sim"),
                Param("activation_id", "str", True, "sms.buy 返回的 id")),
        side_effect=True, check=_check_sms(),
        note="不结单会让号码一直被占；失败的号要及时释放，否则白扣。",
    ),
]
