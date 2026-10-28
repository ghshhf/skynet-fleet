"""渲染器：把「用代码画图/出片」接进设备编队。

这一层让本仓**拥有渲染图片的能力**——不是「能截图」（那是看屏幕），
而是「能凭代码画出新的图」：排版、网格、3D 点云、印刷分色、合成配乐。

为什么做成一个 kind（设备）而不是一个工具函数：
  本仓铁律是「能力唯一真相在注册表」。渲染能力要能被
  终端 / T 版 / 图形版 / MCP 四个面同时看见、要能过闸门、要能记调用流水，
  就必须走 `BaseDevice`（probe / caps / invoke）这一条路。
  它跟 `ai` 那一层是同构的：**本机计算型设备**，via="local"。

为什么不把引擎代码 vendoring 进本仓：
  引擎在 `~/.workbuddy/skills/ruic-motion-reel`（MIT 协议 + OFL 字体）。
  vendoring 进来会有两个坏处：① 仓库体积暴涨（引擎 + 字体 + 参考片）；
  ② 引擎升级要靠人肉同步两份。
  所以本仓只做**适配层**：找到引擎、起子进程、收结果、给失败原因。
  引擎不在 ⇒ probe 说清楚「没装」；引擎在但 ffmpeg 缺 ⇒ 也说清楚缺什么。

能力三项：
  render.styles  只读：列出 16 张风格牌（抽签用）
  render.frame   只读+轻副作用：渲染某一拍的单帧 PNG
  render.reel    副作用重：渲染整条 15 秒片子（写文件、走闸门）

🔴 失败必须带原因：引擎路径不对 / ffmpeg 没装 / 引擎抛异常，
都要说清是哪一层出的问题，不许返回一张空图。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from ..core import BaseDevice, DeviceResult as R
from .. import locate as L
from . import render_html

#: 引擎位置。优先环境变量，其次用户级技能目录。
ENGINE_ENV = "FLEET_RENDER_ENGINE"
DEFAULT_ENGINE_DIRS = [
    os.path.join(os.path.expanduser("~"), ".workbuddy", "skills", "ruic-motion-reel"),
]

#: ffmpeg 可执行文件。优先环境变量，其次 PATH。
FFMPEG_ENV = "FLEET_FFMPEG"

#: 单帧渲染的墙钟上限（秒）。超了就如实报超时，不许无限等。
FRAME_TIMEOUT = 180
REEL_TIMEOUT = 3600


def engine_dir():
    """找到引擎目录。找不到返回 ""。"""
    p = os.environ.get(ENGINE_ENV, "").strip()
    if p and os.path.isdir(p):
        return p
    for d in DEFAULT_ENGINE_DIRS:
        if os.path.isdir(d):
            return d
    return ""


def ffmpeg_path():
    """找到 ffmpeg。找不到返回 ""。

    2026-10-29：原来只在 `D:\\tools\\ffmpeg` 和 `C:\\tools\\ffmpeg` 两个位置
    递归 walk——两个盘符都是那台机器的布局，换盘就找不到，而且 walk 还慢。
    现在「相对结构」交给 locate，盘符由它运行时枚举（见 fleet/locate.py）。
    """
    return L.exe("ffmpeg", env=FFMPEG_ENV,
                 hints=("tools/ffmpeg/bin", "dev/ffmpeg/bin",
                        "_tools/ffmpeg/bin", "ffmpeg/bin",
                        "tools/ffmpeg", "_tools/ffmpeg"),
                 # 本机实测真身是 tools/ffmpeg/ffmpeg-master-latest-win64-gpl/bin/ffmpeg.exe
                 # ——中间那层是版本号目录，写进 hints 就等于又硬绑一次。用 under 递归兜。
                 under=("tools/ffmpeg", "dev/ffmpeg", "_tools/ffmpeg", "ffmpeg"))


def _py():
    """跑引擎用哪个 python：FLEET_PY → 本机便携版 → 当前解释器。

    为什么便携版必须比 PATH 上的 python 优先：引擎的 build.py 要 numpy/PIL。
    本机实测：`tools/python` 那支有 numpy 2.5.4，系统 Python312 没有——
    顺序反了就是 `render.frame 真渲染失败（退出码 1）` + 一串 ImportError。
    2026-10-29 改软绑定时我图省事直接调了 locate（它的 PATH 是第 4 步、
    hints 是第 5 步），PATH 上的 python 抢了先，门禁 eval_t14 当场抓到。
    """
    cand = (os.environ.get("FLEET_PY") or "").strip()
    if cand and os.path.isfile(cand):
        return cand
    # 只认同一个相对结构：本仓工具链的 tools/python。盘符交给 roots()。
    for name in ("python.exe", "python"):
        for root in L.roots():
            p = os.path.join(root, "tools", "python", name)
            if os.path.isfile(p):
                return p
    return sys.executable


class Render(BaseDevice):
    kind = "render"
    via = "local"

    def __init__(self, name="render:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("note", "用代码渲染图与动效片，不控制任何外部设备")

    # ---------------- 契约三方法 ----------------

    def probe(self):
        d = engine_dir()
        if not d:
            return False, ("没装渲染引擎。"
                           "装法：把 RuiC-motion-reel 克隆到 "
                           f"{DEFAULT_ENGINE_DIRS[0]}，"
                           f"或设 {ENGINE_ENV} 指到它")
        if not os.path.isfile(os.path.join(d, "SKILL.md")):
            return False, f"{d} 不像是渲染引擎目录（没有 SKILL.md）"
        return True, ""

    def caps(self):
        return ("render.styles", "render.frame", "render.reel", "render.html")

    def invoke(self, cap, args=None):
        args = args or {}
        if cap == "render.styles":
            return self._styles(args)
        if cap == "render.frame":
            return self._frame(args)
        if cap == "render.reel":
            return self._reel(args)
        if cap == "render.html":
            return self._html(args)
        return super().invoke(cap, args)

    # ---------------- 只读：风格牌 ----------------

    def _styles(self, args):
        d = engine_dir()
        if not d:
            return R.unavailable("render.styles", self._why_no_engine(),
                                 device=self.name, via=self.via)
        script = os.path.join(d, "scripts", "style_lottery.py")
        if not os.path.isfile(script):
            return R.unavailable("render.styles",
                                 f"引擎里没有 style_lottery.py：{script}",
                                 device=self.name, via=self.via)
        cmd = [_py(), script, "--list"]
        r = self._run(cmd, cwd=d, timeout=60)
        if r["code"] != 0:
            return R.error("render.styles",
                           f"列风格牌失败（退出码 {r['code']}）：{r['err'][-400:]}",
                           device=self.name, via=self.via)
        return R.success("render.styles", self.name, via=self.via,
                         data={"listing": r["out"], "engine": d})

    # ---------------- 单帧 ----------------

    def _frame(self, args):
        """渲染某一拍的单帧。

        流程：new_reel.py 起工程 → build.py --at <scene>:<beat> 出单帧。
        出来的 PNG 路径回给调用方。
        """
        d = engine_dir()
        if not d:
            return R.unavailable("render.frame", self._why_no_engine(),
                                 device=self.name, via=self.via)
        at = str(args.get("at") or "1:1.20").strip()
        style = str(args.get("style") or "").strip()
        workdir = str(args.get("workdir") or "").strip()
        tmp = None
        try:
            if workdir:
                proj = os.path.abspath(workdir)
            else:
                tmp = tempfile.mkdtemp(prefix="fleet-reel-")
                proj = tmp
            name = os.path.basename(proj) or "reel"
            pkg = "".join(ch if ch.isalnum() else "_" for ch in name).strip("_").lower()
            if not pkg or pkg[0].isdigit():
                pkg = "reel_" + pkg
            script = os.path.join(d, "scripts", "new_reel.py")
            if not os.path.isfile(script):
                return R.unavailable("render.frame",
                                     f"引擎里没有 new_reel.py：{script}",
                                     device=self.name, via=self.via)
            style_arg = style if style else "random"
            r = self._run([_py(), script, proj, "--name", pkg, "--style", style_arg,
                           "--force"], cwd=d, timeout=120)
            if r["code"] != 0:
                return R.error("render.frame",
                               f"起工程失败（退出码 {r['code']}）：{r['err'][-400:]}",
                               device=self.name, via=self.via)
            # build 必须在工程目录里跑（它是 python -m <pkg>.build）
            r2 = self._run([_py(), "-m", f"{pkg}.build", "--at", at],
                           cwd=proj, timeout=int(args.get("timeout") or FRAME_TIMEOUT))
            pngs = self._find_png(os.path.join(proj, "out"))
            if r2["code"] != 0 and not pngs:
                return R.error("render.frame",
                               f"渲染单帧失败（退出码 {r2['code']}）：{r2['err'][-400:]}",
                               device=self.name, via=self.via)
            if not pngs:
                return R.error("render.frame",
                               f"渲染单帧跑完了但没找到 PNG（at={at}）",
                               device=self.name, via=self.via)
            path = max(pngs, key=os.path.getmtime)
            return R.success("render.frame", self.name, via=self.via, data={
                "path": path, "at": at, "style": style or "(引擎默认)",
                "project": proj, "stdout": r2["out"][-800:],
            })
        except Exception as e:
            return R.error("render.frame", f"{type(e).__name__}: {e}",
                           device=self.name, via=self.via)
        finally:
            if tmp and not args.get("keep"):
                shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 整条片子 ----------------

    def _reel(self, args):
        """渲染整条 15 秒片子。

        这项**写文件**（MP4 落盘）⇒ side_effect=True ⇒ 注册表会挂闸门等主人点头。
        需要 ffmpeg——没有就明说缺什么，不许假装。
        """
        d = engine_dir()
        if not d:
            return R.unavailable("render.reel", self._why_no_engine(),
                                 device=self.name, via=self.via)
        ff = ffmpeg_path()
        if not ff:
            return R.unavailable("render.reel",
                                 "没找到 ffmpeg（出片要它编码）。"
                                 f"装 ffmpeg 后设 {FFMPEG_ENV} 指到 ffmpeg.exe，"
                                 "或把它加进 PATH",
                                 device=self.name, via=self.via)
        outdir = str(args.get("outdir") or "").strip()
        if not outdir:
            return R.error("render.reel", "缺少 outdir（片子落在哪）",
                           device=self.name, via=self.via)
        outdir = os.path.abspath(outdir)
        os.makedirs(outdir, exist_ok=True)
        style = str(args.get("style") or "").strip()
        pkg = "fleet_reel"
        script = os.path.join(d, "scripts", "new_reel.py")
        proj = os.path.join(outdir, "_engine")
        if os.path.isdir(proj):
            shutil.rmtree(proj, ignore_errors=True)
        r = self._run([_py(), script, proj, "--name", pkg,
                       "--style", style if style else "random", "--force"],
                      cwd=d, timeout=120)
        if r["code"] != 0:
            return R.error("render.reel",
                           f"起工程失败（退出码 {r['code']}）：{r['err'][-400:]}",
                           device=self.name, via=self.via)
        env = dict(os.environ)
        env["PATH"] = os.path.dirname(ff) + os.pathsep + env.get("PATH", "")
        env["FFMPEG"] = ff
        r2 = self._run([_py(), "-m", f"{pkg}.build"], cwd=proj,
                       timeout=int(args.get("timeout") or REEL_TIMEOUT), env=env)
        mp4s = [f for f in self._all_files(os.path.join(proj, "out"))
                if f.lower().endswith(".mp4")]
        if not mp4s:
            return R.error("render.reel",
                           f"出片跑完了但没找到 MP4（退出码 {r2['code']}）："
                           f"{r2['err'][-400:]}",
                           device=self.name, via=self.via)
        src = max(mp4s, key=os.path.getmtime)
        dst = os.path.join(outdir, os.path.basename(src))
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                shutil.copy2(src, dst)
        except Exception as e:
            return R.error("render.reel", f"片子生成了但搬不出来：{e}",
                           device=self.name, via=self.via)
        return R.success("render.reel", self.name, side_effect=True, via=self.via, data={
            "path": dst, "bytes": os.path.getsize(dst),
            "style": style or "(引擎默认)", "project": proj,
        })

    # ---------------- HTML 后端（Video as Code） ----------------

    def _html(self, args):
        """把 HTML 渲染成 MP4（写 HTML，代码出片）。

        这条路是「Video as Code」品类的内核：可寻址动画 → 确定性逐帧 → MP4。
        2026-10-21 本机实测：150 帧 / 22.6 秒 = 6.6 帧/秒，
        15 秒片子约 1.1 分钟（比 Python 像素引擎的 47 分钟快约 40 倍），
        而且不占 GPU、不用装新东西 —— Chromium 和 ffmpeg 本机都有。

        为什么要挂在渲染器下面而不是另起一个设备：
        「能画图」和「能出片」是同一件事的两种时长，
        拆成两个设备会让注册表多一个含义重叠的条目。

        三个入口（互斥，取第一个有的）：
          template="demo"  用内置模板（不想自己写 HTML 时）
          src="<路径>"      用本地 .html 文件
          html="<源码>"     直接给源码
        """
        template = str(args.get("template") or "").strip()
        html = str(args.get("html") or "").strip()
        src = str(args.get("src") or "").strip()
        if template:
            tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "templates", template + ".html")
            if not os.path.isfile(tpl):
                avail = []
                tdir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "templates")
                if os.path.isdir(tdir):
                    avail = sorted(f[:-5] for f in os.listdir(tdir)
                                   if f.endswith(".html"))
                return R.error("render.html",
                               f"没有这个模板：{template}。"
                               f"现成的有：{', '.join(avail) or '(一个都没有)'}",
                               device=self.name, via=self.via)
            with open(tpl, encoding="utf-8") as fh:
                html = fh.read()
        elif src:
            p = os.path.abspath(src)
            if not os.path.isfile(p):
                return R.error("render.html", f"找不到这个 HTML 文件：{p}",
                               device=self.name, via=self.via)
            try:
                with open(p, encoding="utf-8") as fh:
                    html = fh.read()
            except Exception as e:
                return R.error("render.html", f"读不了 {p}：{e}",
                               device=self.name, via=self.via)
        if not html:
            return R.error("render.html",
                           "要给内容才能渲：template=<模板名> / src=<本地 .html 路径> "
                           "/ html=<一段 HTML 源码>（三选一）",
                           device=self.name, via=self.via)
        outdir = str(args.get("outdir") or "").strip()
        if not outdir:
            return R.error("render.html", "缺少 outdir（片子落在哪）",
                           device=self.name, via=self.via)
        name = str(args.get("name") or "reel").strip()
        name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name) or "reel"
        r = render_html.render_html(
            html, outdir, name=name,
            width=int(args.get("width") or render_html.DEFAULT_W),
            height=int(args.get("height") or render_html.DEFAULT_H),
            fps=int(args.get("fps") or render_html.DEFAULT_FPS),
            duration=float(args.get("duration") or render_html.DEFAULT_DURATION),
            keep_frames=bool(args.get("keep_frames")),
            timeout=int(args.get("timeout") or 1800),
        )
        if not r.get("ok"):
            return R.error("render.html", r.get("err") or "渲染失败（没给原因）",
                           device=self.name, via=self.via)
        return R.success("render.html", self.name, side_effect=True, via=self.via, data=r)

    # ---------------- 工具 ----------------

    def _why_no_engine(self):
        return ("没装渲染引擎。装法：把 RuiC-motion-reel 克隆到 "
                f"{DEFAULT_ENGINE_DIRS[0]}，或设 {ENGINE_ENV} 指到它")

    def _run(self, cmd, cwd, timeout, env=None):
        """跑一个子进程，带硬超时。返回 {code, out, err}。

        超时必须杀掉进程树——否则渲染的子进程会变成孤儿继续吃 CPU。
        """
        try:
            p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env,
                                 text=True, encoding="utf-8", errors="replace")
        except Exception as e:
            return {"code": -1, "out": "", "err": f"{type(e).__name__}: {e}"}
        try:
            out, err = p.communicate(timeout=timeout)
            return {"code": p.returncode, "out": out or "", "err": err or ""}
        except subprocess.TimeoutExpired:
            self._kill_tree(p)
            return {"code": -9, "out": "", "err": f"超时（{timeout} 秒）"}

    def _kill_tree(self, p):
        try:
            p.kill()
        except Exception:
            pass
        # Windows 上还要杀子孙（渲染是并行的）
        try:
            import subprocess as sp
            if os.name == "nt":
                sp.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                       capture_output=True, timeout=10)
        except Exception:
            pass

    def _find_png(self, root):
        out = []
        for dirpath, _dirs, files in os.walk(root) if os.path.isdir(root) else []:
            for fn in files:
                if fn.lower().endswith(".png"):
                    out.append(os.path.join(dirpath, fn))
        return out

    def _all_files(self, root):
        out = []
        for dirpath, _dirs, files in os.walk(root) if os.path.isdir(root) else []:
            for fn in files:
                out.append(os.path.join(dirpath, fn))
        return out


# ---------------------------------------------------------------- 自测

def _selftest():
    r = Render()
    caps = r.caps()
    assert "render.styles" in caps
    assert "render.frame" in caps
    assert "render.reel" in caps

    # 引擎找不到时，probe 要说清楚为什么
    ok, reason = r.probe()
    d = engine_dir()
    if not d:
        assert not ok
        assert "渲染引擎" in reason
    else:
        assert ok, reason

    # 找不到引擎时调能力 ⇒ unavailable 且带原因，不许崩
    if not d:
        res = r.invoke("render.styles", {})
        assert not res.ok
        assert res.err
        res2 = r.invoke("render.frame", {})
        assert not res2.ok
        assert res2.err

    # ffmpeg 查找不许崩
    assert isinstance(ffmpeg_path(), str)

    print("fleet.kinds.render selftest ok（引擎 %s）" % (d or "未安装"))


if __name__ == "__main__":
    _selftest()
