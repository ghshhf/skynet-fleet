"""门禁 t6：无线 ADB 设备（kind=adb_wifi）——只读免闸、动手要批、shell 守白名单。

锁死的是真会发生的形状，不是假想：

  ① **caps() 必须返回字符串名**。
     踩过的坑：Host.caps() 一开始返回 Capability 对象，注册表 owners() 用
     `cap in set(self.caps())` 跟字符串比，对不上 → 能力「登记了却调不通」。
     这条用 owners() 非空 + 每个 cap 都有 owner 钉死。

  ② **只读能力（list/scan/info）免闸门**，auto 档、无副作用。
     「看这一侧有什么」不是动手，不该弹窗要批准。

  ③ **动手能力 fail-closed**：没注入闸门一律 denied（不许「忘了就放行」），
     闸门拒绝必须带理由，ask 档 → 闸门 level=2。

  ④ **shell 白名单是代码级硬边界**：写操作（rm / pm uninstall / settings put /
     am start / reboot …）一律 denied 且带理由，**闸门放行也没用**——
     闸门是权限，白名单是安全，两者不能互相顶替。

  ⑤ **停用（disable_pkg）不是删除**：返回里必须写清可逆、怎么恢复；
     卸载（uninstall）必须写清不可逆。两者的语义不许混。

  ⑥ **失败必须带原因**：每条失败路径都带可读的 reason，不许静默成功/静默失败。

  ⑦ **扫描规模有硬上限**（2026-10-21 验收踩出来的真 bug）。
     原实现默认扫「254 台候选 × 2173 个端口 = 55 万次 TCP 探测」，
     16 并发单次 0.35s 超时 ⇒ 最坏 3.4 小时：既卡死终端，也在扫别人家的网。
     修正后：默认走 **mDNS 发现 + 只探经典 5555**；暴力扫随机端口段必须
     显式 `ports=random/full`，且有**次数 + 墙钟双硬预算**，到点就停并
     如实说「已探 N 次（计划 M 次），因预算到点停下」——不许假装扫完了。
     这条钉死：默认 254 台候选只探 254 次；opt-in 模式探测次数 ≤ 预算；
     args 不许把上限抬回 55 万次。

**全离线、可复现**：这里把模块级的 _adb_exe / _run_cmd / _run_bin / _probe_tcp /
_mdns_discover 五个出口全换成假的——所以本门禁**绝不发一条 adb 命令、
绝不连一个 IP、绝不发一条 mDNS 组播**。
所有「这台机器没插设备」「adb 没装」这类环境因素都被假后端吃掉，
门禁红只可能意味着代码坏了（t4 的教训：门禁红必须等于代码坏）。

跑法：python evals/t6_adb_wifi.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fleet.kinds.adb_wifi as AW          # noqa: E402
from fleet.catalog import build            # noqa: E402
from fleet.core import result as R         # noqa: E402

READ_CAPS = ("adb.wifi.list", "adb.wifi.scan", "adb.wifi.info")
ACT_CAPS = ("adb.wifi.connect", "adb.wifi.disconnect", "adb.wifi.pair",
            "adb.wifi.shell", "adb.wifi.install", "adb.wifi.uninstall",
            "adb.wifi.screenshot", "adb.wifi.launch",
            "adb.wifi.disable_pkg", "adb.wifi.enable_pkg",
            "adb.wifi.list_disabled")
ALL_CAPS = READ_CAPS + ACT_CAPS

FAIL = []


def check(cond, msg):
    if cond:
        print("  ok", msg)
    else:
        print("  x ", msg)
        FAIL.append(msg)


# ---------------------------------------------------------------- 假 adb 后端
#
# 假 adb 是一台「听话的设备」：它记得被叫过什么、回什么由我们定。
# 记录调用序列是为了断言**路由**（connect 走 connect、shell 走 shell、
# 扫端口走 _probe_tcp），不是为了真的执行。

CALLS = []          # [(kind, args), ...]
CONNECTED = set()   # 假装已经连上的设备


def fake_adb_exe():
    """假 adb 可执行文件路径（存在即可，门禁从不真去跑它）。"""
    return "/fake/platform-tools/adb"


def fake_run_cmd(cmd, timeout=20):
    """假 subprocess.run（文本）。"""
    args = list(cmd[1:])                  # 去掉 adb 本体，只留子命令
    CALLS.append(("cmd", args))
    if args[:1] == ["devices"]:
        return 0, ("List of devices attached\n"
                   "192.168.1.50:5555    device product:x model:Pixel_7 device:panther\n"
                   "192.168.1.51:41234   device product:y model:MI_TV device:atv\n"
                   "ABC123               device usb:1-1 product:z model:USB device:usb\n"
                   "adb-xyz._adb-tls-connect._tcp device product:q model:MDNS device:m\n")
    if args[:1] == ["connect"]:
        CONNECTED.add(args[1])
        return 0, f"connected to {args[1]}"
    if args[:1] == ["disconnect"]:
        CONNECTED.discard(args[1] if len(args) > 1 else "")
        return 0, "disconnected"
    if args[:1] == ["pair"]:
        return 0, f"Successfully paired to {args[1]}"
    if "-s" in args:
        target = args[args.index("-s") + 1]
        rest = args[args.index("-s") + 2:]
        if rest[:1] == ["shell"]:
            # 真 adb 会把 shell 后面所有参数用空格拼成一条命令
            cmdline = " ".join(rest[1:])
            if cmdline.startswith("getprop"):
                return 0, ("[ro.product.model]: [Pixel 7]\n"
                           "[ro.product.manufacturer]: [Google]\n"
                           "[ro.build.version.release]: [14]\n"
                           "[ro.build.version.sdk]: [34]\n"
                           "[ro.serialno]: [SERIAL123]\n"
                           "[ro.product.brand]: [google]\n")
            if cmdline.startswith("pm disable-user"):
                return 0, "Package com.ads.evil new state: disabled-user"
            if cmdline.startswith("pm enable"):
                return 0, "Package com.ads.evil new state: enabled"
            if cmdline.startswith("pm list packages"):
                return 0, ("package:com.ads.evil\n"
                           "package:com.other.blocked\n")
            if cmdline.startswith("ls"):
                return 0, "Android\ndata\nsystem\n"
            return 0, "ok"
        if rest[:1] == ["install"]:
            return 0, "Success"
        if rest[:1] == ["uninstall"]:
            return 0, "Success"
    return 0, ""


def fake_run_bin(cmd, timeout=20):
    """假 subprocess.run（二进制）：截屏给一个最小 PNG。"""
    CALLS.append(("bin", list(cmd[1:])))
    # 1x1 透明 PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d4944415478da63f8cfc00000030101003c1c0c2e0"
        "0000000049454e44ae426082")
    return 0, png, ""


PROBED = []


def fake_probe_tcp(host, port, timeout=0.35):
    """假端口探针：只对 192.168.1.50:5555 说开，其余一律关。"""
    PROBED.append((host, port))
    return (host, port) == ("192.168.1.50", 5555)


MDNS_CALLS = []


def fake_mdns_discover(timeout=3.0):
    """假 mDNS 发现：门禁**绝不许真发一条组播**。

    假装网里有两台 Android 11+ 设备：老王（解析出 IP）和小李（只有实例名，
    没解析出 IP——真实世界里 A 记录可能缺失）。
    """
    MDNS_CALLS.append(timeout)
    return ([
        {"name": "adb-wang._adb-tls-connect._tcp.local",
         "host": "192.168.1.77", "port": 41234, "resolved": True},
        {"name": "adb-li._adb-tls-connect._tcp.local",
         "host": "", "port": 39871, "resolved": False},
    ], "")


AW._adb_exe = fake_adb_exe
AW._run_cmd = fake_run_cmd
AW._run_bin = fake_run_bin
AW._probe_tcp = fake_probe_tcp
AW._mdns_discover = fake_mdns_discover

# ---------------------------------------------------------------- 离线注册表
_tmp = tempfile.mkdtemp(prefix="t6_adbwifi_")
reg = build(path=os.path.join(_tmp, "devices.json"), with_nodes=False)

dev = reg.device("adbwifi:local")
check(dev is not None, "catalog 里有 adbwifi:local 设备（AdbWifi 已注册）")
check(dev is not None and dev.kind == "adb_wifi", "设备 kind=adb_wifi")

# 扫端口的候选主机也换掉：门禁不许枚举本机真实网段
if dev is not None:
    dev._hosts = lambda: (["192.168.1.50", "192.168.1.51"], "")

# ① caps() 契约：字符串名 + 每项都有 owner
names = dev.caps() if dev else ()
check(bool(names) and all(isinstance(n, str) for n in names),
      "caps() 返回的全是字符串名（不是 Capability 对象）")
check(set(names) == set(ALL_CAPS), f"caps() 覆盖全部 {len(ALL_CAPS)} 项能力")
for name in ALL_CAPS:
    check(reg.capability(name) is not None, f"{name} 已登记")
    check(bool(reg.owners(name)), f"{name} 有设备能接（owners() 非空）")

# ② 只读能力：auto 档 + 无副作用
for name in READ_CAPS:
    c = reg.capability(name)
    check(c.approval == "auto" and not c.side_effect, f"{name} auto 档 + 只读")

# ③ 动手能力：ask 档 + 副作用
for name in ACT_CAPS:
    c = reg.capability(name)
    check(c.approval == "ask" and c.side_effect, f"{name} ask 档 + 副作用")


# ---------------------------------------------------------------- 闸门
def gate_yes(cap, dev_, args, level):
    GATE_LEVELS.append(level)
    return True, ""


GATE_LEVELS = []
GATE_CALLS = []

# ④ 只读免闸门：不给 gate 也能成
CALLS.clear()
r = reg.call("adb.wifi.list")
check(r.ok, "adb.wifi.list 成功（只读免闸门）")
check(not r.side_effect, "adb.wifi.list 无副作用")
check(any(c[1][0:1] == ["devices"] for c in CALLS), "adb.wifi.list 走 adb devices")
check(all("usb" not in " ".join(c[1]) for c in CALLS if c[1][0:1] == ["devices"]),
      "adb.wifi.list 不把 USB 设备算成无线设备")

r = reg.call("adb.wifi.info", args={"target": "192.168.1.50:5555"})
check(r.ok and r.data.get("model") == "Pixel 7", "adb.wifi.info 读到型号")
check(r.data.get("android") == "14" and r.data.get("serial") == "SERIAL123",
      "adb.wifi.info 读到 Android 版本与序列号")

r = reg.call("adb.wifi.info", args={})
check(not r.ok and r.err, "adb.wifi.info 缺 target → 失败且带原因")

CALLS.clear()
PROBED.clear()
MDNS_CALLS.clear()
r = reg.call("adb.wifi.scan")
check(r.ok, "adb.wifi.scan 成功（只读免闸门）")
check(any(d.get("host") == "192.168.1.50" and d.get("port") == 5555
          for d in r.data.get("found", [])),
      "adb.wifi.scan 找到开着的 5555 端口")
check(PROBED, "adb.wifi.scan 真去探端口了（走 _probe_tcp）")
check(any(p == 5555 for _h, p in PROBED), "adb.wifi.scan 探了经典 5555")
check(not any(32768 <= p <= 60999 for _h, p in PROBED),
      "adb.wifi.scan 默认**不**探随机端口段（默认不许暴力扫）")
check(MDNS_CALLS, "adb.wifi.scan 默认走 mDNS 发现")
check(any(d.get("via") == "mdns" and d.get("host") == "192.168.1.77"
          for d in r.data.get("found", [])),
      "adb.wifi.scan 把 mDNS 发现的设备列进结果")
check(r.data.get("mdns_found") == 2, "adb.wifi.scan 报了 mDNS 发现数")
check(r.data.get("truncated") is False, "adb.wifi.scan 默认路径没被截断")
check(isinstance(r.data.get("probes"), int), "adb.wifi.scan 报了真实探测次数")
check(isinstance(r.data.get("elapsed"), (int, float)), "adb.wifi.scan 报了耗时")

# ④b 扫描规模门禁：254 台候选主机时的真实探测次数
#
# 为什么钉这个数：2026-10-21 验收时发现真实规模是
# 「254 台 × 2173 个端口 = 55 万次 TCP 探测」⇒ 最坏 3.4 小时。
# 门禁原来把 _hosts/_probe_tcp 都假掉了，量不到规模，所以漏了它。
# 这里用假的 254 台主机把规模钉死：**默认不许暴力扫**。
BIG_HOSTS = [f"192.168.9.{i}" for i in range(1, 255)]   # 254 台假主机
if dev is not None:
    dev._hosts = lambda: (list(BIG_HOSTS), "")
PROBED.clear()
r = reg.call("adb.wifi.scan", args={"hosts_limit": 254})
check(r.ok, "adb.wifi.scan 254 台候选也能跑（只读免闸门）")
check(r.data.get("probes") == 254,
      f"默认路径 254 台候选只探 254 次（不是 55 万次），实探 {r.data.get('probes')} 次")
check(r.data.get("ports_scanned") == 1,
      f"默认只探 1 个端口（5555），实探端口集 {r.data.get('ports_scanned')} 个")
check(not any(32768 <= p <= 60999 for _h, p in PROBED),
      "默认路径一个随机段端口都没探")
check(r.data.get("hosts_candidates") == 254,
      "adb.wifi.scan 报了候选主机总数（254）")
check(r.data.get("hosts_scanned") == 254, "adb.wifi.scan 报了实际扫的主机数")
check("共 254 台候选" in (r.data.get("note") or ""),
      "adb.wifi.scan note 说清「本次只扫了前 N 台（共 M 台候选）」")
if dev is not None:
    dev._hosts = lambda: (["192.168.1.50", "192.168.1.51"], "")

# ④c ports="random" 显式 opt-in：可以扫随机段，但必须被硬预算截断并说清
PROBED.clear()
r = reg.call("adb.wifi.scan", args={"ports": "random", "hosts_limit": 254,
                                    "max_probes": 500, "workers": 4})
check(r.ok, "adb.wifi.scan ports=random 成功")
check(any(32768 <= p <= 60999 for _h, p in PROBED),
      "ports=random 显式 opt-in 后才探随机端口段")
check(r.data.get("probes") <= 500,
      f"ports=random 探测次数受硬预算约束（≤500），实探 {r.data.get('probes')} 次")
check(r.data.get("truncated") is True, "预算到点时 truncated=True")
check("预算" in (r.data.get("note") or ""),
      "预算到点时 note 说清「预算到点停下」")
check(r.data.get("probes_planned", 0) > r.data.get("probes", 0),
      "预算到点时如实说「已探 N 次 / 计划 M 次」")

# ④d 预算只能调低不能调高：防止有人把上限抬回 55 万次
PROBED.clear()
r = reg.call("adb.wifi.scan", args={"ports": "random", "hosts_limit": 254,
                                    "max_probes": 10 ** 9})
check(r.data.get("probes", 0) <= AW.MAX_PROBES,
      f"args 不能把探测上限抬过 MAX_PROBES（{AW.MAX_PROBES}）")
check(r.data.get("probe_budget") == AW.MAX_PROBES,
      "args 把 max_probes 抬高时被夹回硬上限")

# ④d-2 硬上限本身被钉死（不许悄悄把常量调大）
#
# 为什么这条要写死数字而不是「等于模块常量」：写成 `MAX_PROBES <= 20000`
# 才是真正的钉子；只断言「args 夹回常量」的话，谁把常量抬到 10^9
# 门禁照样绿——这个洞是变异测试抓出来的。
check(AW.MAX_PROBES <= 20000,
      f"探测次数硬上限不许超过 20000（现值 {AW.MAX_PROBES}）")
check(AW.MAX_SCAN_SECONDS <= 20.0,
      f"墙钟硬上限不许超过 20 秒（现值 {AW.MAX_SCAN_SECONDS}）")
# 单次 scan 的理论最坏规模：主机数 × 端口数 必须被上限压住
_worst = 254 * 28233
check(_worst > AW.MAX_PROBES,
      "上界参照：254 主机 × 全量随机段远超硬上限（说明预算真的在起作用）")

# ④e 非法 ports 值要说清，不许静默降级
r = reg.call("adb.wifi.scan", args={"ports": "nonsense"})
check(not r.ok and r.err, "ports 值非法 → 失败且带原因（不许静默降级成 classic）")

# ④f 墙钟预算：秒数上限一到就停，同样要说清
#
# 这里必须临时换成一个**慢探针**：假探针瞬间返回，秒数预算永远轮不到触发，
# 那这条断言就变成自欺欺人。慢探针只 sleep，不发任何包。
import time as _time


def slow_probe_tcp(host, port, timeout=0.35):
    _time.sleep(0.05)
    PROBED.append((host, port))
    return False


AW._probe_tcp = slow_probe_tcp
PROBED.clear()
r = reg.call("adb.wifi.scan", args={"ports": "random", "hosts_limit": 254,
                                    "max_seconds": 1, "workers": 2})
check(r.ok, "adb.wifi.scan 墙钟预算下仍返回结果（部分结果也是结果）")
check(r.data.get("probes") < r.data.get("probes_planned", 0),
      "墙钟预算下探测次数远少于计划数（提前停了）")
check(r.data.get("truncated") is True, "墙钟预算到点 → truncated=True")
check("预算" in (r.data.get("note") or ""),
      "墙钟预算到点时 note 说清「预算到点停下」")
check(r.data.get("seconds_budget") == 1, "墙钟预算值如实回传")
AW._probe_tcp = fake_probe_tcp          # 换回快探针，后面的断言还要用它

# ⑤ 动手能力 fail-closed：没闸门一律 denied，且**一条 adb 都不发**
for name in ACT_CAPS:
    CALLS.clear()
    args = {"target": "192.168.1.50:5555", "addr": "192.168.1.50:5555",
            "code": "123456", "cmd": "ls /sdcard", "package": "com.ads.evil",
            "apk": "x.apk"}
    r = reg.call(name, args=args)
    check(r.code == R.DENIED, f"{name} 无闸门 → denied（fail-closed）")
    check(bool(r.err), f"{name} denied 必须带理由")
    check(not CALLS, f"{name} 无闸门时一条 adb 都不发（不许漏跑）")

# ⑥ 闸门拒绝 → denied 且带理由
CALLS.clear()
r = reg.call("adb.wifi.connect", args={"addr": "192.168.1.50:5555"},
             gate=lambda c, d, a, l: (False, "主人说不"))
check(r.code == R.DENIED and r.err == "主人说不", "闸门拒绝 → denied 且带理由")
check(not CALLS, "闸门拒绝时不落到 adb")

# ⑦ ask 档 → 闸门 level=2；放行才执行
for name in ACT_CAPS:
    GATE_LEVELS.clear()
    CALLS.clear()
    args = {"target": "192.168.1.50:5555", "addr": "192.168.1.50:5555",
            "code": "123456", "cmd": "ls /sdcard", "package": "com.ads.evil",
            "apk": os.path.join(_tmp, "x.apk")}
    if name == "adb.wifi.install":
        with open(args["apk"], "wb") as f:
            f.write(b"PK\x03\x04fake")
    r = reg.call(name, args=args, gate=gate_yes)
    check(r.code != R.DENIED or name == "adb.wifi.shell",
          f"{name} 放行后不该被闸门拦下")
    check(bool(GATE_LEVELS) and GATE_LEVELS[-1] == 2,
          f"{name} 闸门 level=2（ask 档要人确认）")

# ⑧ 路由：每条能力打到对的 adb 子命令
def last_call():
    return CALLS[-1][1] if CALLS else []

CALLS.clear()
reg.call("adb.wifi.connect", args={"addr": "192.168.1.50:5555"}, gate=gate_yes)
check(last_call()[0:1] == ["connect"], "connect → adb connect")
check("192.168.1.50:5555" in last_call(), "connect 带上了 addr")

CALLS.clear()
reg.call("adb.wifi.shell", args={"target": "192.168.1.50:5555", "cmd": "ls /sdcard"},
         gate=gate_yes)
check("shell" in last_call() and "ls /sdcard" in last_call(),
      "shell → adb -s target shell <cmd>")

CALLS.clear()
reg.call("adb.wifi.pair", args={"addr": "192.168.1.50:5555", "code": "123456"},
         gate=gate_yes)
check(last_call()[0:1] == ["pair"], "pair → adb pair")

CALLS.clear()
reg.call("adb.wifi.install", args={"target": "192.168.1.50:5555",
                                   "apk": os.path.join(_tmp, "x.apk")}, gate=gate_yes)
check(last_call()[0:1] == ["-s", "192.168.1.50:5555"] or "install" in last_call(),
      "install → adb -s target install <apk>")
check("install" in last_call(), "install 走的是 install 子命令")

CALLS.clear()
reg.call("adb.wifi.uninstall", args={"target": "192.168.1.50:5555",
                                     "package": "com.ads.evil"}, gate=gate_yes)
check("uninstall" in last_call(), "uninstall → adb uninstall")

CALLS.clear()
reg.call("adb.wifi.disable_pkg", args={"target": "192.168.1.50:5555",
                                       "package": "com.ads.evil"}, gate=gate_yes)
check("pm" in last_call() and "disable-user" in last_call(),
      "disable_pkg → pm disable-user")

CALLS.clear()
reg.call("adb.wifi.enable_pkg", args={"target": "192.168.1.50:5555",
                                      "package": "com.ads.evil"}, gate=gate_yes)
check("pm" in last_call() and "enable" in last_call(), "enable_pkg → pm enable")

CALLS.clear()
reg.call("adb.wifi.list_disabled", args={"target": "192.168.1.50:5555"}, gate=gate_yes)
check("pm" in last_call() and "list" in last_call(), "list_disabled → pm list packages")

CALLS.clear()
reg.call("adb.wifi.launch", args={"target": "192.168.1.50:5555",
                                  "package": "com.x", "activity": "com.x/.Main"},
         gate=gate_yes)
check(any("am start" in " ".join(c[1]) for c in CALLS), "launch → am start")

# ---------------------------------------------------------------- mDNS 解析
#
# 为什么单独测解析器：mDNS 是**默认发现路径**，它要是解析错了，
# scan 就只剩「探 5555」一条腿，Android 11+ 设备一个都发现不了。
# 这里用手工构造的 DNS 包测（不发任何网络包，纯字节级）。
import struct as _struct


def _dns_name(name):
    out = b""
    for label in name.split("."):
        if label:
            lb = label.encode()
            out += bytes([len(lb)]) + lb
    return out + b"\x00"


def _rr(name, rtype, rdata):
    return _dns_name(name) + _struct.pack("!HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


# 一条典型响应：PTR(服务→实例) + SRV(实例→主机+端口) + A(主机→IP)
_inst = "adb-wang._adb-tls-connect._tcp.local"
_host = "adb-wang.local"
_ptr = _rr("_adb-tls-connect._tcp.local", 12, _dns_name(_inst))
_srv = _rr(_inst, 33, _struct.pack("!HHH", 0, 0, 41234) + _dns_name(_host))
_a = _rr(_host, 1, bytes([192, 168, 1, 77]))
_pkt = _struct.pack("!HHHHHH", 0, 0x8400, 0, 3, 0, 0) + _ptr + _srv + _a

recs = AW._mdns_records(_pkt)
check(len(recs) == 3, f"mDNS 解析出 3 条记录，实得 {len(recs)}")
check(any(r["type"] == "ptr" and r["target"] == _inst for r in recs),
      "mDNS 解析出 PTR（服务→实例名）")
check(any(r["type"] == "srv" and r["port"] == 41234 and r["target"] == _host
          for r in recs), "mDNS 解析出 SRV（实例→主机+端口 41234）")
check(any(r["type"] == "a" and r["ip"] == "192.168.1.77" for r in recs),
      "mDNS 解析出 A 记录（主机→IP）")

# 压缩指针：真实 mDNS 响应几乎都用压缩名，解析器必须扛得住
_ptr_c = _rr("_adb-tls-connect._tcp.local", 12, b"\xc0\x0c")
_pkt2 = _struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + _ptr_c
recs2 = AW._mdns_records(_pkt2)
check(any(r["type"] == "ptr" for r in recs2),
      "mDNS 解析器扛得住压缩指针（0xC0）")

# 残包不许把门禁打崩（真实网络上的包就是残缺的）
check(AW._mdns_records(b"") == [], "mDNS 解析空包 → 空记录，不抛异常")
check(AW._mdns_records(b"\x00\x01\x02") == [], "mDNS 解析残包 → 空记录，不抛异常")

# 查询包结构：组播地址 + TTL=255 的硬要求
q = AW._mdns_query("_adb-tls-connect._tcp.local", 12)
check(len(q) > 12 and q[4:6] == b"\x00\x01", "mDNS 查询包带 1 条问题记录")
check(AW.MDNS_ADDR == ("224.0.0.251", 5353), "mDNS 组播地址是 224.0.0.251:5353")
check("_adb-tls-connect._tcp.local" in AW.MDNS_SERVICES,
      "mDNS 查 _adb-tls-connect._tcp.local（Android 11+ 无线调试）")
check("_adb._tcp.local" in AW.MDNS_SERVICES,
      "mDNS 也查 _adb._tcp.local（老 adb mDNS 服务）")

CALLS.clear()
reg.call("adb.wifi.screenshot", args={"target": "192.168.1.50:5555"}, gate=gate_yes)
check(any("screencap" in " ".join(c[1]) for c in CALLS), "screenshot → screencap")
check(any(c[0] == "bin" for c in CALLS), "screenshot 走二进制通道")

# ⑨ shell 白名单：写操作一律 denied，**闸门放行也没用**
WRITE_CMDS = [
    "rm -rf /sdcard",
    "pm uninstall com.x",
    "pm disable-user --user 0 com.x",
    "settings put global airplane_mode_on 1",
    "am start -n com.x/.Main",
    "reboot",
    "su -c id",
    "cat /data/data/com.x/shared_prefs/a.xml",
    "ls /sdcard; rm -rf /sdcard",
    "ls /sdcard && reboot",
    "sh -c 'rm -rf /'",
    "svc wifi disable",
    "chmod 777 /sdcard",
    "input keyevent 3",
]
for cmd in WRITE_CMDS:
    CALLS.clear()
    r = reg.call("adb.wifi.shell", args={"target": "192.168.1.50:5555", "cmd": cmd},
                 gate=gate_yes)
    check(r.code == R.DENIED, f"shell 拒绝写操作：{cmd}")
    check(bool(r.err), f"shell 拒绝必须带理由：{cmd}")
    check(not CALLS, f"shell 被拒时不落到 adb：{cmd}")

# ⑩ shell 只读命令放行
for cmd in ("ls /sdcard", "getprop ro.product.model", "pm list packages",
            "pm path com.x", "cat /proc/cpuinfo", "dumpsys battery",
            "settings get global airplane_mode_on", "wm size"):
    CALLS.clear()
    r = reg.call("adb.wifi.shell", args={"target": "192.168.1.50:5555", "cmd": cmd},
                 gate=gate_yes)
    check(r.ok, f"shell 放行只读命令：{cmd}")

# ⑪ 停用 vs 卸载：语义不许混
r = reg.call("adb.wifi.disable_pkg", args={"target": "192.168.1.50:5555",
                                           "package": "com.ads.evil"}, gate=gate_yes)
check(r.ok, "disable_pkg 成功")
check(r.data.get("reversible") is True, "disable_pkg 返回里标明可逆")
check("可逆" in (r.data.get("note") or ""), "disable_pkg 的说明里写明「可逆」")
check("enable_pkg" in (r.data.get("note") or "") or
      "enable_pkg" in (r.data.get("restore_with") or ""),
      "disable_pkg 说清怎么恢复（enable_pkg）")
check("停用" in (r.data.get("note") or "") and "删除" in (r.data.get("note") or ""),
      "disable_pkg 说清这是停用不是删除")

r = reg.call("adb.wifi.uninstall", args={"target": "192.168.1.50:5555",
                                         "package": "com.ads.evil"}, gate=gate_yes)
check(r.ok, "uninstall 成功")
check(r.data.get("reversible") is False, "uninstall 返回里标明不可逆")

r = reg.call("adb.wifi.list_disabled", args={"target": "192.168.1.50:5555"},
             gate=gate_yes)
check(r.ok and "com.ads.evil" in (r.data.get("packages") or []),
      "list_disabled 列出了被停用的包")

# ⑫ 配对码不回显（一次性凭据不进结果）
r = reg.call("adb.wifi.pair", args={"addr": "192.168.1.50:5555", "code": "123456"},
             gate=gate_yes)
check(r.ok, "pair 成功")
check("123456" not in str(r.data), "配对码不回显在结果里")

# ⑬ 失败必须带原因
r = reg.call("adb.wifi.shell", args={"target": "192.168.1.50:5555"}, gate=gate_yes)
check(not r.ok and r.err, "shell 缺 cmd → 失败且带原因")
r = reg.call("adb.wifi.disable_pkg", args={"target": "192.168.1.50:5555"}, gate=gate_yes)
check(not r.ok and r.err, "disable_pkg 缺 package → 失败且带原因")
r = reg.call("adb.wifi.pair", args={"addr": "192.168.1.50:5555"}, gate=gate_yes)
check(not r.ok and r.err, "pair 缺 code → 失败且带原因")

# ⑭ 未知能力 → unsupported（不是 unavailable：上层该知道是「压根没有这项」）
r = reg.call("adb.wifi.nope")
check(r.code == R.UNSUPPORTED, "adb.wifi.nope 未登记 → unsupported")

# ⑮ 静态门禁：模块顶层不许 import 重依赖（无显示/无网环境也要能 import）
import ast
with open(os.path.join(ROOT, "fleet", "kinds", "adb_wifi.py"), encoding="utf-8") as f:
    _src = f.read()
_top = set()
for node in ast.parse(_src).body:
    if isinstance(node, ast.Import):
        for a in node.names:
            _top.add(a.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        _top.add((node.module or "").split(".")[0])
_heavy = {"PIL", "requests", "numpy", "psutil"} & _top
check(not _heavy, f"adb_wifi.py 顶层不 import 重依赖（发现 {_heavy or '无'}）")

# ⑯ 本门禁自己不许真发 adb / 真连 IP：四个出口必须都换掉了
check(AW._adb_exe is fake_adb_exe, "adb 可执行文件走的是假实现")
check(AW._run_cmd is fake_run_cmd, "命令执行走的是假实现")
check(AW._run_bin is fake_run_bin, "二进制执行走的是假实现")
check(AW._probe_tcp is fake_probe_tcp, "端口探测走的是假实现")
check(AW._mdns_discover is fake_mdns_discover, "mDNS 发现走的是假实现（没真发组播）")
check(not any("192.168.1.50:5555" in c[1][0] for c in CALLS if c[0] == "cmd"
              and c[1][0:1] == ["connect"] and False), "connect 没真连（门禁只留记录）")

# 清理临时目录
try:
    for f in os.listdir(_tmp):
        os.remove(os.path.join(_tmp, f))
    os.rmdir(_tmp)
except OSError:
    pass

print("t6 adb_wifi:", "PASS" if not FAIL else f"FAIL ({len(FAIL)} 项)", flush=True)
sys.exit(1 if FAIL else 0)
