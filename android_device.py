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


_PROFILES = {}


def profile(serial, timeout=10):
    """一台设备的「身份指纹」：{model, board, mem_mb, phone}。

    为什么需要它：adb 列表里有手机也有电视盒子/广告机（RK3568 那种 2GB 板子，
    开机吃掉 1.7GB）。两者 `adb shell` 都能通、截图点按都不报错，光看「连上了」
    分不出谁真能干算力活。`android.hardware.telephony` 是手机与盒子最稳的分界。

    一次 shell 走完四项（无线调试下每次往返 200–400ms，分四次问就慢了）。
    """
    if serial in _PROFILES:
        return _PROFILES[serial]
    cmd = ("getprop ro.product.model; getprop ro.board.platform; "
           "grep MemTotal /proc/meminfo; "
           # grep -c 计数为 0 时退出码是 1，会被当成整条命令失败 ⇒ 用 || echo 0 兜住
           "pm list features 2>/dev/null | grep -c android.hardware.telephony || echo 0")
    rc, txt = shell(serial, cmd, timeout=timeout)
    p = {"model": "", "board": "", "mem_mb": 0, "phone": False, "ok": rc == 0}
    if rc == 0:
        lines = [x.strip() for x in txt.replace("\r", "").splitlines() if x.strip()]
        if lines:
            p["model"] = lines[0]
        if len(lines) > 1:
            p["board"] = lines[1]
        for x in lines:
            if x.startswith("MemTotal"):
                digits = "".join(ch for ch in x if ch.isdigit())
                if digits:
                    p["mem_mb"] = int(digits) // 1024
        if lines and lines[-1].isdigit():
            p["phone"] = int(lines[-1]) > 0
    _PROFILES[serial] = p
    return p


def pick_serial(prefer_wireless=True, prefer=None):
    """挑一台给编队用的设备。没有就返回空。

    为什么不「取第一台」：同时接着手机和一块安卓板子时，adb 列表顺序取决于谁先
    连上。按顺序取会把编队绑到最没用的那台上——而且它能截图能点按，不会报错，
    活照派、慢十倍、还查不出原因。这种错最贵。

    优先级（高 → 低）：
      ① 显式指定：prefer 参数 或 环境变量 FLEET_ANDROID_SERIAL（可给完整序列/子串/型号名）
      ② 有电话功能的真手机（盒子/一体机/广告机没有 telephony）
      ③ 无线调试的（手机常态是无线）
      ④ 还是平手就维持 adb 原顺序（绝不因为分不清就报错）
    """
    ds = [d for d in devices() if d["state"] == "device"]
    if not ds:
        return ""

    want = (prefer or os.environ.get("FLEET_ANDROID_SERIAL") or "").strip()
    if want:
        def keys(d):
            return (d["serial"], d["model"], d["device"], d["product"])
        for d in ds:                                  # 先整字命中（避免子串歧义）
            if want in keys(d):
                return d["serial"]
        for d in ds:                                  # 再放宽到子串（rk3568 / 25060 这种半截名）
            if any(want in str(k) for k in keys(d)):
                return d["serial"]

    if len(ds) == 1:
        return ds[0]["serial"]

    def rank(i_d):
        i, d = i_d
        p = profile(d["serial"])
        return (0 if p["phone"] else 1,          # 真手机优先
                0 if (d["wireless"] or not prefer_wireless) else 1,
                i)                                # 最后才看原顺序，保证确定性
    return sorted(enumerate(ds), key=rank)[0][1]["serial"]


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
BRIDGE_SVC = "app.fleet.bridge/app.fleet.bridge.BridgeService"


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


def bridge_health(serial):
    """无障碍服务在没在系统里登记着（不是问它活没活，是问系统认不认它）。

    为什么要单独查：小米/HyperOS 这类 ROM 会在重启后把「侧载应用的无障碍服务」
    静默关掉——`pm list packages` 里有、App 也在，但系统不再往它派事件，
    表现就是「桥连上了、鉴权回空」。这不是坏了，是被关掉了，写回去就能好。
    """
    rc, txt = shell(serial, "settings get secure enabled_accessibility_services")
    on = BRIDGE_SVC in (txt or "")
    return {"ok": rc == 0, "enabled": on, "raw": (txt or "").strip()}


