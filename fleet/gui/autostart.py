"""开机自启：开机之后她在，不用每天先找一下再双击。

## 只写 HKCU，不写 HKLM

`HKEY_CURRENT_USER\\...\\Run` 三个好处：不需要管理员权限、只对当前用户生效、
卸载时删掉自己那一行就干净了。写 HKLM 等于"这台机器谁登录都跑"，
一个会看桌面的程序不该有这种默认。

## 一个容易被忽略的真坑：残留指向旧副本

这个程序会被移动、会被重新打包、会被换个目录再解压一份。注册表那一行是绝对路径，
**改了目录之后它还在，只是指向一个已经不在的地方**——开机时静默什么也没发生。
所以 `state()` 分三种：`off` / `on` / `stale`（有这一行，但指向的不是我现在这份）。
`stale` 在界面上要能显示成"自启指向别处"，而不是简单地显示成"已开启"。

## 非 Windows

返回 `False` 并说清原因，不假装成功。Linux 的 autostart 是 `.desktop` 文件，
形态完全不同，等真正要在 Linux 上发版时再做——现在写一个没验证过的版本，
就是在给以后留一个"看起来支持"的坑。
"""
import os
import sys

APP_NAME = "TianwangFleet"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 源码运行时的薄壳入口（和 PyInstaller 用同一个，见 fleet_gui.py）
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENTRY = os.path.join(_ROOT, "fleet_gui.py")


def supported():
    return sys.platform == "win32"


def command():
    """开机时要执行的那一行。冻结包和源码两种形态各自说对。"""
    hidden = "--autostart"
    if getattr(sys, "frozen", False):
        return '"%s" %s' % (sys.executable, hidden)
    exe = sys.executable
    # pythonw.exe 不带黑框。找不到就退回 python.exe（会闪一下控制台，但能用）。
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(cand):
        exe = cand
    return '"%s" "%s" %s' % (exe, _ENTRY, hidden)


def _norm(cmd):
    """比较用：忽略大小写、忽略首尾空白、路径斜杠统一。"""
    return os.path.normcase((cmd or "").strip().replace("/", "\\"))


def current():
    """注册表里现在写的是什么。没有则返回空串。"""
    if not supported():
        return ""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _kind = winreg.QueryValueEx(k, APP_NAME)
            return val or ""
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def is_enabled():
    """有这一行就算开——不管它指向哪里。想知道指向对不对，用 state()。"""
    return bool(current())


def state():
    """off / on / stale（有这一行，但指向的不是当前这份安装）。"""
    cur = current()
    if not cur:
        return "off"
    if _norm(cur) == _norm(command()):
        return "on"
    return "stale"


def enable():
    """打开开机自启。返回 (ok, 说明)。"""
    if not supported():
        return False, "开机自启目前只做了 Windows（当前平台 %s）" % sys.platform
    import winreg
    cmd = command()
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            # 用 REG_SZ 而不是 REG_EXPAND_SZ：这条命令里的路径全都展开了，
            # 没有 %VAR% 需要再展开。REG_SZ 少一层依赖。
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        return False, "写注册表失败：%s" % e
    back = current()
    if _norm(back) != _norm(cmd):
        return False, "写进去了但读回来不一样（%s），可能是权限问题" % (back or "空")
    return True, "开机自启已开：%s" % cmd


def disable():
    """关掉开机自启。没开过也算成功（幂等）。"""
    if not supported():
        return False, "开机自启目前只做了 Windows（当前平台 %s）" % sys.platform
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        return True, "本来就没开"
    except OSError as e:
        return False, "删注册表项失败：%s" % e
    return (not is_enabled()), "开机自启已关"
