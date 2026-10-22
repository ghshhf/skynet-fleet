"""Android 被控端驱动（控制端执行，手机零安装）。

第三原则在手机上的落地：
    - 手机不装任何软件，只开「开发者选项 → 无线调试」；
    - 工具全留在电脑侧，通过 adb 协议驱动；
    - 手机内存占用为零，老机器也能被控。

能力分两档（实测 Redmi K60 / Android 15 / HyperOS）：
    A 档（无需额外授权）：读屏、读 UI 树、启动应用、读包列表、读电量/通知
    B 档（需开发者选项里的「USB 调试（安全设置）」）：注入点击/滑动/按键/文本
    B 档缺失时 tap() 明确报 INJECT_EVENTS 错误，不静默降级、不假装成功。

用法：
    python android_device.py devices
    python android_device.py info
    python android_device.py shot  D:/tmp/phone.png
    python android_device.py ui    [关键字]
    python android_device.py launch com.tencent.mm
    python android_device.py cap           # 探测两档能力
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ANDROID_HOME = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ""
_ADB_CANDIDATES = [
    (os.path.join(ANDROID_HOME, "platform-tools", "adb.exe") if ANDROID_HOME else ""),
    r"D:/DevTools/Android/platform-tools/adb.exe",
    r"C:/Users/admin/AppData/Local/Android/Sdk/platform-tools/adb.exe",
    "/usr/bin/adb",
]

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


# ---------------------------------------------------------------- adb 基础

def adb_path():
    hit = shutil.which("adb")
    if hit:
        return hit
    for p in _ADB_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return ""


def _run(serial, args, timeout=60):
    """跑一条 adb 命令，返回 (rc, text)。serial 为空表示 host 类命令。"""
    exe = adb_path()
    if not exe:
        return 127, "本机找不到 adb（需 Android SDK platform-tools）"
    cmd = [exe]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"adb 超时：{' '.join(args)}"
    out = (p.stdout or b"") + (p.stderr or b"")
    return p.returncode, out.decode("utf-8", "replace")


def devices():
    """列出已连设备 -> [{serial, state, model, product, device, wireless}]。"""
    rc, txt = _run("", ["devices", "-l"])
    if rc != 0:
        return []
    out = []
    for line in txt.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        kv = {}
        for tok in parts[2:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                kv[k] = v
        out.append({
            "serial": serial,
            "state": state,
            "model": kv.get("model", ""),
            "product": kv.get("product", ""),
            "device": kv.get("device", ""),
            "wireless": (":" in serial) or serial.endswith("._tcp"),
        })
    return out


def pick_serial(prefer_wireless=True):
    """挑一台设备。优先无线调试的（手机），其次第一台。没有就返回空。"""
    ds = [d for d in devices() if d["state"] == "device"]
    if not ds:
        return ""
    if prefer_wireless:
        for d in ds:
            if d["wireless"]:
                return d["serial"]
    return ds[0]["serial"]


# ---------------------------------------------------------------- 自动接入
#
# 无线调试的端口每次开关都会换，adb 掉线后那条 IP:port 记录就没了。所以接入要
# 能自己找回：现成的 → 上次成功的 → mDNS 广播里现找。缓存只是"首猜"，不是依赖。

TARGET_CACHE = os.path.join(os.path.expanduser("~"), ".fleet", "android_target.json")


def _cache_load():
    try:
        with open(TARGET_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_save(data):
    try:
        os.makedirs(os.path.dirname(TARGET_CACHE), exist_ok=True)
        tmp = TARGET_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, TARGET_CACHE)
    except OSError:
        pass


def mdns_adb_targets(timeout=8):
    """从 adb 的 mDNS 发现里捞 adb 服务条目。"""
    rc, txt = _run("", ["mdns", "services"], timeout=timeout)
    out = []
    for line in txt.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in ("_adb-tls-connect._tcp", "_adb._tcp"):
            out.append({"name": parts[0], "service": parts[1], "addr": parts[2]})
    return out


def alive(serial, timeout=15):
    rc, txt = shell(serial, "echo ok", timeout=timeout)
    return rc == 0 and "ok" in txt


def attach(serial="", force_rescan=False):
    """自动接入，掉线自愈。返回 {ok, serial, via}。"""
    tried = []
    if serial and alive(serial):
        _cache_save({"serial": serial})
        return {"ok": True, "serial": serial, "via": "explicit"}

    cur = pick_serial()
    if cur and not force_rescan and alive(cur):
        _cache_save({"serial": cur})
        return {"ok": True, "serial": cur, "via": "current"}

    cached = _cache_load().get("serial", "")
    if cached and cached != cur:
        tried.append(cached)
        _run("", ["connect", cached])
        if alive(cached):
            _cache_save({"serial": cached})
            return {"ok": True, "serial": cached, "via": "cache"}

    for t in mdns_adb_targets():
        for target in (t["name"], t["addr"]):
            if target in tried:
                continue
            tried.append(target)
            _run("", ["connect", target])
            if alive(target):
                _cache_save({"serial": target, "addr": t["addr"]})
                return {"ok": True, "serial": target, "addr": t["addr"], "via": "mdns"}

    return {"ok": False, "err": "找不到设备（手机需开着无线调试且同一网段）",
            "tried": tried}


def pair(addr, code, timeout=45):
    rc, txt = _run("", ["pair", addr, str(code)], timeout=timeout)
    return {"ok": rc == 0 and "Successfully paired" in txt, "out": txt.strip()}


def connect(addr, timeout=30):
    rc, txt = _run("", ["connect", addr], timeout=timeout)
    return {"ok": rc == 0 and "connected to" in txt, "out": txt.strip()}


def disconnect(addr):
    rc, txt = _run("", ["disconnect", addr])
    return {"ok": rc == 0, "out": txt.strip()}


def shell(serial, cmd, timeout=60):
    """执行一条 shell 命令。过红线检查，见 guard()。"""
    why = guard(cmd)
    if why:
        return 1, f"[红线拦截] {why}"
    return _run(serial, ["shell", cmd], timeout=timeout)


# 用户红线（2026-10-20 明确）：①不许断网——这台手机既从本机热点上网，又给另一台
# 笔记本开热点，网一断两头都废；②不许卸载软件。写成代码拦截，不靠记性。

_FORBID_NET_WRITE = (
    "settings put global airplane_mode_on",
    "settings put secure wifi_", "settings put global wifi_",
    "svc wifi", "svc data",
    # 只拦真正会断网的子命令；status / start-scan / list-scan-results 是只读，放行
    "cmd wifi set-wifi-enabled", "cmd wifi connect-network",
    "cmd wifi forget-network", "cmd wifi disable", "cmd wifi set-",
    "cmd connectivity", "cmd telephony",
    "am broadcast -a android.net.wifi",
    "ifconfig wlan", "ip link set wlan",
)
_FORBID_REMOVE = (
    "pm uninstall", "pm clear", "pm disable", "pm disable-user",
    "pm uninstall-system-updates", "rm -rf /data/app",
)


def guard(cmd):
    """返回拒绝理由；允许则返回空串。"""
    low = cmd.lower()
    for pat in _FORBID_NET_WRITE:
        if pat in low:
            return f"禁止改网络（会同时断掉手机和另一台笔记本）：{pat}"
    for pat in _FORBID_REMOVE:
        if pat in low:
            return f"禁止卸载/清数据软件：{pat}"
    return ""


# ---------------------------------------------------------------- 硬件画像

_PROPS = [
    ("manufacturer", "ro.product.manufacturer"),
    ("model", "ro.product.model"),
    ("market_name", "ro.product.marketname"),
    ("android", "ro.build.version.release"),
    ("sdk", "ro.build.version.sdk"),
    ("abi", "ro.product.cpu.abi"),
    ("serial", "ro.serialno"),
    ("rom", "ro.build.version.incremental"),
]


def info(serial):
    # 每条带标记，空值也占一行；否则空属性会让后面的值整体错位
    props = "; ".join(f"echo {key}=$(getprop {prop})" for key, prop in _PROPS)
    rc, txt = shell(serial, props)
    out = {}
    for line in txt.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    rc, size = shell(serial, "wm size")
    m = re.search(r"(\d+)x(\d+)", size)
    out["screen"] = m.group(0) if m else ""
    rc, den = shell(serial, "wm density")
    m = re.search(r"(\d+)", den.split(":")[-1]) if ":" in den else None
    out["density"] = m.group(1) if m else ""
    rc, bat = shell(serial, "dumpsys battery")
    for k in ("level", "status", "temperature", "health"):
        m = re.search(rf"{k}:\s*(-?\d+)", bat)
        if m:
            out[f"battery_{k}"] = m.group(1)
    rc, focus = shell(serial, "dumpsys window | grep -E 'mCurrentFocus'")
    m = re.search(r"([\w.]+/[\w.$]+)", focus)
    out["foreground"] = m.group(1) if m else ""
    return out


# ---------------------------------------------------------------- A 档：看

def shot(serial, path, timeout=60):
    """截屏存到本地。手机侧临时文件用完即删。"""
    remote = "/data/local/tmp/_fleet_shot.png"
    rc, txt = shell(serial, f"screencap -p {remote}", timeout=timeout)
    if rc != 0:
        return {"ok": False, "err": txt.strip()}
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    rc, txt = _run(serial, ["pull", remote, os.path.abspath(path)], timeout=timeout)
    shell(serial, f"rm -f {remote}")
    if rc != 0:
        return {"ok": False, "err": txt.strip()}
    return {"ok": True, "path": os.path.abspath(path),
            "bytes": os.path.getsize(path) if os.path.isfile(path) else 0}


def ui_xml(serial, timeout=60, retries=2):
    """取 UI 树原文（uiautomator dump）。

    必须 pull 文件而不是 shell cat：adb shell 走 pty 会做 CRLF 转换，
    几十 KB 的 XML 被改写过就解析不了。
    dump 会偶发失败（界面还在播动画，等不到 idle 状态），所以重试，
    否则一次抖动就会把整条自动化流程拖停。
    """
    remote = "/data/local/tmp/_fleet_ui.xml"
    local = os.path.join(tempfile.gettempdir(), "_fleet_ui.xml")
    last = ""
    for attempt in range(retries + 1):
        rc, txt = shell(serial, f"uiautomator dump {remote}", timeout=timeout)
        if rc == 0 and "dumped to" in txt:
            rc, txt = _run(serial, ["pull", remote, local], timeout=timeout)
            if rc == 0 and os.path.isfile(local):
                with open(local, encoding="utf-8", errors="replace") as f:
                    xml = f.read()
                os.remove(local)
                shell(serial, f"rm -f {remote}")
                if "<hierarchy" in xml:
                    return {"ok": True, "xml": xml}
                last = "UI 树内容不完整"
            else:
                last = "拉不回 UI 树"
        else:
            last = (txt.strip() or "uiautomator dump 失败")
        if attempt < retries:
            time.sleep(0.5)
    shell(serial, f"rm -f {remote}")
    return {"ok": False, "err": last}


def parse_bounds(text):
    """'[0,0][1080,2400]' -> (x1, y1, x2, y2)，解析不了返回 None。"""
    m = _BOUNDS.search(text or "")
    if not m:
        return None
    return tuple(int(v) for v in m.groups())


def parse_ui(xml):
    """UI 树 -> 扁平节点表，带 parent 索引与中心点。

    只留「有文字/描述」或「可点击」的节点，其余是布局壳子，几十上百个没用。
    注意 parent 必须重映射：过滤会让原生索引错位，父节点被过滤掉时要沿链
    往上找最近的保留祖先，否则「点列表项」这类操作会断链。
    """
    root = ET.fromstring(xml)
    all_nodes = []

    def walk(node, parent):
        idx = len(all_nodes)
        a = node.attrib
        b = parse_bounds(a.get("bounds", ""))
        rec = {
            "i": idx,
            "parent": parent,
            "text": a.get("text", ""),
            "desc": a.get("content-desc", ""),
            "cls": (a.get("class", "") or "").split(".")[-1],
            "id": a.get("resource-id", ""),
            "clickable": a.get("clickable") == "true",
            "checked": a.get("checked") == "true",
            "enabled": a.get("enabled") != "false",
            "bounds": b,
            "center": ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) if b else None,
        }
        all_nodes.append(rec)
        for ch in node:
            walk(ch, idx)

    walk(root, None)
    keep = [r for r in all_nodes if r["text"] or r["desc"] or r["clickable"]]
    remap = {r["i"]: new_i for new_i, r in enumerate(keep)}

    def nearest_kept(old):
        cur = old
        while cur is not None:
            if cur in remap:
                return remap[cur]
            cur = all_nodes[cur]["parent"]
        return None

    for new_i, r in enumerate(keep):
        r["i"] = new_i
        r["parent"] = nearest_kept(r["parent"])
    return keep


def label(rec):
    return rec["text"] or rec["desc"] or rec["cls"]


def find(nodes, keyword, exact=False):
    """按文字/描述找节点。

    默认精确优先、模糊兜底：界面上「7」既是数字键又出现在「3,000×7=21,000」
    这种结果文本里，模糊匹配会抢到错的那个，点下去等于什么都没按。
    """
    kw = keyword.strip()
    exact_hits, fuzzy_hits = [], []
    for r in nodes:
        for field in (r["text"], r["desc"]):
            if not field:
                continue
            if field == kw:
                exact_hits.append(r)
                break
            if not exact and kw in field:
                fuzzy_hits.append(r)
                break
    return exact_hits or fuzzy_hits


def clickable_of(nodes, rec):
    """节点本身不可点时，往上找最近的可点祖先（点父容器才生效的列表项）。"""
    cur = rec
    guard = 0
    while cur is not None and guard < 12:
        if cur["clickable"] and cur["enabled"] and cur["center"]:
            return cur
        cur = nodes[cur["parent"]] if cur["parent"] is not None else None
        guard += 1
    return rec if rec["center"] else None


def format_tree(nodes, limit=40):
    rows = []
    for r in nodes[:limit]:
        mark = "▶" if r["clickable"] else " "
        cx, cy = r["center"] if r["center"] else (0, 0)
        rows.append(f"{mark} {label(r)[:36]:36} {r['cls'][:16]:16} @{cx},{cy}")
    return "\n".join(rows)


def packages(serial, third_only=True):
    rc, txt = shell(serial, f"pm list packages {'-3' if third_only else ''}")
    if rc != 0:
        return []
    return sorted(l.split(":", 1)[1].strip() for l in txt.splitlines() if l.startswith("package:"))


def launch(serial, target):
    """启动应用或任意 intent。target 可为包名 / activity / action / URI。

    三种写法别混：包名走 monkey（最稳），activity 走 -n，action 走 -a，
    以大写段结尾的（android.settings.SETTINGS）才当 action，否则是包名。
    """
    t = target.strip()
    if t.startswith(("http://", "https://", "intent:", "market://", "tel:")):
        cmd = f"am start -a android.intent.action.VIEW -d '{t}'"
    elif "/" in t:
        cmd = f"am start -n {t}"
    elif t.rsplit(".", 1)[-1].isupper():
        cmd = f"am start -a {t}"
    else:
        cmd = f"monkey -p {t} -c android.intent.category.LAUNCHER 1"
    rc, txt = shell(serial, cmd)
    bad = ("Error", "No activities found", "monkey aborted", "does not exist")
    ok = rc == 0 and not any(b in txt for b in bad)
    # monkey 会把命令行参数逐个回显，全是噪音，只留结论行
    noise = ("bash arg:", " arg:", "data=", "args:", "## Network stats")
    keep = [l for l in txt.splitlines()
            if l.strip() and not l.strip().startswith(noise)]
    return {"ok": ok, "out": "\n".join(keep)}


def notifications(serial, limit=10):
    """读通知标题/来源。不点、不展开，纯只读。"""
    rc, txt = shell(serial, "dumpsys notification --noredact", timeout=90)
    if rc != 0:
        return []
    out = []
    pkg = ""
    for line in txt.splitlines():
        m = re.search(r"pkg=(\S+)", line)
        if m:
            pkg = m.group(1)
        m = re.search(r"android\.title=String \((.*)\)", line)
        if m and pkg:
            out.append({"pkg": pkg, "title": m.group(1)})
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- B 档：动
#
# 两条注入通道：
#   ① 无障碍桥（android_bridge/，首选）—— 走 dispatchGesture，不碰
#      InputManager.injectInputEvent，因此绕开 MIUI 的 INJECT_EVENTS 检查，
#      也就不需要插 SIM 卡去开「USB 调试（安全设置）」。
#   ② input 命令（次选）—— 只有 B 档开关真的开着才成功，否则报错。
# 每次动作都回传 via 字段，走哪条路是看得见的，不偷偷降级。

BRIDGE_LOCAL_PORT = 18765     # 电脑侧 adb forward 端口
BRIDGE_REMOTE_PORT = 8765     # 手机侧编队桥监听端口
BRIDGE_TOKEN_ENV = "FLEET_BRIDGE_TOKEN"


def bridge_token():
    """编队桥令牌：环境变量 > 构建产物里的 token.txt。都没有就返回空。"""
    tok = os.environ.get(BRIDGE_TOKEN_ENV, "").strip()
    if tok:
        return tok
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "android_bridge", "build", "token.txt")
    if os.path.isfile(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def bridge_cmd(serial, cmd, timeout=8):
    """经 adb forward 隧道给编队桥发一条指令。

    手机侧服务只听 127.0.0.1，隧道由 adb 建，所以不需要把端口暴露到局域网。
    """
    tok = bridge_token()
    if not tok:
        return {"ok": False, "err": "找不到编队桥令牌（先跑 android_bridge/build.sh）"}
    _run(serial, ["forward", f"tcp:{BRIDGE_LOCAL_PORT}", f"tcp:{BRIDGE_REMOTE_PORT}"])
    try:
        s = socket.create_connection(("127.0.0.1", BRIDGE_LOCAL_PORT), timeout=timeout)
    except OSError as e:
        return {"ok": False, "err": f"桥隧道连不上：{e}"}
    try:
        with s:
            f = s.makefile("rwb")

            def x(line):
                f.write((line + "\n").encode("utf-8"))
                f.flush()
                return f.readline().decode("utf-8", "replace").strip()

            auth = x("AUTH " + tok)
            if not auth.startswith("OK"):
                return {"ok": False, "err": f"鉴权失败：{auth}"}
            out = x(cmd)
            ok = out.startswith("OK") or out == "PONG"   # PING 的回执是 PONG
            return {"ok": ok, "out": out, "err": "" if ok else out}
    except OSError as e:
        return {"ok": False, "err": f"桥通信失败：{e}"}


def bridge_bulk(serial, cmd, timeout=25, max_bytes=1 << 20):
    """给桥发一条「多行回复」的指令（目前只有 UI），读到 END 为止。

    和 bridge_cmd 的区别：那条一问一答只用 readline 取一行，而 UI 树是多行，
    必须按 END 哨兵收尾，否则后面的行会残留在 socket 里污染下一次读取。
    """
    tok = bridge_token()
    if not tok:
        return {"ok": False, "err": "找不到编队桥令牌（先跑 android_bridge/build.sh）"}
    _run(serial, ["forward", f"tcp:{BRIDGE_LOCAL_PORT}", f"tcp:{BRIDGE_REMOTE_PORT}"])
    try:
        s = socket.create_connection(("127.0.0.1", BRIDGE_LOCAL_PORT), timeout=timeout)
    except OSError as e:
        return {"ok": False, "err": f"桥隧道连不上：{e}"}
    try:
        with s:
            f = s.makefile("rwb")

            def send(line):
                f.write((line + "\n").encode("utf-8"))
                f.flush()

            def read():
                raw = f.readline()
                if not raw:
                    return None
                return raw.decode("utf-8", "replace").rstrip("\r\n")

            send("AUTH " + tok)
            auth = read()
            if auth is None:
                return {"ok": False, "err": "桥无应答（服务可能刚被杀，重试一次）"}
            if not auth.startswith("OK"):
                return {"ok": False, "err": f"鉴权失败：{auth}"}

            send(cmd)
            head = read()
            if head is None:
                return {"ok": False, "err": "桥无应答"}
            if not head.startswith("OK ui"):
                # 单行回复（如 ERR …）或非 UI 指令
                return {"ok": head.startswith("OK"), "head": head,
                        "err": "" if head.startswith("OK") else head, "lines": []}

            lines, size = [], 0
            while True:
                ln = read()
                if ln is None:
                    return {"ok": False, "err": "桥在树传完之前断开（服务被杀或读超时）"}
                if ln == "END":
                    break
                lines.append(ln)
                size += len(ln.encode("utf-8")) + 1
                if size > max_bytes:
                    return {"ok": False,
                            "err": f"UI 树超过 {max_bytes} 字节仍未结束，桥侧截断可能失效"}
            parts = head.split()
            ms = -1
            if len(parts) > 4 and parts[4].endswith("ms"):
                try:
                    ms = int(parts[4][:-2])
                except ValueError:
                    pass
            pkg = parts[5][4:] if len(parts) > 5 and parts[5].startswith("pkg=") else ""
            return {"ok": True, "head": head, "lines": lines, "bytes": size,
                    "ms": ms, "pkg": pkg, "err": ""}
    except OSError as e:
        return {"ok": False, "err": f"桥通信失败：{e}"}


def bridge_ui(serial="", max_lines=150, timeout=20):
    """经编队桥读当前窗口 UI 树（同进程直读，毫秒级）。

    刻意**不做**自动回退到 uiautomator dump：两条路径的耗时差一个数量级，
    静默回退会让人以为桥在正常工作，实际每次都慢在 dump 上。
    要走 dump 的调用方自己去调 ui_xml()，那是显式选择。
    """
    return bridge_bulk(serial, f"UI {int(max_lines)}", timeout=timeout)



def can_inject(serial):
    """探 B 档是否已开（「USB 调试（安全设置）」）。用 KEYCODE_UNKNOWN 探，无副作用。"""
    rc, txt = shell(serial, "input keyevent 0")
    if "INJECT_EVENTS" in txt or "SecurityException" in txt:
        return False
    return rc == 0


def tap(serial, x, y):
    """点屏幕坐标。优先走无障碍桥；桥不可用才试 input，两条都不行就明确报错。"""
    r = bridge_cmd(serial, f"TAP {int(x)} {int(y)}")
    if r["ok"]:
        return {"ok": True, "via": "bridge", "out": r["out"]}
    rc, txt = shell(serial, f"input tap {int(x)} {int(y)}")
    if "INJECT_EVENTS" in txt:
        return {"ok": False, "via": "none",
                "err": "无障碍桥不可用，且 input 缺 INJECT_EVENTS："
                       "需装编队桥并在无障碍里授权一次（或不插卡时用 USB 调试安全设置）",
                "bridge_err": r.get("err", "")}
    return {"ok": rc == 0 and not txt.strip(), "via": "input", "out": txt.strip()}


def tap_text(serial, keyword, dry_run=False):
    """按屏幕上的文字点。找不到不瞎点，直接报找不到。

    dry_run=True 只算落点不点屏（对着不认识的界面先看清楚）。
    """
    got = ui_xml(serial)
    if not got["ok"]:
        return {"ok": False, "err": got["err"]}
    nodes = parse_ui(got["xml"])
    hits = find(nodes, keyword)
    if not hits:
        near = [label(r) for r in nodes if r["text"] or r["desc"]]
        return {"ok": False, "err": f"界面上找不到「{keyword}」", "see": near[:15]}
    target = clickable_of(nodes, hits[0])
    if not target or not target["center"]:
        return {"ok": False, "err": f"「{label(hits[0])}」没有可点区域"}
    x, y = target["center"]
    if dry_run:
        return {"ok": True, "dry_run": True, "x": x, "y": y, "hit": label(target)}
    r = tap(serial, x, y)
    r.update({"x": x, "y": y, "hit": label(target)})
    return r


def swipe(serial, x1, y1, x2, y2, ms=300):
    r = bridge_cmd(serial, f"SWIPE {x1} {y1} {x2} {y2} {int(ms)}")
    if r["ok"]:
        return {"ok": True, "via": "bridge"}
    rc, txt = shell(serial, f"input swipe {x1} {y1} {x2} {y2} {ms}")
    if "INJECT_EVENTS" in txt:
        return {"ok": False, "via": "none", "err": "无障碍桥不可用，input 也被拦",
                "bridge_err": r.get("err", "")}
    return {"ok": rc == 0, "via": "input"}


def text_input(serial, text):
    """往当前焦点输入文字。

    桥走 ACTION_SET_TEXT，中文直接可用；input text 只认 ASCII，中文会乱。
    """
    r = bridge_cmd(serial, f"TEXT {text}")
    if r["ok"]:
        return {"ok": True, "via": "bridge"}
    if any(ord(c) > 127 for c in text):
        return {"ok": False, "via": "none",
                "err": "无障碍桥不可用，input text 传不了中文", "bridge_err": r.get("err", "")}
    rc, txt = shell(serial, f"input text '{text.replace(' ', '%s')}'")
    if "INJECT_EVENTS" in txt:
        return {"ok": False, "via": "none", "err": "无障碍桥与 input 都不可用"}
    return {"ok": rc == 0, "via": "input"}


def wait_foreground(serial, want, timeout=10.0):
    """等前台切到某个包/activity（上一步动作到底生效没有，别靠猜）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, txt = shell(serial, "dumpsys window | grep mCurrentFocus")
        if want in txt:
            return {"ok": True, "out": txt.strip()}
        time.sleep(0.4)
    return {"ok": False, "err": f"{timeout}s 内前台没切到 {want}"}