def bridge_ensure(serial, wait=1.6):
    """把无障碍服务写回系统设置（幂等）。返回是否「本来就在」。

    只写设置、不弹 UI：shell 有 WRITE_SECURE_SETTINGS，一步到位，
    不需要人去「设置 → 无障碍」里点。写不进去（ROM 限制）就返回 False，
    由上层如实报「需要手动开一次」。
    """
    h = bridge_health(serial)
    if h.get("enabled"):
        return {"ok": True, "healed": False, "why": "本来就在"}
    shell(serial, f"settings put secure enabled_accessibility_services {BRIDGE_SVC}")
    shell(serial, "settings put secure accessibility_enabled 1")
    time.sleep(wait)
    h2 = bridge_health(serial)
    return {"ok": bool(h2.get("enabled")), "healed": True,
            "why": "已写回" if h2.get("enabled") else f"写不进去：{h2.get('raw','')[:60]}"}


def bridge_cmd(serial, cmd, timeout=8):
    """经 adb forward 隧道给编队桥发一条指令。

    手机侧服务只听 127.0.0.1，隧道由 adb 建，所以不需要把端口暴露到局域网。

    失败时自愈一次：先查无障碍服务有没有被 ROM 关掉，没有就写回去再重试。
    加了这层之后，「重启后中文打不进去」这类事故会自己好，不用人再去设置里点。
    """
    r = _bridge_once(serial, cmd, timeout)
    if r.get("ok"):
        return r
    heal = bridge_ensure(serial)
    if not heal.get("ok"):
        r["err"] = f"{r.get('err','')}（无障碍服务没开，自动写回也失败：{heal.get('why')}）"
        return r
    if not heal.get("healed"):
        time.sleep(0.6)          # 服务在但不是没响应，给一次喘息再问
    r2 = _bridge_once(serial, cmd, timeout)
    if r2.get("ok"):
        r2["healed"] = heal.get("healed")
        return r2
    r2.setdefault("err", "")
    r2["err"] = f"{r2['err']}（自愈已尝试：{heal.get('why')}）"
    return r2


def _bridge_once(serial, cmd, timeout=8):
    """单次尝试，不带任何自愈逻辑（自愈放在 bridge_cmd 里，避免递归爆炸）。"""
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
            # header 形如：OK ui <keep> <bytes> <ms>ms pkg=<pkg> n=<遍历节点数> root=<取根ms>ms
            # 按 key=value 取，不按下标取——以后桥再加字段不会把解析打歪。
            def kv(key, cast=int, default=-1):
                m = re.search(rf"(?:^|\s){key}=(\d+(?:ms)?)", head)
                if not m:
                    return default
                v = m.group(1)
                return cast(v[:-2]) if v.endswith("ms") else cast(v)

            ms = kv("ms", int, -1)
            if ms < 0:                                  # 兼容旧 header：第 5 个字段就是 3ms 这种
                parts = head.split()
                if len(parts) > 4 and parts[4].endswith("ms"):
                    try:
                        ms = int(parts[4][:-2])
                    except ValueError:
                        pass
            m = re.search(r"(?:^|\s)pkg=(\S+)", head)
            pkg = m.group(1) if m else ""
            return {"ok": True, "head": head, "lines": lines, "bytes": size,
                    "ms": ms, "pkg": pkg,
                    "nodes": kv("n", int, 0), "root_ms": kv("root", int, -1), "err": ""}
    except OSError as e:
        return {"ok": False, "err": f"桥通信失败：{e}"}


def bridge_ui(serial="", max_lines=150, timeout=20):
    """经编队桥读当前窗口 UI 树（同进程直读，毫秒级）。

    刻意**不做**自动回退到 uiautomator dump：两条路径的耗时差一个数量级，
    静默回退会让人以为桥在正常工作，实际每次都慢在 dump 上。
    要走 dump 的调用方自己去调 ui_xml()，那是显式选择。
    """
    return bridge_bulk(serial, f"UI {int(max_lines)}", timeout=timeout)


# ---------------------------------------------------------------- 桥树解析
#
# 桥渲染的每一行长这样（两空格缩进表父子，▶可点 ◢不可点）：
#   ▶[Button]T:"发送"ID:btn_send⮞[100,200][300,400]
# 不编序号是刻意的：序号只在一次快照内有效，跨快照即失效，容易诱发
# "拿旧序号点错位置"。寻址只认 id / 文字 / bounds 这三样真实存在的属性。

_RE_UI_LINE = re.compile(
    r'\[(?P<cls>[^\]]*)\]'
    r'(?:T:"(?P<text>[^"]*)")?'
    r'(?:D:"(?P<desc>[^"]*)")?'
    r'(?:ID:(?P<id>\S+?))?'
    r'⮞\[(?P<l>-?\d+),(?P<t>-?\d+)\]\[(?P<r>-?\d+),(?P<b>-?\d+)\]'
)


