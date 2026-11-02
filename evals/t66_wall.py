# -*- coding: utf-8 -*-
"""t66 · 认墙（人机验证厂商与类型识别）

为什么单独一个门禁：`wall_state` 原来只认 Cloudflare 的 DOM，遇到极验/腾讯/阿里
一律判「没墙」——**等于闭着眼撞上去，还白白上报"通过"**。这是 2026-11-02 盘点
翻出来的真实缺陷，不是理论问题。

覆盖：
  1. 七家厂商各认一遍：cloudflare_challenge / turnstile / geetest / tencent /
     aliyun / hcaptcha / recaptcha
  2. **类型判定要对**：slider / click / invisible / js_challenge，
     因为不同类型走完全不同的解法（滑块能打，点选要跑官方 JS，无感无解）
  3. advice 必须能直接照做（说得出下一步干什么）
  4. **不误判**：正常页面 found=False；只是引用了 aliyun CDN 的页面不算墙
  5. 一个页面同时有多种验证时，matches 全记，vendor 取优先级最高的
  6. wall_state 要把 verify 并进来（blocked=True + reason 带厂商名）

为什么用假 page 而不是真浏览器：
  门禁不许依赖外部环境（一起 chromium 就变慢、变脆、还抢焦点）。
  verify_state 只用到 page 的两个方法，我们按这两个方法造一个假对象，
  测的是**判定逻辑**本身——这正是要守的东西。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet import browser_reg as BR                        # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


class _El(object):
    def __init__(self, src=""):
        self._src = src

    def get_attribute(self, name):
        return self._src if name == "src" else None


class FakePage(object):
    """只实现 verify_state / wall_state 真正用到的那几个方法。"""

    def __init__(self, selectors=(), iframes=(), title="", url="http://127.0.0.1/",
                 content="<html><body>ok</body></html>"):
        self._sels = set(selectors)
        self._frames = [_El(s) for s in iframes]
        self._title = title
        self._url = url
        self._content = content

    def query_selector(self, sel):
        return _El() if sel in self._sels else None

    def query_selector_all(self, sel):
        return self._frames if sel == "iframe" else []

    def title(self):
        return self._title

    @property
    def url(self):
        return self._url

    def content(self):
        return self._content


# ---------------------------------------------------------------- P3 用的假 frame / 元素

def _png(color=(255, 0, 0), size=(44, 44)):
    """造一张真 PNG，用来验证「截图能直接被 slider.detect 吃下去」。"""
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", size, color).save(b, "PNG")
    return b.getvalue()


class FakeEl(object):
    def __init__(self, box=None, png=None):
        self._box = box
        self._png = png

    def bounding_box(self):
        return self._box

    def screenshot(self):
        return self._png


class FakeFrame(object):
    def __init__(self, url="", mapping=None):
        self.url = url
        self._m = mapping or {}

    def query_selector(self, sel):
        return self._m.get(sel)


class FakePage3(object):
    """只暴露 locate_slider 用到的 main_frame / frames。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.main_frame = self.frames[0] if self.frames else None


# ---------------------------------------------------------------- headless UA 修复用的假对象

class FakeCDP(object):
    def __init__(self, log):
        self.log = log
        self.detached = False

    def send(self, method, params=None):
        self.log.append((method, params or {}))

    def detach(self):
        self.detached = True


class FakeCtx(object):
    def __init__(self, log):
        self.log = log
        self.last = None

    def new_cdp_session(self, page):
        self.last = FakeCDP(self.log)
        return self.last


class FakePageUA(object):
    def __init__(self, ua, boom=False):
        self._ua = ua
        self._boom = boom

    def evaluate(self, expr):
        if self._boom:
            raise RuntimeError("page 崩了")
        return self._ua


def _mk_session(ua, boom=False):
    """不跑 __enter__，直接拼一个 Session 来测 _fix_headless_ua。"""
    s = BR.Session.__new__(BR.Session)
    s.page = FakePageUA(ua, boom=boom)
    s.ctx = FakeCtx([])
    return s


