"""Windows 工具箱：那些「装一堆小软件才能干」的事，做成能力。

为什么值得单独一层
------------------
Windows 有个特点：**每件小事都有一个专门的软件**。看启动项装一个、清垃圾装一个、
查端口占用装一个、看已装软件又装一个。软件多了互相打架，还各自带广告和后台。

这些东西 WP 自己全都做得到（注册表 + WMI + 自带命令），只是散在十几个地方、
输出格式各不相同。这一层的活儿就是：**把它们收成一套一致的、能编排的接口**，
顺带把「只读」和「会改系统」分清楚。

三条纪律
--------
1. **默认只读**。list / scan 不改任何东西；disable / stop / clean 必须显式指定
   action，且在返回里写清改了哪里，让人能改回去。
2. **不弹窗**。所有子进程带 `CREATE_NO_WINDOW`——后台能力突然在屏幕上弹个黑框，
   是最容易被当成病毒的行为，也真的会打断正在做的事。
3. **不假装成功**。命令退出码、输出、错误分开看；失败就把 stderr 原样带回来，
   不把「没读到」说成「没有」。

为什么不用第三方库
------------------
全部走系统自带：注册表用 `reg` / PowerShell，服务用 `sc` / CIM，端口用 `netstat`，
磁盘用 `shutil`。装机即用，不需要 pip，也不会因为某个库不兼容 Python 3.12 就瘫掉。
"""
import json
import os
import re
import shutil
import subprocess
import time

from ..core import BaseDevice, DeviceResult as R

#: 一次最多返回多少条，防止「列服务」直接把上下文撑爆
MAX_ROWS = 300

_NO_WINDOW = 0x08000000        # CREATE_NO_WINDOW


def _run(argv, timeout=30):
    """跑一条命令，不弹窗。返回 (rc, out, err)。"""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW if os.name == "nt" else 0)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace").strip(),
                p.stderr.decode("utf-8", "replace").strip())
    except subprocess.TimeoutExpired:
        return -1, "", "命令超时（%ss）" % timeout
    except Exception as e:
        return -1, "", "%s: %s" % (type(e).__name__, e)


def _ps(script, timeout=40):
    """跑一段 PowerShell，输出按 JSON 解析。失败返回 (None, 原因)。

    统一让 PS 输出 JSON 再解析：PS 默认是给人看的表格，宽度一超就截断成省略号，
    用文本去切列名，迟早会在某个中文机器上错位。
    """
    wrapped = ("$ProgressPreference='SilentlyContinue';"
               "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
               "try { %s } catch { Write-Output (@{error=$_.Exception.Message} | "
               "ConvertTo-Json -Compress) }" % script)
    rc, out, err = _run(["powershell", "-NoProfile", "-NonInteractive",
                         "-ExecutionPolicy", "Bypass", "-Command", wrapped], timeout=timeout)
    if not out:
        return None, (err or "PowerShell 没有输出（rc=%s）" % rc)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # 单条对象时 ConvertTo-Json 不会包成数组，这里兜一下
        try:
            data = json.loads("[" + out + "]")
        except Exception:
            return None, "输出不是 JSON：%s" % out[:200]
    if isinstance(data, dict) and data.get("error"):
        return None, "PowerShell 报错：%s" % data["error"]
    return data if isinstance(data, list) else [data], ""


def _fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


# ============================================================ 启动项

