"""路径层：本机能从哪些地址被连到，控制面该在哪听。

为什么值得单独一层：这台机器同时挂着内网 v4、联通公网 v6（2408 段）和一堆 fe80，
而监听原来是写死的 `AF_INET` —— 公网 v6 这条资源等于白给。跨网络直连本来是
「只连自己的设备」最干净的做法：不借道第三方中继、不做内网穿透、不把流量交给
别人的服务器。要吃到这条资源只需要两件事：把地址盘清楚、监听同时吃 v4 和 v6。

Windows 上的坑：AF_INET6 默认 IPV6_V6ONLY=1，绑 :: 之后 v4 一律连不进来——
必须显式关掉才是真双栈。另外运营商下发的 IPv6 有两个：稳定的全局地址和
「临时地址」（隐私扩展，会轮换）。当服务端要用稳定的那个，不能写临时地址。

    python netpath.py             # 本机地址盘点（按「谁能连」排序）
    python netpath.py --listen    # 顺便验一下双栈能不能绑
    python netpath.py --selftest
"""
import os
import re
import socket
import subprocess
import sys

IS_WIN = sys.platform.startswith("win")

LOOPBACK = ("127.", "::1")
LAN_PREFIX = ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
              "172.2", "172.30.", "172.31.")

_V4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _pick_v6(line):
    """从一行里挑出合法的 IPv6 字面量。

    正则在这里不可靠：`fe80::1111:2222:3333:4444%12` 会被从头匹配成
    `1111:2222:3333:4444`（把 fe80:: 丢了），分类就全错。所以不猜——按 token 切开，
    去掉 %zone，交给系统解析器 inet_pton 判合法性。
    """
    for tok in re.split(r"[\s,;]+", line):
        t = tok.strip("[](){}<>").split("%")[0].split("/")[0]   # 去 zone 与 /前缀长
        if t.count(":") < 2:
            continue
        try:
            socket.inet_pton(socket.AF_INET6, t)
            return t
        except (OSError, ValueError):
            continue
    return ""

SCOPE_CN = {"loopback": "本机", "lan": "局域网", "public": "公网",
            "linklocal": "链路本地", "other": "其它"}
SCOPE_ORDER = {"public": 0, "lan": 1, "other": 2, "linklocal": 3, "loopback": 4}