CASES = (
    # (说明, page 参数, 期望 vendor, 期望 kind, advice 里必须出现的关键词)
    ("cloudflare 整页挑战",
     dict(selectors=("#challenge-running",)), "cloudflare_challenge", "js_challenge", "JS 挑战"),
    ("cloudflare turnstile",
     dict(iframes=("https://challenges.cloudflare.com/cdn-cgi/challenge-platform/x",)),
     "cloudflare_turnstile", "invisible", "无感"),
    ("极验（iframe）",
     dict(iframes=("https://gcaptcha4.geetest.com/load?captcha_id=abc",)),
     "geetest", "slider", "slider.detect"),
    ("极验（DOM 兜底，无语义 iframe）",
     dict(selectors=(".geetest_holder",)), "geetest", "slider", "slider.drag"),
    ("腾讯 tcaptcha（iframe）",
     dict(iframes=("https://turing.captcha.qcloud.com/TCaptcha?aid=1",)),
     "tencent", "slider", "滑块"),
    ("腾讯（DOM）",
     dict(selectors=(".tcaptcha-transform",)), "tencent", "slider", "滑块"),
    ("阿里 nocaptcha（DOM）",
     dict(selectors=("#nc_1_wrapper",)), "aliyun", "slider", "滑块"),
    ("hCaptcha",
     dict(iframes=("https://newassets.hcaptcha.com/captcha/v1/x",)),
     "hcaptcha", "click", "点选"),
    ("reCAPTCHA",
     dict(selectors=(".g-recaptcha",)), "recaptcha", "click", "点选"),
)

# 不该被认成墙的页面
NEGATIVE = (
    ("完全正常的页面", dict()),
    # 只是引了 aliyun 的 CDN 资源 —— 说明里特意收窄过这条规则
    ("引用 aliyun CDN 但没验证",
     dict(iframes=("https://cdn.aliyun.com/lib/jquery.js",))),
    ("普通登录框",
     dict(selectors=("input[name=user]", "input[name=password]"))),
)


