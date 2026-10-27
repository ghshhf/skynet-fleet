"""门禁 t10：PC 端操控电脑设备（kind=pc_control）——只读免闸、动手要批、高危要说清。

锁死的是真会发生的形状，不是假想：

  ① **caps() 必须返回字符串名**。
     踩过的坑：Host.caps() 一开始返回的是 Capability 对象，注册表 owners() 用
     `cap in set(self.caps())` 跟字符串比，对不上 → 能力「登记了却调不通」。
     这条用 owners() 非空 + 每个 cap 都有 owner 钉死。

  ② **只读能力（screenshot/windows/clipboard.read/proc.list/sysinfo）免闸门**，
     auto 档、无副作用。看不是改，不该弹窗要批准。

  ③ **动手能力 fail-closed**：没注入闸门一律 denied（不许「忘了就放行」），
     闸门拒绝必须带理由，ask 档 → 闸门 level=2；被拒时**一次系统调用都不许有**。

  ④ **所有系统交互都走模块级出口**（`_subprocess_run` / `_ctypes_load`）。
     本门禁把这两个出口换成假的，再加一道「漏网检测」：把模块里的 subprocess
     换成会炸的桩，静态扫描 `subprocess.run` / `ctypes.WinDLL` / `ctypes.windll`
     只许出现在那两个出口里——**任何一条真系统调用都不许发生**。
     门禁红 = 代码坏了（t4 的教训：门禁红必须等于代码坏）。

  ⑤ **失败必须带原因**：缺参数、pid 不是整数、键名不认识、PowerShell 报错、
     taskkill 失败——每条都要带可读 reason，不许静默成功/静默失败。

  ⑥ **高危动作要标风险与可逆性**：结束进程（destructive=True）、强制关窗口
     返回里必须有 risk=high 与 reversible=false；键盘/文本注入必须说清
     「本次注入了什么」——「一个接口能灌任意按键序列却什么都不说」是最阴的坑。

  ⑦ **非 Windows 要明确报不支持并给原因**，不许静默返回空。

**全离线、可复现**：本门禁**绝不真动用户桌面**——截图、鼠标、键盘、进程、
剪贴板全部落在假出口上。跑法：python evals/t10_pc_control.py
"""
import ast
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fleet.kinds.pc_control as PC           # noqa: E402
from fleet.catalog import build                # noqa: E402
from fleet.core import result as R             # noqa: E402

READ_CAPS = ("pcctl.screenshot", "pcctl.windows", "pcctl.clipboard.read",
             "pcctl.proc.list", "pcctl.sysinfo")
ACT_CAPS = ("pcctl.app.launch", "pcctl.window.activate", "pcctl.window.close",
            "pcctl.mouse.move", "pcctl.mouse.click", "pcctl.key",
            "pcctl.text", "pcctl.clipboard.write", "pcctl.proc.kill")
#: 第三档（2026-10-28）：**看，但看的东西会出本机**。
#: 为什么不并进 READ_CAPS：它确实不改这台机器，但截图要送视觉模型 →
#: 屏幕内容离开本地。并进只读档就等于把「外发」登记成了「只读」，
#: 以后谁查「哪些能力不碰外部」会被这张表骗。
LOOK_CAPS = ("pcctl.look",)
ALL_CAPS = READ_CAPS + LOOK_CAPS + ACT_CAPS

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ---------------------------------------------------------------- 假系统层
#
# 只有一个出口：`_subprocess_run`（跑命令）与 `_ctypes_load`（加载 Win32 DLL）。
# 两个都换成假的 → 本门禁**一条真系统调用都不会发生**。

CALLS = []            # [(kind, args), ...]
FLAGS = {"clip_rc": 0, "taskkill_rc": 0, "ps_rc": 0, "windows_json": None}
SHOT_FILES = []       # 假截图「落盘」的路径


class _FakeProc:
    """假 subprocess.run 的返回物：跟真的一样有 returncode/stdout/stderr。"""


def _proc(rc, out="", err=""):
    class P:
        def __init__(self):
            self.returncode = rc
            self.stdout = out.encode("utf-8")
            self.stderr = err.encode("utf-8")
    return P()


def _b64(s):
    import base64
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def _b64_in_script(script):
    """从 PowerShell 脚本里抠出 base64 载荷并解开（假截图路径用）。"""
    import base64
    import re
    m = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)
    if not m:
        return ""
    try:
        return base64.b64decode(m.group(1)).decode("utf-8")
    except Exception:
        return ""


