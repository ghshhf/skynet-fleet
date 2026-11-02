# -*- coding: utf-8 -*-
"""天网 · 伪装层能力（整机汇报，不是解一道题）
==================================================
一条要记住的判断（2026-11-02 主人定的）：

    无感验证不是一道题，是一次**整机汇报的评分**。检测方看三样：
        ① 汇报什么（UA / 平台 / 时区 / 语言 / GPU / Canvas / 字体）
        ② 用什么连接（IP / ASN / 地理位置）
        ③ 两者一致吗，以及这台机器以前来过吗

    所以这条线不是「逐个属性改一遍」，是两层：
        fp.*       造一个自洽的虚拟人，并且能把它身上的矛盾喊出来
        fp.sandbox_*  给这个虚拟人一份独立的、可持久可销毁的历史

    **沙箱比伪装高一级**：伪装是「我假装是另一台机器」，会被抓矛盾；
    沙箱是「我真的重新出生了一次」，矛盾天然不存在。
    但沙箱不管指纹 —— profile 落了盘，注入不会。两个都必须做。

    顺带一句：**沙箱跟滑块是两条线**。滑块是「有题给你做」，
    无感是「没题，只有背景打分」。混在一起就会指望滑块那套去解无感，那是错的。

四层零件
--------
    L1 出口    net.resolve_proxy() / proxy.*     连接层（已有，最厚的一层）
    L2 档案    fleet/fp_profile.py               值 + 矛盾校验（contradictions）
    L3 注入    fleet/fp_inject.py                CDP 原生 + initScript
    L4 历史    fleet/fp_sandbox.py               profile 目录 + 出口绑定

三条边界（写死）
----------------
1. **不打分。** 分数的语义是「检测方会给多少」，我们不知道对方怎么算 ——
   返回一个分数就是拿猜测冒充事实。所以全部能力只报**客观矛盾**（期望 vs 实际）。
2. **不保证过。** 这里能保证的是「内部不自相矛盾」，不是「一定放行」。
   对方有权因为任何理由拒绝，那是他们的权利，我们不假装能买通。
3. **不碰主人日常用的浏览器。** 沙箱一律是独立 profile 目录（`~/.fleet/sandbox/`），
   不碰 `~/.fleet/browser/p9222` 那个平时在用的。
"""
from .core import Capability, Param


FP_CAPABILITIES = [
    Capability(
        "fp.make",
        "造一份自洽的指纹档案（机型 + 地理；同 seed 可复现）",
        params=(Param("platform", "str", False, "win|mac|linux|android（默认 win）"),
                Param("geo", "str", False, "CN|HK|TW|US|JP|DE|...（默认 CN）"),
                Param("seed", "str", False, "同 seed 同档案；不传按 platform-geo 派生"),
                Param("chrome_major", "int", False, "UA 里的主版本号（默认 142）")),
        note="纯计算，不碰浏览器、不落盘。档案是「一组必须互相吻合的属性」，"
             "不是随机值堆砌 —— 造完顺手用 fp.audit 过一遍。",
    ),
    Capability(
        "fp.audit",
        "体检：把一份档案里互相打架的地方全列出来（空列表 = 自洽，能上）",
        params=(Param("profile", "json", False, "直接给一份档案 dict"),
                Param("platform", "str", False, "不给 profile 就按这个现场生成"),
                Param("geo", "str", False, "同上"),
                Param("seed", "str", False, "同上")),
        note="这才是这一层的正品。检测方不需要知道你是什么，只需要知道你不矛盾 —— "
             "所以质量标准是「自不自洽」，不是「像不像人」。",
    ),
    Capability(
        "fp.sandbox_new",
        "建/取一个沙箱（一人一份历史；同 seed 复用，fresh 才另起）",
        params=(Param("platform", "str", False, "机型"),
                Param("geo", "str", False, "地理（决定时区/语言）"),
                Param("seed", "str", False, "同 seed 复用同一个沙箱"),
                Param("fresh", "bool", False, "另起一个全新的（这才是临时沙箱）"),
                Param("persist", "bool", False, "养号：不参与过期清理"),
                Param("ttl_days", "int", False, "一次性沙箱的寿命（默认 7 天）")),
        side_effect=True,
        note="一次性 = 抓一次就走，用完即毁；养号 = 长期用，但**出口必须绑定不许漂**。"
             "两者不是谁更高级，是两种任务用两种工具。",
    ),
    Capability(
        "fp.sandbox_list",
        "列沙箱池（默认滤掉过期的，但**不删**）",
        params=(Param("include_expired", "bool", False, "连过期的也列出来"),),
        note="过期和执行是两件事：列表只是报告，清理要显式调 fp.sandbox_purge。",
    ),
    Capability(
        "fp.sandbox_check",
        "体检某个沙箱还能不能用（重点：出口有没有漂）",
        params=(Param("id", "str", True, "沙箱 id"),
                Param("live_country", "str", False, "当前实际出口的国家码"),
                Param("live_proxy", "str", False, "当前实际出口的代理")),
        note="最要紧的一条是出口漂移：profile 攒的历史是给某个国家的人攒的，"
             "IP 一换国家就变成「同一台机器突然搬家」，比新访客更可疑。",
    ),
    Capability(
        "fp.sandbox_bind",
        "把出口钉在沙箱上（代理 / 国家 / IP / ASN）",
        params=(Param("id", "str", True, "沙箱 id"),
                Param("proxy", "str", False, "出口代理 URL"),
                Param("country", "str", False, "出口国家码"),
                Param("ip", "str", False, "出口 IP"),
                Param("asn", "str", False, "出口 ASN")),
        side_effect=True,
        note="养号必须调这个。不绑的话漂了都不知道 —— 漂一次，攒的历史全部作废。",
    ),
    Capability(
        "fp.sandbox_purge",
        "清掉过期的一次性沙箱（默认只报不删）",
        params=(Param("really", "bool", False, "true 才真删，默认 dry-run"),),
        side_effect=True, destructive=True,
        note="删历史不该是隐式的。默认 dry-run，要真删必须显式传 really=true。",
    ),
    Capability(
        "fp.sandbox_drop",
        "删掉一个沙箱（真删目录）",
        params=(Param("id", "str", True, "沙箱 id"),),
        side_effect=True, destructive=True,
        note="只删沙箱根目录下的子目录；id 不合法直接拒，不做「尽力修正」。",
    ),
    Capability(
        "fp.apply",
        "把档案注入当前浏览器，再读回来核对（返回差异清单）",
        params=(Param("port", "int", False, "CDP 端口（默认 9222）"),
                Param("sandbox", "str", False, "沙箱 id，档案从它取"),
                Param("profile", "json", False, "或者直接给一份档案"),
                Param("reload", "bool", False, "注入后重载页面（默认 true）")),
        side_effect=True,
        note="CDP 原生覆盖 UA/UA-CH/时区/语言/DPR/视口；initScript 补 screen/WebGL/Canvas/"
             "webdriver 这些 CDP 管不到的。**注入不落盘**：沙箱重开必须重新注入。",
    ),
    Capability(
        "fp.probe",
        "不动浏览器，读一次真实环境当前的指纹（裸值）",
        params=(Param("port", "int", False, "CDP 端口（默认 9222）"),),
        note="用来对比「注入前 vs 注入后」。headless 下会直接看到 HeadlessChrome / "
             "SwiftShader / webdriver=true 这些自曝项 —— 这就是会被抓的样子。",
    ),
]
