#!/usr/bin/env python3
"""cloudnode · 云端出口代理（SOCKS5 + HTTP CONNECT 双模）

为什么要有这个文件：
  免费 PaaS 给的都是「HTTP 反向代理」，CONNECT 方法过不去。
  唯一能穿透的形态是「裸 TCP 隧道」——隧道两端都只能看到字节流，
  所以代理必须自己实现 SOCKS5 / CONNECT 握手。

  典型部署：GitHub Actions runner 里跑本文件 → 再用 ssh -R 打 TCP 隧道出去
  → 国内客户端连隧道地址 → 流量经 runner 出网。

设计：
  · 零第三方依赖（只用标准库），runner 上不用 pip install。
  · 同一端口双模：首字节 0x05 走 SOCKS5，其余按 HTTP CONNECT 解析。
  · 纯转发，不解析 TLS，不做 MITM。

    python3 proxy.py --port 1080 --host 127.0.0.1
    python3 proxy.py --selftest
"""
from __future__ import annotations

import argparse
import select
import socket
import struct
import sys
import threading
import time

BUFSIZE = 65536
CONNECT_TIMEOUT = 15
IDLE_TIMEOUT = 180

# 域名 → IP 覆写表。
# 实测依据（2026-10-31）：github.com 的 DNS 结果 20.205.243.166 被掐（TCP 超时），
# 但 20.27.177.113 / 20.200.245.247 / 140.82.121.3 都能直接 200。
# 也就是说这一层是「按 IP 封」，不是「按 SNI 封」——换 IP 即可，不需要分片对抗。
# 注意：只换目标 IP，TLS 的 SNI 与证书校验仍由客户端按真实域名做，所以不会被降级。
# 一个域名可对应多个候选 IP，按顺序试——实测 41 个 GitHub IP 只有 3 个同时可用，
# 且十分钟内就会变，单 IP 必然失败。这里只做 TCP 层重试：
# 一旦握手交给客户端，TLS 失败的连接就补不回来了（客户端已经看到断流）。
IP_MAP: dict[str, list[str]] = {}

# 上游级联代理。这就是「双重」那一层：
# 本机代理对外监听热点网卡，内部把连接交给另一个只绑 127.0.0.1 的代理（如机场客户端）。
# 这样热点下的设备不用装任何东西，也能用上本机已有的通道。
# 上游失败时自动回落到 IP 覆写/直连，不会因为上游挂了就整体不可用。
UPSTREAM: tuple[str, int] | None = None


CRLF = chr(13) + chr(10)
CRLF2 = (chr(13) + chr(10)).encode() * 2


def _dial_upstream(host: str, port: int) -> socket.socket:
    """向上游代理发 CONNECT，成功后把裸 TCP 交回来。"""
    if UPSTREAM is None:
        raise ConnectionError("no upstream")
    up = socket.create_connection(UPSTREAM, timeout=CONNECT_TIMEOUT)
    req = (
        "CONNECT " + host + ":" + str(port) + " HTTP/1.1" + CRLF
        + "Host: " + host + ":" + str(port) + CRLF
        + "Proxy-Connection: keep-alive" + CRLF + CRLF
    )
    up.sendall(req.encode())
    buf = b""
    while CRLF2 not in buf:
        d = up.recv(4096)
        if not d:
            up.close()
            raise ConnectionError("upstream closed")
        buf += d
        if len(buf) > 16384:
            up.close()
            raise ConnectionError("upstream header too large")
    status = buf.split((chr(13) + chr(10)).encode(), 1)[0]
    if b" 200" not in status:
        up.close()
        raise ConnectionError("upstream refused: " + repr(status[:60]))
    return up


def _dial(host: str, port: int) -> socket.socket:
    if UPSTREAM is not None:
        try:
            return _dial_upstream(host, port)
        except Exception:
            pass  # 上游不可用 → 回落，不让热点设备整体断网
    ips = IP_MAP.get(host.lower())
    if not ips:
        return socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    last: Exception | None = None
    for ip in ips:
        try:
            return socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
        except Exception as e:
            last = e
    raise last or OSError("no candidate ip")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


class Buf:
    """带预读缓冲的读取器。

    首包 recv 常常一口气把「版本+方法数+方法列表」甚至 CONNECT 请求头都带进来，
    所以握手必须从同一块缓冲里消费，不能再对 socket 单独 recv —— 那会永久阻塞。
    （自测里 SOCKS5 卡死的直接原因就是这个。）
    """

    def __init__(self, sock: socket.socket, initial: bytes = b""):
        self.sock = sock
        self.buf = initial

    def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("peer closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def drain(self) -> bytes:
        out, self.buf = self.buf, b""
        return out


def pipe(a: socket.socket, b: socket.socket) -> None:
    """双向转发，任一方向关闭即收尾。"""
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, IDLE_TIMEOUT)
            if x or not r:
                break
            for s in r:
                data = s.recv(BUFSIZE)
                if not data:
                    return
                dst = b if s is a else a
                dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass


def _socks5(conn: socket.socket, head: bytes) -> None:
    """SOCKS5，只支持 CONNECT（cmd=1），免认证。"""
    r = Buf(conn, head)
    r.read(1)                       # VER
    nm = r.read(1)[0]               # NMETHODS
    r.read(nm)                      # METHODS（不校验，一律免认证）
    conn.sendall(b"\x05\x00")

    req = r.read(4)
    cmd, atyp = req[1], req[3]
    if atyp == 1:
        host = socket.inet_ntoa(r.read(4))
    elif atyp == 3:
        ln = r.read(1)[0]
        host = r.read(ln).decode("latin1")
    elif atyp == 4:
        host = socket.inet_ntop(socket.AF_INET6, r.read(16))
    else:
        conn.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
        return
    port = struct.unpack(">H", r.read(2))[0]

    if cmd != 1:
        conn.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
        return
    try:
        up = _dial(host, port)
    except Exception:
        conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
        return
    conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
    rest = r.drain()
    if rest:
        up.sendall(rest)
    pipe(conn, up)


