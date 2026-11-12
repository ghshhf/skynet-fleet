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
import hmac
import json
import os
import queue
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

# GBK 防线（2026-11-01 铁律）：中文 Windows 控制台默认 cp936，
# 打印任何 GBK 外字符（· ⇒ 等）会 UnicodeEncodeError 直接炸死。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
CONNECT_TIMEOUT = 15


# ---------------------------------------------------------------- 帧编解码
def _accept_key(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()


def _errno_of(exc):
    """取系统错误码。跨平台：Windows 是 winerror，类 Unix 是 errno。"""
    return (getattr(exc, "winerror", None)
            or getattr(exc, "errno", None) or 0)


def _probe_hint(code):
    """把裸错误码翻成人话。

    为什么必须翻：`WinError 10049` 这种字串对调用方毫无用处，而它和
    `10061（连接被拒绝）`在处置上完全不同 —— 前者是「测不了」，后者是
    「真的进不来」。不区分就会把「参数没给对」当成「防火墙拦了」。
    """
    return {
        10049: "地址无效：多半是端口为 0 或本机没有这个地址（属于测不了，不是进不来）",
        10061: "连接被拒绝：对端没在监听，或被防火墙 REJECT",
        10060: "连接超时：包被丢弃，多半是防火墙 DROP 或地址不可达",
        10065: "无法到达主机：没有到该地址的路由",
        10013: "权限被拒：目标端口被策略禁止",
        11001: "域名解析失败",
        111: "连接被拒绝：对端没在监听",
        113: "无法到达主机：没有到该地址的路由",
        101: "网络不可达",
        60: "连接超时：包被丢弃（多半防火墙 DROP）",
    }.get(int(code or 0), "")


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


# ---------------------------------------------------------------- 中继配对
#
# 上面那一整套是「WS → 某个 host:port」，本质是**出口代理**：
# 客户端说自己要去哪，服务端替它连。
#
# 对等中继要的是另一件事：**两台机器各自接进来，由中继转发彼此的字节**。
# 谁都不指定目标，目标是「另一个也连进来的人」。
#
# 这两件事不能用同一套参数凑：出口代理的语义里压根没有「对端」这个概念。
# 所以 /pair 是一条独立路径，不是 /tunnel 加个 flag。
#
# 协议（/pair）：
#   1. WS 握手后第一条文本帧：{"room": "<id>", "token": "<hmac>"}
#   2. 房间空 → 回 {"ok":true,"waiting":true}，等第二个到
#   3. 房间有人 → 双方各收 {"ok":true,"paired":true}，此后二进制帧双向透传
#   4. 任一方断 → 另一方收 close
#
# 威胁模型（写清楚，免得高估它的安全性）：
#   token = HMAC(secret, room)[:32]，secret 由 WSRELAY_SECRET 下发、客户端共享。
#   它挡的是「扫到 /pair 端点就来占房间」这类被动/随机攻击；
#   **挡不住已经拿到 secret 的人**。要防内部人，得换成非对称签名或一次性票据。
#   房间号本身是 16 hex 随机（会话 id），这是第二道。

PAIR_WAIT = 30.0     # 第一个端点最多等多久
PAIR_PING = 20.0     # 保活间隔：PaaS 空闲超时通常 30~60s，20s 留足余量


def pair_token(room, secret):
    """房间令牌。room 与 secret 一起进 HMAC，改任一个都算不出来。"""
    return hmac.new(str(secret).encode("utf-8"), str(room).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


class PairHub:
    """房间 → 正在等待的端点。第二个到齐时把两个端点互指，配成一对。"""

    def __init__(self):
        self._waiting = {}
        self._lock = threading.Lock()
        self.stats = {"paired": 0, "expired": 0, "rejected": 0}

    def put(self, room, ep):
        """放一个端点进房间。返回已在等的那一个（配对成功），或 None（自己成为等待者）。"""
        with self._lock:
            other = self._waiting.pop(room, None)
            if other is None:
                self._waiting[room] = ep
                return None
            self.stats["paired"] += 1
            return other

    def drop(self, room, ep):
        """撤销等待。只撤自己，别把后来者踢了。"""
        with self._lock:
            if self._waiting.get(room) is ep:
                self._waiting.pop(room, None)


class _PairEnd:
    """一个中继端点。

    每个端点只有一个读线程，它负责把收到的字节**转给对方**；
    两个端点各一个读线程，双向管道就成形了 —— 不需要额外的转发线程。
    """

    def __init__(self, f, reader, addr=""):
        self.f = f
        self.reader = reader
        self.addr = addr
        self.peer = None
        self.stop = threading.Event()
        self._send_lock = threading.Lock()   # 读线程和 ping 线程都会写，必须串行
        self._closed = False

    def send(self, payload, opcode=OP_BIN):
        if self._closed:
            return False
        try:
            with self._send_lock:
                ws_send(self.f, payload, opcode)
            return True
        except Exception:
            self._closed = True
            return False

    def close(self):
        self._closed = True
        self.stop.set()
        try:
            with self._send_lock:
                ws_send(self.f, b"", OP_CLOSE)
        except Exception:
            pass

    def pump(self):
        """把本端点收到的字节转给对方；任一方向断掉就收摊。"""
        try:
            while not self.stop.is_set():
                op, payload = self.reader.read_frame()
                if op == OP_CLOSE:
                    break
                if op == OP_PING:
                    self.send(payload, OP_PONG)
                    continue
                if op in (OP_BIN, OP_CONT) and payload:
                    if self.peer is None or not self.peer.send(payload):
                        break
        except Exception:
            pass
        finally:
            self.stop.set()
            # 一端走了，另一端必须收到 close —— 否则它会在那儿傻等，
            # 上层看到的是「连着的」，实际对端早就没了。
            if self.peer is not None:
                self.peer.close()


def _keepalive(ep, interval=PAIR_PING):
    """定期发 WS ping。PaaS 会掐空闲连接，不保活的话「已连接」几分钟后静默失效。"""
    while not ep.stop.wait(interval):
        if not ep.send(b"", OP_PING):
            return


# ---------------------------------------------------------------- 地址簿
class _AddrBook:
    """设备指纹 → 当前地址。

    为什么需要它：IPv6 地址会轮换（RFC 4941 临时地址按小时变，家宽重新拨号
    前缀也会变）。如果对端把上次看到的地址存进配置，第二天必然连不上，
    而且现象看起来像「网络坏了」而不是「地址变了」。

    正确做法不是「只留稳定地址」，而是**把变化本身当作常态**：
    本机定期上报当前地址，对端按指纹查最新值。这样临时地址也能用，
    而且比只押一个稳定地址更抗变化。

    有效期刻意给 7 天而不是几分钟：设备关机再开机、路上断网几小时都正常，
    一过期就删会让「回家再连」变成「找不到设备」。
    """

    def __init__(self, ttl=7 * 86400, cap=2000, path=None):
        self._d = {}
        self._lock = threading.Lock()
        self.ttl = ttl
        self.cap = cap
        # 落盘路径。空 = 纯内存（旧行为）。PaaS 重启会把纯内存地址簿清空，
        # 而「自动上报」恰恰依赖地址簿活着 —— 所以默认要落盘。
        self.path = (path if path is not None
                     else os.environ.get("SKYNET_ADDR_STORE", "")).strip()
        self.persist = False          # 真的写成功过才算 True，不按「有路径」推断
        self.last_save_err = ""
        self._save_min = 2.0          # 写盘节流：地址每轮都在变，不能每轮都 fsync
        self._last_save = 0.0
        self.stats = {"put": 0, "hit": 0, "miss": 0,
                      "load": 0, "save": 0, "save_err": 0}
        if self.path:
            self._load()

    # ---- 落盘：内存为主，磁盘只做「重启后还在」这一件事
    def _load(self):
        """启动时读回。**任何异常都只降级、不阻断启动**：
        地址簿读不出来最多是「设备暂时找不到」，而中继起不来是全网不可用，
        两者不是一个量级，不能因为一个坏 JSON 让中继罢工。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            self.last_save_err = f"加载失败：{type(e).__name__}: {e}"
            self.stats["save_err"] += 1
            return

        items = raw.get("devices") if isinstance(raw, dict) else None
        now = time.time()
        n = 0
        for rec in items or []:
            if not isinstance(rec, dict):
                continue
            fp = str(rec.get("fp") or "").strip()[:80]
            addrs = rec.get("addrs")
            if not fp or not isinstance(addrs, list) or not addrs:
                continue
            try:
                seen = float(rec.get("seen") or 0)
            except (TypeError, ValueError):
                continue
            if now - seen > self.ttl:      # 落盘期间可能已经过期，读回来照样要清
                continue
            self._d[fp] = {"fp": fp, "addrs": addrs[:16], "ts": rec.get("ts") or seen,
                           "seen": seen,
                           "meta": rec.get("meta") if isinstance(rec.get("meta"), dict) else {}}
            n += 1
        self.stats["load"] = n
        self.persist = True               # 文件在且读得动 ⇒ 上次写得进去

    def _save_locked(self, force=False):
        """原子写：先写 .tmp 再 replace，避免断电留下半个 JSON。"""
        if not self.path:
            return False
        now = time.time()
        if not force and now - self._last_save < self._save_min:
            return False
        items = [{"fp": r["fp"], "addrs": r["addrs"], "ts": r["ts"],
                  "seen": r["seen"], "meta": r["meta"]} for r in self._d.values()]
        tmp = self.path + ".tmp"
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "saved": now, "devices": items},
                          f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.replace(tmp, self.path)
            except OSError:
                # Windows 上目标被占用时 replace 会失败，重试一次通常就过了
                time.sleep(0.05)
                os.replace(tmp, self.path)
            self.persist = True
            self.last_save_err = ""
            self._last_save = now
            self.stats["save"] += 1
            return True
        except Exception as e:
            # 只读磁盘（部分 PaaS）会走到这里。**不抛、不假装成功**：
            # 置 persist=False，状态接口里能看见「地址簿其实只在内存」。
            self.persist = False
            self.last_save_err = f"{type(e).__name__}: {e}"
            self.stats["save_err"] += 1
            return False

    def flush(self):
        with self._lock:
            return self._save_locked(force=True)

    def store_info(self):
        return {"path": self.path, "persist": self.persist,
                "devices": len(self._d), "ttl": self.ttl,
                "last_save_err": self.last_save_err,
                "saves": self.stats["save"], "save_err": self.stats["save_err"]}

    def put(self, fp, addrs, ts=None, meta=None):
        fp = str(fp or "").strip()[:80]
        if not fp:
            return {"ok": False, "err": "fingerprint 不能为空"}
        clean = []
        for c in (addrs or [])[:16]:
            if isinstance(c, dict) and c.get("ip"):
                clean.append({"ip": str(c["ip"])[:64],
                              "port": int(c.get("port") or 0),
                              "temporary": c.get("temporary")})
            elif isinstance(c, str) and c:
                clean.append({"ip": c[:64], "port": 0, "temporary": None})
        if not clean:
            return {"ok": False, "err": "没有可用地址"}

        now = time.time()
        rec = {"fp": fp, "addrs": clean, "ts": float(ts or now),
               "seen": now, "meta": (meta or {}) if isinstance(meta, dict) else {}}
        with self._lock:
            self._d[fp] = rec
            self.stats["put"] += 1
            self._sweep_locked(now)
            self._save_locked()
        return {"ok": True, "fp": fp, "addrs": len(clean)}

    def get(self, fp):
        with self._lock:
            rec = self._d.get(str(fp or "").strip())
            if rec is None:
                self.stats["miss"] += 1
                return None
            self.stats["hit"] += 1
            out = dict(rec)
        out["age"] = round(time.time() - out["seen"], 1)
        return out

    def list(self, limit=200):
        now = time.time()
        with self._lock:
            items = sorted(self._d.values(), key=lambda r: -r["seen"])[:limit]
        return [{"fp": r["fp"], "addrs": len(r["addrs"]),
                 "age": round(now - r["seen"], 1)} for r in items]

    def sweep(self):
        with self._lock:
            n = self._sweep_locked(time.time())
            if n:
                self._save_locked(force=True)
            return n

    def _sweep_locked(self, now):
        dead = [k for k, v in self._d.items() if now - v["seen"] > self.ttl]
        for k in dead:
            self._d.pop(k, None)
        # 上限保护：地址簿是内存里的，被灌爆会把中继一起拖死
        n = len(dead)
        if len(self._d) > self.cap:
            extra = sorted(self._d.items(), key=lambda kv: kv[1]["seen"])[:len(self._d) - self.cap]
            for k, _ in extra:
                self._d.pop(k, None)
            n += len(extra)
        return n


# ---------------------------------------------------------------- 服务端
class WsTunnelServer:
    """把 WS 连接变成一条 TCP 隧道。零依赖，跑在任意能监听 $PORT 的 PaaS 上。

    同一个进程里还挂了两件事，因为它们必须共用一份 secret 和同一个公网门牌：
      /pair  对等中继（两台机器配对后互相转发字节）
      /addr  地址簿（IPv6 地址会轮换，对端要按设备指纹查「现在在哪」）
    """

    def __init__(self, port: int, host: str = "0.0.0.0", verbose: bool = True,
                 secret: str = "", addr_path: str = ""):
        self.port = port
        self.host = host
        self.verbose = verbose
        # secret 为空时 /pair 与 /addr 一律拒绝 —— 开放的无鉴权中继等于给全网
        # 提供一个免费跳板，既危险也会被滥用。宁可不提供服务。
        self.secret = secret or os.environ.get("WSRELAY_SECRET", "")
        self.hub = PairHub()
        # 默认落盘到工作目录：PaaS 重启后地址簿还在，自动上报才有意义
        self.addrbook = _AddrBook(path=addr_path or os.environ.get("SKYNET_ADDR_STORE")
                                  or os.path.join(".", "addrbook.json"))

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
        parts = lines[0].split()
        method = parts[0] if parts else "GET"
        raw_path = parts[1] if len(parts) > 1 else "/"
        hdrs = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()

        u = urllib.parse.urlsplit(raw_path)
        route = (u.path or "/").rstrip("/") or "/"
        q = urllib.parse.parse_qs(u.query)
        is_ws = hdrs.get("upgrade", "").lower() == "websocket"

        # ---- 地址簿与外部回连检查：一次性小请求，开 WS 没意义，走普通 HTTP
        if route in ("/addr", "/probe") and not is_ws:
            self._serve_addr(conn, method, route, q, hdrs, rest)
            return

        # 健康检查：PaaS 会来要 200，不能因为没 WS 头就拒绝
        if not is_ws:
            body = b'{"ok":true,"service":"skynet-wsrelay",' \
                   b'"routes":["/tunnel","/pair","/addr","/probe"]}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            return

        # /tunnel 是出口代理：一旦对公网暴露，没有令牌就等于给全网一个开放代理。
        # 设了 secret 就必须验 HMAC 令牌（/pair 有自己的帧内令牌，另走一套）。
        if route != "/pair" and self.secret:
            if not hmac.compare_digest(pair_token("tunnel", self.secret),
                                       (q.get("k") or [""])[0]):
                self.log(f"tunnel 拒绝：令牌缺失或不对 {addr[0]}")
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
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

        # 第一条文本帧：路由参数
        op, payload = reader.read_frame()
        if op != OP_TEXT:
            ws_send(f, b'{"ok":false,"err":"first frame must be text"}', OP_TEXT)
            return
        try:
            req = json.loads(payload.decode())
        except Exception as e:
            ws_send(f, json.dumps({"ok": False, "err": f"bad request: {e}"}).encode(), OP_TEXT)
            return
        if not isinstance(req, dict):
            ws_send(f, b'{"ok":false,"err":"first frame must be an object"}', OP_TEXT)
            return

        if route == "/pair":
            self._serve_pair(f, reader, req, addr)
        else:
            self._serve_tunnel(f, reader, req, addr)

    # ---- /pair：对等中继
    def _serve_pair(self, f, reader, req, addr):
        if not self.secret:
            ws_send(f, b'{"ok":false,"err":"relay has no secret (set WSRELAY_SECRET)"}',
                    OP_TEXT)
            return

        room = str(req.get("room") or "").strip()[:64]
        token = str(req.get("token") or "")
        if not room or not token:
            ws_send(f, b'{"ok":false,"err":"room and token required"}', OP_TEXT)
            return
        if not hmac.compare_digest(pair_token(room, self.secret), token):
            self.hub.stats["rejected"] += 1
            self.log(f"{addr[0]} pair 拒绝：token 不对（room={room[:8]}）")
            ws_send(f, b'{"ok":false,"err":"bad token"}', OP_TEXT)
            return

        ep = _PairEnd(f, reader, addr=addr[0])
        other = self.hub.put(room, ep)

        if other is None:
            # 我是第一个：先告诉它「在等」，再阻塞等对端。
            # 等待期间必须保活，否则 PaaS 会在第 30 秒把这条连接掐掉，
            # 而配对超时恰好也是 30 秒 —— 会变成「永远差一点点」。
            ws_send(f, b'{"ok":true,"waiting":true}', OP_TEXT)
            threading.Thread(target=_keepalive, args=(ep,), daemon=True).start()

            deadline = time.time() + PAIR_WAIT
            while time.time() < deadline and ep.peer is None and not ep.stop.is_set():
                time.sleep(0.05)
            if ep.peer is None:
                self.hub.drop(room, ep)
                self.hub.stats["expired"] += 1
                ep.send(b'{"ok":false,"err":"no peer in time"}', OP_TEXT)
                ep.close()
                self.log(f"pair 超时 room={room[:8]}")
                return
            # 对端到了。**每个端点各自跑自己的 pump**，谁都不替别人起线程 ——
            # 因为本连接的 socket 生命周期归本线程管（_handle 的 finally 会在
            # 本函数返回后才关，替别人跑的话线程一放出去就被掐断）。
            self.log(f"pair 成对 room={room[:8]} {addr[0]}（等待侧）")
            ep.pump()
            return

        # 我是第二个到：把两边互指，通知双方，然后跑自己的 pump
        other.peer = ep
        ep.peer = other
        self.log(f"pair 成对 room={room[:8]} {other.addr} <-> {addr[0]}")
        if not other.send(b'{"ok":true,"paired":true}', OP_TEXT):
            ep.close()
            return
        if not ep.send(b'{"ok":true,"paired":true}', OP_TEXT):
            other.close()
            return
        threading.Thread(target=_keepalive, args=(ep,), daemon=True).start()
        ep.pump()

    # ---- /tunnel：出口代理（原逻辑）
    def _serve_tunnel(self, f, reader, req, addr):
        try:
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

    # ---- /addr 与 /probe：地址簿 + 外部回连检查
    def _serve_addr(self, conn, method, route, q, hdrs, rest):
        """地址簿读写 + **从外部反向连回来**的入站自检。

        `/probe` 是本文件里唯一能回答「对端到底能不能连进来」的地方。
        本机自己测入站是测不出来的：本机往自己的公网地址发包走的是
        hairpin，很多 NAT 根本不支持，通不通都不代表外网能连。
        只有站在墙外的人来试才算数 —— 中继正好就在墙外。
        """
        body = rest
        try:
            clen = int(hdrs.get("content-length") or 0)
        except ValueError:
            clen = 0
        while len(body) < clen:
            d = conn.recv(65536)
            if not d:
                break
            body += d

        def reply(obj, code=200):
            b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            conn.sendall(
                (f"HTTP/1.1 {code} OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                 f"Content-Length: {len(b)}\r\nConnection: close\r\n\r\n").encode() + b
            )

        if not self.secret:
            reply({"ok": False, "err": "relay has no secret (set WSRELAY_SECRET)"}, 503)
            return

        one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731

        # ---- 入站自检：从现在这台（墙外的）机器去连客户端的地址
        if route == "/probe":
            target = one("target")
            token = one("token")
            if not target:
                reply({"ok": False, "err": "target required"}, 400)
                return
            if not hmac.compare_digest(pair_token(f"probe:{target}", self.secret), token):
                reply({"ok": False, "err": "bad token"}, 403)
                return
            try:
                tport = int(one("port", "0") or 0)
            except ValueError:
                tport = 0
            # 端口 0 不是「一个连不上的端口」，而是**根本不是一个端口**：
            # Windows 会回 10049「请求的地址在其上下文中无效」。若把它当成
            # 「不可达」，就会把「还没开监听」误报成「被防火墙拦了」，
            # 让人去改一通根本没问题的防火墙。所以单独标 invalid，让上层区分。
            if tport <= 0:
                reply({"ok": False, "invalid": True, "target": target,
                       "err": "没有可探测端口（port=0）：先在本机开监听口再验"}, 400)
                return
            try:
                t0 = time.time()
                s = socket.create_connection((target, tport), timeout=5)
                # **连上之后取源地址**，这是整个 /probe 里最容易被忽略的一格，
                # 也是唯一能区分「真的从墙外连进来」和「连的其实是自己」的东西。
                #
                # 为什么必须报：中继如果就跑在对方机器上（或者对方用隧道把本地
                # 中继暴露成一个公网域名），它来连那个公网地址走的是**回环路径**，
                # 必然成功、毫秒级。那个「成功」什么都证明不了，却会被判成
                # 「入站通了」—— 对端拿到这个结论会直接去连、然后超时。
                # 客户端拿这个 from 与目标地址比前缀就能识破。
                try:
                    src = s.getsockname()[0]
                except Exception:
                    src = ""
                s.close()
                reply({"ok": True, "target": target, "from": src,
                       "ms": round((time.time() - t0) * 1000)})
            except Exception as e:
                reply({"ok": False, "target": target, "errno": _errno_of(e),
                       "err": "%s: %s" % (type(e).__name__, e),
                       "hint": _probe_hint(_errno_of(e))})
            return

        # ---- 地址簿：GET 查、POST 写
        if method.upper() == "POST":
            try:
                req = json.loads(body.decode("utf-8") or "{}")
            except Exception as e:
                reply({"ok": False, "err": f"bad json: {e}"}, 400)
                return
            fp = str(req.get("fp") or "")
            if not hmac.compare_digest(pair_token(f"put:{fp}", self.secret),
                                       str(req.get("token") or "")):
                reply({"ok": False, "err": "bad token"}, 403)
                return
            reply(self.addrbook.put(fp, req.get("addrs"),
                                    ts=req.get("ts"), meta=req.get("meta")))
            return

        fp = one("fp")
        if not fp:
            # 不给 fp = 列全表。顺带把「到底落没落盘」说清楚：
            # 只读磁盘的 PaaS 上 persist 会是 False，重启就清空，
            # 部署方必须能看见这件事，而不是等重启后才发现设备全消失了。
            reply({"ok": True, "devices": self.addrbook.list(),
                   "store": self.addrbook.store_info()})
            return
        rec = self.addrbook.get(fp)
        if rec is None:
            reply({"ok": False, "err": "unknown fingerprint", "fp": fp}, 404)
            return
        reply({"ok": True, **rec})

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

    def fileno(self):
        """让 proxy.py 的 select() 能把它和本地 socket 一起监听。

        没有这个方法时 pipe() 的 select 会抛 TypeError 并被静默吞掉，
        表现为「CONNECT 200 之后立刻断流」。
        """
        return self.sock.fileno()

    def settimeout(self, timeout):
        """隧道建立后转阻塞模式，空闲由上层 select 的 IDLE_TIMEOUT 管。

        握手期的 20s 超时如果留着，keep-alive 空闲超过 20s 就会被
        socket.timeout 打断，爬虫的会话复用会莫名其妙断掉。
        """
        return self.sock.settimeout(timeout)

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


def _tunnel_auth_path(path: str, query: str = "") -> str:
    """给 /tunnel 的 URL 补上访问令牌。

    服务端设了 WSRELAY_SECRET 就会验 `?k=`（不验就等于开放代理）；
    /pair 走的是帧内令牌，这里不动它。
    """
    if not path.rstrip("/").endswith("/tunnel"):
        return path
    secret = os.environ.get("WSRELAY_SECRET", "")
    q = urllib.parse.parse_qs(query)
    if secret and "k" not in q:
        q["k"] = [pair_token("tunnel", secret)]
    if not q:
        return path
    return path + "?" + urllib.parse.urlencode({k: v[0] for k, v in q.items()})


def connect_via_ws(url: str, host: str, port: int, timeout: float = 20.0) -> WsSock:
    """连 `wss://xxx/tunnel`，让它替我们连 host:port，返回可读写的隧道端点。"""
    u = urllib.parse.urlsplit(url)
    tls = u.scheme in ("wss", "https")
    thost = u.hostname
    tport = u.port or (443 if tls else 80)
    path = _tunnel_auth_path(u.path or "/", u.query)

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


def _open_ws(url: str, timeout: float = 20.0):
    """握手到 101，返回 (raw, f, reader)。connect_via_ws 与 connect_pair 共用。"""
    u = urllib.parse.urlsplit(url)
    tls = u.scheme in ("wss", "https")
    thost = u.hostname
    tport = u.port or (443 if tls else 80)
    path = _tunnel_auth_path(u.path or "/", u.query)

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
        f"User-Agent: skynet-wsrelay/2\r\n\r\n".encode()
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
    return raw, f, FrameReader(f)


class PairedSock:
    """对等中继端点，形状和 socket 一样（sendall / recv / close）。

    与 `WsSock` 的区别是「谁在对端」：
      WsSock      —— 对端是中继替我们连上的某个 host:port（出口代理）
      PairedSock  —— 对端是**另一个也连进同一房间的人**，中继只转发不看内容

    收数据必须用后台线程 + 队列：隧道层会随时 `recv`，而 WS 帧是异步到达的。
    没有队列就得让调用方精确掌握收发节奏，任何一处节奏错位都会变成「卡住」。
    """

    def __init__(self, sock, f, reader, room=""):
        self.sock = sock
        self.f = f
        self.reader = reader
        self.room = room
        self.q = queue.Queue()
        self.closed = False
        self.err = ""
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._pending = b""
        threading.Thread(target=self._reader_loop, daemon=True).start()

    def _reader_loop(self):
        try:
            while not self._stop.is_set():
                op, payload = self.reader.read_frame()
                if op == OP_CLOSE:
                    break
                if op == OP_PING:
                    self._raw_send(payload, OP_PONG)
                    continue
                if op in (OP_BIN, OP_CONT) and payload:
                    # 队列满说明下游读得比上游发得慢太多。**不能丢数据**——
                    # 文件传输丢一块就是 sha256 对不上。这里阻塞等，由 stop 兜底。
                    while not self._stop.is_set():
                        try:
                            self.q.put(payload, timeout=1.0)
                            break
                        except queue.Full:
                            continue
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
        finally:
            self.closed = True
            self.q.put(None)      # 唤醒还在 recv 里的调用方

    def _raw_send(self, data, opcode=OP_BIN):
        if self.closed:
            return False
        try:
            with self._send_lock:
                ws_send(self.f, data, opcode, mask=True)
            return True
        except Exception as e:
            self.err = f"{type(e).__name__}: {e}"
            self.closed = True
            return False

    # ---- socket 形状
    def sendall(self, data: bytes):
        if not self._raw_send(data):
            raise ConnectionError(self.err or "pair closed")

    def send(self, data: bytes):
        """隧道层只要 send 返回「发出去多少」。发不出去返回 0，不抛。"""
        return len(data) if self._raw_send(data) else 0

    def recv(self, n: int = 65536) -> bytes:
        if self._pending:
            out, self._pending = self._pending[:n], self._pending[n:]
            return out
        item = self.q.get()
        if item is None:
            return b""
        if len(item) > n:
            self._pending = item[n:]
            return item[:n]
        return item

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                with self._send_lock:
                    ws_send(self.f, b"", OP_CLOSE, mask=True)
            except Exception:
                pass
        self._stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


def connect_pair(url: str, room: str, secret: str, timeout: float = 30.0) -> PairedSock:
    """连中继的 /pair，等对端到齐，返回可双向读写的端点。

    会阻塞到配对成功（或超时）。`timeout` 默认与服务端 PAIR_WAIT 对齐：
    短了对端还没连上来就放弃，长了要白占一条 PaaS 连接。
    """
    raw, f, reader = _open_ws(url, timeout=timeout)
    ws_send(f, json.dumps({"room": room, "token": pair_token(room, secret)}).encode(),
            OP_TEXT, mask=True)

    op, payload = reader.read_frame()
    if op != OP_TEXT:
        raise ConnectionError(f"expected text reply, got op={op}")
    resp = json.loads(payload.decode())
    if not resp.get("ok"):
        raise ConnectionError(f"pair refused: {resp.get('err')}")

    if resp.get("waiting"):
        deadline = time.time() + timeout
        paired = False
        while time.time() < deadline:
            op, payload = reader.read_frame()
            if op == OP_PING:
                ws_send(f, payload, OP_PONG, mask=True)
                continue
            if op != OP_TEXT:
                continue
            r2 = json.loads(payload.decode())
            if r2.get("paired"):
                paired = True
                break
            if not r2.get("ok"):
                raise ConnectionError(f"pair lost: {r2.get('err')}")
        if not paired:
            raise ConnectionError("pair timeout: 对端在等待窗口内没有连上来")

    return PairedSock(raw, f, reader, room=room)


# ---------------------------------------------------------------- 地址簿客户端
#
# 这三个函数只做一件事：让「地址会变」不再是问题。
# 设备指纹不变，地址随时上报 —— 对端永远只问指纹，不问 IP。
# 这就是主人口里那句「另类的固态」：对外是固定标识，对内随便漂。

def addr_put(origin: str, fp: str, addrs, secret: str, meta=None, timeout: float = 8.0,
             headers: dict = None):
    """把「我现在在哪」写进中继的地址簿。地址变了就再写一次。"""
    body = json.dumps({"fp": fp, "addrs": addrs, "ts": time.time(),
                       "meta": meta or {},
                       "token": pair_token(f"put:{fp}", secret)}).encode("utf-8")
    return _http_json(origin, "/addr", "POST", body, timeout=timeout, headers=headers)


def addr_get(origin: str, fp: str, timeout: float = 8.0, headers: dict = None):
    """按设备指纹查「它现在在哪」。查不到如实返回 ok:false，不编一个地址出来。"""
    return _http_json(origin, "/addr?" + urllib.parse.urlencode({"fp": fp}),
                      "GET", b"", timeout=timeout, headers=headers)


def addr_list(origin: str, timeout: float = 8.0, headers: dict = None):
    return _http_json(origin, "/addr", "GET", b"", timeout=timeout, headers=headers)


def probe_from_relay(origin: str, target: str, port: int, secret: str,
                     timeout: float = 8.0, headers: dict = None):
    """**从墙外回连我自己**，验证入站到底通不通。

    这是整套方案里唯一可信的入站判据：本机往自己的公网地址发包走 hairpin，
    很多 NAT 根本不支持 hairpin，通不通都不代表外网能连进来。
    只有站在墙外的机器来试才算数 —— 中继正好在墙外。
    """
    qs = urllib.parse.urlencode({
        "target": target, "port": port,
        "token": pair_token(f"probe:{target}", secret)})
    return _http_json(origin, "/probe?" + qs, "GET", b"", timeout=timeout,
                      headers=headers)


def _http_json(origin: str, route: str, method: str, body: bytes = b"",
               timeout: float = 8.0, headers: dict = None):
    """一次性 HTTP 请求，只返回解析后的 JSON。

    `origin` 只要 scheme://host:port，路径全由 route 决定 ——
    不让调用方拼路径，是避免「base 带了没带尾斜杠」这类低级不一致
    （上一版就是靠字符串包含判断，既绕又容易错）。

    `headers` 是给「中继外面还套了一层网关」用的：比如 Daytona 的 preview
    代理（daytonaproxy01.net）自己在前面加了一道 Bearer 鉴权，
    不带就 401。它和本文件自己的 HMAC token（放在请求体里）是两层独立的东西，
    互不替代 —— 所以这里做成透传参数，而不是写死某个平台的 token。
    """
    u = urllib.parse.urlsplit(origin)
    tls = u.scheme in ("wss", "https")
    thost = u.hostname
    tport = u.port or (443 if tls else 80)
    path = route if route.startswith("/") else "/" + route

    raw = socket.create_connection((thost, tport), timeout=timeout)
    if tls:
        raw = ssl.create_default_context().wrap_socket(raw, server_hostname=thost)
    try:
        extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        raw.sendall(
            (f"{method} {path} HTTP/1.1\r\nHost: {thost}\r\n"
             f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
             f"Connection: close\r\nUser-Agent: skynet-wsrelay/2\r\n"
             f"{extra}\r\n").encode() + body
        )
        buf = b""
        while True:
            d = raw.recv(65536)
            if not d:
                break
            buf += d
    finally:
        raw.close()

    _, _, payload = buf.partition(b"\r\n\r\n")
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as e:
        return {"ok": False, "err": f"响应不是 JSON：{e}",
                "raw": payload[:200].decode("utf-8", "replace")}


# ---------------------------------------------------------------- 自测
def _selftest() -> int:
    ok = 0
    print("[1/5] 起一个本地 echo 服务 …", end=" ")
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

    print("[2/5] 起隧道服务端 …", end=" ")
    selftest_secret = "wsrelay-selftest"
    # 自测的地址簿必须落在临时目录：默认路径是 ./addrbook.json（部署环境的真实地址簿），
    # 跑一次自测就会把 selftest-fp 这类假设备写进生产数据里。
    import tempfile
    _selftest_addr = os.path.join(tempfile.mkdtemp(prefix="wsrelay-selftest-"),
                                  "addrbook.json")
    srv = WsTunnelServer(0, verbose=False, secret=selftest_secret,
                         addr_path=_selftest_addr)
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

    print("[3/5] 经 WS 隧道打 echo …", end=" ")
    tun = connect_via_ws(
        f"ws://127.0.0.1:{srv_port}/tunnel?k={pair_token('tunnel', selftest_secret)}",
        "127.0.0.1", echo_port)
    payload = b"skynet-wsrelay-selftest-" + os.urandom(8).hex().encode()
    tun.sendall(payload)
    got = b""
    deadline = time.time() + 8
    while len(got) < len(payload) and time.time() < deadline:
        got += tun.recv(65536)
    tun.close()
    assert got == payload, f"回显不一致: 发 {payload!r} 收 {got!r}"
    ok += 1
    print(f"OK ({len(payload)}B 完整往返)")

    # ---- /pair：这一条才是「对等中继」的验收。部署到 PaaS 之后先跑它，
    #      验不过就说明两端根本凑不到一起，后面什么都白搭。
    print("[4/5] 对等配对（/pair 双向）…", end=" ")
    pair_url = f"ws://127.0.0.1:{srv_port}/pair"
    box2 = queue.Queue()

    def _one(tag, first):
        try:
            s = connect_pair(pair_url, "selftest-room", selftest_secret, timeout=8)
            if first:
                s.sendall(first)
            data = s.recv(65536)
            box2.put((tag, True, data))
            s.close()
        except Exception as e:
            box2.put((tag, False, f"{type(e).__name__}: {e}"))

    threading.Thread(target=_one, args=("A", b"pair-ping"), daemon=True).start()
    time.sleep(0.2)          # 先让 A 进等待态，覆盖「先到者等待」这条路径
    threading.Thread(target=_one, args=("B", None), daemon=True).start()
    res = {}
    for _ in range(2):
        tag, fine, val = box2.get(timeout=15)
        res[tag] = (fine, val)
    assert res.get("A", (False, ""))[0], f"A 没配成: {res.get('A')}"
    assert res.get("B", (False, b""))[1] == b"pair-ping", f"数据没到 B: {res.get('B')}"
    ok += 1
    print("OK (两端配对，字节双向透传)")

    print("[5/5] 地址簿（/addr 读写 + 鉴权）…", end=" ")
    origin = f"http://127.0.0.1:{srv_port}"
    r1 = addr_put(origin, "selftest-fp", [{"ip": "240e::1", "port": 9,
                                          "temporary": True}], selftest_secret)
    assert r1.get("ok"), f"写地址簿失败: {r1}"
    r2 = addr_get(origin, "selftest-fp")
    assert r2.get("ok") and r2["addrs"][0]["ip"] == "240e::1", f"读回不一致: {r2}"
    r3 = addr_put(origin, "selftest-fp", [{"ip": "1::1"}], "wrong-secret")
    assert r3.get("ok") is False, "错 secret 竟然写进去了"
    ok += 1
    print("OK (读写通、错 token 被拒)")

    print(f"\n自测结果：5 步全过（其中 {ok} 项带断言）")
    print("说明：这只验证搬运机制没问题，不验证能不能到墙外目标 ——")
    print("      那取决于部署在墙外的那台机器，本机测不了。")
    print("      部署到 PaaS 后务必先跑一遍：python wsrelay.py --selftest")
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