def fake_run(argv, timeout=20):
    """假 _subprocess_run：记住调用，按命令给 canned 回答。

    形状必须跟真出口**一模一样**（(rc, stdout, stderr) 三元组）——假后端形状
    一旦不同，门禁就在测另一个接口，绿了也说明不了真实现对不对。
    """
    argv = list(argv)
    CALLS.append(("cmd", argv))
    if argv[:1] == ["tasklist"]:
        return (0, '"System Idle Process","0","1","8 K"\n'
                    '"notepad.exe","4321","Console","1","12,345 K"\n', "")
    if argv[:1] == ["taskkill"]:
        if FLAGS["taskkill_rc"]:
            return (FLAGS["taskkill_rc"], "", "ERROR: 找不到进程")
        return (0, "SUCCESS: 已终止 PID %s。" % argv[2], "")
    if argv[:1] == ["powershell"]:
        script = argv[-1]
        if FLAGS["ps_rc"]:
            return (FLAGS["ps_rc"], "", "假 PowerShell 报错")
        if "CopyFromScreen" in script:
            # 假截图：把 base64 里的路径解出来，建一个空文件（绝不含真实像素）
            p = _b64_in_script(script)
            if p:
                with open(p, "wb") as f:
                    f.write(b"\x89PNG\r\n\x1a\nFAKE")
                SHOT_FILES.append(p)
            return (0, "", "")
        if "GetText()" in script:
            if FLAGS["clip_rc"]:
                return (FLAGS["clip_rc"], "", "剪贴板是空的")
            return (0, _b64("假剪贴板内容"), "")
        if "SetText" in script:
            return (0, "", "")
        if "EnumWindows" in script:
            payload = FLAGS["windows_json"]
            if payload is None:
                payload = json.dumps([{"hwnd": 1001, "title": "记事本",
                                       "pid": 4321},
                                      {"hwnd": 1002, "title": "浏览器",
                                       "pid": 5555}])
            return (0, payload, "")
        if "Start-Process" in script:
            return (0, "", "")
        return (0, "", "")
    return (0, "", "")


class _FakeUser32:
    """假 user32.dll：只记录调用。"""

    def __init__(self):
        self.calls = []

    def SetCursorPos(self, x, y):
        self.calls.append(("SetCursorPos", (x, y)))
        return 1

    def mouse_event(self, f, dx, dy, d, e):
        self.calls.append(("mouse_event", (f,)))
        return 1

    def SendInput(self, n, arr, size):
        self.calls.append(("SendInput", (n,)))
        return n

    def ShowWindow(self, h, n):
        self.calls.append(("ShowWindow", (str(h), n)))
        return 1

    def SetForegroundWindow(self, h):
        self.calls.append(("SetForegroundWindow", (str(h),)))
        return 1

    def PostMessageW(self, h, m, w, l):
        self.calls.append(("PostMessageW", (str(h), m)))
        return 1

    def GetWindowThreadProcessId(self, h, pidref):
        self.calls.append(("GetWindowThreadProcessId", (str(h),)))
        try:
            pidref._obj.value = 4321
        except Exception:
            pass
        return 1


FAKE_USER32 = _FakeUser32()


def fake_dll(name):
    CALLS.append(("dll", [name]))
    return FAKE_USER32


PC._subprocess_run = fake_run
PC._ctypes_load = fake_dll

# 漏网检测：把模块里的 subprocess 换成会炸的桩。
# 任何一条**没走出口**的真系统调用（直接 subprocess.run 之类）都会当场炸。
class _Boom:
    def run(self, *a, **k):
        raise AssertionError("漏网：有人直接调了 subprocess.run（必须走 _subprocess_run）")

    def Popen(self, *a, **k):
        raise AssertionError("漏网：有人直接调了 subprocess.Popen")


PC.subprocess = _Boom()

# ---------------------------------------------------------------- 离线注册表
_tmp = tempfile.mkdtemp(prefix="t10_pcctl_")
reg = build(path=os.path.join(_tmp, "devices.json"), with_nodes=False)

dev = reg.device("pcctl:local")
check(dev is not None, "catalog 里有 pcctl:local 设备（PcControl 已注册）")
check(dev is not None and dev.kind == "pc_control", "设备 kind=pc_control")

# ① caps() 契约：字符串名 + 覆盖全部能力 + 每项都有 owner
names = dev.caps() if dev else ()
check(bool(names) and all(isinstance(n, str) for n in names),
      "caps() 返回的全是字符串名（不是 Capability 对象）")
