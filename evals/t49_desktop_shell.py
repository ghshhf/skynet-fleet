"""t49 —— 桌面端能不能当成一个"装上去就会用"的软件。

为什么有这条（2026-10-29）
========================
这一轮之前，这个程序的形态是"一个窗口"：双击开、关掉没。它其实是个**常驻服务**——
接手机心跳、等审批、看住被控机，全都在窗口关掉之后才真正有用。于是有四个硬缺口：

  ① 双击第二次会起第二份服务（抢端口、各自认为自己在管编队、审批弹窗跑到看不见的窗口）
  ② 关掉窗口 = 把她关了（想常驻就只能一直占着屏幕）
  ③ 开机不自启（每天先找一下再双击）
  ④ 窄窗口是**禁止**而不是适配（最小宽度 1080 > 网页断点 900，那条断点从未生效过）

这条门禁守的是"这四件事有没有真做到"，以及两个容易悄悄退化的协同点：

  A. **requirements 里必须有 pystray。** 没写进去 → 冻扁包里 import 失败 →
     托盘静默降级成"关窗即退出"。这和 2026-10-22 的 edge_tts 是同一个形状：
     代码在、能力没了、不报错。只有"依赖清单 vs 代码里的 import"能抓。
  B. **WIN_MIN 必须 ≤ 网页断点宽度。** 两个数字写在不同文件里，谁也不会提醒谁；
     窗口最小宽度一旦大于断点，断点就是死代码。这和 t47 的"域覆盖 == 工具表"同类。

不碰注册表、不开窗口、不起服务：
自启的三态判定用**注入的注册表读数**来测（不真写 HKCU），
单实例用进程内互斥 + 本机回环暗号，跑完锁全放。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from fleet.gui import autostart as A                 # noqa: E402
from fleet.gui import single as S                    # noqa: E402

OK = 0
FAIL = 0


def check(cond, msg):
    global OK, FAIL
    if cond:
        OK += 1
        print("  ok   %s" % msg)
    else:
        FAIL += 1
        print("  FAIL %s" % msg)


def _read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- A 依赖声明

def part_requirements():
    print("\n[A] 依赖清单：托盘能不能进包")
    req = _read("requirements.txt")
    # 只认未被注释掉的行
    live = [ln.split("#")[0].strip() for ln in req.splitlines()]
    live = [ln for ln in live if ln]
    names = [re.split(r"[<>=!~\[]", ln, 1)[0].strip().lower() for ln in live]
    check("pywebview" in names, "requirements 里有 pywebview（窗口壳）")
    check("pystray" in names, "requirements 里有 pystray（托盘）——没有它，关窗就是退出")
    # 托盘模块确实 import 了 pystray（防止"清单里有、代码里没人用"的反向漂移）
    tray_src = _read("fleet/gui/tray.py")
    check("import pystray" in tray_src, "tray.py 真的用了 pystray")


# ---------------------------------------------------------------- B 宽度协同

def part_width():
    print("\n[B] 窄窗口：最小宽度 vs 网页断点")
    app = _read("fleet/gui/app.py")
    css = _read("ui/app.css")
    m = re.search(r"WIN_MIN\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", app)
    check(m is not None, "app.py 里能解析出 WIN_MIN")
    if not m:
        return
    w, h = int(m.group(1)), int(m.group(2))
    breaks = [int(x) for x in re.findall(r"max-width\s*:\s*(\d+)px", css)]
    check(bool(breaks), "app.css 里有 max-width 断点")
    if not breaks:
        return
    widest = max(breaks)
    check(w <= widest,
          "WIN_MIN 宽 %d <= 最大断点 %d（否则那条断点永远进不去）" % (w, widest))
    check(h <= 700, "WIN_MIN 高 %d <= 700（1366×768 的笔记本要放得下）" % h)


# ---------------------------------------------------------------- C 单实例

def part_single():
    print("\n[C] 单实例：第二个进不来，且能叫醒第一个")
    key = "t49-selftest-%d" % os.getpid()
    a = S.SingleInstance(key=key)
    try:
        got = a.acquire()
        check(got is True, "第一个实例拿到锁")
        if not got:
            return
        hits = []
        listening = a.listen(lambda: hits.append(1))
        check(listening, "唤起通道起来了")

        b = S.SingleInstance(key=key)
        check(b.acquire() is False, "第二个实例被拦下（不会再起一份服务）")
        if listening:
            check(b.poke(timeout=1.2, tries=3) is True, "第二个能把第一个叫出来（收到 ACK）")
            import time
            time.sleep(0.2)
            check(len(hits) == 1, "唤起回调真的被触发了一次（实际 %d 次）" % len(hits))
        else:
            check(True, "（唤起通道没起来，跳过唤起测试）")
    finally:
        a.release()

    # 释放之后必须真的能再拿到（防止"锁一直挂着"）
    c = S.SingleInstance(key=key)
    try:
        check(c.acquire() is True, "上一个释放后，新的能拿到锁（锁没卡死）")
    finally:
        c.release()

    # 没人听时 poke 必须 False
    lonely = S.SingleInstance(key="t49-nobody-%d" % os.getpid())
    check(lonely.poke(timeout=0.6, tries=1) is False, "没人听时 poke 返回 False（不装成功）")


# ---------------------------------------------------------------- D 自启三态

def part_autostart():
    print("\n[D] 开机自启：off / on / stale 三态")
    orig = A.current
    try:
        A.current = lambda: ""
        check(A.state() == "off", "读不到 = off")
        A.current = lambda: A.command()
        check(A.state() == "on", "写的正好是当前这份 = on")
        A.current = lambda: '"C:\\old\\python.exe" "C:\\old\\fleet_gui.py" --autostart'
        check(A.state() == "stale", "指向别的副本 = stale（不是简单的已开启）")
        A.current = lambda: ""
        check(A.is_enabled() is False, "没有这一行 = 未开")
    finally:
        A.current = orig

    cmd = A.command()
    check("--autostart" in cmd, "自启命令行带 --autostart（开机不弹窗）")
    check(cmd.count('"') >= 2, "路径带引号（装在带空格的目录也不会散架）")
    check(os.path.normcase(cmd).count(os.path.normcase(ROOT)) >= 1,
          "自启指向当前这份安装，不是别处")


# ---------------------------------------------------------------- E 关窗策略

def part_close_policy():
    print("\n[E] 关窗口 = 藏起来，不是退出")
    app = _read("fleet/gui/app.py")
    check("window.events.closing +=" in app, "接上了 closing 事件")
    check("window.hide()" in app, "关窗时调 hide()")
    check("--quit-on-close" in app, "留了 --quit-on-close（要真退出的人有路）")
    check("flags[\"quit\"]" in app or "flags['quit']" in app, "退出走同一个闸（不会关不干净）")

    # 没有托盘就必须**不隐藏**：否则是一个看不见也点不到的进程。
    m = re.search(r"hidden_start\s*=\s*([^\n]+)", app)
    check(m is not None, "hidden_start 是一处显式判定")
    if m:
        check("tray_available" in m.group(1),
              "静默启动的前提是托盘可用（没托盘就不隐藏，避免无头进程）")

    # 先占坑再起服务：顺序反了就挡不住抢端口
    i_inst = app.find("inst.acquire()")
    i_srv = app.find("FleetServer(port=port")
    check(i_inst > 0 and i_srv > 0 and i_inst < i_srv,
          "单实例锁在起服务之前拿（顺序反了就是白拿）")

    # 唤起通道失败要说出来，不能静默
    check("唤起通道没起来" in app, "唤起通道起不来会如实报出来")


def main():
    print("=" * 52)
    print("t49_desktop_shell —— 桌面端成品化")
    print("=" * 52)
    part_requirements()
    part_width()
    part_single()
    part_autostart()
    part_close_policy()
    print()
    print("=" * 52)
    print("t49_desktop_shell: %d ok / %d fail" % (OK, FAIL))
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
