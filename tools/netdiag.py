#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""netdiag —— 跨机连通性诊断：把「通了 / 没通」拆成「哪一层没通」。

2026-10-25 一次跨机接入踩了四个坑，全部固化成这里的判据：

1. **TIMEOUT 不等于被防火墙拦。**
   Windows 对没有任何监听的端口，常常也是静默丢包（不回 RST）——
   所以在 Windows 目标上，「超时」只能读成「这条路上没有能接的东西」，
   不能直接宣判「防火墙 DROP」。要分叉必须去**端上**看监听表。

2. **REFUSED(RST) 才是好消息。**
   远端回了 RST（或 ICMP port unreachable）说明包已经到对方 TCP 栈了：
   路通、防火墙放行，只是那个端口上没人接。此时该查「程序跑没跑 / 绑没绑 0.0.0.0」。

3. **本机测本机不算数。**
   Windows 连本机非回环 IP 走本地环回，WFP 对 loopback 有例外
   ⇒ `Test-NetConnection <自己的局域网IP> -Port N` 在任何防火墙配置下都是 True。
   证明「对外开放」只能**从另一台机器探**。

4. **报 IP 给对端前先看网卡类型。**
   RNDIS（USB 网络共享）/ vEthernet / VMware / VirtualBox 都是虚拟网段，
   对端通常没有到这些网段的路由 —— 把这种地址报成「局域网地址」，
   对方永远连不上，而两边日志都看不出问题。

用法：
    python tools/netdiag.py 192.168.0.103:8765 192.168.0.103:445
    python tools/netdiag.py --local            # 只看本机网卡分类
    python tools/netdiag.py --json 1.2.3.4:80  # 机读输出
