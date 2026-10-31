"""对照实验：同一个 Google IP，下面三个域名到底谁能握手。

目的：分清「IP 被封」和「域名被封」。
  - 如果 google 自己的域名也不能握手 → 这个 IP 的 TLS 被整段掐死（IP 层封锁）
  - 如果 google 能握手、youtube 不能 → 按域名封（可救）
  - 如果都能握手 → 之前是 DNS 污染，拿真 IP 就能用（最理想）

同时探 QUIC：YouTube 大量走 HTTP/3，TCP 死不代表 UDP 死。
"""
from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import time

TARGETS = ["34.102.136.180", "34.120.208.123", "35.244.181.201", "74.125.24.190"]
SNIS = ["www.google.com", "www.youtube.com", "pypi.org", "cloudflare.com"]


def tls(ip: str, sni: str, timeout: float = 5.0) -> str:
    try:
        s = socket.create_connection((ip, 443), timeout=4)
    except Exception as e:
        return "TCP:" + type(e).__name__
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=sni)
        v = ss.version() or "?"
        ss.close()
        return "OK " + v
    except Exception as e:
        return type(e).__name__
    finally:
        try:
            s.close()
        except Exception:
            pass


def quic_curl(url: str) -> str:
    """真发 QUIC。curl 若不支持 --http3 就退回普通测，标注清楚。"""
    exe = shutil.which("curl")
    if not exe:
        return "无 curl"
    try:
        r = subprocess.run(
            [exe, "-sS", "--http3-only", "--max-time", "10", "-o", "/dev/null",
             "-w", "code=%{http_code}", url],
            capture_output=True, text=True, timeout=15,
        )
        out = (r.stdout or "") + (r.stderr or "")
        if "not supported" in out or "unrecognized" in out or "unknown" in out.lower():
            return "curl 不支持 h3（无法判定）"
        return out.strip()[:90]
    except Exception as e:
        return type(e).__name__


print("=" * 74)
print("同一个 IP，换域名 —— 谁过谁不过")
print("=" * 74)
print("%-17s %-20s %-20s %-18s %-18s" % ("IP", *SNIS))
for ip in TARGETS:
    row = [tls(ip, sni) for sni in SNIS]
    print("%-17s %-20s %-20s %-18s %-18s" % (ip, *row))

print()
print("=" * 74)
print("QUIC / HTTP3 实测（curl --http3-only，真发 QUIC 包）")
print("=" * 74)
for u in ["https://www.youtube.com", "https://www.google.com"]:
    print("  %-30s %s" % (u, quic_curl(u)))

print()
print("=" * 74)
print("DNS 污染取证：查这些「假 IP」到底属于谁")
print("=" * 74)
for ip in ["31.13.92.37", "104.244.42.197"]:
    try:
        h = socket.gethostbyaddr(ip)
        print("  %-16s 反查 -> %s" % (ip, h[0]))
    except Exception:
        print("  %-16s 反查失败（正常，很多 IP 无 PTR）" % ip)
