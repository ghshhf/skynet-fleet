"""指纹档案：一个「自洽的虚拟人」。

为什么需要这个模块（2026-11-02 主人定的判断）
------------------------------------------------
无感验证不是一道题，是一次**整机汇报的评分**。检测方看到的不是
「你是不是机器人」，而是「你浑身上下有没有矛盾」，以及「你用哪根线连过来」。

所以伪装的评价标准不是「像不像人」，而是「**自不自洽**」。
任何单点伪装都会在另一个点露出矛盾：

    · 改了 UA 不改 navigator.platform       → 一眼假
    · 改了时区不跟出口 IP 的地理对上        → 一眼假
    · 改了 UA 语言不改 Accept-Language      → 一眼假
    · 伪装了 GPU 却在 WebGL 里漏出 SwiftShader → 一眼假，而且是最硬的一种
    · UA 说 Chrome 142、UA-CH 说 138        → 一眼假（这两个必须同源）

⇒ 本模块的主功能**不是**「生成随机值」，是 `contradictions()`：
   把矛盾喊出来。生成随便生成，能过校验的才叫档案。

    **不打分。** 分数的语义是「检测方会给多少」，我们不知道检测方怎么算。
    返回一个分数就是猜数字冒充事实。这里只报**客观可核验的矛盾条目**。

四层结构
--------
    L1 出口  egress / geo / ip_type     → 决定时区、语言（网络层已有，这里只登记）
    L2 平台  os / ua / platform / 硬件 / 屏幕
    L3 图形  webgl / canvas 噪声种子 / audio 噪声种子
    L4 历史  age_days / cookies        → 沙箱层管生命周期，这里只登记「期望」

    L1 与 L2/L3 的吻合，就是「整机汇报」的全部内容。

用法
----
    p = make(os="win", geo="CN", seed=7)
    contradictions(p)        # → []  空列表才叫能上
    bad = make(os="win"); bad["tz"] = "Asia/Tokyo"
    contradictions(bad)      # → [{'rule': 'tz_not_in_geo', ...}]  精确点名
"""

from __future__ import annotations

import hashlib
import random

# ---------------------------------------------------------------- 地理 → 时区 / 语言
# 名称按中国口径写（中国香港 / 中国台湾 / 中国澳门），ISO 码只是键。
# tz 用 IANA 名；locale 第一位是主语言，必须与 geo 匹配。
GEO = {
    "CN": {"name": "中国",     "tz": ["Asia/Shanghai"],                    "locale": ["zh-CN", "zh"]},
    "HK": {"name": "中国香港", "tz": ["Asia/Hong_Kong"],                   "locale": ["zh-HK", "zh-Hant", "en-HK"]},
    "MO": {"name": "中国澳门", "tz": ["Asia/Macau"],                       "locale": ["zh-MO", "zh-Hant", "pt-MO"]},
    "TW": {"name": "中国台湾", "tz": ["Asia/Taipei"],                      "locale": ["zh-TW", "zh-Hant"]},
    "JP": {"name": "日本",     "tz": ["Asia/Tokyo"],                       "locale": ["ja-JP", "ja"]},
    "KR": {"name": "韩国",     "tz": ["Asia/Seoul"],                       "locale": ["ko-KR", "ko"]},
    "SG": {"name": "新加坡",   "tz": ["Asia/Singapore"],                   "locale": ["en-SG", "zh-SG"]},
    "US": {"name": "美国",     "tz": ["America/New_York", "America/Chicago",
                                      "America/Denver", "America/Los_Angeles"],
            "locale": ["en-US", "en"]},
    "CA": {"name": "加拿大",   "tz": ["America/Toronto", "America/Vancouver"],
            "locale": ["en-CA", "fr-CA"]},
    "GB": {"name": "英国",     "tz": ["Europe/London"],                    "locale": ["en-GB", "en"]},
    "DE": {"name": "德国",     "tz": ["Europe/Berlin"],                    "locale": ["de-DE", "de"]},
    "NL": {"name": "荷兰",     "tz": ["Europe/Amsterdam"],                 "locale": ["nl-NL", "nl"]},
    "FR": {"name": "法国",     "tz": ["Europe/Paris"],                     "locale": ["fr-FR", "fr"]},
    "RU": {"name": "俄罗斯",   "tz": ["Europe/Moscow", "Asia/Novosibirsk"], "locale": ["ru-RU", "ru"]},
    "BR": {"name": "巴西",     "tz": ["America/Sao_Paulo"],                "locale": ["pt-BR", "pt"]},
    "IN": {"name": "印度",     "tz": ["Asia/Kolkata"],                     "locale": ["en-IN", "hi-IN"]},
    "AU": {"name": "澳大利亚", "tz": ["Australia/Sydney", "Australia/Perth"], "locale": ["en-AU", "en"]},
}