def _http_connect(conn: socket.socket, head: bytes) -> None:
    """HTTP CONNECT（绝对 URI 的普通代理请求不支持，只做隧道）。"""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(BUFSIZE)
        if not chunk:
            return
        head += chunk
        if len(head) > 32768:
            conn.sendall(b"HTTP/1.1 431 Too Large\r\n\r\n")
            return

    line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = line.split()
    if len(parts) < 3:
        conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        return
    method, target = parts[0].upper(), parts[1]
    if method != "CONNECT":
        conn.sendall(b"HTTP/1.1 405 Only CONNECT\r\n\r\n")
        return
    try:
        host, port_s = target.rsplit(":", 1)
        port = int(port_s)
    except Exception:
        conn.sendall(b"HTTP/1.1 400 Bad Target\r\n\r\n")
        return
    try:
        up = _dial(host.strip("[]"), port)
    except Exception:
        conn.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        return
    conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    # 客户端可能在等 200 之前就把 TLS ClientHello 一起塞过来了，这部分不能丢
    rest = head[head.find(b"\r\n\r\n") + 4:]
    if rest:
        up.sendall(rest)
    pipe(conn, up)


def _serve_one(conn: socket.socket) -> None:
    try:
        conn.settimeout(CONNECT_TIMEOUT)
        head = conn.recv(BUFSIZE)
        if not head:
            return
        if head[0] == 0x05:
            _socks5(conn, head)
        else:
            _http_connect(conn, head)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def serve(host: str, port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)
    print(f"[cloudnode] listening {host}:{port} (SOCKS5 + CONNECT)", flush=True)
    while True:
        try:
            conn, addr = srv.accept()
        except KeyboardInterrupt:
            break
        except Exception:
            continue
        conn.settimeout(None)
        threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()


def _selftest() -> int:
    """本机自测：起服务 → 用 SOCKS5 与 CONNECT 各走一次 → 校验字节往返。"""
    import subprocess

    srv_port = 18080
    threading.Thread(target=serve, args=("127.0.0.1", srv_port), daemon=True).start()
    time.sleep(0.6)
    ok = 0
    fail = 0

    # ① SOCKS5：连本地一个 echo 服务
    echo = socket.socket()
    echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    echo.bind(("127.0.0.1", 18081))
    echo.listen(1)

    def _echo_once():
        c, _ = echo.accept()
        c.sendall(b"ECHO-" + c.recv(64))
        c.close()

    threading.Thread(target=_echo_once, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv_port), timeout=5)
        s.sendall(b"\x05\x01\x00")
        assert s.recv(2) == b"\x05\x00", "socks5 握手失败"
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack(">H", 18081))
        rep = s.recv(10)
        assert rep[1] == 0, f"socks5 connect 失败 code={rep[1]}"
        s.sendall(b"ping")
        assert s.recv(64) == b"ECHO-ping", "socks5 转发字节不符"
        s.close()
        print("  [1/2] SOCKS5 隧道 ................ OK")
        ok += 1
    except Exception as e:
        print(f"  [1/2] SOCKS5 隧道 ................ FAIL {type(e).__name__}: {e}")
        fail += 1

    # ② HTTP CONNECT：同样打 echo
    threading.Thread(target=_echo_once, daemon=True).start()
    try:
        s = socket.create_connection(("127.0.0.1", srv_port), timeout=5)
        s.sendall(b"CONNECT 127.0.0.1:18081 HTTP/1.1\r\nHost: x\r\n\r\n")
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(256)
        assert b"200" in resp.split(b"\r\n")[0], f"CONNECT 未 200: {resp[:60]}"
        s.sendall(b"ping")
        assert s.recv(64) == b"ECHO-ping", "CONNECT 转发字节不符"
        s.close()
        print("  [2/2] HTTP CONNECT 隧道 .......... OK")
        ok += 1
    except Exception as e:
        print(f"  [2/2] HTTP CONNECT 隧道 .......... FAIL {type(e).__name__}: {e}")
        fail += 1

    print(f"\n自测结果：{ok} 过 / {fail} 败")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1080)
    ap.add_argument("--map", action="append", default=[], metavar="DOMAIN=IP",
                    help="域名到 IP 的覆写，逗号分隔多个候选；可重复，例如 --map github.com=1.2.3.4,5.6.7.8")
    ap.add_argument("--upstream", metavar="HOST:PORT",
                    help="级联到一个上游代理，例如 127.0.0.1:3067；失败自动回落")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    global UPSTREAM
    if a.upstream:
        uh, up_ = a.upstream.rsplit(":", 1)
        UPSTREAM = (uh, int(up_))
        print(f"[cloudnode] 上游级联 -> {UPSTREAM[0]}:{UPSTREAM[1]}", flush=True)
    for item in a.map:
        if "=" not in item:
            raise SystemExit(f"--map 格式应为 DOMAIN=IP，收到：{item}")
        dom, ips = item.split("=", 1)
        IP_MAP[dom.strip().lower()] = [x.strip() for x in ips.split(",") if x.strip()]
    if IP_MAP:
        print(f"[cloudnode] IP 覆写 {len(IP_MAP)} 条: {IP_MAP}", flush=True)
    if a.selftest:
        return _selftest()
    try:
        serve(a.host, a.port)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