def parse_bridge_ui(lines):
    """桥的渲染行 -> 结构化节点表（含 depth，供缩进还原层级）。

    行里认不出来的（截断提示行、空行）直接跳过，不做静默猜测。
    """
    nodes = []
    for raw in lines:
        if not raw.strip():
            continue
        depth = (len(raw) - len(raw.lstrip(" "))) // 2
        m = _RE_UI_LINE.search(raw)
        if not m:
            continue
        b = (int(m["l"]), int(m["t"]), int(m["r"]), int(m["b"]))
        nodes.append({
            "depth": depth,
            "clickable": raw.lstrip()[:1] == "▶",
            "cls": m["cls"],
            "text": m["text"] or "",
            "desc": m["desc"] or "",
            "id": m["id"] or "",
            "bounds": b,
            "center": ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2),
        })
    return nodes


def bridge_screen(serial="", max_lines=200, timeout=20):
    """一步拿到「前台包 + 结构化节点表」，比 ui_xml + parse_ui 快一个数量级。"""
    got = bridge_ui(serial, max_lines=max_lines, timeout=timeout)
    if not got["ok"]:
        return {"ok": False, "err": got.get("err", ""), "nodes": [], "pkg": ""}
    return {"ok": True, "err": "", "pkg": got.get("pkg", ""),
            "nodes": parse_bridge_ui(got["lines"]),
            "ms": got.get("ms", -1), "bytes": got.get("bytes", 0)}


# ---------------------------------------------------------------- 元素级动作（P1）
#
# 和坐标动作（tap/swipe）的区别：调用方只说"哪个元素"，坐标由设备侧解析。
# 界面一变，坐标动作得全部重算；元素动作不用。而且模型说得清"点发送"，
# 说不清"点 1024,2388"。
#
# 选择器：id:xxx / text:xxx / desc:xxx / bounds:[l,t][r,b]，可加 #n 消歧。
# 桥侧找不到会明确回 not-found + 界面候选，匹配多个回 ambiguous + 候选列表，
# 一律不静默。

def _sel(kind, value, index=0):
    v = str(value)
    if not v.strip():
        raise ValueError("选择器不能为空")
    # 值里的 # 只有在末尾跟数字时才当消歧序号，其余原样保留
    return f"{kind}:{v}" + (f"#{int(index)}" if index else "")


def bridge_tap_el(serial, kind, value, index=0):
    """按元素点一下。失败时 err 是桥给的原话（找不到/歧义/越界/不可见）。"""
    r = bridge_cmd(serial, "TAPEL " + _sel(kind, value, index))
    if not r["ok"]:
        return {"ok": False, "via": "bridge", "err": r.get("err", ""), "sel": kind + ":" + str(value)}
    head = "OK tapel "
    rest = r["out"][len(head):] if r["out"].startswith(head) else ""
    p = rest.split(" ", 2)
    if len(p) < 2:
        return {"ok": False, "via": "bridge", "err": f"桥的回报看不懂：{r['out'][:80]}"}
    return {"ok": True, "via": "bridge", "x": int(p[0]), "y": int(p[1]),
            "hit": p[2] if len(p) > 2 else "", "err": ""}


def bridge_set_text(serial, kind, value, text, index=0):
    """按元素设文本（ACTION_SET_TEXT）。

    文本里若含 '|' 会和选择器分隔符打架，按桥的约定转义成 %7C；'%' 先转 %25，
    否则一个原始 %7C 会被误还原。
    """
    safe = str(text).replace("%", "%25").replace("|", "%7C")
    r = bridge_cmd(serial, "SETTEXT " + _sel(kind, value, index) + "|" + safe)
    if not r["ok"]:
        return {"ok": False, "via": "bridge", "err": r.get("err", "")}
    return {"ok": True, "via": "bridge", "hit": r["out"][len("OK settext "):], "err": ""}


def bridge_set_seek(serial, kind, value, percent, index=0):
    """把滑条设到控件自身区间的 percent%（0~100），走 ACTION_SET_PROGRESS。

    注意语义：这是**控件区间的百分比**，不等于它控制的东西的百分比。
    实测 MIUI 亮度条 seekbar 72% ≈ 系统亮度 30%（0~255 的 76），因为中间有伽马映射。
    要"亮度 30%"就得设 seekbar 72%，别按 30% 传。
    """
    r = bridge_cmd(serial, "SETSEEK " + _sel(kind, value, index) + "|" + str(int(percent)))
    if not r["ok"]:
        return {"ok": False, "via": "bridge", "err": r.get("err", "")}
    out = r["out"][len("OK setseek "):]
    # 形如：30%=4915 [SeekBar]D:"屏幕亮度"ID:...
    pct_part, _, rest = out.partition(" ")
    raw = pct_part.split("=")[1] if "=" in pct_part else ""
    return {"ok": True, "via": "bridge", "raw": raw, "hit": rest, "err": ""}