# ---------------------------------------------------------------- 机型 → 合法取值空间
# 只放**真实存在**的组合。校验的力度来自「池子小」，而不是「规则多」——
# 池子越小，随便编的值越容易被抓出来。
#
# ua_os   : 插进 UA 括号里的**完整段落**（真 Chrome 的形状，一个分号都不能少）
# ua_marks: 校验时找的子串（比 ua_os 松，因为版本号会漂）
# 这两个必须分开 —— 早先只用一个，结果拼出 "Mozilla/5.0 (Windows NT 10.0)"，
# 括号里少了 "; Win64; x64"，真 Chrome 绝不会这么写，比不改更显眼。
OS_SPECS = {
    "win": {
        "platform": ["Win32"],
        # 不要 WOW64：那是 32 位进程跑在 64 位系统上的标记，而 UA-CH 的
        # bitness 我们是 64 —— 两者一撞就是矛盾。而且现在没人用 32 位 Chrome 了，
        # 报 WOW64 本身就是个可疑信号。改成只留 Win64。
        "ua_os": ["Windows NT 10.0; Win64; x64"],
        "ua_marks": ["Windows NT 10.0"],
        "hc": [4, 6, 8, 12, 16, 20, 24, 32],
        "dm": [4, 8, 16],
        # (宽, 高, 该分辨率下真实存在的 DPR 集合) —— dpr 必须跟分辨率绑，
        # 独立随机会造出「1366x768 配 1.5 → 等效 2049x1152」这种不存在的屏。
        "screen": [
            (1920, 1080, [1.0, 1.25]),
            (2560, 1440, [1.0, 1.25]),
            (1366, 768, [1.0]),
            (1536, 864, [1.25, 1.5]),
            (3840, 2160, [1.5, 2.0]),
        ],
        # GPU 厂商标记：renderer 里必须能对上其中一个
        "gpu_marks": ["NVIDIA", "Intel", "AMD", "Microsoft"],
        "fonts": ["Arial", "Calibri", "Segoe UI", "Tahoma", "Microsoft YaHei"],
        "webgl": [
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
            ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ],
    },
    "mac": {
        "platform": ["MacIntel"],
        "ua_os": ["Macintosh; Intel Mac OS X 10_15_7"],
        "ua_marks": ["Macintosh; Intel Mac OS X"],
        "hc": [8, 10, 12],
        "dm": [8, 16],
        # Mac 屏只有 Retina 的几种 + 外接 1080p。**没有 1.0 的内建屏**。
        "screen": [
            (1440, 900, [2.0]),
            (1512, 982, [2.0]),
            (1728, 1117, [2.0]),
            (2560, 1600, [2.0]),
            (1920, 1080, [1.0]),            # 外接 1080p 才是 1.0
        ],
        "gpu_marks": ["Apple", "Intel Inc.", "AMD"],
        "fonts": ["Helvetica", "Helvetica Neue", "PingFang SC", "Menlo"],
        "webgl": [
            ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"),
            ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2 Pro, Unspecified Version)"),
            ("Google Inc. (Intel Inc.)", "ANGLE (Intel Inc., Intel(R) Iris(TM) Plus Graphics 640, OpenGL 4.1)"),
        ],
    },
    "linux": {
        "platform": ["Linux x86_64"],
        "ua_os": ["X11; Linux x86_64"],
        "ua_marks": ["X11; Linux x86_64"],
        "hc": [4, 8, 16, 32],
        "dm": [4, 8],
        "screen": [
            (1920, 1080, [1.0]),
            (2560, 1440, [1.0]),
            (1366, 768, [1.0]),
        ],
        "gpu_marks": ["Intel", "Mesa", "AMD", "NVIDIA"],
        "fonts": ["DejaVu Sans", "Liberation Sans", "Noto Sans CJK SC", "Ubuntu"],
        "webgl": [
            ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)"),
            ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti (nvidia-470), OpenGL 4.6)"),
        ],
    },
    "android": {
        "platform": ["Linux armv8l", "Linux aarch64"],
        "ua_os": ["Linux; Android 13; SM-G991B", "Linux; Android 14; Pixel 8"],
        "ua_marks": ["Linux; Android"],
        "hc": [8],
        "dm": [4, 6, 8],
        "screen": [
            (1080, 2400, [2.75, 3.0]),
            (1080, 2340, [2.75, 3.0]),
            (1440, 3120, [3.5]),
            (1080, 1920, [2.625, 3.0]),
        ],
        "gpu_marks": ["Qualcomm", "ARM", "Mali"],
        "fonts": ["Roboto", "Noto Sans CJK SC", "Droid Sans"],
        "webgl": [
            ("Qualcomm", "ANGLE (Qualcomm, Adreno (TM) 640, OpenGL ES 3.2)"),
            ("ARM", "ANGLE (ARM, Mali-G78 MP14, OpenGL ES 3.2)"),
        ],
    },
}

