# -*- coding: utf-8 -*-
"""天网 · 邮箱能力（伪装登录第一级「邮箱层」）
====================================================
主人的洞察：临时邮箱服务的本质 = catch-all 域名 + API 收信，所以能无限批量生成
邮箱收验证码。「借天借地借万物，融于自己」——先借现成的免费服务（guerrillamail
主力 / mail.tm 后备）跑通「生成邮箱 → 收验证码 → 注册账号」链路，零成本、不实名、
不花钱（付费=不可续，免费=理论时间可续）。

能力族 email.* 聚焦「邮箱供料」层（伪装登录的地基，不是攻破）：

  email.new_mailbox   -> 生成免费临时邮箱（guerrillamail / mail.tm）
  email.read          -> 读收件箱（收验证码邮件）
  email.wait_code     -> 轮询等验证码 + 自动提取（自动注册的关键一步）
  email.extract_code  -> 从文本提取验证码（纯本地，离线）

授权边界：仅用于「本人账号 / 已授权流程」的自动化注册登录；不用于绕过第三方平台
风控或批量注册违规账号。这是正常的注册登录自动化基建。
"""
from .core import Capability, Param


def _check_email():
    """邮箱池在线：email_pool 模块可导入（guerrillamail / mail.tm 免费源）。"""
    def _chk(reg, dev):
        try:
            from . import email_pool  # noqa: F401
            return True, "邮箱池在线（guerrillamail + mail.tm）"
        except Exception as e:
            return False, f"邮箱池不可用：{e}"
    return _chk


EMAIL_CAPABILITIES = [
    Capability("email.new_mailbox",
               "生成一个免费临时邮箱（guerrillamail 主力 / mail.tm 后备），返回地址+凭证，作为正常注册登录的供料口",
               params=(Param("provider", "str", doc="邮箱服务商：guerrillamail（默认）/ mailtm / maildrop")),
               side_effect=False, check=_check_email(),
               note="[EMAIL] 借免费临时邮箱服务，零成本不实名；仅用于本人/已授权账号注册"),
    Capability("email.read",
               "读临时邮箱收件箱（收验证码邮件）",
               params=(Param("mailbox", "json", True, "new_mailbox 返回的邮箱凭证对象")),
               side_effect=False, check=_check_email(),
               note="[EMAIL] 只读免费邮箱收件箱"),
    Capability("email.wait_code",
               "轮询等待验证码邮件并自动提取验证码（自动注册的关键一步）",
               params=(Param("mailbox", "json", True, "邮箱凭证对象"),
                       Param("timeout", "int", doc="等待秒数，默认 90"),
                       Param("length", "int", doc="验证码位数，0=自动识别，默认 0"),
                       Param("interval", "int", doc="轮询间隔秒，默认 5")),
               side_effect=False, check=_check_email(),
               note="[EMAIL] 轮询收信+验证码提取，喂给自动注册流程"),
    Capability("email.extract_code",
               "从文本里提取验证码（纯本地正则，离线可用）",
               params=(Param("text", "str", True, "含验证码的文本"),
                       Param("length", "int", doc="验证码位数，0=自动识别，默认 0")),
               side_effect=False, check=_check_email(),
               note="[EMAIL] 纯本地正则提取，不联网"),
]