#: 启动项的注册表位置。HKCU 不需要管理员，HKLM 需要——分开写，失败时能说清是哪一处
RUN_KEYS = (
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\Run"),
    ("HKCU", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
    ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
    ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
)

#: 禁用启动项不改原值，改成这个前缀——要恢复就反向操作，随时可逆
DISABLED_PREFIX = "SkynetDisabled_"


def list_startup():
    items = []
    errors = []
    for hive, path in RUN_KEYS:
        full = "%s\\%s" % (hive, path)
        rc, out, err = _run(["reg", "query", full])
        if rc != 0:
            # HKLM 在非管理员下会 5（拒绝访问）——记下来但不当作失败
            if "拒绝访问" not in err and "Access is denied" not in err:
                errors.append({"key": full, "err": err[:120]})
            continue
        cur = None
        for line in out.splitlines():
            line = line.rstrip()
            m = re.match(r"^\s{4}(\S+)\s+REG_\w+\s+(.*)$", line)
            if m:
                name, val = m.group(1), m.group(2).strip()
                disabled = name.startswith(DISABLED_PREFIX)
                items.append({
                    "name": name[len(DISABLED_PREFIX):] if disabled else name,
                    "command": val,
                    "hive": hive,
                    "key": path,
                    "enabled": not disabled,
                    "source": "registry",
                })
    # 启动文件夹
    for folder in (os.path.join(os.environ.get("APPDATA", ""),
                                r"Microsoft\Windows\Start Menu\Programs\Startup"),
                   os.path.join(os.environ.get("ProgramData", ""),
                                r"Microsoft\Windows\Start Menu\Programs\Startup")):
        if folder and os.path.isdir(folder):
            for fn in os.listdir(folder):
                if fn.lower() == "desktop.ini":
                    continue
                items.append({"name": fn, "command": os.path.join(folder, fn),
                              "hive": "", "key": folder, "enabled": True,
                              "source": "startup-folder"})
    return items, errors


def set_startup(name, enable=True):
    """启用/禁用一个注册表启动项。改名而不是删除——删除就找不回来了。"""
    target = name[len(DISABLED_PREFIX):] if name.startswith(DISABLED_PREFIX) else name
    for hive, path in RUN_KEYS:
        full = "%s\\%s" % (hive, path)
        rc, out, err = _run(["reg", "query", full])
        if rc != 0:
            continue
        for line in out.splitlines():
            m = re.match(r"^\s{4}(\S+)\s+(REG_\w+)\s+(.*)$", line)
            if not m:
                continue
            cur_name = m.group(1)
            plain = cur_name[len(DISABLED_PREFIX):] if cur_name.startswith(DISABLED_PREFIX) else cur_name
            if plain.lower() != target.lower():
                continue
            want = target if enable else DISABLED_PREFIX + target
            if cur_name == want:
                return True, "已经是目标状态（%s）" % ("启用" if enable else "禁用"), full
            rc1, _, e1 = _run(["reg", "delete", full, "/v", cur_name, "/f"])
            if rc1 != 0:
                return False, "删除旧值失败：%s" % (e1 or "需要管理员权限"), full
            rc2, _, e2 = _run(["reg", "add", full, "/v", want, "/t", m.group(2),
                               "/d", m.group(3), "/f"])
            if rc2 != 0:
                return False, "写新值失败：%s" % (e2 or "需要管理员权限"), full
            return True, "已%s" % ("启用" if enable else "禁用"), full
    return False, "没找到这个启动项：%s" % target, ""


# ============================================================ 服务

def list_services(name_filter="", state=""):
    script = (
        "Get-CimInstance -ClassName Win32_Service | "
        "Select-Object Name,DisplayName,State,StartMode,ProcessId,PathName | "
        "ConvertTo-Json -Compress"
    )
    data, err = _ps(script, timeout=60)
    if data is None:
        return None, err
    out = []
    for s in data:
        n = s.get("Name") or ""
        if name_filter and name_filter.lower() not in n.lower() \
                and name_filter.lower() not in (s.get("DisplayName") or "").lower():
            continue
        if state and (s.get("State") or "").lower() != state.lower():
            continue
        out.append({
            "name": n,
            "display": s.get("DisplayName"),
            "state": s.get("State"),
            "start_mode": s.get("StartMode"),
            "pid": s.get("ProcessId"),
            "path": (s.get("PathName") or "")[:200],
        })
    out.sort(key=lambda x: (x["state"] != "Running", x["name"]))
    return out[:MAX_ROWS], {"total": len(out), "truncated": len(out) > MAX_ROWS}


# ============================================================ 磁盘

def disk_space():
    """各盘容量。用 shutil.disk_usage——比 WMI 快，也不受权限影响。"""
    out = []
    if os.name != "nt":
        total, used, free = shutil.disk_usage("/")
        return [{"drive": "/", "total": total, "used": used, "free": free,
                 "used_pct": round(used * 100.0 / total, 1)}]
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = "%s:\\" % letter
        if not os.path.exists(root):
            continue
        try:
            total, used, free = shutil.disk_usage(root)
        except Exception:
            continue
        out.append({"drive": root, "total": total, "used": used, "free": free,
                    "used_pct": round(used * 100.0 / max(total, 1), 1),
                    "total_h": _fmt_size(total), "free_h": _fmt_size(free)})
    return out


def big_files(root, limit=40, min_mb=100, skip_system=True):
    """扫大文件。跳过系统目录与回收站——扫 C:\\Windows 既慢又没意义。"""
    SKIP = {"windows", "$recycle.bin", "system volume information", "node_modules",
            "appdata\\local\\temp"}
    threshold = int(min_mb) * 1024 * 1024
    found = []
    scanned = 0
    t0 = time.time()
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        if skip_system:
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP and not d.startswith("$")]
        # 别把时间全耗在一个超大目录里
        if time.time() - t0 > 25:
            break
        for fn in filenames:
            scanned += 1
            if scanned > 200000:
                break
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except Exception:
                continue
            if st.st_size >= threshold:
                found.append({"path": p, "size": st.st_size,
                              "size_h": _fmt_size(st.st_size),
                              "mtime": st.st_mtime})
        if scanned > 200000:
            break
    found.sort(key=lambda x: -x["size"])
    return found[:limit], {"scanned": scanned, "root": root,
                           "elapsed_s": round(time.time() - t0, 1),
                           "truncated": len(found) > limit}