# 软件渲染标记：出现即死。这是 headless 最硬的破绽，也是最常见的漏改项。
SOFTWARE_GPU = ("SwiftShader", "llvmpipe", "Software Rasterizer",
                "Mesa OffScreen", "Microsoft Basic Render Driver")

# UA-CH 的正常形状（Chromium 的 GREASE 顺序会变，所以只校验「有没有毒」与「版本对不对」）
_BAD_BRANDS = ("HeadlessChrome",)

_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


# ---------------------------------------------------------------- 生成
def _rng(seed, *parts):
    """同一 (seed, 用途) 必须给出同一结果 —— 档案跨进程要能复现。"""
    h = hashlib.sha256(("%s|" % seed + "|".join(str(x) for x in parts)).encode())
    return random.Random(int.from_bytes(h.digest()[:8], "big"))


def noise_seed(seed, name):
    """给 canvas / audio 用的噪声种子：同一档案恒定，跨档案不同。

    恒定这一条不能省 —— 每次访问噪点都变，本身就是「这人在换身份」的信号。
    """
    return int(hashlib.sha256(("%s|noise|%s" % (seed, name)).encode()).hexdigest()[:8], 16)


def make(platform=None, geo="CN", seed=None, chrome_major=142, **over):
    """生成一个自洽档案。想改哪项就传哪项，改完记得跑 contradictions()。

    第一个参数正名是 `platform`（win/mac/linux/android）。
    也接受 `os=` 这个旧写法 —— 因为 `os` 在本模块里是模块名，
    当参数名用会把 `os.path` 变成 `'win'.path`。这是踩过的坑，不是洁癖。
    """
    if platform is None:
        platform = over.pop("os", "win")
    os_ = platform
    if os_ not in OS_SPECS:
        raise ValueError("未知机型 %r，可选 %s" % (os_, sorted(OS_SPECS)))
    if geo not in GEO:
        raise ValueError("未知地理 %r，可选 %s" % (geo, sorted(GEO)))
    seed = seed if seed is not None else "%s-%s" % (os_, geo)
    r = _rng(seed, os_, geo)
    spec = OS_SPECS[os_]
    g = GEO[geo]

    sw, sh, dprs = r.choice(spec["screen"])
    scr = [sw, sh]
    dpr = r.choice(dprs)
    vendor, renderer = r.choice(spec["webgl"])
    # 可用高度 = 屏高减任务栏/菜单栏（真机 availHeight 一定小于 height）
    avail_h = scr[1] - r.choice([0, 24, 40, 48, 64, 80])
    # 视口 = 屏幕再减掉浏览器自己的 chrome（地址栏 + 书签栏 + 标签页）。
    # 这一项不能省：把 viewport 设成整屏，innerHeight 就会大于 availHeight，
    # 那是物理上不可能的状态 —— 真机实测时就是这么露的（864 > 816）。
    if os_ == "android":
        vp_h = avail_h - r.choice([0, 56, 96])
        vp_w = scr[0]
    else:
        vp_h = avail_h - r.choice([87, 105, 120, 135])
        vp_w = scr[0] - r.choice([0, 0, 15, 17])     # 非 overplay 滚动条会吃掉 15~17px
    p = {
        "seed": str(seed),
        "os": os_,
        "geo": geo,
        # L1 出口：真实出口在运行时由 net.resolve_proxy() 定，这里只登记「该跟谁对」
        "egress": None,
        "ip_type": r.choice(["residential", "residential", "isp"]),
        "tz": r.choice(g["tz"]),
        # navigator.language 用 **第一项**，不随机。
        # 列表里的 "zh" / "en" / "ja" 这种裸语言码只配当 languages 数组的备选，
        # 真 Chrome 报的是 "zh-CN" 这种带地区的完整标签。抽到裸码就是假。
        "locale": g["locale"][0],
        "accept_language": None,            # 下面按 locale 补齐
        # L2 平台
        # Android 的结尾是 "Mobile Safari" 而不是 "Safari" —— 少了这个 Mobile，
        # UA 与 UA-CH.mobile 就打架，是最常见的「手工拼 UA」痕迹。
        "ua": ("Mozilla/5.0 (%s) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/%d.0.0.0 %sSafari/537.36"
               % (r.choice(spec["ua_os"]), chrome_major,
                  "Mobile " if os_ == "android" else "")),
        "chrome_major": int(chrome_major),
        "ua_ch": _ua_ch(chrome_major, os_),
        "platform": r.choice(spec["platform"]),
        "hardware_concurrency": r.choice(spec["hc"]),
        "device_memory": r.choice(spec["dm"]),
        "screen": scr,
        "avail": [scr[0], avail_h],
        "viewport": [vp_w, vp_h],
        "dpr": dpr,
        "fonts": list(spec["fonts"]),
        # L3 图形
        "webgl_vendor": vendor,
        "webgl_renderer": renderer,
        "canvas_seed": noise_seed(seed, "canvas"),
        "audio_seed": noise_seed(seed, "audio"),
        # L4 历史（沙箱层负责生命周期）
        "age_days": 0,
        "cookies": 0,
        "webdriver": False,
    }
    p["accept_language"] = _accept_language(p["locale"])
    if os_ == "android":
        p["mobile"] = True
    p.update(over or {})
    return p