# ---------------------------------------------------------------- 离线自测

_TREE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout"
        bounds="[0,0][1080,2400]" clickable="true">
    <node index="0" text="" class="android.widget.LinearLayout"
          bounds="[0,1000][1080,1200]" clickable="false">
      <node index="0" text="\u8bbe\u7f6e" class="android.widget.TextView"
            bounds="[40,1020][300,1180]" clickable="false"/>
      <node index="1" text="" content-desc="\u9000\u683c" class="android.widget.ImageView"
            bounds="[860,1020][1040,1180]" clickable="false"/>
    </node>
  </node>
  <node index="1" text="3,000\u00d77= 21,000" class="android.widget.TextView"
        bounds="[0,700][1080,850]" clickable="false"/>
  <node index="2" text="7" class="android.widget.TextView"
        bounds="[180,2070][306,2196]" clickable="true"/>
</hierarchy>"""


def _selftest():
    # ① bounds 解析：正常、空、垃圾
    assert parse_bounds("[0,0][1080,2400]") == (0, 0, 1080, 2400)
    assert parse_bounds("") is None
    assert parse_bounds("bounds?") is None

    nodes = parse_ui(_TREE_XML)
    texts = [r["text"] for r in nodes]
    # ② 布局壳子被过滤：只剩 可点根 + 设置 + 退格 + 结果文本 + 数字键
    assert len(nodes) == 5, texts
    assert "\u8bbe\u7f6e" in texts
    assert any(r["desc"] == "\u9000\u683c" for r in nodes)

    # ③ parent 重映射：中间层（LinearLayout）被过滤后要能上溯到可点祖先
    she = [r for r in nodes if r["text"] == "\u8bbe\u7f6e"][0]
    tui = [r for r in nodes if r["desc"] == "\u9000\u683c"][0]
    assert she["parent"] == 0 and tui["parent"] == 0, (she["parent"], tui["parent"])
    target = clickable_of(nodes, she)
    assert target["cls"] == "FrameLayout", target
    assert target["center"] == (540, 1200), target["center"]

    # ④ 精确优先：'7' 不能命中 '3,000\u00d77= 21,000'
    hit = find(nodes, "7")
    assert hit and hit[0]["text"] == "7", [r["text"] for r in hit]
    # 模糊兜底仍然可用
    fuzzy = find(nodes, "21,000")
    assert fuzzy and "21,000" in fuzzy[0]["text"], fuzzy

    # ⑤ 找不到就是找不到，不返回空落点
    assert find(nodes, "\u4e0d\u5b58\u5728\u7684\u5b57") == []

    # ⑥ 中心点、标签回退、树渲染
    assert nodes[-1]["center"] == (243, 2133), nodes[-1]
    assert label({"text": "", "desc": "", "cls": "Button"}) == "Button"
    assert "7" in format_tree(nodes)

    # ⑦ 不可点节点原样兜底（点自身范围），不抛异常
    alone = parse_ui("""<hierarchy><node text="x"
        bounds="[10,10][20,20]" clickable="false"/></hierarchy>""")
    assert clickable_of(alone, alone[0])["center"] == (15, 15)

    # ⑧ 桥没令牌时必须明确失败，不能假装点成了
    real = globals()["bridge_token"]
    globals()["bridge_token"] = lambda: ""
    try:
        r = bridge_cmd("fake-serial", "PING")
        assert r["ok"] is False and "令牌" in r["err"], r
    finally:
        globals()["bridge_token"] = real

    print("android_device \u81ea\u6d4b\uff1a8 \u9879\u5168\u8fc7")


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Android 被控端驱动（手机零安装）")
    ap.add_argument("--serial", default="", help="设备序列号，默认自动挑无线调试那台")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("devices", help="列出设备")
    sub.add_parser("info", help="硬件与状态画像")
    sub.add_parser("cap", help="探测 A/B 两档能力")

    p = sub.add_parser("shot", help="截屏到本地")
    p.add_argument("path")

    p = sub.add_parser("ui", help="列 UI 树（默认走 uiautomator dump；--bridge 走编队桥，毫秒级）")
    p.add_argument("keyword", nargs="?", default="")
    p.add_argument("--bridge", action="store_true",
                   help="经编队桥直读（带层级、毫秒级）；需要桥已授权且 adb forward 可用")
    p.add_argument("--max", type=int, default=150, help="--bridge 时最多输出多少行")

    p = sub.add_parser("launch", help="启动应用/intent")
    p.add_argument("target")

    sub.add_parser("packages", help="列第三方应用")
    sub.add_parser("notify", help="读通知标题")
    sub.add_parser("attach", help="自动接入（掉线自愈，含桥探活）")

    p = sub.add_parser("tap", help="按文字点击（--dry 只算落点）")
    p.add_argument("keyword")
    p.add_argument("--dry", action="store_true")

    p = sub.add_parser("pair", help="用配对码配对")
    p.add_argument("addr")
    p.add_argument("code")

    p = sub.add_parser("connect", help="连接无线调试端口")
    p.add_argument("addr")

    p = sub.add_parser("bridge", help="给编队桥发原始指令（PING/TAP/SWIPE/TEXT/FOCUS/BACK…）")
    p.add_argument("raw", nargs="+")

    args = ap.parse_args(argv)
    serial = args.serial or pick_serial()

    if args.cmd == "attach":
        r = attach(args.serial or "")
        print(json.dumps(r, ensure_ascii=False, indent=2))
        if r["ok"]:
            br = bridge_cmd(r["serial"], "PING")
            print("桥      ", "PONG（无障碍通道可用）" if br["ok"] else f"不可用：{br.get('err')}")
        return 0 if r["ok"] else 1

    if not serial and args.cmd not in ("pair", "connect"):
        # 掉线了先自己找回来再干活，不用人插手
        r = attach()
        if r["ok"]:
            serial = r["serial"]
            print(f"[自动接入] {serial}（via {r['via']}）", file=sys.stderr)

    if args.cmd in (None, "devices"):
        ds = devices()
        if not ds:
            print("没有设备。手机开无线调试后用 pair/connect 接入。")
        for d in ds:
            print(f"{d['serial']:40} {d['state']:8} model={d['model'] or '?'} "
                  f"{'无线' if d['wireless'] else '有线'}")
        return 0
    if not serial and args.cmd not in ("pair", "connect"):
        print("没有在线设备", file=sys.stderr)
        return 2

    if args.cmd == "info":
        for k, v in info(serial).items():
            print(f"{k:14} {v}")
    elif args.cmd == "cap":
        a = ui_xml(serial)
        b = can_inject(serial)
        br = bridge_cmd(serial, "PING")
        print("设备        ", serial)
        print("A 档读屏    ", "OK" if a["ok"] else f"FAIL {a.get('err')}")
        print("A 档启动    ", "OK" if launch(serial, "android.settings.SETTINGS")["ok"] else "FAIL")
        print("B 档无障碍桥", "OK（可点、可滑、可输入中文）" if br["ok"]
              else f"不可用：{br.get('err')}")
        print("B 档 input  ", "OK（USB 调试安全设置已开）" if b
              else "被 MIUI 拦（无 SIM 卡时这是常态，不影响上面那条）")
    elif args.cmd == "bridge":
        r = bridge_cmd(serial, " ".join(args.raw))
        print(r.get("out") or r.get("err"))
    elif args.cmd == "shot":
        r = shot(serial, args.path)
        print(r.get("path") or r.get("err"))
    elif args.cmd == "ui":
        if args.bridge:
            got = bridge_ui(serial, max_lines=args.max)
            if not got["ok"]:
                print(got["err"], file=sys.stderr)
                return 1
            print(f"包名 {got['pkg'] or '?'} · {len(got['lines'])} 行 · "
                  f"{got['bytes']} 字节 · 服务端遍历 {got['ms']}ms")
            for ln in got["lines"]:
                if args.keyword and args.keyword not in ln:
                    continue
                print(ln)
            return 0
        got = ui_xml(serial)
        if not got["ok"]:
            print(got["err"], file=sys.stderr)
            return 1
        nodes = parse_ui(got["xml"])
        if args.keyword:
            nodes = find(nodes, args.keyword)
        print(f"共 {len(nodes)} 个可见节点")
        print(format_tree(nodes))
    elif args.cmd == "launch":
        print(launch(serial, args.target))
    elif args.cmd == "packages":
        for p_ in packages(serial):
            print(p_)
    elif args.cmd == "notify":
        for n in notifications(serial):
            print(f"{n['pkg']:32} {n['title']}")
    elif args.cmd == "tap":
        print(tap_text(serial, args.keyword, dry_run=args.dry))
    elif args.cmd == "pair":
        print(pair(args.addr, args.code))
    elif args.cmd == "connect":
        print(connect(args.addr))
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