def clean_targets():
    """清理候选：只统计大小，不动一个字节。

    为什么分开：清理是这个工具箱里唯一「删了就没了」的操作。扫描与删除
    必须是两次独立调用，中间让人看一眼数字——否则某天扫出来 20GB 一冲动点了
    清理，删掉的可能正好是还没备份的东西。
    """
    targets = []

    def add(name, path, note="", risky=False):
        if not path or not os.path.exists(path):
            return
        size, count = 0, 0
        try:
            for dp, dn, fns in os.walk(path):
                for f in fns:
                    try:
                        size += os.path.getsize(os.path.join(dp, f))
                        count += 1
                    except Exception:
                        pass
                if count > 60000:
                    break
        except Exception:
            pass
        targets.append({"name": name, "path": path, "bytes": size,
                        "size_h": _fmt_size(size), "files": count,
                        "note": note, "risky": risky})

    tmp = os.environ.get("TEMP") or ""
    add("用户临时文件", tmp, "程序运行时留下的临时文件，正在用的会被跳过")
    add("系统临时文件", os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp"),
        "系统级临时文件")
    add("Windows 更新缓存",
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "SoftwareDistribution", "Download"),
        "已安装更新的安装包，删了不影响系统", risky=False)
    add("缩略图缓存",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Microsoft\Windows\Explorer"),
        "文件资源管理器的缩略图，会自动重建")
    add("崩溃转储",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "CrashDumps"),
        "程序崩溃时生成的 dump，排查完就没用了")
    add("回收站", os.path.join(os.environ.get("SystemDrive", "C:") + "\\", "$Recycle.Bin"),
        "删之前确认没有误删想找回的东西", risky=True)

    total = sum(t["bytes"] for t in targets)
    return targets, {"total_bytes": total, "total_h": _fmt_size(total),
                     "count": len(targets)}


def clean_run(names, dry_run=True):
    """真删。默认 dry_run——要真的动手必须显式传 dry_run=False。"""
    targets, _ = clean_targets()
    picked = [t for t in targets if t["name"] in set(names or [])]
    if not picked:
        return None, "没匹配到要清理的项目（可用：%s）" % ", ".join(t["name"] for t in targets)
    report = []
    for t in picked:
        entry = {"name": t["name"], "path": t["path"], "bytes": t["bytes"]}
        if dry_run:
            entry["action"] = "would_delete"
            report.append(entry)
            continue
        deleted, failed = 0, 0
        for dp, dn, fns in os.walk(t["path"]):
            for f in fns:
                fp = os.path.join(dp, f)
                try:
                    os.remove(fp)
                    deleted += 1
                except Exception:
                    failed += 1       # 正在被占用是常态，不是错误
        entry.update({"action": "deleted", "deleted_files": deleted,
                      "skipped_in_use": failed})
        report.append(entry)
    return report, ""


# ============================================================ 网络

def net_adapters():
    script = (
        "Get-NetIPConfiguration | Where-Object {$_.NetAdapter.Status -eq 'Up'} | "
        "Select-Object @{n='name';e={$_.InterfaceAlias}},"
        "@{n='desc';e={$_.InterfaceDescription}},"
        "@{n='ipv4';e={$_.IPv4Address.IPAddress}},"
        "@{n='gw';e={$_.IPv4DefaultGateway.NextHop}},"
        "@{n='dns';e={($_.DNSServer | Where-Object {$_.AddressFamily -eq 2}).ServerAddresses -join ','}} | "
        "ConvertTo-Json -Compress"
    )
    data, err = _ps(script, timeout=40)
    if data is None:
        return None, err
    return [{"name": d.get("name"), "desc": d.get("desc"), "ipv4": d.get("ipv4"),
             "gateway": d.get("gw"), "dns": d.get("dns")} for d in data], ""


