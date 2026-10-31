"""WebSocket TCP 隧道：让「只给 HTTP 的免费 PaaS」也能当代理出口。

## 为什么需要这一层

2026-10-31 实测把路收敛到一条：
  · Google 整段在 TLS 阶段被切断（③ 类），本地怎么弄都没用；
  · 公共 YouTube 镜像 16 个全黑洞 —— 公开的就被点名；
  · 但免费平台的**部署域名**有 10 个没被点名（onrender.com / koyeb.app / netlify.app …）。

矛盾在于：这些平台只给 HTTP 反向代理，而代理需要裸 TCP（CONNECT）。
解法是把 TCP 装进 WebSocket —— 所有 PaaS 都支持 WS 长连接：

    本机 proxy.py ──WS(TLS)──→ 平台上的 relay（墙外）──TCP──→ 目标站

本机看到的门牌号是 `xxx.onrender.com`（没被点名），
真正的 TCP 由墙外那台机器发出，不经过任何检查。

## 协议（故意做得很小，方便用任何语言重写）

  1. 客户端连 `wss://<host>/tunnel`，做标准 WebSocket 握手；
  2. 客户端发一条 **文本帧**：{"host": "www.youtube.com", "port": 443}
  3. 服务端连上后回一条文本帧：{"ok": true} 或 {"ok": false, "err": "..."}
  4. 之后双方只发**二进制帧**，帧内容就是原始 TCP 字节流，双向。

  一个 WS 连接只承载一条 TCP 流（不做多路复用）。理由：视频流是单条大流量，
  多路复用省下的那点握手开销，抵不上它带来的队头阻塞和实现复杂度。

## 用法

  服务端（部署到 PaaS，监听 $PORT）：
      python wsrelay.py --serve --port 8080

  客户端（本机，给 proxy.py 当上游）：
      python proxy.py --port 7822 --upstream-ws wss://xxx.onrender.com/tunnel

  自测（本机同时起服务端和客户端，验证机制）：
      python wsrelay.py --selftest
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
CONNECT_TIMEOUT = 15


# ---------------------------------------------------------------- 帧编解码
def _accept_key(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()


def ws_send(f, payload: bytes, opcode: int = OP_BIN, mask: bool = False):
    """发一帧。客户端必须加掩码（RFC 6455 6.1），服务端不许加。"""
    n = len(payload)
    head = bytearray()
    head.append(0x80 | opcode)
    m_bit = 0x80 if mask else 0
    if n < 126:
        head.append(m_bit | n)
    elif n < 65536:
        head.append(m_bit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(m_bit | 127)
        head += struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    f.write(bytes(head))
    if payload:
        f.write(payload)
    f.flush()


class FrameReader:
    """从 socket 文件对象读 WS 帧。带缓冲，因为一帧可能被拆在多次 recv 里。"""

    def __init__(self, f):
        self.f = f
        self.buf = bytearray()

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.f.read(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("peer closed")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_frame(self):
        """返回 (opcode, payload)。收到 close 返回 (OP_CLOSE, b'')。"""
        b0, b1 = self._need(2)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._need(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._need(8))[0]
        key = self._need(4) if masked else None
        payload = self._need(n) if n else b""
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return opcode, payload


# ---------------------------------------------------------------- 服务端
class WsTunnelServer:
    """把 WS 连接变成一条 TCP 隧道。零依赖，跑在任意能监听 $PORT 的 PaaS 上。"""

    def __init__(self, port: int, host: str = "0.0.0.0", verbose: bool = True):
        self.port = port
        self.host = host
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print("[wsrelay]", *a, flush=True)

    def serve_forever(self, ready_cb=None):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(64)
        # port=0 时由系统分配，必须回填真实端口，否则调用方拿到的是 0
        self.port = srv.getsockname()[1]
        self.log(f"listening {self.host}:{self.port}")
        if ready_cb:
            ready_cb(self.port)
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr):
        try:
            self._serve_conn(conn, addr)
        except Exception as e:
            self.log(f"{addr} 断开: {type(e).__name__}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _read_http_head(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = conn.recv(4096)
            if not d:
                raise ConnectionError("closed during handshake")
            buf += d
            if len(buf) > 32768:
                raise ConnectionError("header too large")
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head.decode("latin1"), rest

    def _serve_conn(self, conn, addr):
        head, rest = self._read_http_head(conn)
        lines = head.split("\r\n")
        path = lines[0].split()[1] if len(lines[0].split()) > 1 else "/"
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()

        # 健康检查：PaaS 会来要 200，不能因为没 WS 头就拒绝
        if hdrs.get("upgrade", "").lower() != "websocket":
            body = b'{"ok":true,"service":"skynet-wsrelay"}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            return

        key = hdrs.get("sec-websocket-key", "")
        if not key:
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            return
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + _accept_key(key).encode() + b"\r\n\r\n"
        )

        f = conn.makefile("rwb", buffering=0)
        if rest:
            f = _PreloadedFile(rest, f)
        reader = FrameReader(f)

        # 第一条文本帧：目标地址
        op, payload = reader.read_frame()
        if op != OP_TEXT:
            ws_send(f, b'{"ok":false,"err":"first frame must be text"}', OP_TEXT)
            return
        try:
            req = json.loads(payload.decode())
            thost, tport = req["host"], int(req.get("port", 443))
        except Exception as e:
            ws_send(f, json.dumps({"ok": False, "err": f"bad request: {e}"}).encode(), OP_TEXT)
            return

        try:
            up = socket.create_connection((thost, tport), timeout=CONNECT_TIMEOUT)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            self.log(f"{thost}:{tport} 连接失败 {msg}")
            ws_send(f, json.dumps({"ok": False, "err": msg}).encode(), OP_TEXT)
            return

        self.log(f"{addr[0]} → {thost}:{tport}")
        ws_send(f, b'{"ok":true}', OP_TEXT)
        self._pump(reader, f, up)

    def _pump(self, reader, f, up):
        """双向泵：WS 二进制帧 ↔ TCP。任一侧断开就收摊。"""
        stop = threading.Event()

        def down():
            # TCP → WS
            try:
                while not stop.is_set():
                    data = up.recv(65536)
                    if not data:
                        break
                    ws_send(f, data, OP_BIN)
            except Exception:
                pass
            finally:
                stop.set()
                try:
                    ws_send(f, b"", OP_CLOSE)
                except Exception:
                    pass

        t = threading.Thread(target=down, daemon=True)
        t.start()
        try:
            while not stop.is_set():
                op, payload = reader.read_frame()
                if op == OP_CLOSE:
                    break
                if op == OP_PING:
                    ws_send(f, payload, OP_PONG)
                    continue
                if op in (OP_BIN, OP_CONT) and payload:
                    up.sendall(payload)
        except Exception:
            pass
        finally:
            stop.set()
            try:
                up.close()
            except Exception:
                pass


class _PreloadedFile:
    """握手时多读出来的字节要塞回流的开头，否则会丢首包（SOCKS5 踩过同款坑）。"""

    def __init__(self, head: bytes, f):
        self.head = head
        self.f = f

    def read(self, n=-1):
        if self.head:
            if n is None or n < 0:
                out, self.head = self.head, b""
                return out
            out, self.head = self.head[:n], self.head[n:]
            return out
        return self.f.read(n)

    def write(self, data):
        return self.f.write(data)

    def flush(self):
        return self.f.flush()


# ---------------------------------------------------------------- 客户端
class WsSock:
    """伪装成 socket 的 WS 隧道端点：proxy.py 只用到 sendall / recv / close。"""

    def __init__(self, sock: socket.socket, f, reader: FrameReader):
        self.sock = sock
        self.f = f
        self.reader = reader
        self.pending = b""
        self.closed = False

    def sendall(self, data: bytes):
        if self.closed:
            raise ConnectionError("tunnel closed")
        ws_send(self.f, data, OP_BIN, mask=True)

    def recv(self, n: int = 65536) -> bytes:
        while not self.pending:
            if self.closed:
                return b""
            op, payload = self.reader.read_frame()
            if op == OP_CLOSE:
                self.closed = True
                return b""
            if op == OP_PING:
                ws_send(self.f, payload, OP_PONG, mask=True)
                continue
            if op in (OP_BIN, OP_CONT):
                self.pending = payload
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                ws_send(self.f, b"", OP_CLOSE, mask=True)
            except Exception:
                pass
        try:
            self.sock.close()
        except Exception:
            pass


def connect_via_ws(url: str, host: str, port: int, timeout: float = 20.0) -> WsSock:
    """连 `wss://xxx/tunnel`，让它替我们连 host:port，返回可读写的隧道端点。"""
    u = urllib.parse.urlsplit(url)
    tls = u.scheme in ("wss", "https")
    thost = u.hostname
    tport = u.port or (443 if tls else 80)
    path = u.path or "/"

    raw = socket.create_connection((thost, tport), timeout=timeout)
    if tls:
        ctx = ssl.create_default_context()
        raw = ctx.wrap_socket(raw, server_hostname=thost)
    raw.settimeout(timeout)

    key = base64.b64encode(os.urandom(16)).decode()
    raw.sendall(
        f"GET {path} HTTP/1.1\r\nHost: {thost}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        f"User-Agent: skynet-wsrelay/1\r\n\r\n".encode()
    )

    buf = b""
    while b"\r\n\r\n" not in buf:
        d = raw.recv(4096)
        if not d:
            raise ConnectionError("handshake closed")
        buf += d
    head, _, rest = buf.partition(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0]
    if b" 101 " not in first:
        raise ConnectionError(f"upgrade refused: {first[:80]!r}")

    f = raw.makefile("rwb", buffering=0)
    if rest:
        f = _PreloadedFile(rest, f)
    reader = FrameReader(f)

    ws_send(f, json.dumps({"host": host, "port": port}).encode(), OP_TEXT, mask=True)
    op, payload = reader.read_frame()
    if op != OP_TEXT:
        raise ConnectionError(f"expected text reply, got op={op}")
    resp = json.loads(payload.decode())
    if not resp.get("ok"):
        raise ConnectionError(f"relay refused: {resp.get('err')}")
    return WsSock(raw, f, reader)


# ---------------------------------------------------------------- 自测
def _selftest() -> int:
    print("[1/3] 起一个本地 echo 服务 …", end=" ")
    echo_srv = socket.socket()
    echo_srv.bind(("127.0.0.1", 0))
    echo_srv.listen(4)
    echo_port = echo_srv.getsockname()[1]

    def echo():
        while True:
            try:
                c, _ = echo_srv.accept()
            except OSError:
                return
            threading.Thread(target=lambda s: _echo_loop(s), args=(c,), daemon=True).start()

    threading.Thread(target=echo, daemon=True).start()
    print(f"OK (port {echo_port})")

    print("[2/3] 起隧道服务端 …", end=" ")
    srv = WsTunnelServer(0, verbose=False)
    box = {}
    t = threading.Thread(
        target=srv.serve_forever, kwargs={"ready_cb": lambda p: box.update(port=p)}, daemon=True
    )
    t.start()
    for _ in range(100):
        if "port" in box:
            break
        time.sleep(0.05)
    assert "port" in box, "服务端没起来"
    srv_port = box["port"]
    print(f"OK (port {srv_port})")

    print("[3/3] 经 WS 隧道打 echo …", end=" ")
    tun = connect_via_ws(f"ws://127.0.0.1:{srv_port}/tunnel", "127.0.0.1", echo_port)
    payload = b"skynet-wsrelay-selftest-" + os.urandom(8).hex().encode()
    tun.sendall(payload)
    got = b""
    deadline = time.time() + 8
    while len(got) < len(payload) and time.time() < deadline:
        got += tun.recv(65536)
    tun.close()
    assert got == payload, f"回显不一致: 发 {payload!r} 收 {got!r}"
    print(f"OK ({len(payload)}B 完整往返)")

    print("\n自测结果：3 过 / 0 败")
    print("说明：这只验证 WS↔TCP 的搬运没问题。")
    print("      能不能到 YouTube 取决于部署在墙外的那台机器，本机测不了。")
    return 0


def _echo_loop(s):
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            s.sendall(d)
    except Exception:
        pass
    finally:
        try:
            s.close()
        except Exception:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="WebSocket TCP 隧道（PaaS 友好）")
    ap.add_argument("--serve", action="store_true", help="服务端模式（部署到 PaaS 时用）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()
    if a.serve:
        WsTunnelServer(a.port, a.host).serve_forever()
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