def _ua_ch(major, os_):
    """UA-CH 的 brands。头两个必须是 Chromium + Google Chrome，版本跟 UA 同源。"""
    brands = [
        {"brand": "Chromium", "version": str(major)},
        {"brand": "Google Chrome", "version": str(major)},
        {"brand": "Not?A_Brand", "version": "24"},      # GREASE，Chromium 固定塞一个假品牌
    ]
    if os_ == "android":
        brands.append({"brand": "Android WebView", "version": str(major)})
        return {"brands": brands, "fullVersionList": brands, "mobile": True,
                "platform": "Android"}
    plat = {"win": "Windows", "mac": "macOS", "linux": "Linux"}[os_]
    return {"brands": brands, "fullVersionList": brands, "mobile": False, "platform": plat}


def _accept_language(locale):
    """Accept-Language 必须与 navigator.languages 同源。

    真 Chrome 的形状就是「主语言, en;q=0.9」—— 连 en-US 的用户报的也是
    `en-US,en;q=0.9`（主语言和次要语言本来就允许重复语言族）。
    只有一项的 Accept-Language 几乎不存在，写一项反而是特征。
    """
    return "%s,en;q=0.9" % locale


# ---------------------------------------------------------------- 校验（本模块的真正功能）
def _c(rule, field, why):
    return {"rule": rule, "field": field, "why": why}


