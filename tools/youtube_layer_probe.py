"""YouTube 到底是被哪一层封的 —— 分层实测，不靠猜。

要回答的问题：
  1. 是 DNS 被污染（返回假 IP），还是真 IP 的 TCP 就不通？
     —— 分开测：先看本机 DNS 返回什么，再拿已知 Google IP 段直接连，绕过 DNS。
  2. TCP 能通但 TLS 被 reset？那封的是 SNI（和 GitHub 同一种），不是 IP。
  3. 同一个 IP 换 SNI 能不能过？能过 → 按域名封；不能过 → 按 IP 封。
  4. UDP/443（QUIC，YouTube 主要走这个）通不通？

判定表：
  TCP 通 + TLS(youtube) 失败 + TLS(无害域名) 成功 → 只封 SNI，可用分片/ECH 试
  TCP 通 + 两种 SNI 都失败                      → 按 IP 封（TCP 层被中间设备掐断）
  TCP 不通                                      → 单纯 IP 黑洞或 DNS 污染
"""
from __future__ import annotations

import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor

# 已知的 Google / YouTube 段，硬编码绕过 DNS。
# 142.250 / 172.217 / 216.58 / 74.125 / 34.102 都是 Google 自有 AS15169。
CANDIDATES = [
    "142.250.66.78", "142.250.71.142", "142.250.72.14", "142.250.74.206",
    "142.250.185.78", "142.250.190.46", "142.250.191.110", "142.250.4.190",
    "172.217.163.46", "172.217.14.206", "172.217.160.78", "172.217.24.14",
    "172.217.194.190", "172.217.12.206",
    "216.58.200.46", "216.58.214.14", "216.58.207.78", "216.58.210.14",
    "74.125.24.190", "74.125.130.190", "74.125.68.190", "74.125.200.190",
    "34.102.136.180", "34.120.208.123", "35.244.181.201",
]

# 无害 SNI：如果这个能过而 youtube 不能，说明按域名封而非按 IP 封。
BENIGN_SNI = "pypi.org"


def tcp_probe(ip: str, timeout: float = 4.0) -> int | None:
    """返回 TCP 建连耗时 ms；失败返回 None。"""
    try:
        t = time.time()
        s = socket.create_connection((ip, 443), timeout=timeout)
        ms = round((time.time() - t) * 1000)
        s.close()
        return ms
    except Exception:
        return None


def tls_probe(ip: str, sni: str, timeout: float = 6.0) -> tuple[bool, str, int]:
    """向 ip:443 用指定 SNI 握手。返回 (成功, 说明, 耗时ms)。"""
    try:
        t = time.time()
        s = socket.create_connection((ip, 443), timeout=timeout)
    except Exception as e:
        return False, f"TCP:{type(e).__name__}", 0
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=sni)
        ver = ss.version()
        ss.close()
        return True, ver or "?", round((time.time() - t) * 1000)
    except Exception as e:
        return False, type(e).__name__, round((time.time() - t) * 1000)
    finally:
        try:
            s.close()
        except Exception:
            pass


def udp_probe(ip: str, timeout: float = 2.0) -> str:
    """粗略探测 UDP/443（QUIC）。静默=可能被丢也可能服务端不回；RST=端口不可达但网通。"""
    try:
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(timeout)
        # 发一个最小 UDP 载荷，不构成完整 QUIC Initial，服务端必然丢弃；
        # 所以只看有没有 ICMP 回来 —— 有回包说明链路层通，纯静默说明被丢。
        u.sendto(b"\x00" * 48, (ip, 443))
        try:
            u.recvfrom(2048)
            return "有回包(链路通)"
        except socket.timeout:
            return "静默(被丢或服务端忽略)"
        except ConnectionRefusedError:
            return "ICMP不可达(链路通)"
        except Exception as e:
            return type(e).__name__
    finally:
        try:
            u.close()
        except Exception:
            pass


def main() -> None:
    print("=" * 78)
    print("① DNS 层：先看本机解析出来的 IP 是不是真的 Google 段")
    print("=" * 78)
    for host in ("www.youtube.com", "youtube.com", "www.google.com"):
        try:
            infos = socket.getaddrinfo(host, 443, socket.AF_INET)
            ips = sorted({i[4][0] for i in infos})
            in_g = [i for i in ips if i.startswith(("142.250.", "172.217.", "216.58.", "74.125.", "34.", "35."))]
            tag = "看起来是真 IP" if in_g else "★ 疑似被污染（不在 Google 段）"
            print(f"  {host:18} -> {', '.join(ips[:6])}   {tag}")
        except Exception as e:
            print(f"  {host:18} -> 解析失败 {type(e).__name__}")

    print()
    print("=" * 78)
    print("② IP 层：直连已知 Google IP 的 TCP/443（完全绕过 DNS）")
    print("=" * 78)
    with ThreadPoolExecutor(max_workers=16) as ex:
        tcp_res = list(ex.map(tcp_probe, CANDIDATES))
    ok_tcp = [(ip, ms) for ip, ms in zip(CANDIDATES, tcp_res) if ms is not None]
    print(f"  TCP 可建连: {len(ok_tcp)} / {len(CANDIDATES)}")
    for ip, ms in sorted(ok_tcp, key=lambda x: x[1]):
        print(f"    {ip:18} {ms:5}ms")

    if not ok_tcp:
        print("\n  → 全部 TCP 不通：YouTube 走的是「IP 层黑洞」或 DNS 污染，没有 SNI 层的余地。")
        print("  → 结论：本地无论怎么改 ClientHello 都救不了，必须有境外节点。")
        return

    print()
    print("=" * 78)
    print("③ TLS 层：TCP 通的那几个，换 SNI 实验（分清「按 IP 封」还是「按域名封」）")
    print("=" * 78)
    sample = [ip for ip, _ in sorted(ok_tcp, key=lambda x: x[1])[:5]]
    for ip in sample:
        ok_y, why_y, ms_y = tls_probe(ip, "www.youtube.com")
        ok_p, why_p, ms_p = tls_probe(ip, BENIGN_SNI)
        print(f"  {ip:18} SNI=youtube  -> {'OK ' + why_y if ok_y else why_y:22} {ms_y:5}ms")
        print(f"  {'':18} SNI={BENIGN_SNI:11} -> {'OK ' + why_p if ok_p else why_p:22} {ms_p:5}ms")
        if not ok_y and ok_p:
            print(f"  {'':18} ⇒ 同一 IP，换 SNI 能过 ⇒ 按【域名】封，不是按 IP 封")
        elif not ok_y and not ok_p:
            print(f"  {'':18} ⇒ 两种 SNI 都死 ⇒ 按【IP】封（TCP 层被中间设备切断）")
        print()

    print("=" * 78)
    print("④ UDP/443（QUIC）：YouTube 大量走 HTTP/3，TCP 封了不代表 UDP 也封")
    print("=" * 78)
    for ip in sample[:3]:
        print(f"  {ip:18} UDP/443 -> {udp_probe(ip)}")


if __name__ == "__main__":
    main()
