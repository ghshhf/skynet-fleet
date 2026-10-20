"""本机状态快照：天网跑在 Windows 上，却一直看不见这台机器本身。

为什么必须补：到 2026-10-20，感知层能看手机、看局域网、看蓝牙，唯独看不见
**宿主自己**——主人此刻在电脑前干什么、离开了多久、机器累不累、盘满没满。
「她」要是连「你现在在不在」都不知道，主动开口就只能靠猜。

三条硬规矩：
  · 零依赖：只用 ctypes 调 Win32，不引入 psutil/pywin32（这个仓库一直是纯 stdlib）。
  · 只读：没有一次 Set/Write；读剪贴板也只读「有没有、多长」，内容要显式要才给。
  · 分节失败各自降级：哪一节读不出来就进 missing，其余照常（与 phone_state 同构）。

    python pc_state.py            # 人读报告
    python pc_state.py --json
    python pc_state.py --brief    # 一句话
    python pc_state.py --clip     # 连剪贴板内容一起给（默认不给内容）
    python pc_state.py --selftest
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import sys
import time

IS_WIN = os.name == "nt"

# ---------------------------------------------------------------- Win32 声明
# 每一个函数都显式声明 argtypes/restype。不声明的话 ctypes 默认按 32 位 int 处理
# 返回值，HANDLE/指针会被截断——剪贴板那次 `access violation reading 0xFFFFFFFF...`
# 就是这么来的：看着像读越界，其实是高位被砍掉。

_W = ctypes.WinDLL("user32", use_last_error=True) if IS_WIN else None
_K = ctypes.WinDLL("kernel32", use_last_error=True) if IS_WIN else None
# GetUserNameW 不在 user32 也不在 kernel32，它在 advapi32 —— 放错 DLL 会报
# "function 'GetUserNameW' not found"，这种错只在真机跑才暴露，所以写在注释里
_A = ctypes.WinDLL("advapi32", use_last_error=True) if IS_WIN else None


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

    def value(self):
        return (self.dwHighDateTime << 32) | self.dwLowDateTime


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", wt.BYTE), ("BatteryFlag", wt.BYTE),
                ("BatteryLifePercent", wt.BYTE), ("SystemStatusFlag", wt.BYTE),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


# 回调类型必须在声明 argtypes 之前定义好：拿 c_void_p 去接函数指针，
# ctypes 会在调用时报类型不符，而不是安静地传进去
_MONITORENUMPROC = (ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC,
                                       ctypes.c_void_p, wt.LPARAM) if IS_WIN else None)


def _declare():
    """一次性把用到的签名钉死。读代码时就能看出每个调用的真实类型。"""
    _W.GetForegroundWindow.restype = wt.HWND
    _W.GetWindowTextLengthW.argtypes = [wt.HWND]
    _W.GetWindowTextLengthW.restype = ctypes.c_int
    _W.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _W.GetWindowTextW.restype = ctypes.c_int
    _W.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _W.GetWindowThreadProcessId.restype = wt.DWORD
    _W.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _W.GetLastInputInfo.restype = wt.BOOL
    _W.GetSystemMetrics.argtypes = [ctypes.c_int]
    _W.GetSystemMetrics.restype = ctypes.c_int
    _W.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _W.OpenInputDesktop.restype = wt.HANDLE
    _W.CloseDesktop.argtypes = [wt.HANDLE]
    _W.OpenClipboard.argtypes = [wt.HWND]
    _W.OpenClipboard.restype = wt.BOOL
    _W.GetClipboardData.argtypes = [wt.UINT]
    _W.GetClipboardData.restype = wt.HANDLE
    _W.CloseClipboard.restype = wt.BOOL
    _A.GetUserNameW.argtypes = [wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _A.GetUserNameW.restype = wt.BOOL
    _W.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p, _MONITORENUMPROC, wt.LPARAM]
    _W.EnumDisplayMonitors.restype = wt.BOOL

    _K.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _K.OpenProcess.restype = wt.HANDLE
    _K.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                              ctypes.POINTER(wt.DWORD)]
    _K.QueryFullProcessImageNameW.restype = wt.BOOL
    _K.CloseHandle.argtypes = [wt.HANDLE]
    _K.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    _K.GlobalMemoryStatusEx.restype = wt.BOOL
    _K.GetSystemTimes.argtypes = [ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                                  ctypes.POINTER(_FILETIME)]
    _K.GetSystemTimes.restype = wt.BOOL
    _K.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SYSTEM_POWER_STATUS)]
    _K.GetSystemPowerStatus.restype = wt.BOOL
    _K.GlobalLock.argtypes = [wt.HANDLE]
    _K.GlobalLock.restype = ctypes.c_void_p          # 不声明就会被截成 32 位
    _K.GlobalUnlock.argtypes = [wt.HANDLE]
    _K.GetTickCount64.restype = ctypes.c_ulonglong


if IS_WIN:
    _declare()


# ---------------------------------------------------------------- 分节
# 每节独立 try：缺一节不该让整份快照消失（与 phone_state 同构）。

def _foreground():
    hwnd = _W.GetForegroundWindow()
    if not hwnd:
        raise ValueError("没有前台窗口（可能是锁屏或全屏切换中）")
    n = _W.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _W.GetWindowTextW(hwnd, buf, n + 1)
    pid = wt.DWORD()
    _W.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = _K.OpenProcess(0x1000, False, pid.value)          # QUERY_LIMITED_INFORMATION
    if h:
        try:
            pb = ctypes.create_unicode_buffer(1024)
            sz = wt.DWORD(1024)
            if _K.QueryFullProcessImageNameW(h, 0, pb, ctypes.byref(sz)):
                exe = os.path.basename(pb.value)
        finally:
            _K.CloseHandle(h)
    return {"title": buf.value or "", "exe": exe, "pid": pid.value}


def _input():
    li = _LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _W.GetLastInputInfo(ctypes.byref(li)):
        raise ValueError("GetLastInputInfo 失败")
    # GetTickCount 是 32 位、49 天回绕，用 64 位那个
    idle_ms = max(int(_K.GetTickCount64() & 0xFFFFFFFF) - li.dwTime, 0)
    # 用 64 位 tick 的低 32 位与 dwTime 相减：dwTime 本身就是 32 位的，
    # 直接拿 64 位相减会在开机超过 49 天时多出一个巨大数
    return {"idle_sec": idle_ms // 1000, "idle_human": human_dur(idle_ms // 1000)}


def _session():
    d = _W.OpenInputDesktop(0, False, 0x0100)     # DESKTOP_READOBJECTS
    if d:
        _W.CloseDesktop(d)
        locked = False
    else:
        locked = True                              # 打不开输入桌面 = 被锁屏占着
    buf = ctypes.create_unicode_buffer(256)
    sz = wt.DWORD(256)
    user = buf.value if _A.GetUserNameW(buf, ctypes.byref(sz)) else ""
    return {"locked": locked, "user": user,
            "uptime_human": human_dur(_K.GetTickCount64() // 1000)}


def _cpu():
    def snap():
        i, k, u = _FILETIME(), _FILETIME(), _FILETIME()
        if not _K.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
            raise ValueError("GetSystemTimes 失败")
        return i.value(), k.value(), u.value()

    i0, k0, u0 = snap()
    time.sleep(0.2)
    i1, k1, u1 = snap()
    di, dk, du = i1 - i0, k1 - k0, u1 - u0
    total = dk + du                      # kernel 时间里含 idle，所以 total = kernel + user
    if total <= 0:
        raise ValueError("采样窗口太短，算不出占用")
    return {"busy_pct": round(100 * (1 - di / total), 1), "cores": os.cpu_count() or 0}


def _mem():
    m = _MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not _K.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise ValueError("GlobalMemoryStatusEx 失败")
    return {"total_mb": m.ullTotalPhys // 2 ** 20,
            "avail_mb": m.ullAvailPhys // 2 ** 20,
            "load_pct": m.dwMemoryLoad}


def _disk():
    rows = []
    for letter in "CDEFG":
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            u = shutil.disk_usage(root)
        except OSError as e:
            rows.append({"drive": letter, "err": str(e)[:60]})
            continue
        rows.append({"drive": letter,
                     "total_gb": round(u.total / 2 ** 30, 1),
                     "used_gb": round(u.used / 2 ** 30, 1),
                     "free_gb": round(u.free / 2 ** 30, 1),
                     "used_pct": round(100 * u.used / u.total, 1)})
    if not rows:
        raise ValueError("一个盘都没读到")
    return rows


def _power():
    s = _SYSTEM_POWER_STATUS()
    if not _K.GetSystemPowerStatus(ctypes.byref(s)):
        raise ValueError("GetSystemPowerStatus 失败")
    # BatteryLifePercent=255 表示「不知道」——台式机没电池就是这个值，
    # 不能当成 255% 电量往上报
    pct = None if s.BatteryLifePercent == 255 else s.BatteryLifePercent
    return {"ac": s.ACLineStatus == 1, "battery_pct": pct,
            "has_battery": pct is not None}


def _display():
    w, h = _W.GetSystemMetrics(0), _W.GetSystemMetrics(1)
    n = [0]

    def _cb(hmon, hdc, rect, data):
        n[0] += 1
        return True

    CB = _MONITORENUMPROC
    _W.EnumDisplayMonitors(None, None, CB(_cb), 0)
    return {"width": w, "height": h, "monitors": max(n[0], 1)}


def _clipboard(want_text=False):
    """剪贴板：默认只报「有没有文本、多长」。

    内容属于隐私：不进流水、不进报告，只有显式 --clip 才在本地打印前 80 字。
    """
    if not _W.OpenClipboard(None):
        raise ValueError("剪贴板被别的程序占着")
    try:
        h = _W.GetClipboardData(13)                # CF_UNICODETEXT
        if not h:
            return {"has_text": False, "chars": 0, "preview": ""}
        p = _K.GlobalLock(h)
        if not p:
            raise ValueError("GlobalLock 失败")
        try:
            txt = ctypes.wstring_at(p)
        finally:
            _K.GlobalUnlock(h)
        return {"has_text": bool(txt), "chars": len(txt),
                "preview": (txt[:80] if want_text else "")}
    finally:
        _W.CloseClipboard()


_SECTIONS = [
    ("foreground", _foreground, {}),
    ("input", _input, {}),
    ("session", _session, {}),
    ("cpu", _cpu, {}),
    ("mem", _mem, {}),
    ("disk", _disk, {}),
    ("power", _power, {}),
    ("display", _display, {}),
    ("clipboard", _clipboard, {}),
]


def human_dur(sec):
    sec = max(int(sec or 0), 0)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分钟"
    return f"{sec} 秒"


# ---------------------------------------------------------------- 组装

def snapshot(want_clip=False):
    if not IS_WIN:
        return {"ok": False, "at": time.time(), "err": "只有 Windows 支持（当前不是）"}
    secs, missing = {}, []
    for key, fn, kw in _SECTIONS:
        try:
            secs[key] = fn(**({**kw, **({"want_text": want_clip} if key == "clipboard" else {})}))
        except Exception as e:
            missing.append({"section": key, "err": str(e)[:120]})
    return {"ok": True, "at": time.time(), "sections": secs, "missing": missing,
            "anomalies": anomalies({"sections": secs})}


ABSENT_SEC = 300          # 5 分钟没有任何输入 = 人不在（不是「刚好没动」）


def anomalies(st):
    """值得主动说出来的事。规则先当数据写，后面 policies 接进来就能用。"""
    s = st.get("sections") or {}
    out = []
    cpu = s.get("cpu") or {}
    if cpu.get("busy_pct", 0) >= 90:
        out.append(f"CPU 占用 {cpu['busy_pct']}%")
    mem = s.get("mem") or {}
    if mem.get("load_pct", 0) >= 90:
        out.append(f"内存占用 {mem['load_pct']}%")
    for d in s.get("disk") or []:
        if d.get("used_pct", 0) >= 92:
            out.append(f"{d['drive']}: 盘只剩 {d.get('free_gb')}GB（{d['used_pct']}%）")
    pw = s.get("power") or {}
    if pw.get("has_battery") and pw.get("battery_pct") is not None \
            and pw["battery_pct"] <= 20 and not pw.get("ac"):
        out.append(f"电池只剩 {pw['battery_pct']}%，没插电")
    return out


def presence(st):
    """在座/离开：把空闲时长折成一句能直接用的判断。"""
    inp = (st.get("sections") or {}).get("input") or {}
    if not inp:
        return ""
    if inp.get("idle_sec", 0) < 120:
        return "在座"
    return f"离开 {inp.get('idle_human')}"


def brief(st):
    if not st.get("ok"):
        return f"本机状态没读到：{st.get('err', '')}"
    s = st.get("sections") or {}
    parts = []
    p = presence(st)
    if p:
        parts.append(p)
    fg = s.get("foreground") or {}
    title = (fg.get("title") or "")[:28]
    if title or fg.get("exe"):
        parts.append(f"前台「{title or fg.get('exe')}」")
    ses = s.get("session") or {}
    if ses.get("locked"):
        parts.append("锁屏中")
    cpu = s.get("cpu") or {}
    mem = s.get("mem") or {}
    if cpu or mem:
        seg = []
        if cpu:
            seg.append(f"CPU {cpu.get('busy_pct')}%")
        if mem:
            seg.append(f"内存 {mem.get('load_pct')}%")
        parts.append("、".join(seg))
    for d in s.get("disk") or []:
        if d.get("used_pct", 0) >= 85:
            parts.append(f"{d['drive']}: 剩 {d.get('free_gb')}GB")
            break
    if not parts:
        return "本机连上了，但每一项都没读出内容"
    line = "；".join(parts) + "。"
    if st.get("anomalies"):
        line += " 注意：" + "、".join(st["anomalies"]) + "。"
    return line


def report(st):
    if not st.get("ok"):
        return f"本机状态没读到：{st.get('err', '')}"
    s = st.get("sections") or {}
    L = []
    if st.get("anomalies"):
        L.append("【要注意】")
        for a in st["anomalies"]:
            L.append(f"  ! {a}")
        L.append("")
    fg = s.get("foreground") or {}
    inp = s.get("input") or {}
    ses = s.get("session") or {}
    if fg or inp:
        head = f"【前台】{fg.get('title') or '（无标题）'}"
        if fg.get("exe"):
            head += f"  [{fg['exe']} pid={fg.get('pid')}]"
        L.append(head)
    if inp:
        L.append(f"【在座】{presence(st)}（空闲 {inp.get('idle_human')}）")
    if ses:
        L.append(f"【会话】{'锁屏' if ses.get('locked') else '已解锁'}  "
                 f"用户 {ses.get('user')}  已开机 {ses.get('uptime_human')}")
    cpu = s.get("cpu") or {}
    mem = s.get("mem") or {}
    if cpu or mem:
        seg = []
        if cpu:
            seg.append(f"CPU {cpu.get('busy_pct')}%（{cpu.get('cores')} 核）")
        if mem:
            seg.append(f"内存 {mem.get('avail_mb')}/{mem.get('total_mb')}MB 可用"
                       f"（占用 {mem.get('load_pct')}%）")
        L.append("【负载】" + "  ".join(seg))
    dis = s.get("disk") or []
    if dis:
        L.append("【磁盘】" + "  ".join(
            f"{d['drive']}: {d.get('used_gb')}/{d.get('total_gb')}GB"
            f"（剩 {d.get('free_gb')}GB）" for d in dis if "err" not in d))
    pw = s.get("power") or {}
    if pw:
        L.append("【供电】" + ("插着电" if pw.get("ac") else "电池供电")
                 + (f"  电量 {pw.get('battery_pct')}%" if pw.get("has_battery") else "  无电池"))
    dp = s.get("display") or {}
    if dp:
        L.append(f"【显示】{dp.get('width')}x{dp.get('height')}  显示器 {dp.get('monitors')} 台")
    cp = s.get("clipboard") or {}
    if cp:
        L.append("【剪贴板】" + (f"有文本 {cp.get('chars')} 字"
                                + (f"：{cp.get('preview')}…" if cp.get("preview") else
                                   "（内容不打印，加 --clip 才给）")
                                if cp.get("has_text") else "没有文本"))
    if st.get("missing"):
        L.append("")
        L.append("【读不到】")
        for m in st["missing"]:
            L.append(f"  - {m['section']}: {m['err']}")
    if not L:
        return "本机连上了，但各项都没读出内容"
    return "\n".join(L)


# ---------------------------------------------------------------- 自测

def _selftest():
    # ① 时间折算（离开时长是她开口的第一句话，说错就全错）
    assert human_dur(0) == "0 秒" and human_dur(90) == "1 分钟", human_dur(90)
    assert human_dur(7200) == "2 小时 0 分", human_dur(7200)
    assert human_dur(90000) == "1 天 1 小时", human_dur(90000)

    # ② 在座/离开的分界：119 秒还算在座，120 秒起算离开
    at = lambda sec: presence({"sections": {"input": {"idle_sec": sec,
                                                      "idle_human": human_dur(sec)}}})
    assert at(119) == "在座" and at(300).startswith("离开"), (at(119), at(300))
    assert presence({"sections": {}}) == ""        # 读不到就不瞎判

    # ③ 异常规则：CPU/内存/磁盘/电池各自成立，且互不误报
    assert anomalies({"sections": {"cpu": {"busy_pct": 95}}}) == ["CPU 占用 95%"]
    assert not anomalies({"sections": {"cpu": {"busy_pct": 60}}})
    assert any("C: 盘" in x for x in anomalies(
        {"sections": {"disk": [{"drive": "C", "used_pct": 95, "free_gb": 2.0}]}}))
    assert any("电池" in x for x in anomalies(
        {"sections": {"power": {"has_battery": True, "battery_pct": 10, "ac": False}}}))
    # 插着电的低电量不算异常：正在充，说出来只是噪声
    assert not anomalies({"sections": {"power": {"has_battery": True,
                                                 "battery_pct": 10, "ac": True}}})

    # ④ 一句话与报告：不许抛，不许输出空句
    st = {"ok": True, "at": 0, "missing": [], "anomalies": ["CPU 占用 95%"],
          "sections": {"input": {"idle_sec": 10, "idle_human": "10 秒"},
                       "foreground": {"title": "WorkBuddy", "exe": "WorkBuddy.exe", "pid": 1},
                       "cpu": {"busy_pct": 95, "cores": 8},
                       "mem": {"load_pct": 68, "avail_mb": 5000, "total_mb": 16000},
                       "disk": [{"drive": "C", "used_gb": 100, "total_gb": 200,
                                 "free_gb": 100, "used_pct": 50}]}}
    line = brief(st)
    assert "在座" in line and "WorkBuddy" in line and "注意" in line, line
    assert "前台" in report(st) and "磁盘" in report(st)
    assert brief({"ok": False, "err": "不是 Windows"}) == "本机状态没读到：不是 Windows"

    # ⑤ 剪贴板内容默认不外泄：preview 必须为空
    st2 = {"ok": True, "at": 0, "missing": [], "anomalies": [],
           "sections": {"clipboard": {"has_text": True, "chars": 12, "preview": ""}}}
    assert "内容不打印" in report(st2), report(st2)
    st3 = {"ok": True, "at": 0, "missing": [], "anomalies": [],
           "sections": {"clipboard": {"has_text": True, "chars": 12, "preview": "秘密"}}}
    assert "秘密" in report(st3)          # 显式要了才给

    # ⑥ 非 Windows 上必须优雅失败，不能抛
    if not IS_WIN:
        r = snapshot()
        assert r["ok"] is False and "Windows" in r["err"], r
    else:
        # ⑦ 真机：九节里至少前台/内存/磁盘/显示四节必须读得到
        live = snapshot()
        assert live["ok"] is True, live
        got = set(live["sections"])
        assert {"foreground", "mem", "disk", "display"} <= got, (got, live["missing"])
        assert live["sections"]["display"]["width"] > 0
        assert live["sections"]["mem"]["total_mb"] > 0
        # 签名声明过就必须能拿到非空指针类结果（剪贴板是 64 位指针的老坑）
        clip = live["sections"].get("clipboard") or {}
        assert "has_text" in clip or any(m["section"] == "clipboard"
                                        for m in live["missing"]), clip

    print("pc_state 自测：7 项全过")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        _selftest()
        return 0
    st = snapshot(want_clip="--clip" in argv)
    if "--json" in argv:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif "--brief" in argv:
        print(brief(st))
    else:
        print(report(st))
    return 0 if st.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