def contradictions(p):
    """把这份档案里**互相打架**的地方全列出来。空列表 = 自洽 = 能上。

    每一条都指向一个具体字段和一句人话原因，不许返回「看起来没问题」这种结论。
    """
    out = []
    if not isinstance(p, dict):
        return [_c("not_a_profile", "", "档案必须是 dict，实得 %r" % type(p).__name__)]

    os_ = p.get("os")
    geo = p.get("geo")
    if os_ not in OS_SPECS:
        out.append(_c("bad_os", "os", "机型 %r 不在支持列表" % (os_,)))
    if geo not in GEO:
        out.append(_c("bad_geo", "geo", "地理 %r 不在支持列表" % (geo,)))
    if out:                                  # 后面的规则都依赖这两项
        return out

    spec, g = OS_SPECS[os_], GEO[geo]
    ua = str(p.get("ua") or "")

    # --- L1 出口 ⇄ 时区/语言 ---
    if p.get("tz") not in g["tz"]:
        out.append(_c("tz_not_in_geo", "tz",
                      "时区 %r 不在 %s 的时区集合 %s 里 —— 检测方一查 IP 归属地就对不上"
                      % (p.get("tz"), g["name"], g["tz"])))
    loc = str(p.get("locale") or "")
    if loc not in g["locale"]:
        out.append(_c("locale_not_in_geo", "locale",
                      "语言 %r 不是 %s 的常见语言 %s" % (loc, g["name"], g["locale"])))
    elif "-" not in loc:
        # 真 Chrome 报的是 zh-CN 这种带地区的完整标签，裸的 "zh"/"en" 是特征
        out.append(_c("locale_bare_language", "locale",
                      "语言 %r 是不带地区的裸语言码 —— 真浏览器报的是 zh-CN 这种完整标签"
                      % (loc,)))

    # --- L2 平台自身 ---
    if "Headless" in ua:
        out.append(_c("ua_headless", "ua", "UA 里带 Headless —— 等于门口挂块牌子写「我是机器人」"))
    if not any(m in ua for m in spec["ua_marks"]):
        out.append(_c("ua_os_mismatch", "ua",
                      "UA 里找不到机型标记 %s 之一（UA=%r）" % (spec["ua_marks"], ua[:80])))
    if p.get("platform") not in spec["platform"]:
        out.append(_c("platform_mismatch", "platform",
                      "navigator.platform=%r 与 UA 宣称的 %s 不符（应为 %s）"
                      % (p.get("platform"), os_, spec["platform"])))
    if p.get("hardware_concurrency") not in spec["hc"]:
        out.append(_c("hc_impossible", "hardware_concurrency",
                      "%s 上不存在 %r 核这种配置（可选 %s）"
                      % (os_, p.get("hardware_concurrency"), spec["hc"])))
    if p.get("device_memory") not in spec["dm"]:
        out.append(_c("dm_impossible", "device_memory",
                      "%s 上 deviceMemory=%r 不常见（可选 %s）"
                      % (os_, p.get("device_memory"), spec["dm"])))
    scr = list(p.get("screen") or [0, 0])
    allow = [x for x in spec["screen"] if [x[0], x[1]] == scr]
    if not allow:
        out.append(_c("screen_impossible", "screen",
                      "%s 上 %s 不是常见分辨率（可选 %s）"
                      % (os_, scr, [[x[0], x[1]] for x in spec["screen"]])))
    elif p.get("dpr") not in allow[0][2]:
        # 关键联合校验：dpr 必须跟分辨率搭配。
        # 1366x768 配 1.5 = 等效 2049x1152，这种屏不存在 —— 比单看 dpr 更能露。
        out.append(_c("dpr_not_for_screen", "dpr",
                      "%s 上 %s 配 devicePixelRatio=%r 不合理（该分辨率只有 %s）—— "
                      "等效物理分辨率会变成不存在的规格"
                      % (os_, scr, p.get("dpr"), allow[0][2])))
    av = p.get("avail") or [0, 0]
    if av and scr and (av[0] > scr[0] or av[1] > scr[1]):
        out.append(_c("avail_bigger_than_screen", "avail",
                      "可用区域 %s 比屏幕 %s 还大 —— 物理上不可能" % (av, scr)))
    vp = p.get("viewport") or [0, 0]
    if vp:
        if vp[0] > scr[0] or vp[1] > av[1]:
            out.append(_c("viewport_bigger_than_avail", "viewport",
                          "视口 %s 比可用区域 %s 还大 —— 浏览器自己的地址栏/标签页"
                          "没有地方放，真机上不可能出现这个状态" % (vp, av)))
        elif int(vp[1] or 0) < 200:
            out.append(_c("viewport_too_small", "viewport",
                          "视口高度 %r 不像一个真窗口" % (vp[1],)))

    # --- UA 与 UA-CH 必须同源（这两个不同步是最常见的假）---
    ch = p.get("ua_ch") or {}
    brands = ch.get("brands") or []
    names = [str(b.get("brand", "")) for b in brands]
    for bad in _BAD_BRANDS:
        if any(bad in n for n in names):
            out.append(_c("uach_headless", "ua_ch.brands",
                          "UA-CH 里出现 %s —— 请求头干净、JS 读到脏，两头对不上" % bad))
    major = p.get("chrome_major")
    if major:
        vers = {str(b.get("version")) for b in brands if "Chrome" in str(b.get("brand"))}
        if vers and str(major) not in vers:
            out.append(_c("uach_version_mismatch", "ua_ch.brands",
                          "UA 说 Chrome %s、UA-CH 说 %s —— 同一份汇报里两个版本号"
                          % (major, sorted(vers))))
    if ua and major and ("Chrome/%s." % major) not in ua:
        out.append(_c("ua_version_mismatch", "ua",
                      "ua 里的版本号与 chrome_major=%s 不一致" % major))
    # mobile 标记三方一致：UA / UA-CH / os
    is_mobile_ua = "Mobile" in ua
    if is_mobile_ua != (os_ == "android"):
        out.append(_c("mobile_flag_mismatch", "ua",
                      "UA 的 Mobile 标记=%r，但机型是 %s" % (is_mobile_ua, os_)))
    if bool(ch.get("mobile")) != (os_ == "android"):
        out.append(_c("uach_mobile_mismatch", "ua_ch.mobile",
                      "UA-CH.mobile=%r 与机型 %s 不符" % (ch.get("mobile"), os_)))

    # --- L3 图形（headless 最硬的破绽在这里）---
    renderer = str(p.get("webgl_renderer") or "")
    for sw in SOFTWARE_GPU:
        if sw.lower() in renderer.lower():
            out.append(_c("software_gpu", "webgl_renderer",
                          "WebGL 报的是软件渲染（%s）—— 真机不会有，这一条基本等于自曝" % sw))
            break
    if renderer and not any(m.lower() in renderer.lower() for m in spec["gpu_marks"]):
        out.append(_c("gpu_os_mismatch", "webgl_renderer",
                      "GPU %r 与机型 %s 不符（%s 上应出现 %s 之一）"
                      % (renderer[:60], os_, os_, spec["gpu_marks"])))
    vendor = str(p.get("webgl_vendor") or "")
    if vendor and "SwiftShader" in vendor:
        out.append(_c("software_gpu", "webgl_vendor", "WebGL vendor 里带 SwiftShader"))

    # --- 字体必须含该系统的牌子（缺了就是「不是这台机器」）---
    fonts = [str(f) for f in (p.get("fonts") or [])]
    if not fonts:
        out.append(_c("no_fonts", "fonts", "字体集为空 —— 真机至少有一堆系统字体"))
    else:
        hit = [f for f in spec["fonts"] if f in fonts]
        if not hit:
            out.append(_c("fonts_os_mismatch", "fonts",
                          "字体集里一个 %s 的系统字体都没有（应有 %s 之一）"
                          % (os_, spec["fonts"])))

    # --- L4 历史自洽：零年龄不许有 cookie ---
    if int(p.get("age_days") or 0) == 0 and int(p.get("cookies") or 0) > 0:
        out.append(_c("history_impossible", "cookies",
                      "档案年龄 0 天却有 %s 个 cookie —— 历史是凭空长出来的"
                      % p.get("cookies")))
    if p.get("webdriver") is True:
        out.append(_c("webdriver_true", "webdriver",
                      "navigator.webdriver=true —— 自动化特征的直球自曝"))

    # --- Accept-Language 与 locale 同源 ---
    al = str(p.get("accept_language") or "")
    if loc and al and not al.lower().startswith(loc.lower()):
        out.append(_c("accept_language_mismatch", "accept_language",
                      "Accept-Language=%r 与 navigator.language=%r 不同源" % (al, loc)))

    return out