check(set(names) == set(ALL_CAPS), f"caps() 覆盖全部 {len(ALL_CAPS)} 项能力")
for name in ALL_CAPS:
    check(reg.capability(name) is not None, f"{name} 已登记")
    check(bool(reg.owners(name)), f"{name} 有设备能接（owners() 非空）")

# ② 分档：只读 auto + 无副作用；动手 ask + 副作用
for name in READ_CAPS:
    c = reg.capability(name)
    check(c.approval == "auto" and not c.side_effect, f"{name} auto 档 + 只读")
for name in ACT_CAPS:
    c = reg.capability(name)
    check(c.approval == "ask" and c.side_effect, f"{name} ask 档 + 副作用")
for name in LOOK_CAPS:
    c = reg.capability(name)
    check(c.approval == "ask" and c.side_effect,
          f"{name} ask 档 + 标副作用（外发内容不是只读）")
c = reg.capability("pcctl.proc.kill")
check(c.destructive is True, "pcctl.proc.kill 标了 destructive（闸门要双确认）")

# ③ 只读免闸门：不给 gate 也能成
GATE_LEVELS = []


def gate_yes(cap, dev_, args, level):
    GATE_LEVELS.append(level)
    return True, ""


CALLS.clear()
r = reg.call("pcctl.sysinfo")
check(r.ok and r.data.get("os"), "pcctl.sysinfo 成功（只读免闸门）")
check(not r.side_effect, "pcctl.sysinfo 无副作用")

r = reg.call("pcctl.proc.list")
check(r.ok and r.data.get("count") == 2, "pcctl.proc.list 列出了 2 个假进程")
check(any(c[1][:1] == ["tasklist"] for c in CALLS if c[0] == "cmd"),
      "pcctl.proc.list 走 tasklist 出口")

r = reg.call("pcctl.proc.list", args={"name": "notepad"})
check(r.ok and r.data.get("count") == 1, "pcctl.proc.list 按名字过滤生效")

r = reg.call("pcctl.windows")
check(r.ok and r.data.get("count") == 2, "pcctl.windows 列出 2 个假窗口")
check(r.data.get("windows")[0].get("hwnd") == 1001, "pcctl.windows 回窗口句柄")

r = reg.call("pcctl.clipboard.read")
check(r.ok and r.data.get("text") == "假剪贴板内容", "pcctl.clipboard.read 读到内容")
check(r.data.get("reversible") is True and r.data.get("risk") == "low",
      "只读能力返回里带 risk/reversible")

r = reg.call("pcctl.screenshot")
check(r.ok and r.data.get("path"), "pcctl.screenshot 成功（假截图）")
check(r.data.get("bytes", 0) > 0, "pcctl.screenshot 回文件大小")
check(SHOT_FILES, "pcctl.screenshot 走 _subprocess_run 出口落盘")

# ④ 动手能力 fail-closed：没闸门一律 denied，且**一次系统调用都不许有**
for name in ACT_CAPS:
    CALLS.clear()
    args = {"target": "notepad.exe", "hwnd": 1001, "x": 10, "y": 20,
            "keys": "ctrl+c", "text": "你好", "pid": 4321}
    r = reg.call(name, args=args)
    check(r.code == R.DENIED, f"{name} 无闸门 → denied（fail-closed）")
    check(bool(r.err), f"{name} denied 必须带理由")
    check(not CALLS, f"{name} 无闸门时一次系统调用都没有（不许漏跑）")

# ⑤ 闸门拒绝 → denied 且带理由，仍然不落地
CALLS.clear()
r = reg.call("pcctl.mouse.click", args={"x": 1, "y": 2},
             gate=lambda c, d, a, l: (False, "主人说不"))
check(r.code == R.DENIED and r.err == "主人说不", "闸门拒绝 → denied 且带理由")
check(not CALLS, "闸门拒绝时不落到系统调用")

# ⑥ ask 档 → 闸门 level=2；放行才执行并标记副作用
for name in ACT_CAPS:
    GATE_LEVELS.clear()
    CALLS.clear()
    args = {"target": "notepad.exe", "hwnd": 1001, "x": 10, "y": 20,
            "keys": "ctrl+c", "text": "你好", "pid": 4321}
    r = reg.call(name, args=args, gate=gate_yes)
    check(r.ok, f"{name} 放行后执行成功（err={r.err}）")
    check(r.side_effect, f"{name} 标记为副作用")
    check(bool(GATE_LEVELS) and GATE_LEVELS[-1] == 2, f"{name} 闸门 level=2")