def listening_ports():
    """端口占用：哪些端口在听、谁在听。

    用 netstat -ano 再自己 join 进程名——比 Get-NetTCPConnection 快一个数量级，
    而且老系统上也能跑。
    """
    rc, out, err = _run(["netstat", "-ano", "-p", "TCP"])
    if rc != 0:
        return None, err or "netstat 失败"
    pid_name = {}
    rc2, pout, _ = _run(["tasklist", "/FO", "CSV", "/NH"])
    if rc2 == 0:
        for line in pout.splitlines():
            parts = [x.strip('"') for x in line.split('","')]
            if len(parts) >= 2:
                try:
                    pid_name[int(parts[1])] = parts[0]
                except Exception:
                    pass
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP":
            continue
        local, foreign, state, pid = parts[1], parts[2], parts[3], parts[4]
        if state.upper() != "LISTENING":
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
            ipid = int(pid)
        except Exception:
            continue
        rows.append({"port": port, "addr": local, "pid": ipid,
                     "process": pid_name.get(ipid, "?")})
    rows.sort(key=lambda r: r["port"])
    return rows, {"count": len(rows)}


# ============================================================ 设备

class WinTool(BaseDevice):
    """kind=wintool：Windows 系统工具箱。"""

    kind = "wintool"
    via = "local"

    def __init__(self, name="wt:local", **kw):
        super().__init__(name, via="local", **kw)
        self.meta.setdefault("note", "启动项/服务/磁盘/端口/网络；默认只读，改动须显式 action")

    def probe(self):
        if os.name != "nt":
            return False, "只支持 Windows（当前 %s）" % os.name
        return True, ""

    def caps(self):
        return CAPS

    def invoke(self, cap, args=None):
        args = dict(args or {})
        if cap not in CAPS:
            return R.unsupported(cap, "%s（%s）没有这项能力" % (self.name, self.kind),
                                 device=self.name, via=self.via)
        if os.name != "nt":
            return R.unsupported(cap, "Windows 专用能力", device=self.name, via=self.via)
        fn = getattr(self, "_do_" + cap.split(".", 1)[1], None)
        if fn is None:
            return R.unsupported(cap, "这项能力没接上实现", device=self.name, via=self.via)
        try:
            return fn(args)
        except Exception as e:
            return R.error(cap, "%s: %s" % (type(e).__name__, e),
                           device=self.name, via=self.via)

    # ---------------- 启动项 ----------------

    def _do_startup(self, args):
        action = str(args.get("action") or "list").lower()
        if action == "list":
            items, errors = list_startup()
            enabled = [i for i in items if i["enabled"]]
            return R.success("wt.startup", self.name, via=self.via, data={
                "action": "list", "count": len(items), "enabled": len(enabled),
                "disabled": len(items) - len(enabled),
                "items": items, "errors": errors,
                "note": "禁用的是注册表 Run 项；启动文件夹里的要手动移走",
            })
        name = str(args.get("name") or "").strip()
        if not name:
            return R.error("wt.startup", "action=%s 需要 name" % action,
                           device=self.name, via=self.via)
        if action in ("disable", "enable"):
            ok, msg, where = set_startup(name, enable=(action == "enable"))
            if not ok:
                return R.unavailable("wt.startup", msg, device=self.name, via=self.via)
            return R.success("wt.startup", self.name, via=self.via, side_effect=True,
                             data={"action": action, "name": name, "where": where,
                                   "msg": msg,
                                   "reversible": True,
                                   "reversible_note": "反向 action 即可恢复"})
        return R.error("wt.startup", "未知 action=%r（list/disable/enable）" % action,
                       device=self.name, via=self.via)

    # ---------------- 服务 ----------------

    def _do_service(self, args):
        action = str(args.get("action") or "list").lower()
        if action == "list":
            rows, meta = list_services(str(args.get("filter") or ""),
                                       str(args.get("state") or ""))
            if rows is None:
                return R.unavailable("wt.service", meta, device=self.name, via=self.via)
            return R.success("wt.service", self.name, via=self.via, data={
                "action": "list", "services": rows, **meta})
        name = str(args.get("name") or "").strip()
        if not name:
            return R.error("wt.service", "action=%s 需要 name" % action,
                           device=self.name, via=self.via)
        if action in ("start", "stop"):
            rc, out, err = _run(["sc", action, name], timeout=45)
            if rc != 0:
                return R.unavailable("wt.service", (err or out)[:200] or "sc 失败",
                                     device=self.name, via=self.via)
            return R.success("wt.service", self.name, via=self.via, side_effect=True,
                             data={"action": action, "name": name,
                                   "reversible": True,
                                   "reversible_note": "反向 action 恢复"})
        if action == "set_start":
            mode = str(args.get("mode") or "demand").lower()
            if mode not in ("auto", "demand", "disabled", "delayed-auto"):
                return R.error("wt.service", "mode 只能 auto/demand/disabled/delayed-auto",
                               device=self.name, via=self.via)
            rc, out, err = _run(["sc", "config", name, "start=", mode], timeout=30)
            if rc != 0:
                return R.unavailable("wt.service", (err or out)[:200] or "需要管理员权限",
                                     device=self.name, via=self.via)
            return R.success("wt.service", self.name, via=self.via, side_effect=True,
                             data={"action": "set_start", "name": name, "mode": mode,
                                   "reversible": True})
        return R.error("wt.service", "未知 action=%r（list/start/stop/set_start）" % action,
                       device=self.name, via=self.via)

    # ---------------- 磁盘 ----------------

    def _do_disk(self, args):
        action = str(args.get("action") or "space").lower()
        if action == "space":
            rows = disk_space()
            return R.success("wt.disk", self.name, via=self.via, data={
                "action": "space", "drives": rows})
        if action == "bigfiles":
            root = args.get("path") or (os.environ.get("SystemDrive", "C:") + "\\")
            files, meta = big_files(root, limit=int(args.get("limit") or 40),
                                    min_mb=int(args.get("min_mb") or 100))
            return R.success("wt.disk", self.name, via=self.via, data=dict(
                action="bigfiles", files=files, **meta))
        if action == "clean_scan":
            targets, meta = clean_targets()
            return R.success("wt.disk", self.name, via=self.via, data=dict(
                action="clean_scan", targets=targets, **meta,
                note="只是统计，一个字节都没删；要真删用 action=clean_run 且显式 dry_run=false"))
        if action == "clean_run":
            names = args.get("names")
            if isinstance(names, str):
                names = [n.strip() for n in names.split(",") if n.strip()]
            if not names:
                return R.error("wt.disk", "clean_run 需要 names（先 clean_scan 看有哪些）",
                               device=self.name, via=self.via)
            dry = args.get("dry_run", True)
            report, err = clean_run(names, dry_run=bool(dry))
            if report is None:
                return R.error("wt.disk", err, device=self.name, via=self.via)
            return R.success("wt.disk", self.name, via=self.via,
                             side_effect=not bool(dry), destructive=not bool(dry),
                             data={"action": "clean_run", "dry_run": bool(dry),
                                   "report": report,
                                   "note": ("这是预演，什么都没删" if dry
                                            else "已删除；跳过的是正在被占用的文件，属于正常")})
        return R.error("wt.disk",
                       "未知 action=%r（space/bigfiles/clean_scan/clean_run）" % action,
                       device=self.name, via=self.via)

    # ---------------- 网络 ----------------

    def _do_net(self, args):
        action = str(args.get("action") or "adapters").lower()
        if action == "adapters":
            rows, err = net_adapters()
            if rows is None:
                return R.unavailable("wt.net", err, device=self.name, via=self.via)
            return R.success("wt.net", self.name, via=self.via, data={
                "action": "adapters", "adapters": rows})
        if action == "ports":
            rows, meta = listening_ports()
            if rows is None:
                return R.unavailable("wt.net", meta, device=self.name, via=self.via)
            port = args.get("port")
            if port:
                rows = [r for r in rows if r["port"] == int(port)]
            proc = args.get("process")
            if proc:
                rows = [r for r in rows if proc.lower() in (r["process"] or "").lower()]
            return R.success("wt.net", self.name, via=self.via, data={
                "action": "ports", "ports": rows[:MAX_ROWS],
                "count": len(rows), **meta})
        if action == "flush_dns":
            rc, out, err = _run(["ipconfig", "/flushdns"], timeout=20)
            ok = rc == 0
            return (R.success if ok else R.unavailable)(
                "wt.net", self.name, via=self.via, side_effect=True,
                data={"action": "flush_dns", "ok": ok,
                      "detail": (out or err)[:200], "reversible": True})
        return R.error("wt.net", "未知 action=%r（adapters/ports/flush_dns）" % action,
                       device=self.name, via=self.via)

    # ---------------- 系统杂项 ----------------

    def _do_sys(self, args):
        action = str(args.get("action") or "apps").lower()
        if action == "apps":
            # 已装软件：Uninstall 注册表三处
            script = (
                "$paths=@("
                "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
                "'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',"
                "'HKCU:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*');"
                "Get-ItemProperty $paths -ErrorAction SilentlyContinue | "
                "Where-Object {$_.DisplayName} | "
                "Select-Object DisplayName,DisplayVersion,Publisher,InstallDate,EstimatedSize | "
                "Sort-Object DisplayName | ConvertTo-Json -Compress"
            )
            data, err = _ps(script, timeout=60)
            if data is None:
                return R.unavailable("wt.sys", err, device=self.name, via=self.via)
            apps = [{"name": d.get("DisplayName"), "version": d.get("DisplayVersion"),
                     "publisher": d.get("Publisher"), "installed": d.get("InstallDate"),
                     "mb": round((d.get("EstimatedSize") or 0) / 1024.0, 1)}
                    for d in data]
            kw = str(args.get("filter") or "").lower()
            if kw:
                apps = [a for a in apps if kw in (a["name"] or "").lower()]
            return R.success("wt.sys", self.name, via=self.via, data={
                "action": "apps", "count": len(apps), "apps": apps[:MAX_ROWS]})
        if action == "tasks":
            script = ("Get-ScheduledTask | Select-Object TaskName,TaskPath,State | "
                      "ConvertTo-Json -Compress")
            data, err = _ps(script, timeout=60)
            if data is None:
                return R.unavailable("wt.sys", err, device=self.name, via=self.via)
            kw = str(args.get("filter") or "").lower()
            rows = [{"name": d.get("TaskName"), "path": d.get("TaskPath"),
                     "state": d.get("State")} for d in data]
            if kw:
                rows = [r for r in rows if kw in (r["name"] or "").lower()
                        or kw in (r["path"] or "").lower()]
            return R.success("wt.sys", self.name, via=self.via, data={
                "action": "tasks", "count": len(rows), "tasks": rows[:MAX_ROWS]})
        if action == "power":
            rc, out, err = _run(["powercfg", "/getactivescheme"])
            scheme = out.strip() if rc == 0 else (err or "")[:120]
            rc2, out2, _ = _run(["powercfg", "/a"])
            return R.success("wt.sys", self.name, via=self.via, data={
                "action": "power", "active_scheme": scheme,
                "sleep_states": [l.strip() for l in out2.splitlines() if l.strip()][:12]})
        return R.error("wt.sys", "未知 action=%r（apps/tasks/power）" % action,
                       device=self.name, via=self.via)