"""
from __future__ import print_function

import json
import socket
import sys
import time

OPEN = "OPEN"          # 有人接
REFUSED = "REFUSED"    # 路通、无人接（RST）
DROP = "DROP"          # 无响应（丢包 / 主机不在）
UNREACH = "UNREACH"    # 网络不可达（无路由）

# 虚拟网卡特征词：命中即判定「非真实局域网」
VIRTUAL_HINTS = (
    "rndis", "remote ndis", "usb",            # 手机 USB 网络共享
    "vethernet", "hyper-v", "default switch",  # Hyper-V / WSL
    "vmware", "virtualbox", "vbox",            # 桌面虚拟化
    "tap-", "tun", "wireguard", "zerotier", "tailscale", "openvpn",
    "bluetooth", "loopback",
)

_NOTES = {
    OPEN: "有人接：路通 + 防火墙放行 + 程序在听。可以直接对接。",
    REFUSED: "路通、防火墙放行，但那个端口上没人接。查：程序跑没跑 / 绑没绑 0.0.0.0"
             "（绑 127.0.0.1 时外部一律 RST）。",
    DROP: "无响应。三种可能：①防火墙丢包 ②目标端口没有监听且系统选择静默丢包"
          "（Windows 常见）③主机不存在。**必须去端上看监听表才能分叉**。",
    UNREACH: "没有路由 / 网络不可达。先查网段与物理链路。",
}


def probe(host, port, timeout=2.0, read_banner=True):
    """探一个 TCP 端口。返回 dict(status, ms, banner, err)。"""
    t0 = time.time()
    out = {"host": host, "port": port, "status": DROP, "ms": 0, "banner": "", "err": ""}
    s = None
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        out["status"] = OPEN
        out["ms"] = round((time.time() - t0) * 1000)
        if read_banner:
            try:
                s.settimeout(min(1.0, timeout))
                b = s.recv(128)
                out["banner"] = "" if not b else repr(b[:96])
            except Exception:
                out["banner"] = ""          # 不主动发 banner 的服务很多，不是错
    except ConnectionRefusedError:
        out["status"] = REFUSED
        out["ms"] = round((time.time() - t0) * 1000)
        out["err"] = "RST"
    except socket.timeout:
        out["status"] = DROP
        out["ms"] = round((time.time() - t0) * 1000)
        out["err"] = "timeout"
    except OSError as e:
        out["ms"] = round((time.time() - t0) * 1000)
        msg = str(e)
        out["err"] = msg
        # 10051/10065 = 网络/主机不可达，10060 = 超时
        if getattr(e, "errno", None) in (10051, 10065) or "unreachable" in msg.lower():
            out["status"] = UNREACH
        else:
            out["status"] = DROP
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    return out


def local_adapters():
    """本机网卡 → (别名, 描述, IPv4)。仅 Windows；失败返回 []。

    `virtual` 字段是这张表存在的**唯一理由**：避免把 RNDIS/虚拟网段当成
    「局域网地址」报给对端。
    """
    import subprocess
    ps = (
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | ForEach-Object { "
        "$n=$_.Name; $d=$_.InterfaceDescription; $i=$_.ifIndex; "
        "Get-NetIPAddress -InterfaceIndex $i -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | ForEach-Object { "
        # 这里必须用**双引号**：PowerShell 单引号是字面量，`t 不会被转义，
        # 会原样吐出反引号（踩过，输出被误判成一行）。
        "\"{0}`t{1}`t{2}\" -f $n,$d,$_.IPAddress } }"
    )
    try:
        # 两个坑一起躲：
        # ① 不能直接 text=True —— 中文 Windows 下 PowerShell 吐的是 GBK，
        #    按 utf-8 解会在读线程里抛 UnicodeDecodeError（还把整个调用带崩）。
        # ② 所以先拿 bytes，再按 utf-8 → gbk 顺序试。
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=25)
        raw = r.stdout or b""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
    except Exception:
        return []
    rows = []
    for line in text.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3 or not parts[2]:
            continue
        alias, desc, ip = (p.strip() for p in parts)
        low = (alias + " " + desc).lower()
        virt = any(h in low for h in VIRTUAL_HINTS)
        rows.append({"alias": alias, "desc": desc, "ip": ip, "virtual": virt})
    return rows


def _parse_target(tok):
    if ":" not in tok:
        raise ValueError("目标要写成 host:port：%r" % tok)
    h, _, p = tok.rpartition(":")
    return h, int(p)


def main(argv):
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    only_local = "--local" in argv
    argv = [a for a in argv if a != "--local"]

    report = {"adapters": [], "targets": []}

    # 网卡信息**默认就带**：2026-10-25 那次错就错在「没看网卡就把地址报给对端」，
    # 把 RNDIS 的 DHCP 地址当成局域网地址发出去，对端永远连不上。
    # 放在默认路径里，才不会再被忘掉。
    report["adapters"] = local_adapters()

    for tok in argv:
        try:
            host, port = _parse_target(tok)
        except ValueError as e:
            if not as_json:
                print("[!] %s" % e)
            continue
        report["targets"].append(probe(host, port))

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if report["adapters"]:
        print("=== 本机网卡（报地址前先看这一栏）===")
        for a in report["adapters"]:
            tag = "虚拟/非局域网" if a["virtual"] else "物理"
            print("  %-16s %-38s %-15s  %s" % (a["alias"][:16], a["desc"][:38],
                                               a["ip"], tag))
        virt = [a for a in report["adapters"] if a["virtual"]]
        if virt and len(virt) == len(report["adapters"]):
            print("  [!!] 本机所有在用地址都在虚拟网段上 —— 对端大概率没有到这里的路由，")
            print("       别把这些地址报成「局域网地址」，要么让对端连出，要么换物理网段。")

    if report["targets"]:
        print("=== 目标 ===")
        for t in report["targets"]:
            print("  %s:%s  %-8s %5dms  %s" % (t["host"], t["port"], t["status"],
                                               t["ms"], t["banner"] or ""))
            print("      ↳ %s" % _NOTES[t["status"]])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