def _decode(b):
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _raw_text():
    cmd = ["ipconfig"] if IS_WIN else ["ip", "addr"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _decode(r.stdout or b"")


def parse(text):
    """从 ipconfig / ip addr 的输出里抽地址。返回 [{addr, family, scope, temp}]。

    temp 很重要：Windows 会同时给「IPv6 地址」和「临时 IPv6 地址」，后者会轮换。
    服务端要绑稳定的那个，所以这里必须把两者分开标，不能让调用方猜。
    """
    rows = []
    for line in (text or "").splitlines():
        low = line.lower()
        if not line.strip():
            continue
        if "gateway" in low or "网关" in line:
            continue
        fam = 0
        if "inet6" in low or "ipv6" in low:
            fam = 6
        elif "inet " in low or "inet\t" in low or "ipv4" in low:
            fam = 4
        if not fam:
            continue
        if fam == 6:
            addr = _pick_v6(line)
            if not addr:
                continue
        else:
            m = _V4.search(line)
            if not m:
                continue
            addr = m.group(0)
        if addr in ("0.0.0.0", "::") or addr.endswith(":"):
            continue
        temp = ("临时" in line) or ("temporary" in low)
        rows.append({"addr": addr, "family": fam, "scope": scope(addr), "temp": temp})
    # 同一个地址可能既出现在标签行又出现在续行，去重保序
    seen, out = set(), []
    for r in rows:
        if r["addr"] in seen:
            continue
        seen.add(r["addr"])
        out.append(r)
    return out


def scope(addr):
    a = (addr or "").lower()
    if not a:
        return "other"
    if a in ("::1", "127.0.0.1") or a.startswith("127."):
        return "loopback"
    if a.startswith("fe80"):
        return "linklocal"
    if any(a.startswith(p) for p in LAN_PREFIX):
        return "lan"
    if ":" in a:
        # 全局单播 2000::/3（2000–3fff）；运营商给中国的常见是 2408/240e/2409
        head = a.split(":")[0]
        try:
            return "public" if 0x2000 <= int(head, 16) <= 0x3fff else "other"
        except ValueError:
            return "other"
    if a.startswith(("169.254.", "100.")):
        return "other"
    return "lan"


def addresses():
    """本机地址，按「外人能不能连进来」排序：公网在前。"""
    rows = parse(_raw_text())
    rows.sort(key=lambda r: (SCOPE_ORDER.get(r["scope"], 9), r["temp"], r["family"]))
    return rows


def fmt(rows):
    if not rows:
        return "  （没读到任何地址：网卡全断，或 ipconfig 读不出来）"
    lines = []
    for r in rows:
        tag = SCOPE_CN.get(r["scope"], "?")
        extra = "（临时，会轮换）" if r["temp"] else ""
        fam = "v6" if r["family"] == 6 else "v4"
        lines.append(f"  {fam}  {r['addr']:<44} {tag}{extra}")
    return "\n".join(lines)


def listen(host, port, backlog=32):
    """建监听套接字，返回 (sock, 说明)。

    双栈规则：host 为 "0.0.0.0" / "" / "*" 时绑 :: 并关掉 IPV6_V6ONLY，这样 v4 和
    v6 一次全吃；系统不支持 v6 时退回 v4，不让「没有 v6」把服务搞挂。

    Windows 上的坑（2026-10-21 实测）：SO_REUSEADDR 在 Windows 上允许**第二个
    进程绑上同一个端口**，且新 socket 会抢走后来者的连接（Linux 上不是这个语义）。
    后果实测过：四个 commander 自检并发跑，三个的节点注册进了第一个实例的
    注册表 ⇒ 自己的进程永远等不到节点上线 ⇒ 偶发红。
    所以 Windows 上改用 SO_EXCLUSIVEADDRUSE：端口被占就抛 10048 明确报错，
    绝不静默抢连接。端口重复是调用方的错，让它响。

    端口 0 = 让系统随机分配，配合 getsockname() 取真实端口。
    """
    want_v6 = (":" in (host or "")) or (host or "") in ("", "*", "0.0.0.0")

    def _prep(s):
        if IS_WIN and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s

    if want_v6 and socket.has_ipv6:
        try:
            s = _prep(socket.socket(socket.AF_INET6, socket.SOCK_STREAM))
            if hasattr(socket, "IPV6_V6ONLY"):
                try:
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass          # 某些系统上这选项只读，绑上了就还是能用
            s.bind(("::", port))
            s.listen(backlog)
            return s, "双栈（v4 + v6）"
        except OSError as e:
            if host and ":" in host:
                raise
            _v6_err = e
    s = _prep(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    s.bind((host if (host and host not in ("*",)) else "0.0.0.0", port))
    s.listen(backlog)
    return s, "仅 v4"


def hint(port):
    """该怎么让人/节点连进来。不改防火墙——那是系统级动作，必须人点头。"""
    rows = [r for r in addresses() if r["scope"] in ("public", "lan") and not r["temp"]]
    if not rows:
        return ["  没读到可对外的地址。"]
    pub6 = [r for r in rows if r["scope"] == "public"]
    lan4 = [r for r in rows if r["scope"] == "lan" and r["family"] == 4]
    out = []
    if pub6:
        a = pub6[0]["addr"]
        out.append(f"  公网直连：install_windows.py --host {a} --port {port}")
        out.append(f"  （v6 不需要端口映射；但防火墙默认拦入站，第一次要管理员跑一次 "
                   f"`netpath.py --open-firewall {port}`）")
    if lan4:
        a = lan4[0]["addr"]
        out.append(f"  同一局域网：--host {a} --port {port}")
    if not pub6:
        out.append("  没有公网 v6：跨网络需要 Tailscale 或内网穿透。")
    return out


FIREWALL_RULE = "Skynet Fleet Commander"


def fw_open_cmd(port):
    """放行控制面入站的 netsh 命令。只开一个 TCP 端口，不做「整个程序放行」。"""
    return ["netsh", "advfirewall", "firewall", "add", "rule",
            f"name={FIREWALL_RULE} {port}", "dir=in", "action=allow",
            "protocol=TCP", f"localport={port}"]


def fw_close_cmd(port):
    return ["netsh", "advfirewall", "firewall", "delete", "rule",
            f"name={FIREWALL_RULE} {port}"]


def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def firewall(port, remove=False):
    """开/关控制面端口的入站规则。

    刻意做成**显式命令**而不是安装时自动改防火墙：改系统防火墙属于「你自己设备的
    管理员动作」，得有人明确说「开这个端口」。规则名带端口号，一眼能看出是哪条。
    """
    if not IS_WIN:
        return {"ok": False, "err": "这个工具只处理 Windows 防火墙；"
                                    "Linux 请用 ufw/firewalld 放行该端口"}
    if not is_admin():
        cmd = " ".join(fw_close_cmd(port) if remove else fw_open_cmd(port))
        return {"ok": False, "err": f"需要管理员权限。请以管理员身份运行：\n    {cmd}"}
    cmd = fw_close_cmd(port) if remove else fw_open_cmd(port)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
        txt = _decode(r.stdout or b"") + _decode(r.stderr or b"")
        ok = r.returncode == 0 and "确定" in txt or "Ok." in txt or r.returncode == 0
        return {"ok": ok, "cmd": cmd, "out": txt.strip()[:300]}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "cmd": cmd, "err": str(e)}


def _selftest():
    win = """以太网适配器 以太网:

   连接特定的 DNS 后缀 . . . . . . . :
   IPv6 地址 . . . . . . . . . . . . : 2408:8207:1941:a220:a17e:5332:b9f0:fd2f
   临时 IPv6 地址. . . . . . . . . . : 2408:8207:1941:a220:5d72:345a:f5b5:641c
   本地链接 IPv6 地址. . . . . . . . : fe80::1111:2222:3333:4444%12
   IPv4 地址 . . . . . . . . . . . . : 192.168.0.103
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . : fe80::1%12
"""
    rows = parse(win)
    addrs = [r["addr"] for r in rows]
    assert "2408:8207:1941:a220:a17e:5332:b9f0:fd2f" in addrs, addrs
    assert "192.168.0.103" in addrs, addrs
    # ① 网关行必须跳过，否则 fe80::1 会被当成自己的地址
    assert "fe80::1%12" not in addrs and "fe80::1" not in addrs, addrs
    # ② 临时地址要单独标出来：服务端不能绑它
    temps = [r for r in rows if r["temp"]]
    assert len(temps) == 1 and temps[0]["addr"].endswith("5d72:345a:f5b5:641c"), temps
    # ③ 分类
    by = {r["addr"]: r["scope"] for r in rows}
    assert by["192.168.0.103"] == "lan", by
    assert by["2408:8207:1941:a220:a17e:5332:b9f0:fd2f"] == "public", by
    assert any(r["scope"] == "linklocal" for r in rows), by

    linux = """2: eth0: <BROADCAST>
    inet 10.1.2.3/24 brd 10.1.2.255 scope global eth0
    inet6 240e:3b00::1/64 scope global
    inet6 fe80::42:1/64 scope link
"""
    lr = parse(linux)
    la = {r["addr"]: r["scope"] for r in lr}
    assert la.get("10.1.2.3") == "lan", la
    assert la.get("240e:3b00::1") == "public", la

    # ④ scope 单测：边界值
    assert scope("127.0.0.1") == "loopback"
    assert scope("172.20.1.1") == "lan"        # 172.16–31 是私网，172.20 不能漏
    assert scope("172.32.1.1") == "lan"        # 不在私网段，也仍按可达处理，不算公网
    assert scope("8.8.8.8") == "lan"           # 裸 v4 无法判定归属，交给上层别当公网
    assert scope("2a00:1450::1") == "public"
    assert scope("fd00::1") == "other"         # 唯一本地地址不是公网
    assert scope("") == "other"

    # ⑤ 排序：公网在前、临时让位
    rows.append({"addr": "127.0.0.1", "family": 4, "scope": "loopback", "temp": False})
    rows.sort(key=lambda r: (SCOPE_ORDER.get(r["scope"], 9), r["temp"], r["family"]))
    assert rows[0]["scope"] == "public" and rows[0]["temp"] is False, rows[0]
    assert rows[-1]["scope"] == "loopback", rows[-1]

    # ⑥ 双栈：真绑一次 loopback 端口，v4 和 v6 都该连得上
    s, note = listen("127.0.0.1", 0)
    port = s.getsockname()[1]
    s.close()
    assert note == "仅 v4", note
    if socket.has_ipv6:
        s6, note6 = listen("::", 0)
        p6 = s6.getsockname()[1]
        try:
            c = socket.create_connection(("127.0.0.1", p6), timeout=2)
            c.close()
            dual = True
        except OSError:
            dual = False
        s6.close()
        assert note6.startswith("双栈"), note6
        assert dual, "绑 :: 后 v4 连不进来：IPV6_V6ONLY 没关掉"

    # ⑦ 格式化不炸
    assert "公网" in fmt(rows) and "临时" in fmt(rows)
    assert "没读到" in fmt([])

    # ⑧ 防火墙命令：拼得对、只开一个端口（绝不能变成整程序放行）
    c = fw_open_cmd(9100)
    assert c[:4] == ["netsh", "advfirewall", "firewall", "add"], c
    joined = " ".join(c)
    assert "dir=in" in joined and "action=allow" in joined and "protocol=TCP" in joined, c
    assert "localport=9100" in joined and "9100" in c[5], c
    assert all("program" not in x for x in c), "不该按程序放行"
    d = fw_close_cmd(9100)
    assert d[3] == "delete" and d[4] == "rule", d
    # 非管理员时必须给出可复制的管理员命令，而不是静默失败
    if not is_admin():
        r = firewall(9100)
        assert r["ok"] is False and "管理员" in r["err"], r
        assert "netsh" in r["err"], r

    print("netpath \u81ea\u6d4b\uff1a8 \u9879\u5168\u8fc7")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    for flag, remove in (("--open-firewall", False), ("--close-firewall", True)):
        if flag in argv:
            i = argv.index(flag)
            port = int(argv[i + 1]) if len(argv) > i + 1 and argv[i + 1].isdigit() else 9100
            r = firewall(port, remove=remove)
            if r.get("ok"):
                print(f"  已{'删除' if remove else '添加'}防火墙规则：{' '.join(r['cmd'])}")
                if r.get("out"):
                    print("  " + r["out"].replace("\n", " ")[:200])
            else:
                print("  ✗ " + r["err"])
            return 0 if r.get("ok") else 2

    rows = addresses()
    print("【本机地址】按「外人能不能连进来」排序：")
    print(fmt(rows))
    print()
    print("【连法建议】")
    for line in hint(9100):
        print(line)
    if "--listen" in argv:
        print()
        s, note = listen("0.0.0.0", 0)
        print(f"【双栈验证】绑 0.0.0.0 -> {note}，端口 {s.getsockname()[1]}")
        s.close()
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
        sys.exit(0)
    sys.exit(main())