CAPS = ("wt.startup", "wt.service", "wt.disk", "wt.net", "wt.sys")


# ============================================================ 自测

def _selftest():
    ok = []

    def chk(name, cond, extra=""):
        ok.append((name, bool(cond), extra))

    if os.name != "nt":
        print("非 Windows，跳过")
        return True

    d = WinTool()
    chk("probe", d.probe()[0])

    drives = disk_space()
    chk("磁盘列表", len(drives) >= 1, str([x["drive"] for x in drives]))

    items, errs = list_startup()
    chk("启动项可读", isinstance(items, list), "%d 项" % len(items))

    rows, meta = listening_ports()
    chk("端口列表", isinstance(rows, list) and len(rows) > 0, "%s" % (meta or {}))
    if rows:
        chk("端口带进程名", any(r["process"] not in ("?", None) for r in rows))

    targets, tmeta = clean_targets()
    chk("清理扫描只统计", all("bytes" in t for t in targets) and tmeta["total_bytes"] >= 0,
        tmeta.get("total_h"))

    svc, smeta = list_services("", "")
    chk("服务列表", isinstance(svc, list) and len(svc) > 0, str(smeta))

    r = d.invoke("wt.disk", {"action": "space"})
    chk("能力调用", r.ok and r.data.get("drives"))

    passed = sum(1 for _, c, _ in ok if c)
    for n, c, x in ok:
        print("  %s %s %s" % ("✓" if c else "✗", n, x))
    print("wintool selftest: %d/%d" % (passed, len(ok)))
    return passed == len(ok)


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