def main():
    # ---------------- 1~3. 逐家识别 + 类型 + advice
    for name, kw, want_v, want_k, want_advice in CASES:
        page = FakePage(**kw)
        st = BR.verify_state(page)
        check(st["vendor"] == want_v,
              "%s: vendor 应为 %s，实得 %r（signals=%s）"
              % (name, want_v, st["vendor"], st["signals"]))
        check(st["kind"] == want_k,
              "%s: kind 应为 %s，实得 %r" % (name, want_k, st["kind"]))
        check(st["found"] is True, "%s: found 应为 True" % name)
        check(want_advice in (st["advice"] or ""),
              "%s: advice 应含「%s」，实得 %r" % (name, want_advice, st["advice"]))
        check(bool(st["signals"]), "%s: 必须留下可核查的证据 signals" % name)

    # ---------------- 4. 不误判
    for name, kw in NEGATIVE:
        st = BR.verify_state(FakePage(**kw))
        check(st["found"] is False,
              "%s: 不该被认成墙，实得 vendor=%r（signals=%s）"
              % (name, st["vendor"], st["signals"]))

    # ---------------- 5. 同时存在多种：全记 + 优先级
    #   Cloudflare 整页挑战在前，极验在后 —— 页面被 CF 拦着，轮不到极验出场
    page = FakePage(iframes=("https://gcaptcha4.geetest.com/load?x=1",
                             "https://challenges.cloudflare.com/turnstile/v0/api.js"),
                    selectors=("#challenge-running",))
    st = BR.verify_state(page)
    check(st["vendor"] == "cloudflare_challenge",
          "多个验证并存时取优先级最高的（应为 cloudflare_challenge，实得 %r）"
          % st["vendor"])
    check(len(st["matches"]) >= 2,
          "并存的厂商要全记进 matches（实得 %s）" % st["matches"])
    check(len(st["frames"]) == 2,
          "frames 要把所有 iframe 记下来供穿透用（实得 %d）" % len(st["frames"]))

    # ---------------- 6. wall_state 要把 verify 并进去
    #   用真 wall_state 打一个假 page：极验层在，必须判 blocked
    pg = FakePage(iframes=("https://static.geetest.com/static/js/gt.0.4.9.js",),
                  title="登录", content="<html><body>" + "x" * 2000 + "</body></html>")
    ws = BR.wall_state(pg)
    check(ws["blocked"] is True, "极验页面 wall_state 必须判 blocked（实得 %s）" % ws["blocked"])
    check("geetest" in (ws["reason"] or ""),
          "reason 要带厂商名（实得 %r）" % ws["reason"])
    check((ws.get("verify") or {}).get("vendor") == "geetest",
          "wall_state 要把 verify 明细带出来（实得 %r）" % (ws.get("verify") or {}).get("vendor"))

    # 正常页面不能被反判成 blocked
    ws2 = BR.wall_state(FakePage(title="首页",
                                 content="<html><body>" + "y" * 2000 + "</body></html>"))
    check(ws2["blocked"] is False, "正常页面不该判 blocked（实得 %s）" % ws2["blocked"])
    check((ws2.get("verify") or {}).get("found") is False,
          "正常页面 verify.found 应为 False")

    # ---------------- 7. P3 穿 iframe：locate_slider 要能在跨域 frame 里找到滑块
    #   极验 v4 / 腾讯的滑块层都在 iframe 里，只查主文档永远找不到 ——
    #   会得出"页面上没有滑块"的假结论，而它其实就长在那儿。
    bg_box = {"x": 120.0, "y": 240.0, "width": 300.0, "height": 150.0}
    hd_box = {"x": 100.0, "y": 400.0, "width": 44.0, "height": 44.0}

    #   ① iframe 里找到
    f_iframe = FakeFrame("https://gcaptcha4.geetest.com/load?x=1", {
        ".geetest_bg": FakeEl(bg_box, _png((90, 120, 160))),
        ".geetest_btn": FakeEl(hd_box, _png((200, 200, 200))),
    })
    page3 = FakePage3([FakeFrame("http://target.test/login", {}), f_iframe])
    loc = BR.locate_slider(page3, vendor="geetest", want_png=True)
    check((loc or {}).get("ok") is True, "iframe 里的极验滑块要能找到（实得 %r）" % (loc or {}).get("err"))
    check((loc or {}).get("in_iframe") is True,
          "要标记出「在 iframe 内」（实得 %r）" % (loc or {}).get("in_iframe"))
    check("geetest.com" in ((loc or {}).get("frame_url") or ""),
          "要记下命中的 frame url（实得 %r）" % (loc or {}).get("frame_url"))
    check((loc.get("bg") or {}).get("box") == {k: round(v, 2) for k, v in
          {"x": 120, "y": 240, "w": 300, "h": 150}.items()},
          "背景图 box 要正确（实得 %r）" % (loc.get("bg") or {}).get("box"))
    check((loc.get("handle") or {}).get("selector") == ".geetest_btn",
          "把手选择器要正确（实得 %r）" % (loc.get("handle") or {}).get("selector"))
    # 截图必须是能直接喂给 slider.detect 的真 PNG
    b64 = (loc.get("bg") or {}).get("png_b64") or ""
    check(bool(b64), "want_png=True 要给出背景图 base64")
    if b64:
        import base64 as _b64
        raw = _b64.b64decode(b64)
        check(raw[:8] == b"\x89PNG\r\n\x1a\n", "截图应该是真 PNG（头 %r）" % raw[:8])
        from fleet import slider_cv as SC
        img = SC.load_image(raw)
        check(img.size == (44, 44), "截图能被 load_image 直接读（尺寸 %s）" % (img.size,))

    #   ② 主文档里找到（in_iframe=False）
    page4 = FakePage3([FakeFrame("http://target.test/login", {
        ".geetest_canvas_bg": FakeEl(bg_box),
        ".geetest_slider_button": FakeEl(hd_box),
    })])
    loc2 = BR.locate_slider(page4, vendor="geetest")
    check((loc2 or {}).get("ok") is True and (loc2 or {}).get("in_iframe") is False,
          "主文档里找到时 in_iframe 必须是 False（实得 %r）" % (loc2 or {}).get("in_iframe"))

    #   ③ 零尺寸元素要跳过（隐藏的模板元素常是 0x0，选中它等于选中空气）
    page5 = FakePage3([FakeFrame("http://t/", {
        ".geetest_bg": FakeEl({"x": 0, "y": 0, "width": 0, "height": 0}),
    })])
    loc3 = BR.locate_slider(page5, vendor="geetest")
    check((loc3 or {}).get("ok") is False,
          "0x0 的隐藏元素必须跳过（实得 ok=%r）" % (loc3 or {}).get("ok"))

    #   ④ 都找不到要如实报错并给出试过的选择器（别让人瞎猜为什么没找到）
    loc4 = BR.locate_slider(FakePage3([FakeFrame("http://t/", {})]), vendor="geetest")
    check((loc4 or {}).get("ok") is False and "tried" in (loc4 or {}),
          "找不到时要给出 tried 列表（实得 %r）" % (loc4 or {}).get("tried"))

    #   ⑤ 不指定厂商时全表试一遍
    page6 = FakePage3([FakeFrame("http://t/", {"#nc_1_n1z": FakeEl(hd_box)})])
    loc5 = BR.locate_slider(page6)
    check((loc5 or {}).get("ok") is True, "不指定 vendor 应全表试（实得 %r）" % (loc5 or {}).get("err"))
    check((loc5.get("handle") or {}).get("selector") == "#nc_1_n1z",
          "应认出阿里把手（实得 %r）" % (loc5.get("handle") or {}).get("selector"))

    # ---------------- 8. headless UA 自曝要修掉
    #   headless 的 UA 带 "HeadlessChrome"，等于在门口挂块牌子写「我是机器人」。
    #   这段逻辑最容易在将来被人"优化"掉，所以钉住。
    real_ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) HeadlessChrome/156.0.0.0 Safari/537.36")
    s = _mk_session(real_ua)
    ok_fix = s._fix_headless_ua()
    check(ok_fix is True, "带 Headless 的 UA 要判定为「需要修」（实得 %r）" % ok_fix)
    log = s.ctx.log
    check(len(log) == 1, "修复时只该发一条 CDP 命令（实得 %d 条）" % len(log))
    if log:
        method, params = log[0]
        check(method == "Emulation.setUserAgentOverride",
              "必须用 Emulation.setUserAgentOverride（Network 那条只改请求头，"
              "页面脚本照样读得到 Headless），实得 %s" % method)
        ua_sent = params.get("userAgent") or ""
        check("Headless" not in ua_sent, "覆盖后的 UA 不能再含 Headless（实得 %r）" % ua_sent)
        check("Chrome/156.0.0.0" in ua_sent,
              "版本号要跟着浏览器走、不能写死（实得 %r）" % ua_sent)
        check(ua_sent.count("Chrome") == 1,
              "只该把 HeadlessChrome 换成 Chrome，不能变成两个 Chrome（实得 %r）" % ua_sent)
    check(s.ctx.last is not None and s.ctx.last.detached is False,
          "**绝不能 detach** —— 覆盖绑在 CDP 会话上，一断开就还原（实测踩过）")

    # 本来就不带 Headless 的 UA：不该乱改
    s2 = _mk_session("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/156.0.0.0 Safari/537.36")
    check(s2._fix_headless_ua() is False, "正常 UA 不该被改（实得 True）")
    check(len(s2.ctx.log) == 0, "正常 UA 不该发任何 CDP 命令（实得 %d 条）" % len(s2.ctx.log))

    # evaluate 抛错不能拖垮会话
    s3 = _mk_session(real_ua, boom=True)
    check(s3._fix_headless_ua() is False, "页面求值失败要安静返回 False，不许抛")

    print()
    if FAIL:
        print("T66 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("PASS: 认墙全过（7 家厂商 / 4 种类型 / advice 可照做 / 正常页与 CDN 引用不误判 / "
          "多验证并存取优先级 / wall_state 带出 verify 明细 / "
          "P3 穿 iframe 找滑块 / 截图可直接喂 slider.detect / 零尺寸元素跳过 / "
          "headless UA 自曝被摘掉且不许 detach）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
