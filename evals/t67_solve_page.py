# -*- coding: utf-8 -*-
"""t67 · 打真实页面（slider.solve_page 全链路）

这条门禁要守住的是**一个判断**，不是一个数字：

    「拖完了」和「进去了」是两件事。

所以核心断言是 `passed` 三态（true / false / null）：
  · 墙没了        → true
  · 墙还在        → **false**（拖完了但没进去，必须这么说）
  · 没真提交/判断不了 → null

如果哪一天有人把它简化成「拖成功了就返回 ok」，这条门禁会红。

覆盖：
  1. 正常链路：认墙 → 定位（iframe 内）→ 截图 → 算缺口 → 拖 → 再认墙 → passed=true
  2. **拖完墙还在 → passed=false**（关键：不许把 submitted 当 passed）
  3. 不是滑块（点选/无感）→ 明确拒绝，并给出该走哪条路
  4. 缺缺口图（腾讯/阿里位移式）→ 明确说「整图分析未实现」，不假装能打
  5. 把握不足 → 不提交、一个事件都不发
  6. dry_run → 不提交、passed=null
  7. 事件序列本身：buttons=1、起点是把手中心、距离等于算出来的缺口位置

为什么用假 page 而不是真浏览器：
  真起 chromium 一次 8 秒、还会因为页面内容变化而红（门禁必须天天能跑）。
  这个假 page 只实现 wall_state / locate_slider / CDP 真正用到的那几个方法，
  测的是**链路逻辑**。真浏览器的行为另有一次性验证（见 2026-11-02 实验记录）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet import catalog, slider_cv as SC               # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def _gate(cap, dev, args, level):
    """本地假页面，全放行。这里没有任何真实站点。"""
    return True


# ---------------------------------------------------------------- 假页面

class _El(object):
    def __init__(self, box=None, png=None):
        self._box = box
        self._png = png

    def bounding_box(self):
        return self._box

    def screenshot(self):
        return self._png

    def get_attribute(self, name):
        return None


class _SrcEl(object):
    def __init__(self, src):
        self._src = src

    def get_attribute(self, name):
        return self._src if name == "src" else None

    def bounding_box(self):
        return None

    def screenshot(self):
        return None


class _CDP(object):
    def __init__(self, page):
        self.page = page

    def send(self, method, params=None):
        params = params or {}
        self.page.sent.append((method, dict(params)))
        # 松手就代表「提交了」。真页面过不过另说，这里由 release_clears_wall 决定。
        if params.get("type") == "mouseReleased" and self.page.release_clears_wall:
            self.page.is_clear = True

    def detach(self):
        pass


class _Ctx(object):
    def __init__(self, page):
        self.page = page

    def new_cdp_session(self, p):
        return _CDP(self.page)


class _Frame(object):
    def __init__(self, owner, url="", mapping=None):
        self.owner = owner
        self.url = url
        self._m = mapping or {}

    def query_selector(self, sel):
        v = self._m.get(sel)
        if v is None:
            return None
        return v


class SolvePage(object):
    """一个会「拖完变状态」的假页面。

    release_clears_wall=True  → 松手后墙消失（模拟真过了）
    release_clears_wall=False → 松手后墙还在（模拟没过，风控要求再试）
    """

    def __init__(self, bg_png, slice_png, handle_png,
                 kind="slider", release_clears_wall=True, with_slice=True):
        self.bg_png, self.slice_png, self.handle_png = bg_png, slice_png, handle_png
        self.kind = kind
        self.release_clears_wall = release_clears_wall
        self.with_slice = with_slice
        self.is_clear = False
        self.sent = []
        self.context = _Ctx(self)
        self.url = "http://target.test/login"

        from PIL import Image
        import io as _io
        _bg_w, _bg_h = Image.open(_io.BytesIO(bg_png)).size
        bg_box = {"x": 120.0, "y": 240.0, "width": float(_bg_w), "height": float(_bg_h)}
        hd_box = {"x": 100.0, "y": 400.0, "width": 44.0, "height": 44.0}
        sl_box = {"x": 0.0, "y": 0.0, "width": 44.0, "height": 44.0}

        sub_map = {
            ".geetest_bg": _El(bg_box, bg_png),
            ".geetest_btn": _El(hd_box, handle_png),
        }
        if with_slice:
            sub_map[".geetest_canvas_slice"] = _El(sl_box, slice_png)

        main = _Frame(self, "http://target.test/login", {})
        sub = _Frame(self, "https://gcaptcha4.geetest.com/load?x=1", sub_map)
        self.frames = [main, sub]
        self.main_frame = main

    # ---- wall_state / verify_state 用到的
    def query_selector(self, sel):
        # 滑块层的容器只在「墙还在」时存在 —— 过了就没了
        if not self.is_clear and sel in (".geetest_holder", ".geetest_panel"):
            return _El()
        return None

    def query_selector_all(self, sel):
        # 墙过了，厂商会把 iframe 摘掉 —— 假页面也得跟着摘，
        # 否则「过了」之后还能认出 geetest，passed 就永远真不了。
        if sel == "iframe" and not self.is_clear:
            return [_SrcEl("https://gcaptcha4.geetest.com/load?x=1")]
        return []

    def title(self):
        return "登录"

    def content(self):
        return "<html><body>" + "填充正文" * 400 + "</body></html>"


def _mk_page(release=True, with_slice=True, kind="slider"):
    """造一张背景图 + 缺口图（用 slider_cv 的合成夹具，已知真值 187）。"""
    bg, tile, tx, ty = SC._synth(mode="shadow")
    import io as _io
    b1, b2, b3 = _io.BytesIO(), _io.BytesIO(), _io.BytesIO()
    bg.save(b1, "PNG")
    tile.save(b2, "PNG")
    tile.save(b3, "PNG")
    p = SolvePage(b1.getvalue(), b2.getvalue(), b3.getvalue(),
                  kind=kind, release_clears_wall=release, with_slice=with_slice)
    return p, tx


def main():
    reg = catalog.build(gate=_gate)

    # ---------------- 1. 正常链路
    page, tx = _mk_page(release=True)
    r = reg.call("slider.solve_page", args={"page": page, "settle": 0, "seed": 7})
    check(r.ok, "solve_page 调用失败：%s" % r.err)
    d = r.data or {}
    steps = [s.get("step") for s in (d.get("steps") or [])]
    check(steps[:2] == ["wall", "locate"], "步骤顺序应从 wall → locate 开始（实得 %s）" % steps)
    check("detect" in steps and "drag" in steps and "verify" in steps,
          "步骤里要有 detect / drag / verify（实得 %s）" % steps)
    check((d.get("wall_before") or {}).get("verify", {}).get("vendor") == "geetest",
          "认墙要认出 geetest（实得 %r）"
          % (d.get("wall_before") or {}).get("verify", {}).get("vendor"))
    check((d.get("locate") or {}).get("in_iframe") is True,
          "要记录滑块是在 iframe 里找到的（实得 %r）"
          % (d.get("locate") or {}).get("in_iframe"))
    det = d.get("detect") or {}
    check(det.get("x") == tx, "算出的缺口 x 要精确（真值 %d，实得 %r）" % (tx, det.get("x")))
    check(d.get("submitted") is True, "正常链路应该真提交了（实得 %r）" % d.get("submitted"))
    check(d.get("passed") is True,
          "拖完墙没了 → passed 必须是 true（实得 %r，why=%r）" % (d.get("passed"), d.get("why")))

    # ---------------- 2. 【关键】拖完墙还在 → passed 必须 false
    page2, tx2 = _mk_page(release=False)
    r2 = reg.call("slider.solve_page", args={"page": page2, "settle": 0, "seed": 7})
    d2 = r2.data or {}
    check(d2.get("submitted") is True, "第二种情况确实提交了（实得 %r）" % d2.get("submitted"))
    check(d2.get("passed") is False,
          "**拖完但墙还在，passed 必须是 false**（实得 %r）——"
          "把 submitted 当 passed 是最危险的错" % d2.get("passed"))
    check("没进去" in (d2.get("why") or "") or "还挡着" in (d2.get("why") or ""),
          "要说清楚「还挡着」而不是含糊（实得 %r）" % d2.get("why"))

    # ---------------- 3. 事件序列本身要对
    moves = [p for (m, p) in page.sent if p.get("type") == "mouseMoved"]
    check(len(moves) >= 26, "真发了完整轨迹（%d 个 move 事件）" % len(moves))
    check(all(p.get("buttons") == 1 for p in moves),
          "move 事件必须带 buttons=1（缺了会被判成悬停，不触发拖拽）")
    pressed = [p for (m, p) in page.sent if p.get("type") == "mousePressed"]
    check(bool(pressed), "必须有按下事件")
    if pressed:
        # 起点应该是把手中心：(100+44/2, 400+44/2) = (122, 422)
        check(abs(pressed[0]["x"] - 122.0) < 0.5 and abs(pressed[0]["y"] - 422.0) < 0.5,
              "起点要落在把手中心（期望 122,422；实得 %s,%s）"
              % (pressed[0]["x"], pressed[0]["y"]))
    rel = [p for (m, p) in page.sent if p.get("type") == "mouseReleased"]
    check(bool(rel), "必须有松手事件")
    if rel and pressed:
        moved = rel[-1]["x"] - pressed[0]["x"]
        check(abs(moved - tx) <= 2.5,
              "净位移要等于算出的缺口位置（期望 %d，实得 %.1f）" % (tx, moved))

    # ---------------- 4. 不是滑块 → 明确拒绝并指路
    page3, _ = _mk_page()
    # 要测「不是滑块」，就得先把极验那层清掉，否则认墙先认出 geetest（kind=slider）——
    # 那就变成在测极验了，测错了对象。
    page3.is_clear = True
    page3.query_selector_all = lambda sel: ([_SrcEl("https://newassets.hcaptcha.com/captcha/v1/x")]
                                            if sel == "iframe" else [])
    r3 = reg.call("slider.solve_page", args={"page": page3, "settle": 0})
    d3 = r3.data or {}
    check(d3.get("ok") is False, "点选类不该走滑块逻辑（实得 ok=%r）" % d3.get("ok"))
    check("不是滑块" in (d3.get("why") or ""), "要说清「不是滑块」（实得 %r）" % d3.get("why"))
    check(d3.get("passed") is None, "没动手就判断不了过没过，passed 应为 null（实得 %r）"
          % d3.get("passed"))

    # ---------------- 5. 缺缺口图 → 如实说边界，不假装能打
    page4, _ = _mk_page(with_slice=False)
    r4 = reg.call("slider.solve_page", args={"page": page4, "settle": 0})
    d4 = r4.data or {}
    check(d4.get("ok") is False, "缺缺口图不该硬算（实得 ok=%r）" % d4.get("ok"))
    check("整图" in (d4.get("why") or "") or "缺口图" in (d4.get("why") or ""),
          "要说明缺的是什么、没实现的是什么（实得 %r）" % d4.get("why"))
    check(not any(p.get("type") == "mouseMoved" for (_m, p) in page4.sent),
          "缺零件时一个事件都不许发（实得 %d）"
          % len([1 for (_m, p) in page4.sent if p.get("type") == "mouseMoved"]))

    # ---------------- 6. dry_run → 不提交、passed=null
    page5, _ = _mk_page()
    r5 = reg.call("slider.solve_page", args={"page": page5, "settle": 0, "dry_run": True})
    d5 = r5.data or {}
    check(d5.get("submitted") is False, "dry_run 不该提交（实得 %r）" % d5.get("submitted"))
    check(d5.get("passed") is None, "dry_run 判断不了过没过，passed 应为 null（实得 %r）"
          % d5.get("passed"))
    check(not any(p.get("type") == "mouseMoved" for (_m, p) in page5.sent),
          "dry_run 一个事件都不许发")

    # ---------------- 7. 把握门槛：要求 strong 而实际只有 medium 时不许提交
    #   把背景换成纯色抹掉内容那种（裁决为 medium），要求 strong
    import io as _io
    bg, tile, tx7, ty7 = SC._synth(mode="solid")
    b1, b2, b3 = _io.BytesIO(), _io.BytesIO(), _io.BytesIO()
    bg.save(b1, "PNG")
    tile.save(b2, "PNG")
    tile.save(b3, "PNG")
    page7 = SolvePage(b1.getvalue(), b2.getvalue(), b3.getvalue())
    r7 = reg.call("slider.solve_page", args={"page": page7, "settle": 0,
                                            "min_confidence": "strong"})
    d7 = r7.data or {}
    check(d7.get("submitted") is False,
          "把握 medium 低于要求 strong 时不许提交（实得 %r）" % d7.get("submitted"))
    check(not any(p.get("type") == "mouseMoved" for (_m, p) in page7.sent),
          "拒提交时一个事件都不许发")

    print()
    if FAIL:
        print("T67 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("PASS: 全链路过（认墙→iframe 定位→截图→算缺口→拖动→再认墙 / "
          "拖完墙还在必须 passed=false / 非滑块与缺零件如实拒绝 / "
          "低把握不提交 / dry_run 不提交 / 起点=把手中心 净位移=缺口位置）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
