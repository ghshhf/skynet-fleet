"""门禁 t69：指纹层三件套 —— 档案自洽 / 注入正确 / 沙箱隔离。

为什么需要这条门禁
------------------
「伪装」有个特点：**看起来生效了，不等于没露**。
UA 改了、UA-CH 没改；时区改了、语言没改；屏幕改了、视口比屏幕还大——
页面上一切正常，只有检测方看得见矛盾。

所以这条门禁守的不是「函数返回值对不对」，是**矛盾能不能被喊出来**。
手段是反向注入：造 20 种矛盾，每种都必须被**精确点名**。
如果哪天有人把 contradictions() 改成永远返回 []，第一节的防假绿立刻变红。

另外守三条纪律（都是从 t68 那次教训来的）：
  · 三个模块顶层不许 import 第三方 —— 缺 Pillow/playwright 不许连带 415 条能力一起哑
  · 沙箱路径必须防穿越 —— 删目录的代码一旦能被 ../ 带跑，代价是删别的东西
  · 出口漂移必须报 —— 「同一台机器突然换了国家」比新访客更可疑，这条不能漏

    python evals/t69_fingerprint.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_OK = _FAIL = 0


def check(cond, msg):
    global _OK, _FAIL
    if cond:
        _OK += 1
    else:
        _FAIL += 1
        print("  x %s" % msg)


def sec(t):
    print("\n[%s]" % t)


def main():
    from fleet import fp_profile as FP
    from fleet import fp_inject as FI
    from fleet import fp_sandbox as SB

    # ---------------------------------------------------------------- 1
    sec("1 档案自洽：四种机型 × 四个地理，生成即自洽")
    n = 0
    for plat in FP.OS_SPECS:
        for geo in ("CN", "US", "JP", "DE"):
            p = FP.make(platform=plat, geo=geo, seed="t69-%s-%s" % (plat, geo))
            bad = FP.contradictions(p)
            check(not bad, "%s/%s 应自洽，实得 %s" % (plat, geo, [b["rule"] for b in bad]))
            n += 1
    print("  · %d 份生成档案全部自洽" % n)

    # 防假绿：校验器本身必须真的会喊
    check(FP.contradictions({}) != [], "空档案必须报错（否则前面全绿没意义）")
    check(FP.contradictions(None) != [], "None 必须报错")
    check(FP.contradictions({"os": "win", "geo": "CN"}) != [],
          "只有 os/geo、其他字段全缺的档案必须报错，不许当自洽")

    # ---------------------------------------------------------------- 2
    sec("2 反向注入：每一种矛盾都要被**精确点名**")
    base = FP.make(platform="win", geo="CN", seed="t69-neg")
    cases = [
        ("tz_not_in_geo", {"tz": "Asia/Tokyo"}, "中国档案配东京时区"),
        ("locale_not_in_geo", {"locale": "ja-JP"}, "中国档案配日语"),
        ("locale_bare_language", {"locale": "zh"}, "裸语言码（真 Chrome 报 zh-CN）"),
        ("ua_headless", {"ua": base["ua"].replace("Chrome/", "HeadlessChrome/")}, "UA 自曝 headless"),
        ("platform_mismatch", {"platform": "MacIntel"}, "UA 说 Windows、platform 说 Mac"),
        ("hc_impossible", {"hardware_concurrency": 7}, "不存在的核数"),
        ("dm_impossible", {"device_memory": 3}, "不存在的内存档位"),
        ("screen_impossible", {"screen": [800, 600]}, "不存在的分辨率"),
        ("dpr_not_for_screen", {"dpr": 3.0}, "Windows 上 3.0 的 DPR"),
        ("dpr_not_for_screen", {"screen": [1366, 768], "dpr": 1.5},
         "1366x768 配 1.5 = 等效 2049x1152，这种屏不存在（真机实测踩到）"),
        ("software_gpu", {"webgl_renderer": "ANGLE (Google, Vulkan 1.3.0 "
                                            "(SwiftShader Device (Subzero)), SwiftShader)"},
         "**软件渲染自曝（最硬的一条）**"),
        ("gpu_os_mismatch", {"webgl_renderer": "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, "
                                               "Unspecified Version)"}, "Windows 报苹果 GPU"),
        ("uach_version_mismatch", {"ua_ch": {"brands": [{"brand": "Google Chrome",
                                                         "version": "130"}],
                                             "mobile": False, "platform": "Windows"}},
         "UA 说 142、UA-CH 说 130"),
        ("uach_headless", {"ua_ch": {"brands": [{"brand": "HeadlessChrome", "version": "142"}],
                                     "mobile": False, "platform": "Windows"}},
         "UA-CH 里藏 HeadlessChrome"),
        ("mobile_flag_mismatch", {"ua": base["ua"] + " Mobile"}, "桌面 UA 带 Mobile"),
        ("uach_mobile_mismatch", {"ua_ch": dict(base["ua_ch"], mobile=True)}, "UA-CH 说 mobile"),
        ("history_impossible", {"cookies": 5}, "0 天年龄却有 cookie"),
        ("webdriver_true", {"webdriver": True}, "navigator.webdriver=true"),
        ("accept_language_mismatch", {"accept_language": "ja-JP,ja;q=0.9"}, "Accept-Language 不同源"),
        ("fonts_os_mismatch", {"fonts": ["DejaVu Sans"]}, "Windows 只有 Linux 字体"),
        ("avail_bigger_than_screen", {"avail": [9999, 9999]}, "可用区域比屏幕大"),
        ("viewport_bigger_than_avail", {"viewport": [9999, 9999]}, "视口比可用区域大"),
        ("viewport_too_small", {"viewport": [800, 100]}, "视口像条缝"),
        ("bad_os", {"os": "symbian"}, "不存在的机型"),
    ]
    for want, patch, why in cases:
        p = dict(base)
        p.update(patch)
        rules = [c["rule"] for c in FP.contradictions(p)]
        check(want in rules, "注入「%s」应报 %s，实得 %s" % (why, want, rules))
    print("  · %d 种注入矛盾全部被精确点名" % len(cases))

    # ---------------------------------------------------------------- 3
    sec("3 注入清单：CDP 该发的四个调用一个都不能少")
    p = FP.make(platform="win", geo="CN", seed="t69-cdp")
    calls = FI.cdp_calls(p)
    by = dict(calls)
    for m in ("Emulation.setUserAgentOverride", "Emulation.setTimezoneOverride",
              "Emulation.setLocaleOverride", "Emulation.setDeviceMetricsOverride"):
        check(m in by, "cdp_calls 应含 %s，实得 %s" % (m, sorted(by)))

    ua = by.get("Emulation.setUserAgentOverride") or {}
    check(ua.get("userAgent") == p["ua"], "CDP 的 UA 必须来自档案")
    check(ua.get("acceptLanguage") == p["accept_language"], "Accept-Language 必须来自档案")
    md = ua.get("userAgentMetadata") or {}
    check(md.get("platform") == "Windows", "win 的 UA-CH platform 应是 Windows")
    check(md.get("mobile") is False, "win 的 UA-CH mobile 必须 False")
    check(str(md.get("fullVersion", "")).startswith(str(p["chrome_major"])),
          "UA-CH fullVersion 必须与 UA 版本同源（这是 UA 与 UA-CH 不打架的唯一正解）")
    check(any(b.get("brand") == "Google Chrome" for b in (md.get("brands") or [])),
          "UA-CH brands 必须含 Google Chrome，否则不像真 Chrome")

    dm = by.get("Emulation.setDeviceMetricsOverride") or {}
    check(dm.get("width") == p["viewport"][0] and dm.get("height") == p["viewport"][1],
          "视口必须用档案的 viewport，不许用整屏（真机实测踩过：inner 864 > avail 816）")
    check(dm.get("screenWidth") == p["screen"][0],
          "screen 也要走 CDP 原生覆盖（比 JS 补更硬）")

    # 四种机型都验一遍，别只顾 win
    for plat in FP.OS_SPECS:
        pp = FP.make(platform=plat, geo="CN", seed="t69-cdp-%s" % plat)
        try:
            FI.cdp_calls(pp)
            okc = True
        except Exception as e:
            okc = False
            print("      %s: %s" % (plat, e))
        check(okc, "%s 的 cdp_calls 不许抛异常" % plat)
        mdp = FI.ua_metadata(pp)
        check(mdp["mobile"] is (plat == "android"), "%s 的 UA-CH mobile 应与机型一致" % plat)

    # ---------------------------------------------------------------- 4
    sec("4 注入脚本：该盖的破绽一处都不能漏")
    for token, why in (
        ("__fp_fonts", "字体探针"),
        ("37445", "WebGL UNMASKED_VENDOR_WEBGL"),
        ("37446", "WebGL UNMASKED_RENDERER_WEBGL"),
        ("getParameter", "WebGL 覆盖点"),
        ("outerWidth", "headless 的老破绽 outerWidth=0"),
        ("hardwareConcurrency", "核数"),
        ("getImageData", "Canvas 噪声"),
        ("native code", "被替换函数必须伪装成 native"),
        ("webdriver", "navigator.webdriver"),
        ("Screen.prototype", "screen.* 覆盖"),
    ):
        check(token in FI.INIT_JS, "INIT_JS 必须覆盖 %s（%s）" % (token, why))
    check("SwiftShader" not in FI.INIT_JS,
          "INIT_JS 里不许写死 SwiftShader —— 那会变成「只认得一个字符串的补丁」，"
          "换 llvmpipe/OffScreen 就失效。一律按档案无条件返回。")

    # ---------------------------------------------------------------- 5
    sec("5 读回核对：每种破绽都要在 diff 里现形")
    p = FP.make(platform="win", geo="CN", seed="t69-diff")
    good = {
        "ua": p["ua"], "platform": p["platform"], "language": p["locale"],
        "hardware_concurrency": p["hardware_concurrency"],
        "device_memory": p["device_memory"], "webdriver": False, "tz": p["tz"],
        "screen": p["screen"], "avail": p["avail"], "dpr": p["dpr"],
        "outer": p["screen"], "inner": p["viewport"],
        "webgl_vendor": p["webgl_vendor"], "webgl_renderer": p["webgl_renderer"],
        "ua_ch": {"brands": FI.ua_metadata(p)["brands"], "mobile": False},
        "fonts_ok": {f: True for f in p["fonts"]},
    }
    check(not FI.diff(p, good), "完全按档案注入后 diff 必须为空，实得 %s" % FI.diff(p, good))

    bads = [
        ("ua", {"ua": "Mozilla/5.0 ... HeadlessChrome/156"}, "UA 没盖住"),
        ("webdriver", {"webdriver": True}, "webdriver 没盖住"),
        ("tz", {"tz": "UTC"}, "时区没盖住"),
        ("screen", {"screen": [1280, 720]}, "屏幕没盖住"),
        ("outer", {"outer": [0, 0]}, "outerWidth=0（headless 标志）"),
        ("webgl_renderer", {"webgl_renderer": "SwiftShader"}, "软件渲染泄漏"),
        ("ua_ch.brands", {"ua_ch": {"brands": [{"brand": "HeadlessChrome", "version": "156"}],
                                    "mobile": False}}, "UA-CH 泄漏"),
        ("inner", {"inner": [9999, 9999]}, "视口比档案大"),
        ("fonts[Segoe UI]", {"fonts_ok": {"Segoe UI": False}}, "声明的字体查不到"),
    ]
    for want, patch, why in bads:
        a = dict(good)
        a.update(patch)
        fields = [x["field"] for x in FI.diff(p, a)]
        check(any(f == want or f.startswith(want) for f in fields),
              "「%s」应报 %s，实得 %s" % (why, want, fields))
    check(FI.diff(p, None) != [], "readback 不是 dict 时要报错，不能当通过")

    # ---------------------------------------------------------------- 6
    sec("6 沙箱：路径防穿越 / 出口漂移 / 养号不参与过期清理")
    import tempfile
    import shutil
    import time as _t

    real = SB.SANDBOX_DIR
    tmp = tempfile.mkdtemp(prefix="t69-")
    SB.SANDBOX_DIR = os.path.join(tmp, "sandbox")
    try:
        for bad in ("../etc", "..", "cn-win", "CN-WIN-abc123", "x/y", "", None,
                    "cn-win-../../x", "cn win-abc123"):
            try:
                SB.sandbox_path(bad)
                check(False, "非法 id %r 必须被拒（防穿越）" % (bad,))
            except ValueError:
                check(True, "")
        print("  · 非法 id 全部拒绝（含 ../、绝对路径、大写、空格）")

        r = SB.create(platform="win", geo="CN", seed="t69")
        check(r["ok"], "沙箱应建得出来：%s" % r.get("err"))
        sid = r["id"]
        check(not FP.contradictions(r["profile"]), "沙箱里的档案也必须自洽")
        check(SB.check(sid) == [], "干净沙箱应无问题")

        r2 = SB.create(platform="win", geo="CN", seed="t69")
        check(r2["reused"] is True and r2["id"] == sid, "同 seed 必须复用（重造会丢历史）")
        r3 = SB.create(platform="win", geo="CN", seed="t69", fresh=True)
        check(r3["id"] != sid, "fresh=True 才另起一个")

        SB.bind_egress(sid, proxy="http://127.0.0.1:7891", country="CN", ip="1.2.3.4")
        check(SB.check(sid, live_country="CN") == [], "出口一致时不该报问题")
        iss = SB.check(sid, live_country="US")
        check(any(i["rule"] == "egress_drift" for i in iss),
              "出口从 CN 漂到 US 必须报 egress_drift，实得 %s" % [i["rule"] for i in iss])
        check(any("搬家" in i["why"] for i in iss),
              "要说清「搬家比新访客更危险」，不能只说「不一致」")
        iss = SB.check(sid, live_proxy="http://127.0.0.1:9999")
        check(any(i["rule"] == "proxy_changed" for i in iss), "换线路也必须报")

        # 手改 meta 也要被体检出来
        m = SB.get(sid)
        m["profile"]["tz"] = "Asia/Tokyo"
        SB._write_meta(sid, m)
        check(any(i["rule"] == "profile_tz_not_in_geo" for i in SB.check(sid)),
              "meta 被手改坏也要体检出来")

        # 过期只在一次性沙箱上算
        m["profile"]["tz"] = "Asia/Shanghai"
        m["created"] = _t.time() - 86400 * 30
        SB._write_meta(sid, m)
        check(not any(x["id"] == sid for x in SB.list_pool()),
              "过期的 sid 不该出现在默认列表里（池里其他未过期的可以留着）")
        keep = SB.create(platform="win", geo="JP", seed="t69-keep", persist=True, ttl_days=1)["id"]
        mk = SB.get(keep)
        mk["created"] = _t.time() - 86400 * 30
        SB._write_meta(keep, mk)
        SB.purge(dry_run=False)
        check(os.path.isdir(SB.sandbox_path(keep)), "persist=True 的沙箱不该被 purge 清掉")
        check(not os.path.isdir(SB.sandbox_path(sid)), "过期的一次性沙箱应被 purge 清掉")

        check(SB.destroy(keep)["ok"], "destroy 应成功")
        check(SB.destroy(keep)["ok"] is False, "删不存在的应返回 ok=False 而不是抛异常")
    finally:
        SB.SANDBOX_DIR = real
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 7
    sec("7 可选依赖纪律：三个模块顶层不许拽第三方库")
    import io
    for fn in ("fp_profile.py", "fp_inject.py", "fp_sandbox.py"):
        src = io.open(os.path.join(ROOT, "fleet", fn), encoding="utf-8").read()
        for bad in ("\nimport numpy", "\nimport PIL", "\nfrom PIL", "\nfrom playwright",
                    "\nimport playwright", "\nimport cv2"):
            check(bad not in src, "%s 顶层不许出现 %r（缺依赖会连带 415 条能力一起哑，见 t68）"
                  % (fn, bad.strip()))
    print("  · 三模块顶层无第三方 import（playwright 只在真调用时延迟 import）")

    print("\n=== t69: %d 过 / %d 败 ===" % (_OK, _FAIL))
    return _FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
