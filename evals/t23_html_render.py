"""门禁 t23：HTML 渲染后端（Video as Code）。

要验的不是「函数不崩」，而是**真出了一条能播的片**，且**两次渲一样**。

四段：
  ① 环境：node / Playwright / Chromium 在不在（不在就 skip 并说清缺什么）
  ② 真渲一段 HTML 出 MP4：文件在、分辨率对、帧数对、时长对
  ③ 确定性：同一份 HTML 渲两次 ⇒ 像素级一致（不是「差不多」）
  ④ 确定性 lint：用了 Date.now()/Math.random()/fetch ⇒ 拒绝渲染并说清原因
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet.kinds import render_html            # noqa: E402
from fleet.core.registry import Registry      # noqa: E402
from fleet.core.result import DeviceResult     # noqa: E402

PASS, FAIL = [], []


def check(cond, msg):
    if cond:
        PASS.append(msg)
        print("  ok", msg)
    else:
        FAIL.append(msg)
        print("  x ", msg)
    return bool(cond)


HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:640px;height:360px;overflow:hidden;background:#0b0f14}
.b{position:absolute;left:60px;top:120px;width:300px;height:120px;border-radius:18px;
   background:linear-gradient(135deg,#0f766e,#7c3aed);color:#fff;font:700 32px system-ui;
   display:flex;align-items:center;justify-content:center;
   animation:sl 2s linear both}
@keyframes sl{from{transform:translateX(-120px);opacity:0}to{transform:none;opacity:1}}
</style></head><body><div class="b">编队</div></body></html>"""

#: 故意犯确定性错误的 HTML
HTML_BAD = """<!DOCTYPE html><html><body><div id="x"></div>
<script>document.getElementById("x").textContent = String(Date.now());</script>
</body></html>"""


