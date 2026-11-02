# -*- coding: utf-8 -*-
"""t65 · 滑块验证（视觉 + 轨迹 + 拖动 + 一条龙）

覆盖：
  1. NCC 数学正确性（不依赖任何合成夹具 —— 随机图上裁块必须 1.0 分、位置精确）
  2. 两种缺口形态定位（压暗保留内容 / 纯色抹掉内容）都要 0 误差
  3. 缩放换算：2x 图按显示宽度算，坐标要减半
  4. 轨迹性质：点数 / 时长 / 钟形速度 / 过冲 / y 抖动 / 时间戳单调
  5. CDP 序列化：首按下末抬起 + move 事件必带 buttons=1（缺了会被判成悬停）
  6. **slider.auto 的纪律**：把握低于 min_confidence 必须拒提交，且不发送任何事件
  7. executor 注入：真把「该拖多远」传下去（距离、事件序列、起点）
  8. 边界：滑块图比背景大要报错、纯色模板要 warn

**为什么第 1 条单独列在最前**：其他断言都依赖 `slider_cv._synth` 造的夹具，
夹具和被测代码同源 —— 万一算法系统性错了，夹具会跟着一起错，门禁照样绿。
第 1 条用随机图裁块，是纯数学恒等式，造不了假。
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np                                       # noqa: E402
from PIL import Image                                    # noqa: E402

from fleet import catalog, slider_cv as SC               # noqa: E402

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


def _gate(cap, dev, args, level):
    """副作用能力在这条门禁里全放行。

    安全前提：本门禁**完全离线**，日志、轨迹、executor 全是本进程内的假对象，
    不碰任何真实页面。真跑线上时靠 agent_server 的审批闸门，不靠这里。
    """
    return True


def main():
    reg = catalog.build(gate=_gate)

    # ---------------- 1. NCC 数学正确性（造不了假的那条）
    rng = np.random.default_rng(0)
    bg = rng.normal(100, 30, (100, 100)).astype(np.float32)
    tile = bg[20:60, 30:70].copy()
    s = SC._ncc_2d(bg, tile)
    y, x = np.unravel_index(int(np.argmax(s)), s.shape)
    check((y, x) == (20, 30), "NCC 定位精确：期望 (20,30)，实得 (%d,%d)" % (y, x))
    check(abs(float(s.max()) - 1.0) < 1e-5,
          "NCC 完全匹配必须是 1.0（实得 %.6f）" % float(s.max()))

    # 尺度不变：背景压暗 0.4 后仍应 1.0（比对的是形状不是亮度）
    bg2 = bg.copy()
    bg2[20:60, 30:70] *= 0.40
    s2 = SC._ncc_2d(bg2, tile)
    y2, x2 = np.unravel_index(int(np.argmax(s2)), s2.shape)
    check((y2, x2) == (20, 30), "压暗 0.4 后位置不变（实得 (%d,%d)）" % (y2, x2))
    check(abs(float(s2.max()) - 1.0) < 1e-5,
          "压暗 0.4 后得分仍是 1.0（尺度不变，实得 %.6f）" % float(s2.max()))

    # ---------------- 2. 两种缺口形态定位
    tmp = tempfile.mkdtemp(prefix="t65-")
    for mode, want_conf in (("shadow", "strong"), ("solid", "medium")):
        bg_img, tile_img, tx, ty = SC._synth(mode=mode)
        pbg = os.path.join(tmp, "bg_%s.png" % mode)
        ptl = os.path.join(tmp, "tile_%s.png" % mode)
        bg_img.save(pbg)
        tile_img.save(ptl)

        r = reg.call("slider.detect", args={"bg": pbg, "tile": ptl,
                                            "probe": "%d,%d" % (ty, tx)})
        check(r.ok, "%s: slider.detect 调用失败：%s" % (mode, r.err))
        d = r.data or {}
        check(d.get("x") == tx, "%s: 缺口 x 精确命中（真值 %d，实得 %s）"
              % (mode, tx, d.get("x")))
        check(d.get("confidence") == want_conf,
              "%s: 置信度应为 %s（实得 %s，依据 %s）"
              % (mode, want_conf, d.get("confidence"), d.get("basis")))
        # 真值排名只在 shadow 形态查：solid 形态缺口内容被抹掉，
        # 内容匹配本来就不该赢（它赢在 flat 那条腿），查排名是查错了对象。
        if mode == "shadow":
            check(d.get("rank_at_probe") == 1,
                  "%s: 真值处排名第 1（实得第 %s）" % (mode, d.get("rank_at_probe")))
        check((d.get("tile_std") or 0) > SC.TILE_STD_MIN,
              "%s: 滑块有纹理（std %.1f）" % (mode, d.get("tile_std") or 0))

    # ---------------- 3. 2x 图缩放换算
    bg_img, tile_img, tx, ty = SC._synth(mode="shadow")
    pbg = os.path.join(tmp, "bg_scale.png")
    ptl = os.path.join(tmp, "tile_scale.png")
    bg_img.save(pbg)
    tile_img.save(ptl)
    bg_w = bg_img.width
    half = bg_w / 2.0                     # 显示宽度 = 图片宽度的一半 = 2x 像素密度
    r = reg.call("slider.detect", args={"bg": pbg, "tile": ptl,
                                        "viewport_width": half,
                                        "probe": "%d,%d" % (ty, tx)})
    d = r.data or {}
    check(abs((d.get("scale") or 0) - 0.5) < 1e-3,
          "2x 图识别出缩放 0.5（图宽 %d / 显示宽 %.1f，实得 scale %s）"
          % (bg_w, half, d.get("scale")))
    check(abs((d.get("x_display") or -1) - tx * 0.5) <= 1.0,
          "按显示宽度换算正确（期望 %.1f，实得 %s）" % (tx * 0.5, d.get("x_display")))

    # ---------------- 4. 轨迹性质
    rt = reg.call("slider.track", args={"distance": 187, "seed": 11})
    check(rt.ok, "slider.track 调用失败：%s" % rt.err)
    t = rt.data or {}
    events = t.get("events") or []
    moves = [e for e in events if e["type"] == "mouseMoved"]
    check(len(moves) >= 26, "轨迹点 >= 26（实得 %d，不是瞬移）" % len(moves))
    check(0.5 <= (t.get("duration") or 0) <= 2.2,
          "时长落在 0.5~2.2s（实得 %s）" % t.get("duration"))
    check(abs((t.get("x_max") or 0) - 187) < 1.5,
          "到达目标（x_max %s vs 187）" % t.get("x_max"))
    check((t.get("y_span") or 0) >= 0.5,
          "y 有抖动、不是直线（幅度 %spx）" % t.get("y_span"))

    xs = [e["x"] for e in moves]
    steps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    head = steps[:4]
    mid = steps[len(steps) // 2 - 2: len(steps) // 2 + 3]
    check(sum(mid) / 5 > sum(head) / 4 * 1.2,
          "速度曲线是钟形：中段 %.2f > 首段 %.2f×1.2"
          % (sum(mid) / 5, sum(head) / 4))
    check(max(xs) > 187, "有过冲（真人不精确停在目标上，max %.2f）" % max(xs))
    samp = t.get("sampling_ms") or []
    check(len(samp) >= 20, "采样间隔有记录（%d 条）" % len(samp))
    check(all(8 <= v <= 31 for v in samp),
          "采样间隔落在 8~30ms（实得 %.1f~%.1f）" % (min(samp), max(samp)))

    # ---------------- 5. CDP 序列化
    check(events and events[0]["type"] == "mousePressed", "首事件是按下")
    check(events and events[-1]["type"] == "mouseReleased", "末事件是抬起")
    check(all(e.get("buttons") == 1 for e in moves),
          "所有 move 事件都带 buttons=1（缺了会被判成悬停而非拖动）")

    # ---------------- 6. auto 的纪律：把握不够就不提交
    #    solid 形态裁决为 medium，要求 strong 时必须拒，且一个事件都不许发。
    sent = []

    def spy(ev, meta):
        sent.append((len(ev), meta))
        return {"ok": True, "spy": True}

    bg_img, tile_img, tx, ty = SC._synth(mode="solid")
    pbg = os.path.join(tmp, "bg_solid.png")
    ptl = os.path.join(tmp, "tile_solid.png")
    bg_img.save(pbg)
    tile_img.save(ptl)

    ra = reg.call("slider.auto", args={"bg": pbg, "tile": ptl,
                                       "min_confidence": "strong",
                                       "executor": spy})
    check(ra.ok, "slider.auto 调用失败：%s" % ra.err)
    da = ra.data or {}
    check(da.get("submitted") is False,
          "把握 medium 低于要求 strong 时必须拒提交（实得 submitted=%s）"
          % da.get("submitted"))
    check("why" in da and "未提交" in str(da.get("why")),
          "拒提交要给出原因（实得 %r）" % da.get("why"))
    check(len(sent) == 0, "拒提交时一个事件都不许发（实得发了 %d 批）" % len(sent))

    # 门槛降到 medium 就该提交了
    sent.clear()
    ra2 = reg.call("slider.auto", args={"bg": pbg, "tile": ptl,
                                        "min_confidence": "medium",
                                        "executor": spy})
    da2 = ra2.data or {}
    check(da2.get("submitted") is True, "门槛降到 medium 后应提交（实得 %s）"
          % da2.get("submitted"))
    check(len(sent) == 1, "提交时执行器被调用一次（实得 %d）" % len(sent))

    # ---------------- 7. executor 收到的是真东西
    if sent:
        n_ev, meta = sent[0]
        check(meta.get("distance") == float(tx),
              "执行器收到的距离 = 缺口位置 %d（实得 %s）" % (tx, meta.get("distance")))
        check(meta.get("points", 0) >= 26, "执行器收到的点数 >= 26（实得 %s）" % meta.get("points"))
        check(n_ev >= 28, "执行器收到完整事件序列（%d 条 >= 28）" % n_ev)
        check(meta.get("seed") is None or isinstance(meta.get("seed"), int),
              "meta 里带 seed（%r）" % meta.get("seed"))

    # ---------------- 8. 边界
    #   滑块图比背景还大 → 必须明确报错，不能"算出"一个数字
    big = Image.new("RGBA", (400, 400), (255, 0, 0, 255))
    pbig = os.path.join(tmp, "big.png")
    big.save(pbig)
    rerr = reg.call("slider.detect", args={"bg": pbg, "tile": pbig})
    check(not rerr.ok, "滑块图比背景大时必须报错（实得 ok=%s）" % rerr.ok)

    #   纯色滑块 → 必须 warn（NCC 对无纹理模板无意义）
    #   注意用 shadow 背景：solid 背景本身就有个纯色缺口，flat 那条腿能找到它
    #   是**正确**行为，拿它测"纯色模板不可信"是测错了对象。
    flat_tile = Image.new("RGBA", (44, 44), (128, 128, 128, 255))
    pflat = os.path.join(tmp, "flat.png")
    flat_tile.save(pflat)
    shadow_bg = os.path.join(tmp, "bg_shadow.png")
    rw = reg.call("slider.detect", args={"bg": shadow_bg, "tile": pflat})
    dw = rw.data or {}
    check(bool(dw.get("warn")), "纯色滑块必须给 warn（实得 %r）" % dw.get("warn"))
    check(dw.get("confidence") == "weak",
          "纯色滑块不该给出 strong 结论（实得 %s/%s）"
          % (dw.get("confidence"), dw.get("basis")))

    print()
    if FAIL:
        print("T65 红：%d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("PASS: 滑块验证全过（NCC 数学/NCC 尺度不变/两形态 0 误差定位/2x 缩放换算/"
          "轨迹钟形+过冲+抖动/CDP buttons=1/把握不足拒提交/executor 收到真实距离/边界报错）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
