"""HTML 渲染后端：写 HTML，代码出片（Video as Code）。

为什么要有这个后端
------------------
「Video as Code」是个已经成型的品类（HyperFrames / Remotion / Motion Canvas），
内核都一样：**HTML + 可寻址动画 → 确定性逐帧 → ffmpeg 合成 MP4**。
2026-10-21 实测本机跑通这条管线：

    Playwright + Chromium 逐帧截图 = 6.6 帧/秒
    ⇒ 15 秒（450 帧）片子约 1.1 分钟

对比本仓原有的 Python 引擎（RuiC-motion-reel，numpy + Pillow 画每一帧）：
独占 CPU 约 47 分钟，被别的任务抢 CPU 会掉到 2 小时以上。
**快 40 倍，而且不占 GPU、不需要装任何新东西**（Chromium 和 ffmpeg 本机都有）。

最要紧的一点是**它跟本仓是同一个技术栈**：
本仓的产品（桌面图形版）本身就是纯手写 HTML/CSS/JS、零构建、断网可用。
所以「出一张图/一条片」用的就是本仓写界面那套东西 ——
不需要另学一套图形语言，也不需要在 Python 和前端之间来回切换。

「确定性」是怎么保证的
--------------------
不按真实时间播放动画，而是**手动拨时间**：
页面里的动画一律通过 Web Animations API 拿到实例，pause 住，
渲染第 f 帧时把每个动画的 currentTime 设成 f/30 秒。
于是同一份 HTML 每次渲染出的每一帧都一样 ⇒ 可以进 git、可以 diff、可以回归。

⚠️ 由此带来一条硬约束（写 HTML 的人必须遵守）：
**不许用 Date.now()、不许用没播种的 Math.random()、不许在渲染时发网络请求拿视觉状态。**
否则同样的输入会渲出不同的画面 —— 确定性就没了。
这三样都会被 `check()` 检出来。

产出
----
    <outdir>/<name>.mp4          1920×1080 / 30fps / H.264
    <outdir>/frames/f0000.png …  中间帧（可留可删）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

#: 渲染后端脚本（Node）。仓库内，随包走。
HTML_BACKEND = "htmlrender.js"

#: 默认参数
DEFAULT_W, DEFAULT_H = 1920, 1080
DEFAULT_FPS = 30
DEFAULT_DURATION = 5.0


def _node():
    """跑后端用哪个 node。优先 PATH 上的，其次本仓工具链旁的。"""
    for cand in (shutil.which("node"), r"D:\node.exe", r"C:\Program Files\nodejs\node.exe"):
        if cand and os.path.isfile(cand):
            return cand
    return ""


def _playwright_root():
    """Playwright 的 node_modules 在哪儿。

    本仓不把 node_modules 塞进 git（太大），所以在几个约定位置找：
    仓库内 tools/uicheck/node_modules（软链接）→ 工具链目录。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(os.path.dirname(here))
    cands = [
        os.path.join(repo, "tools", "uicheck", "node_modules"),
        r"D:\tools\uitools\node_modules",
        os.path.join(repo, "node_modules"),
    ]
    for d in cands:
        if os.path.isdir(os.path.join(d, "playwright")):
            return d
    return ""


def backend_dir():
    """渲染后端脚本在哪。找不到返回 ""。"""
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, HTML_BACKEND)
    return os.path.dirname(p) if os.path.isfile(p) else ""


def ffmpeg_path():
    """找到 ffmpeg。找不到返回 ""。

    优先环境变量，其次 PATH，最后扫 D:\\tools\\ffmpeg。
    （本机 2026-09-29 就装过 ffmpeg，别再傻傻下载一遍。）
    """
    p = os.environ.get("FLEET_FFMPEG", "").strip()
    if p and os.path.isfile(p):
        return p
    w = shutil.which("ffmpeg")
    if w:
        return w
    for d in (r"D:\tools\ffmpeg", r"C:\tools\ffmpeg"):
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.lower() == "ffmpeg.exe":
                    return os.path.join(root, fn)
    return ""


