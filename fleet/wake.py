"""远程唤醒（WOL）：让设备层从「能连已开机的」升级到「能叫醒睡着的」。

 Wake-on-LAN 是标准协议（magic packet 无版权问题）：一个 UDP 包，
 载荷 = 6 字节 0xFF + 目标 MAC 重复 16 遍。网卡收到这个形状就开机。

为什么单独一个模块而不是塞进某个 kind：唤醒是**跨设备**动作——
电脑发广播唤醒任何LAN设备（手机/另一台电脑/NAS），不属于哪一台。
挂在 `host` kind 上当 `device.wake` 能力出口。

🔴 边界（照 UU 远程功能盘点计划书，只做标准件）：
  · 子网广播：`255.255.255.255` / 定向广播，发完即走
  · 定向唤醒：知道对方 IP 时单播（跨子网要靠路由器转发，属 P2）
  · **不管「对方收不收得到」**：WOL 无应答、无确认——发出去就完了。
    上层要的是「尝试过 + 包构造对了」，不是「一定醒」。想要确认，
    唤醒后配 `host.monitor`/`pc.state` 或 arp 表复查，那是另一拍。

参照实现：MeshCentral（Apache-2.0）的 WOL 模块——只看协议形状，
不抄代码（本仓 MIT，GPL/AGPL 的红线见融合计划书第五节）。
"""
from __future__ import annotations

import re
import socket
import struct

#: WOL 默认端口（7 与 9 最常见，路由器/WOL 工具两边都认）
DEFAULT_PORT = 9
#: magic packet 的「同步串」：6 字节全 1
SYNC = b"\xff" * 6
#: MAC 重复多少遍（协议规定 16）
REPEAT = 16

#: 接受的 MAC 写法：aa:bb / aa-bb / aabb / aabb.ccdd.eeff（点号也放行，
#: 部分路由器和交换机导出的配置就是点分格式）
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2})[:\-.]?([0-9a-fA-F]{2})[:\-.]?"
                     r"([0-9a-fA-F]{2})[:\-.]?([0-9a-fA-F]{2})[:\-.]?"
                     r"([0-9a-fA-F]{2})[:\-.]?([0-9a-fA-F]{2})$")


class WakeError(ValueError):
    """MAC 不合法或参数错误——调用方拿到就能说清哪错了。"""


def normalize_mac(mac):
    """把各种写法的 MAC 归一成 `aa:bb:cc:dd:ee:ff`（小写）。

    接受：`AA:BB:CC:DD:EE:FF` / `aa-bb-cc-dd-ee-ff` / `aabbccddeeff` /
    `aa.bb.cc.dd.ee.ff`（六组两位点分）。Cisco 三段式 `aabb.ccdd.eeff`
    **不认**——它和第 6 组点分格式歧义，宁可不收也不错收。
    """
    s = str(mac or "").strip()
    m = _MAC_RE.match(s)
    if not m:
        raise WakeError(
            f"MAC 不合法：{mac!r}（要 6 组十六进制，如 aa:bb:cc:dd:ee:ff）")
    return ":".join(g.lower() for g in m.groups())


def magic_packet(mac):
    """构造 magic packet：6×0xFF + MAC×16（共 102 字节）。"""
    norm = normalize_mac(mac)
    raw = bytes.fromhex(norm.replace(":", ""))
    return SYNC + raw * REPEAT


def wake(mac, ip="255.255.255.255", port=DEFAULT_PORT, timeout=0.0):
    """发一个 magic packet。返回 dict（不抛异常，失败也带原因）。

    `ip`：
      · `255.255.255.255`——受限广播（默认，多数网卡/NAT 下可达）
      · 定向广播如 `192.168.1.255`——某些路由器只放行子网广播
      · 具体 IP 如 `192.168.1.50`——单播（跨子网靠路由器设静态 ARP
        或转发，那是路由器侧的事，本模块只负责把包发对）

    🔴 WOL 无应答：这里只保证「包构造对、socket 发出去了」。
    `sent=True` 不等于「目标醒了」——调用方别拿它当确认。
    """
    out = {"mac": "", "ip": ip, "port": port, "sent": False,
           "bytes": 0, "error": ""}
    try:
        norm = normalize_mac(mac)
        pkt = magic_packet(norm)
    except WakeError as e:
        out["error"] = str(e)
        return out
    out["mac"] = norm
    out["bytes"] = len(pkt)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if timeout and timeout > 0:
            s.settimeout(timeout)
        s.sendto(pkt, (ip, int(port)))
        out["sent"] = True
    except OSError as e:
        # 广播被防火墙/网卡禁掉时明确说，别静默失败
        out["error"] = f"发送失败：{e}"
    finally:
        s.close()
    return out


def _selftest():
    """离线验证：MAC 归一 / 包构造逐字节对 / loopback 广播不报错。"""
    # ① 各种写法归一成同一串
    for w in ("AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aAbBcCdDeEfF",
              "aa.bb.cc.dd.ee.ff"):
        assert normalize_mac(w) == "aa:bb:cc:dd:ee:ff", w
    # ② 非法 MAC 必须抛且带原因
    for bad in ("", "zz:bb:cc:dd:ee:ff", "aa:bb:cc", "aabbccddeeff11"):
        try:
            normalize_mac(bad)
            raise AssertionError(f"{bad!r} 应该被拒")
        except WakeError:
            pass
    # ③ magic packet 逐字节对：6×FF + MAC×16 = 102 字节
    pkt = magic_packet("aa:bb:cc:dd:ee:ff")
    assert pkt == b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16, pkt[:20]
    assert len(pkt) == 102, len(pkt)
    # ④ loopback 广播不报错（127.0.0.1 不开广播位也能测通路径）
    r = wake("aa:bb:cc:dd:ee:ff", ip="127.0.0.1", port=9)
    assert r["sent"] and not r["error"] and r["mac"] == "aa:bb:cc:dd:ee:ff", r
    # ⑤ 非法 MAC 走 wake 也不抛，错误在返回里
    r2 = wake("不是mac")
    assert not r2["sent"] and "不合法" in r2["error"], r2
    print("wake selftest OK（5 项）")


if __name__ == "__main__":
    _selftest()