def ffprobe(path):
    """用 ffmpeg 读片子信息。返回 dict 或 None。"""
    ff = render_html.ffmpeg_path() if hasattr(render_html, "ffmpeg_path") else None
    if not ff:
        for root, _d, files in os.walk(r"D:\tools\ffmpeg"):
            for fn in files:
                if fn.lower() == "ffmpeg.exe":
                    ff = os.path.join(root, fn)
                    break
            if ff:
                break
    if not ff or not os.path.isfile(ff):
        return None
    # ffmpeg 没有 ffprobe 时，用 ffmpeg 自己读
    import subprocess
    r = subprocess.run([ff, "-i", path], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    err = r.stderr or ""
    out = {}
    import re
    m = re.search(r"(\d{2,5})x(\d{2,5})", err)
    if m:
        out["w"], out["h"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", err)
    if m:
        out["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", err)
    if m:
        out["fps"] = float(m.group(1))
    return out


def frame_hash(frames_dir):
    """把一串 PNG 的字节哈希起来（顺序敏感）。"""
    h = hashlib.sha256()
    for fn in sorted(os.listdir(frames_dir)):
        if fn.endswith(".png"):
            with open(os.path.join(frames_dir, fn), "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def main():
    print("=== t23 HTML 渲染后端（Video as Code） ===")

    # ---- ① 环境 ----
    ok, why = render_html.check_env()
    if not ok:
        print("  -- 环境不可用，跳过：%s" % why)
        print("t23: SKIP")
        return 0
    check(True, "环境齐（node + Playwright + Chromium）：%s" % why if why else "环境齐")

    tmp = tempfile.mkdtemp(prefix="fleet-t23-")
    try:
        # ---- ② 真渲出片 ----
        outdir = os.path.join(tmp, "out")
        r = render_html.render_html(HTML, outdir, name="demo",
                                    width=640, height=360, fps=30, duration=2.0,
                                    keep_frames=True)
        if not check(r.get("ok"), "HTML 渲成 MP4（%s）" % (r.get("err") or "ok")):
            return 1
        mp4 = r.get("mp4")
        check(os.path.isfile(mp4) and os.path.getsize(mp4) > 1000,
              "MP4 落盘且非空（%d 字节）" % (os.path.getsize(mp4) if os.path.isfile(mp4) else 0))
        check(r.get("frames") == 60, "帧数 = 时长×帧率（实际 %s，期望 60）" % r.get("frames"))
        check(r.get("width") == 640 and r.get("height") == 360,
              "尺寸参数对（%sx%s）" % (r.get("width"), r.get("height")))

        info = ffprobe(mp4)
        if info:
            check(info.get("w") == 640 and info.get("h") == 360,
                  "ffprobe 读到的分辨率 = 640x360（实际 %sx%s）" % (info.get("w"), info.get("h")))
            check(abs((info.get("dur") or 0) - 2.0) < 0.3,
                  "片长 ≈ 2 秒（实际 %s）" % info.get("dur"))
            check(abs((info.get("fps") or 0) - 30) < 1,
                  "帧率 = 30（实际 %s）" % info.get("fps"))

        # ---- ③ 确定性：渲两次，帧要一模一样 ----
        frames1 = os.path.join(tmp, "out", "_html", "frames")
        h1 = frame_hash(frames1) if os.path.isdir(frames1) else None
        outdir2 = os.path.join(tmp, "out2")
        r2 = render_html.render_html(HTML, outdir2, name="demo",
                                     width=640, height=360, fps=30, duration=2.0,
                                     keep_frames=True)
        if check(r2.get("ok"), "第二次渲染成功（%s）" % (r2.get("err") or "ok")):
            frames2 = os.path.join(tmp, "out2", "_html", "frames")
            h2 = frame_hash(frames2) if os.path.isdir(frames2) else None
            check(h1 is not None and h1 == h2,
                  "两次渲染逐字节一致（确定性）")

        # ---- ④ 确定性 lint ----
        outdir3 = os.path.join(tmp, "out3")
        r3 = render_html.render_html(HTML_BAD, outdir3, name="bad",
                                     width=320, height=200, fps=10, duration=1.0)
        check(not r3.get("ok"), "用了 Date.now() 的 HTML 被拒绝渲染")
        check("Date.now" in (r3.get("err") or "") or "Date" in (r3.get("err") or ""),
              "拒绝时说清了原因（%s）" % (r3.get("err") or "")[:120])

        # ---- ④b 内置模板：不想自己写 HTML 也能出片 ----
        from fleet.kinds.render import Render
        dev = Render()
        tpl_out = os.path.join(tmp, "tpl")
        r4 = dev.invoke("render.html",
                        {"template": "reel_demo", "outdir": tpl_out, "name": "demo",
                         "width": 640, "height": 360, "fps": 10, "duration": 2.0})
        check(r4.ok, "用内置模板 demo 渲出片（%s）" % (r4.err or "ok"))
        if r4.ok:
            check(os.path.isfile(r4.data.get("mp4", "")),
                  "模板片子落盘（%s）" % r4.data.get("mp4"))
        r5 = dev.invoke("render.html",
                        {"template": "没有这个模板", "outdir": tpl_out})
        check(not r5.ok and "模板" in (r5.err or ""),
              "要一个不存在的模板 → 报错并说清有哪些现成的")

        # ---- ⑤ 注册表里能看见这项能力 ----
        from fleet.catalog import CAPABILITIES
        from fleet.kinds.render import Render
        reg = Registry(path=os.path.join(tmp, "reg.json"))
        reg.register_all(CAPABILITIES)
        reg.add(Render())
        cap = reg.capability("render.html")
        check(cap is not None, "注册表里查得到 render.html")
        if cap:
            check(cap.side_effect and cap.approval == "ask",
                  "render.html 是副作用 + 要审批（写 MP4）")
        # 没闸门 ⇒ 必须拒绝（fail-closed）
        res = reg.call("render.html", "render:local",
                       {"html": HTML, "outdir": os.path.join(tmp, "out4")})
        check(isinstance(res, DeviceResult) and not res.ok,
              "没有闸门时 render.html 被拒（fail-closed）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=== t23 %s（%d 通过 / %d 失败）===" %
          ("全通过" if not FAIL else "有失败", len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
