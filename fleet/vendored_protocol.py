# -*- coding: utf-8 -*-
"""天网 · 协议能力（伪装登录第三级「协议层」）
================================================
三级定位（前两级已存在，这一级补的是中间那块）：

    第一级  email.*     造身份 —— 临时邮箱，不碰主人真实邮箱
    第二级  register.*  走流程 —— 浏览器自动化，人机断点友好（真人只点验证码那一步）
    第三级  re.protocol.*  走协议 —— 纯 HTTP，不开浏览器

为什么必须有第三级，而不是「有浏览器就够了」：

  1. **速度差一个量级**。起一次 chromium 3~8 秒，纯协议一个注册往返 0.3~1 秒。
     要批量、要常驻、要「随便给个平台就能进」，浏览器是兜底，不是主力。
  2. **浏览器会崩、会残留进程、会抢焦点**。协议层没有这些故障模式。
  3. **协议层才是「渗透 + 逆向 + 爬虫」的汇合点**：
        逆向（rea.*）      → 拿到签名算法与端点
        渗透（pentest.*）  → 拿到攻击面与可达性
        爬虫（crawler）    → 拿到数据流
        re.protocol.*         → 把上面三个变成「一个能用的身份 + 一条能复用的会话」
     没有这一层，前三个各自为战；有了它，才是「整合」。

能力族 re.protocol.*（6 个）：

  re.protocol.recon     给一个 URL，回答「这平台怎么进」：免登录 / 要注册 / 要登录 / 进不去
  re.protocol.identity   造一个干净身份（临时邮箱 + 设备指纹），不碰主人真实信息
  re.protocol.form       解析页面表单（字段名 / 类型 / 提交地址 / 隐藏字段）
  re.protocol.register   协议注册全流程（填表 → 提交 → 等码 → 回填 → 拿会话）
  re.protocol.login      协议登录（账号密码 / 已有 token），返回可复用会话
  re.protocol.session    会话存取与检活

边界（写死，别指望它做不到的事）：

  - **JS 挑战过不了**。Cloudflare 5 秒盾 / 阿里的滑块 / 瑞数的 acw 这类要跑 JS 的，
    协议层直接报 `antibot: js_challenge` 并建议切第二级（register.* 有头浏览器）。
    这是如实上报，不是失败——知道自己打不动，比假装打进去了有价值。
  - **语义不猜**。表单字段靠 name/id/type 认，认不出来就如实列出来让人指定，
    不靠「猜哪个是密码框」蒙一个。
  - **不碰钱、不碰实名**。协议层只做「成为用户」和「保持会话」，不做支付、
    不做实名认证。
"""
from .core import Capability, Param


PROTOCOL_CAPABILITIES = [
    Capability(
        "re.protocol.recon",
        "给一个 URL，回答「这平台怎么进」：免登录通道 / 要注册 / 要登录 / 有墙进不去",
        params=(Param("url", "str", doc="目标地址，必填（http/https 均可）"),
                Param("timeout", "float", doc="单次请求超时，默认 15"),
                Param("proxy", "str", doc="显式代理；不传=按 net.resolve_proxy 选路")),
        note="[PROTO] 只读侦察：GET 首页 + 解析结构，不提交任何表单"),

    Capability(
        "re.protocol.identity",
        "造一个干净身份（临时邮箱 + 设备指纹 + 密码），不碰主人的真实邮箱和真实设备信息",
        params=(Param("provider", "str", doc="临时邮箱服务：maildrop/guerrillamail/mailtm，默认 maildrop"),
                Param("locale", "str", doc="语言，默认 zh-CN"),
                Param("preset", "str", doc="指纹档位：desktop_win / desktop_mac / mobile_android，默认 desktop_win")),
        note="[PROTO] 只生成不注册：返回身份包，注册交给 re.protocol.register"),

    Capability(
        "re.protocol.form",
        "解析页面上的表单（提交地址 / 方法 / 每个字段的 name、type、值、是否隐藏）",
        params=(Param("url", "str", doc="页面地址，必填"),
                Param("timeout", "float", doc="请求超时，默认 15"),
                Param("proxy", "str", doc="显式代理")),
        note="[PROTO] 只读：不填不提交"),

    Capability(
        "re.protocol.register",
        "协议注册全流程：造身份 → 取表单 → 填表 → 提交 → 等验证码 → 回填 → 拿会话",
        params=(Param("url", "str", doc="注册页地址，必填"),
                Param("email_field", "str", doc="邮箱字段名；不传=自动按 name/type 认"),
                Param("password_field", "str", doc="密码字段名；不传=自动认"),
                Param("code_field", "str", doc="验证码字段名；不传=自动认"),
                Param("submit_url", "str", doc="提交地址；不传=用表单 action"),
                Param("verify_url", "str", doc="提交验证码的地址；不传=用 submit_url"),
                Param("provider", "str", doc="临时邮箱服务，默认 maildrop"),
                Param("code_timeout", "float", doc="等验证码最长秒数，默认 120"),
                Param("proxy", "str", doc="显式代理")),
        side_effect=True,
        note="[PROTO] 有副作用：会在目标站点创建一个真实账号（请只对自己有权测试的站点用）"),

    Capability(
        "re.protocol.login",
        "协议登录：POST 账号密码（或带 token），返回可复用的会话（cookie/token）",
        params=(Param("url", "str", doc="登录接口地址，必填"),
                Param("user_field", "str", doc="账号字段名，默认 username"),
                Param("pass_field", "str", doc="密码字段名，默认 password"),
                Param("username", "str", doc="账号"),
                Param("password", "str", doc="密码"),
                Param("extra", "dict", doc="额外字段（签名/token/设备号等）"),
                Param("json_body", "bool", doc="true=按 JSON 提交，默认 form 表单"),
                Param("proxy", "str", doc="显式代理")),
        side_effect=True,
        note="[PROTO] 有副作用：会向目标站点发起一次真实登录"),

    Capability(
        "re.protocol.session",
        "会话存取与检活：save 落盘 / load 读回 / probe 打一发看还活着没",
        params=(Param("op", "str", doc="save / load / probe / list，必填"),
                Param("name", "str", doc="会话名，默认 default"),
                Param("url", "str", doc="probe 时打的目标地址"),
                Param("proxy", "str", doc="显式代理")),
        note="[PROTO] save 会在 ~/.fleet/sessions/ 落盘（文件系统副作用）"),
]