def check_env():
    """这套后端能不能跑？返回 (ok, 原因)。**缺什么要说清是什么。**"""
    node = _node()
    if not node:
        return False, ("没找到 node（跑 HTML 渲染后端要用它）。"
                       "装 Node 18+ 或设 PATH")
    pw = _playwright_root()
    if not pw:
        return False, ("没找到 Playwright 的 node_modules。"
                       "装法：cd tools/uicheck && npm i playwright，"
                       "再 npx playwright install chromium")
    # Chromium 内核在不在
    try:
        import glob
        home = os.path.expanduser("~")
        found = glob.glob(os.path.join(home, "AppData", "Local", "ms-playwright", "chromium*"))
        if not found:
            return False, ("装了 Playwright 但没有 Chromium 内核。"
                           "跑一次：npx playwright install chromium")
    except Exception:
        pass
    return True, ""


def render_html(html, outdir, name="reel", width=DEFAULT_W, height=DEFAULT_H,
                fps=DEFAULT_FPS, duration=DEFAULT_DURATION, keep_frames=False,
                timeout=1800):
    """把一段 HTML 渲染成 MP4。

    返回 dict：{ok, mp4, frames, seconds, ms, engine}
    失败时 ok=False 且 err 写明是哪一层出的问题。
    """
    ok, why = check_env()
    if not ok:
        return {"ok": False, "err": why}
    node = _node()
    pw = _playwright_root()
    bdir = backend_dir()
    if not bdir:
        return {"ok": False, "err": f"找不到渲染后端脚本目录（{HTML_BACKEND} 丢了？）"}

    outdir = os.path.abspath(outdir)
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, "_html")
    os.makedirs(tmp, exist_ok=True)
    html_path = os.path.join(tmp, "index.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    frames_dir = os.path.join(tmp, "frames")
    if os.path.isdir(frames_dir):
        shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir, exist_ok=True)

    env = dict(os.environ)
    env["NODE_PATH"] = pw + os.pathsep + env.get("NODE_PATH", "")
    env["FLEET_HTML_FRAMES"] = frames_dir
    env["FLEET_HTML_OUT"] = os.path.join(tmp, "out.mp4")
    ff = ffmpeg_path()
    if not ff:
        return {"ok": False,
                "err": ("没找到 ffmpeg（出片要它编码）。装 ffmpeg 后设 FLEET_FFMPEG "
                        "指到 ffmpeg.exe，或把它加进 PATH")}
    env["FLEET_FFMPEG"] = ff

    cmd = [node, os.path.join(bdir, HTML_BACKEND),
           "--html", html_path,
           "--width", str(width), "--height", str(height),
           "--fps", str(fps), "--duration", str(duration),
           "--frames", frames_dir, "--out", env["FLEET_HTML_OUT"]]
    try:
        p = subprocess.run(cmd, cwd=bdir, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"渲染超时（{timeout} 秒）"}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}

    report = None
    rpt_path = os.path.join(tmp, "report.json")
    if os.path.isfile(rpt_path):
        try:
            with open(rpt_path, encoding="utf-8") as fh:
                report = json.load(fh)
        except Exception:
            report = None

    if p.returncode != 0:
        return {"ok": False,
                "err": f"渲染后端退出码 {p.returncode}：{(p.stderr or p.stdout or '')[-600:]}"}

    mp4 = env["FLEET_HTML_OUT"]
    if not os.path.isfile(mp4):
        return {"ok": False, "err": f"渲染跑完了但没产出 MP4：{mp4}"}

    dst = os.path.join(outdir, name + ".mp4")
    try:
        if os.path.abspath(mp4) != os.path.abspath(dst):
            shutil.copy2(mp4, dst)
    except Exception as e:
        return {"ok": False, "err": f"片子生成了但搬不出来：{e}"}

    frames = 0
    if os.path.isdir(frames_dir):
        frames = len([f for f in os.listdir(frames_dir) if f.endswith(".png")])

    if not keep_frames:
        shutil.rmtree(frames_dir, ignore_errors=True)

    return {
        "ok": True,
        "mp4": dst,
        "bytes": os.path.getsize(dst),
        "frames": frames,
        "seconds": round(frames / float(fps or 1), 2),
        "width": width, "height": height, "fps": fps,
        "lint": (report or {}).get("lint"),
        "engine": "html",
    }


# ---------------------------------------------------------------- 自测

def _selftest():
    ok, why = check_env()
    print("html 后端环境：", "OK" if ok else "不可用（%s）" % why)
    assert isinstance(ok, bool)
    assert isinstance(why, str)
    print("fleet.kinds.render_html selftest ok")


if __name__ == "__main__":
    _selftest()