def ok(p):
    """能上不能上，一句话。"""
    bad = contradictions(p)
    return (True, "自洽") if not bad else (False, "；".join("%s(%s)" % (b["rule"], b["field"]) for b in bad))


# ---------------------------------------------------------------- 自测
def _selftest():
    okc = failc = 0

    def chk(cond, msg):
        nonlocal okc, failc
        if cond:
            okc += 1
        else:
            failc += 1
            print("  FAIL:", msg)

    # 四种机型 × 四个地理，生成即自洽
    n = 0
    for os_ in OS_SPECS:
        for geo in ("CN", "US", "JP", "DE"):
            p = make(os=os_, geo=geo, seed="t-%s-%s" % (os_, geo))
            bad = contradictions(p)
            chk(not bad, "生成的 %s/%s 应自洽，实得 %s" % (os_, geo, bad))
            n += 1
    print("  · %d 份生成档案全部自洽" % n)

    # UA 括号里必须是**完整**段：真 Chrome 是 "(Windows NT 10.0; Win64; x64)"，
    # 少了 "; Win64; x64" 就是一眼假 —— 这个坑真机实测时踩过。
    for os_ in OS_SPECS:
        pp = make(os=os_, geo="CN", seed="ua-%s" % os_)
        chk(any(m in pp["ua"] for m in OS_SPECS[os_]["ua_os"]),
            "%s 的 UA 必须含完整 ua_os 段（实得 %r）" % (os_, pp["ua"][:70]))
        chk("NT 10.0)" not in pp["ua"], "UA 括号里不许丢了 ; Win64; x64（实得 %r）" % pp["ua"][:70])
        chk("(X11; Linux x86_64)" in pp["ua"] or os_ != "linux",
            "linux 的 UA 括号内容不对（实得 %r）" % pp["ua"][:70])
    print("  · 4 种机型的 UA 括号段完整")

    # 视口必须小于可用区域（真机实测时 innerHeight 864 > availHeight 816 露的）
    for os_ in OS_SPECS:
        pp = make(os=os_, geo="US", seed="vp-%s" % os_)
        chk(pp["viewport"][0] <= pp["screen"][0] and pp["viewport"][1] <= pp["avail"][1],
            "%s 视口 %s 不该超过可用区域 %s" % (os_, pp["viewport"], pp["avail"]))

    # 复现性：同 seed 两次必须一模一样
    a = make(os="win", geo="CN", seed="same")
    b = make(os="win", geo="CN", seed="same")
    chk(a == b, "同 seed 两次生成的档案必须完全相同")

    # 反向：每一种矛盾都要被**精确点名**
    cases = [
        ("tz_not_in_geo", {"tz": "Asia/Tokyo"}),
        ("locale_not_in_geo", {"locale": "ja-JP"}),
        ("locale_bare_language", {"locale": "zh"}),
        ("ua_headless", {"ua": a["ua"].replace("Chrome/", "HeadlessChrome/")}),
        ("platform_mismatch", {"platform": "MacIntel"}),
        ("hc_impossible", {"hardware_concurrency": 7}),
        ("screen_impossible", {"screen": [800, 600]}),
        ("dpr_not_for_screen", {"dpr": 3.0}),
        ("dpr_not_for_screen", {"screen": [1366, 768], "dpr": 1.5}),
        ("software_gpu", {"webgl_renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), SwiftShader)"}),
        ("gpu_os_mismatch", {"webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)"}),
        ("uach_version_mismatch", {"ua_ch": {"brands": [{"brand": "Google Chrome", "version": "130"}],
                                             "mobile": False, "platform": "Windows"}}),
        ("mobile_flag_mismatch", {"ua": a["ua"] + " Mobile"}),
        ("history_impossible", {"cookies": 5}),
        ("webdriver_true", {"webdriver": True}),
        ("accept_language_mismatch", {"accept_language": "ja-JP,ja;q=0.9"}),
        ("fonts_os_mismatch", {"fonts": ["DejaVu Sans"]}),
        ("avail_bigger_than_screen", {"avail": [9999, 9999]}),
        ("viewport_bigger_than_avail", {"viewport": [9999, 9999]}),
        ("viewport_too_small", {"viewport": [800, 100]}),
        ("bad_os", {"os": "symbian"}),
    ]
    for want, patch in cases:
        p = make(os="win", geo="CN", seed="neg")
        p.update(patch)
        rules = [c["rule"] for c in contradictions(p)]
        chk(want in rules, "注入 %s 应被点名，实得 %s" % (want, rules))
    print("  · %d 种注入矛盾全部被精确点名" % len(cases))

    # 空档案不许当自洽
    chk(contradictions({}) != [], "空档案必须报错，不能返回「自洽」")
    chk(contradictions(None) != [], "None 必须报错")
    chk(ok(make(os="mac", geo="US", seed="x"))[0] is True, "ok() 对合法档案应返回 True")

    print("fp_profile 自测：%d 过 / %d 败" % (okc, failc))
    return failc == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