def bridge_scroll(serial, direction):
    """滚动。direction: up/down/left/right。

    走手势滑动而不是 ACTION_SCROLL_FORWARD —— 后者实测会返回 true 但界面不动
    （详见桥里 SCROLL 那段注释）。
    """
    d = str(direction).strip().lower()
    if d not in ("up", "down", "left", "right"):
        return {"ok": False, "err": f"方向只能是 up/down/left/right，收到 {direction!r}"}
    r = bridge_cmd(serial, "SCROLL " + d)
    if not r["ok"]:
        return {"ok": False, "via": "bridge", "err": r.get("err", "")}
    return {"ok": True, "via": "bridge", "hit": r["out"][len("OK scroll " + d + " "):], "err": ""}




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


# ---------------------------------------------------------------- 传感 / 相机（Termux:API）

def camera_photo(serial, remote_path=None, camera_id=0, timeout=60):
    """用 Termux:API 拍一张照（需手机装 Termux:API 插件）。

    走 `termux-camera-photo -c {id} -f {path}`（adb shell）。文件留在手机上，
    要再 pull 到本机（见 pull()）才能喂模型。返回 (ok, remote_path | err)。
    """
    rp = remote_path or f"/sdcard/DCIM/fleet_see_{int(time.time())}.jpg"
    rc, txt = shell(serial, f"termux-camera-photo -c {int(camera_id)} -f {rp}")
    if rc != 0:
        return False, (txt.strip()[:200]
                       or "termux-camera-photo 无输出（Termux:API 未装？）")
    return True, rp


def pull(serial, remote, local, timeout=90):
    """从手机拉文件到本机（adb pull，只读传输，不过红线守卫）。返回 (ok, local | err)。"""
    os.makedirs(os.path.dirname(os.path.abspath(local)), exist_ok=True)
    rc, txt = _run(serial, ["pull", remote, local], timeout=timeout)
    if rc != 0:
        return False, f"pull 失败(rc={rc}): {txt.strip()[:200]}"
    return True, local


def sensor_read(serial, types="ambient_light,light,game_rotation_vector,step_counter,"
                              "accelerometer,magnetic_field,proximity",
                delay=1, timeout=60):
    """读手机传感器（Termux:API termux-sensor）。返回 (ok, list_of_readings | err)。

    termux-sensor 输出是 JSON 数组，逐元素形如
    {"type":"...","values":[...],"accuracy":..}。-n 1 采一次即可；
    delay 是采样前的稳定等待秒。
    """
    cmd = f"termux-sensor -s {types} -d {int(delay)} -n 1"
    rc, txt = shell(serial, cmd, timeout=timeout)
    if rc != 0:
        return False, (txt.strip()[:200]
                       or "termux-sensor 无输出（Termux:API 未装？）")
    return True, _parse_sensor_json(txt)


def _parse_sensor_json(txt):
    """termux-sensor 会在 JSON 前后塞日志/stderr，这里尽力抽出第一个 JSON 数组。"""
    txt = txt.replace("\r", "")
    start = txt.find("[")
    if start < 0:
        return []
    depth, end = 0, -1
    for i in range(start, len(txt)):
        if txt[i] == "[":
            depth += 1
        elif txt[i] == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return []
    try:
        return json.loads(txt[start:end + 1])
    except ValueError:
        return []