# ⑦ 高危动作：风险等级 + 可逆性
r = reg.call("pcctl.proc.kill", args={"pid": 4321, "force": True}, gate=gate_yes)
check(r.ok, "pcctl.proc.kill 成功")
check(r.data.get("risk") == "high", "结束进程标了 risk=high")
check(r.data.get("reversible") is False, "结束进程标了不可逆")
check("不可逆" in (r.data.get("reversible_note") or ""), "结束进程说清为什么不可逆")

r = reg.call("pcctl.window.close", args={"hwnd": 1001, "force": True}, gate=gate_yes)
check(r.ok, "pcctl.window.close(force) 成功")
check(r.data.get("risk") == "high", "强制关窗口标了 risk=high")
check(r.data.get("reversible") is False, "强制关窗口标了不可逆")

r = reg.call("pcctl.window.close", args={"hwnd": 1001}, gate=gate_yes)
check(r.data.get("risk") == "medium", "普通关窗口是 medium 风险")
check(r.data.get("reversible") is True, "普通关窗口标了可逆")

# ⑧ 键盘注入必须说清「本次注入了什么」
r = reg.call("pcctl.key", args={"keys": "ctrl+c"}, gate=gate_yes)
check(r.ok, "pcctl.key 成功")
acts = r.data.get("actions") or []
check(len(acts) >= 2, f"pcctl.key 回逐条按键动作（{len(acts)} 条）")
check(any("ctrl" in a for a in acts) and any(a.endswith("c") for a in acts),
      "pcctl.key 说清了按了哪些键")
check("本次注入了" in (r.data.get("summary") or ""), "pcctl.key 有一句人话总结")
check(r.data.get("reversible") is False, "按键标了不可逆")

r = reg.call("pcctl.text", args={"text": "你好"}, gate=gate_yes)
check(r.ok, "pcctl.text 成功")
check(r.data.get("chars") == 2, "pcctl.text 说清打了几字符")
check("本次注入了" in (r.data.get("summary") or ""), "pcctl.text 有一句人话总结")

r = reg.call("pcctl.mouse.click", args={"x": 5, "y": 6}, gate=gate_yes)
check(r.ok and r.data.get("actions"), "pcctl.mouse.click 说清点了什么")
check(r.data.get("reversible") is False, "点击标了不可逆")

# ⑨ 失败必须带原因（每条失败路径）
BAD = [
    ("pcctl.proc.kill", {}, "缺 pid"),
    ("pcctl.proc.kill", {"pid": "abc"}, "pid 不是整数"),
    ("pcctl.window.activate", {}, "缺 hwnd"),
    ("pcctl.window.close", {}, "缺 hwnd"),
    ("pcctl.mouse.move", {"x": 1}, "缺 y"),
    ("pcctl.mouse.click", {}, "缺坐标"),
    ("pcctl.key", {}, "缺按键"),
    ("pcctl.text", {}, "缺文本"),
    ("pcctl.app.launch", {}, "缺 target"),
]
for name, args, why in BAD:
    r = reg.call(name, args=args, gate=gate_yes)
    check(not r.ok and r.err, f"{name} {why} → 失败且带原因")

r = reg.call("pcctl.key", args={"keys": "不存在的键"}, gate=gate_yes)
check(not r.ok and "不认识的键名" in r.err, "键名不认识 → 失败且说清原因")

# ⑩ 底层失败也必须带原因（PowerShell 报错 / taskkill 失败 / 剪贴板拿不到）
FLAGS["ps_rc"] = 1
CALLS.clear()
r = reg.call("pcctl.screenshot", gate=gate_yes)
check(not r.ok and "截图失败" in r.err, "截图底层失败 → 报错且带原因")
FLAGS["ps_rc"] = 0

FLAGS["clip_rc"] = 3
r = reg.call("pcctl.clipboard.read")
check(not r.ok and "读剪贴板失败" in r.err, "剪贴板拿不到 → 报错且带原因")
FLAGS["clip_rc"] = 0

FLAGS["taskkill_rc"] = 128
r = reg.call("pcctl.proc.kill", args={"pid": 9999}, gate=gate_yes)
check(not r.ok and "结束进程" in r.err, "taskkill 失败 → 报错且带原因")
FLAGS["taskkill_rc"] = 0

# ⑪ 未知能力 → unsupported（不是 unavailable：上层该知道是「压根没有这项」）
r = reg.call("pcctl.nope")
check(r.code == R.UNSUPPORTED, "pcctl.nope 未登记 → unsupported")

