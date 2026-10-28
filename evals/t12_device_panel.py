#!/usr/bin/env python
"""门禁 t12：W3 设备页（主机可视化）——数据真实性 + 截图端点的安全边界。

**这个门禁钉的是三件真事**：

  ① **截图端点不许变成任意文件读取的洞**。
     前端要显示屏幕预览 ⇒ 后端得把 PNG 发回去。两种做法：
       甲）前端传路径，后端照着读 —— 这等于开了一个「读这台机器任意文件」的口子；
       乙）后端自己截图时**登记**一张图，给前端一个 id，前端只能回取登记过的那张。
     门禁用行为断言钉死：路径穿越（`../../etc/passwd`）必须被拒，
     未登记的 id 必须被拒。

  ② **设备页的一切能力调用必须走 `registry.call`**。
     前端绕不过闸门——这是全篇的底线。前端不许直接调设备，
     也不许后端在设备页另开一条「直连设备」的路。

  ③ **界面不许自己算一套状态**。
     「这台设备现在能不能用」只有注册表知道（它在探测、它在解释失败原因）。
     设备页必须取 `reg.availability()` 的结果，而不是自己判。

**全离线**：不起真服务、不截真屏、不点真鼠标。截图用临时假 PNG。
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ---------------------------------------------------------------- 假 PNG
_tmp = tempfile.mkdtemp(prefix="fleet_t12_")
_png = os.path.join(_tmp, "shot.png")
with open(_png, "wb") as _f:
    _f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)   # 只要头对，够测试用


# ---------------------------------------------------------------- 假设备
class _FakeReg:
    """最小注册表替身：只提供设备页要用的两个方法。"""

    def __init__(self):
        self.avail = [{"name": "host:local", "kind": "host", "via": "local",
                       "ok": True, "reason": "", "caps": ["host.monitor"]}]
        self.calls = []

    def availability(self, ttl=None):
        return self.avail

    def call(self, cap, args=None, gate=None, device=""):
        self.calls.append((cap, args or {}, device))
        if cap == "host.monitor":
            return _ok(cap, {"cpu": 12.5, "mem": 30.0, "mem_used": 100,
                             "mem_total": 200, "disk": 5.0,
                             "net_up": 1.0, "net_down": 2.0, "procs": []})
        if cap == "host.apps":
            return _ok(cap, {"apps": [{"pid": 1, "name": "a.exe"}]})
        if cap == "host.screen":
            return _ok(cap, {"path": _png, "preview": "..."})
        return _err(cap, "不支持")


class _R:
    def __init__(self, ok, data=None, err=""):
        self.ok = ok
        self.data = data or {}
        self.err = err

    def as_dict(self):
        return {"ok": self.ok, "data": self.data, "err": self.err}


def _ok(cap, data):
    return _R(True, data, "")


def _err(cap, err):
    return _R(False, {}, err)


from fleet.gui.server import FleetServer   # noqa: E402

_reg = _FakeReg()
_srv = FleetServer(registry=_reg, port=0)
_port = _srv.start()


def _get(path, token=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (_port, path))
    if token is not None:
        req.add_header("X-Fleet-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---------------------------------------------------------------- ① 鉴权
st, _ = _get("/api/host")
check(st == 401, "设备页无 token → 401（没带凭据）")
st, _ = _get("/api/host", "wrong-token")
# 2026-10-29：403 → 401，理由见 t7_gui_backend.py（凭据无效属 401；
# 403 会让客户端以为「重试没用」而不刷新凭据）。
check(st == 401, "设备页带错 token → 401（凭据无效）")
st, body = _get("/api/host", _srv.token)
check(st == 200, "设备页带对 token → 200")

# ---------------------------------------------------------------- ② 数据聚合
d = json.loads(body)
check(d.get("ok") is True, "设备页返回 ok")
check(d.get("device") == "host:local", "设备页选中目标设备")
caps_called = [c[0] for c in _reg.calls]
check("host.monitor" in caps_called, "设备页取了资源快照")
check("host.apps" in caps_called, "设备页取了进程/应用清单")
check("host.screen" in caps_called, "设备页取了屏幕")
check(all(c[0].startswith("host.") for c in _reg.calls),
      "设备页只通过 registry.call 取数据（不直连设备）")
check(d.get("avail") is not None and d["avail"].get("name") == "host:local",
      "设备页状态来自 reg.availability()（界面不自己算）")
check(d.get("notes") == [], "一切正常时没有提示条")

# ---------------------------------------------------------------- ③ 截图登记制
sid = (d.get("screen") or {}).get("shot_id")
check(bool(sid), "屏幕预览拿到登记过的 shot_id")
st, png = _get("/api/shot?id=%s" % sid, _srv.token)
check(st == 200 and png.startswith(b"\x89PNG"), "登记过的截图能取回（真 PNG）")

st, _ = _get("/api/shot?id=nope", _srv.token)
check(st == 404, "未登记的 id → 404（不许照着路径读文件）")
st, _ = _get("/api/shot?id=../../../../etc/passwd", _srv.token)
check(st == 404, "路径穿越 → 404（截图端点不是任意文件读取的洞）")
st, _ = _get("/api/shot", _srv.token)
check(st == 400, "缺 id → 400（说清楚要什么）")
st, _ = _get("/api/shot?id=%s" % sid)
check(st == 401, "取截图也要鉴权（无 token → 401）")

# ---------------------------------------------------------------- ④ 失败要说原因
class _BadReg(_FakeReg):
    def call(self, cap, args=None, gate=None, device=""):
        self.calls.append((cap, args or {}, device))
        return _err(cap, "本机未装 psutil（pip install psutil）")


_reg2 = _BadReg()
_srv2 = FleetServer(registry=_reg2, port=0)
_p2 = _srv2.start()
req = urllib.request.Request("http://127.0.0.1:%d/api/host" % _p2)
req.add_header("X-Fleet-Token", _srv2.token)
with urllib.request.urlopen(req, timeout=15) as r:
    d2 = json.loads(r.read())
notes = d2.get("notes") or []
check(notes and "psutil" in " ".join(notes),
      "取不到就写明原因（不许给假的 0 或空表）")
_srv2.stop()

# ---------------------------------------------------------------- ⑤ 设备不可用
class _DownReg(_FakeReg):
    def __init__(self):
        super().__init__()
        self.avail = [{"name": "host:local", "kind": "host", "via": "local",
                       "ok": False, "reason": "设备不在线", "caps": []}]


_reg3 = _DownReg()
_srv3 = FleetServer(registry=_reg3, port=0)
_p3 = _srv3.start()
req = urllib.request.Request("http://127.0.0.1:%d/api/host" % _p3)
req.add_header("X-Fleet-Token", _srv3.token)
with urllib.request.urlopen(req, timeout=15) as r:
    d3 = json.loads(r.read())
check(d3.get("notes") and "不在线" in " ".join(d3["notes"]),
      "设备不可用时说清为什么（来自 availability 的 reason）")
check(d3.get("monitor") is None, "设备不可用时不取数据（不硬来）")
_srv3.stop()

_srv.stop()

# 清理临时目录
try:
    for f in os.listdir(_tmp):
        os.remove(os.path.join(_tmp, f))
    os.rmdir(_tmp)
except OSError:
    pass

print("t12 device_panel: %s" % ("PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL)),
      flush=True)
sys.exit(1 if FAIL else 0)