def battery_json(serial, timeout=30):
    """读手机电池状态（Termux:API termux-battery-status）。返回 (ok, dict | err)。"""
    rc, txt = shell(serial, "termux-battery-status", timeout=timeout)
    if rc != 0:
        return False, f"电池状态读取失败(rc={rc}): {txt.strip()[:200]}"
    try:
        return True, json.loads(txt)
    except ValueError:
        return False, f"电池状态非 JSON: {txt.strip()[:200]}"


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

    # ⑧ 桥渲染行 -> 结构化节点：层级、可点、ID、bounds 一个都不能丢
    ui = parse_bridge_ui([
        "\u25b6[FrameLayout]\u2b9e[1,1][1280,2771]",
        "  \u25b6[Button]T:\"\u53d1\u9001\"ID:btn_send\u2b9e[100,200][300,400]",
        "  \u25c2[TextView]D:\"\u641c\u7d22\"\u2b9e[0,0][10,10]",
        "\u2026 \u5df2\u622a\u65ad 3 \u884c",          # 非节点行，必须被跳过
    ])
    assert len(ui) == 3, ui
    assert ui[0]["depth"] == 0 and ui[0]["clickable"] is True
    assert ui[1]["depth"] == 1 and ui[1]["text"] == "\u53d1\u9001"
    assert ui[1]["id"] == "btn_send" and ui[1]["center"] == (200, 300)
    assert ui[2]["clickable"] is False and ui[2]["desc"] == "\u641c\u7d22"
    # 只有 ID 没有文字的行也要解析出来（图标按钮常见）
    only_id = parse_bridge_ui(["\u25b6[ImageView]ID:app:id/ic_menu\u2b9e[0,0][96,96]"])
    assert only_id and only_id[0]["id"] == "app:id/ic_menu", only_id

    # ⑧ 桥没令牌时必须明确失败，不能假装点成了
    real = globals()["bridge_token"]
    globals()["bridge_token"] = lambda: ""
    try:
        r = bridge_cmd("fake-serial", "PING")
        assert r["ok"] is False and "令牌" in r["err"], r
    finally:
        globals()["bridge_token"] = real

    # ⑨ 选设备：手机优先于盒子——这是「2GB 板子排前面就把活全派给砖头」的防线
    _real_devices, _real_shell = globals()["devices"], globals()["shell"]
    _real_env = os.environ.pop("FLEET_ANDROID_SERIAL", None)

    def _fake_devices(_rows):
        return lambda: _rows

    def _fake_shell(_prof):
        def f(serial, cmd, timeout=60):
            p = _prof.get(serial, {})
            if not p:
                return 1, ""
            txt = "%s\n%s\nMemTotal:       %d kB\n%d\n" % (
                p.get("model", ""), p.get("board", ""),
                p.get("mem_mb", 0) * 1024, 1 if p.get("phone") else 0)
            return 0, txt
        return f

    BOARD = {"serial": "192.168.0.105:5555", "state": "device", "model": "SYKJ_18C",
             "product": "rk3568_d", "device": "rk3568_d", "wireless": True}
    PHONE = {"serial": "192.168.137.40:5555", "state": "device", "model": "25060RK16C",
             "product": "dali", "device": "dali", "wireless": True}
    prof = {BOARD["serial"]: {"model": "SYKJ_18C", "board": "rk356x", "mem_mb": 1957, "phone": False},
            PHONE["serial"]: {"model": "25060RK16C", "board": "kalama", "mem_mb": 11285, "phone": True}}
    try:
        _PROFILES.clear()
        # 板子排在前面（这正是 adb 的真实顺序），仍然要选手机
        globals()["devices"], globals()["shell"] = _fake_devices([BOARD, PHONE]), _fake_shell(prof)
        assert pick_serial() == PHONE["serial"], "只连一台手机一台盒子时必须选手机"
        # 显式指定能翻盘（就要那块板子也得听得懂）
        assert pick_serial(prefer="SYKJ") == BOARD["serial"]
        assert pick_serial(prefer=BOARD["serial"]) == BOARD["serial"]
        assert pick_serial(prefer="25060") == PHONE["serial"]
        # 环境变量同权，且优先于自动排序
        os.environ["FLEET_ANDROID_SERIAL"] = "rk3568"
        assert pick_serial() == BOARD["serial"], "FLEET_ANDROID_SERIAL 没生效"
        os.environ.pop("FLEET_ANDROID_SERIAL")
        # 只有一台：不去 profile 也直接给（少一次往返）
        globals()["shell"] = _fake_shell({})           # 任何 shell 都失败
        globals()["devices"] = _fake_devices([BOARD])
        assert pick_serial() == BOARD["serial"]
        # 一台都没有：空串，不抛
        globals()["devices"] = _fake_devices([])
        assert pick_serial() == ""
        # 两台都是盒子：维持原顺序（分不清就按列表来，不瞎猜）
        _PROFILES.clear()
        globals()["devices"], globals()["shell"] = _fake_devices([BOARD, PHONE]), _fake_shell(
            {BOARD["serial"]: {"phone": False}, PHONE["serial"]: {"phone": False}})
        assert pick_serial() == BOARD["serial"]
    finally:
        globals()["devices"], globals()["shell"] = _real_devices, _real_shell
        _PROFILES.clear()
        if _real_env is not None:
            os.environ["FLEET_ANDROID_SERIAL"] = _real_env

    print("android_device \u81ea\u6d4b\uff1a9 \u9879\u5168\u8fc7")


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