# ⑫ 非 Windows → 明确报不支持并给原因（不许静默返回空）
_win = PC._is_windows
try:
    PC._is_windows = lambda: False
    ok, reason = dev.probe()
    check(not ok, "非 Windows 时 probe 不可用")
    check("Windows" in (reason or ""), f"非 Windows 时 probe 说清原因（{reason}）")
    r = dev.invoke("pcctl.screenshot", {})
    check(r.code == R.UNSUPPORTED, "非 Windows 时 invoke 回 unsupported")
    check(bool(r.err) and "Windows" in r.err, "非 Windows 时 invoke 带原因")
    r = reg.call("pcctl.screenshot")
    check(not r.ok and bool(r.err), "非 Windows 时注册表调用失败且带原因")
finally:
    PC._is_windows = _win

# ⑬ 静态门禁：系统调用只许走两个出口
#
# 为什么这条要有：「所有系统交互都走模块级出口」是硬约束，但运行期换掉出口
# 只能证明「本门禁没真调」，证不了「代码里没有第二条真调的路」。
# 所以静态扫一遍源码：subprocess.run / ctypes.WinDLL / ctypes.windll 只许出现在
# `_subprocess_run` / `_ctypes_load` 这两个函数体里。
_src_path = os.path.join(ROOT, "fleet", "kinds", "pc_control.py")
with open(_src_path, encoding="utf-8") as f:
    _src = f.read()
_tree = ast.parse(_src)
ALLOWED = {"_subprocess_run", "_ctypes_load"}
leaks = []
for node in ast.walk(_tree):
    if not isinstance(node, ast.FunctionDef):
        continue
    seg = ast.get_source_segment(_src, node) or ""
    for bad in ("subprocess.run", "subprocess.Popen", "ctypes.WinDLL",
                "ctypes.windll", "os.system"):
        if bad in seg and node.name not in ALLOWED:
            leaks.append("%s 里出现 %s" % (node.name, bad))
check(not leaks, f"系统调用只走两个出口（发现 {leaks or '无'}）")
check("subprocess.run" in _src and "ctypes.WinDLL" in _src,
      "两个出口本身真的调了系统（否则静态检查是空转）")
check("_is_windows" in _src, "非 Windows 的判定在源码里")

# ⑭ 模块顶层不许 import 重依赖（无显示/无网环境也要能 import）
_top = set()
for node in ast.parse(_src).body:
    if isinstance(node, ast.Import):
        for a in node.names:
            _top.add(a.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        _top.add((node.module or "").split(".")[0])
_heavy = {"pyautogui", "PIL", "mss", "requests", "numpy"} & _top
check(not _heavy, f"pc_control.py 顶层不 import 重依赖（发现 {_heavy or '无'}）")

# ⑮ 本门禁自己不许真调系统：两个出口必须都换掉了
check(PC._subprocess_run is fake_run, "命令执行走的是假实现")
check(PC._ctypes_load is fake_dll, "Win32 加载走的是假实现")

# ⑯ 剪贴板必须**字节精确**：PowerShell 往 stdout 写东西会自带一个换行，
#     直接吐文本会让「读到的剪贴板」比真实内容多一个 "\n"。
#     差一个换行就可能把密码剪坏、把代码剪坏，所以传输必须走 base64。
_src2 = _src
check("ToBase64String" in _src2 and "FromBase64String" in _src2,
      "剪贴板读写走 base64 传输（字节精确，不被 PowerShell 的换行污染）")
check("Get-Clipboard" not in _src2,
      "不用 Get-Clipboard（它会自己加换行）")
# 假后端回 base64，验证解出来是原文（不是多了换行）
FLAGS["clip_rc"] = 0
r = reg.call("pcctl.clipboard.read")
check(r.ok and r.data.get("text") == "假剪贴板内容",
      "读剪贴板解出来是原文（没有多一个换行）")
# 空串也必须能写（.NET 的 SetText("") 会抛，实现里要特判）
r = reg.call("pcctl.clipboard.write", args={"text": ""}, gate=gate_yes)
check(r.ok, "写空剪贴板不炸（实现里特判了 .NET 的空串异常）")

# ⑰ 截图路径也走 base64：path 是调用方给的，拼进 PowerShell 源码就是注入口子
check("FromBase64String" in _src2, "截图路径走 base64（不留注入口子）")

# 清理临时目录
try:
    for f in os.listdir(_tmp):
        os.remove(os.path.join(_tmp, f))
    os.rmdir(_tmp)
except OSError:
    pass

print("t10 pc_control:", "PASS" if not FAIL else f"FAIL ({len(FAIL)} 项)", flush=True)
sys.exit(1 if FAIL else 0)
