"""天网·SkyNet relay 节点（fleet.skynet_relay）—— 把本机出口经端口转发借给别的节点。

与 SkyNet 内核（D:/code/netforce/skynet）的 RelayFetcher 协议兼容：
  客户端发一行 JSON  {"type":"task","id","kind":"fetch","url"}
   relay 回一行 JSON  {"type":"result","id","ok","status","body_b64","text","error"}

本机充当 relay 节点时，别的设备经端口转发连这个端口，就能借本机出口
取页/调能力——即「任意节点 AI 可经端口转发调用」的落地（SkyNet SSI 算力
底座的本机一端）。

设计约束（与项目铁律一致）：
  · 默认只绑 127.0.0.1（本机/经 adb reverse 进来的手机），不主动绑 0.0.0.0；
    想让局域网其他节点借，由上层显式选 --lan（并加 token 鉴权，留给下一版）。
  · 未装 netforce 时如实说「没装」，不静默降级；
  · 任何异常都不外抛，失败带原因；
  · 零第三方依赖（stdlib socket/threading/json/base64）。
"""
import base64
import hmac
import json
import os
import secrets
import socket
import threading

from .netforce_skynet import _ensure_path  # 复用 netforce 定位逻辑

# 跨会话持久化：把 relay 的 host/port/token 落到用户目录，进程重启后能复用同一
# 端口与 token（避免端口漂移，且 relay_client/commander 能发现上次起的 relay）。
RELAY_STATE_PATH = os.path.join(
    os.path.expanduser("~/.fleet"), "relay.json")

# 回环地址：只有它能免 token（本机 / 经 adb reverse 进来的手机在本进程眼里就是回环）。
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host):
    """host 是否是回环（含 127.0.0.0/8）。"""
    h = (host or "").strip().lower()
    if h in _LOOPBACK:
        return True
    return h.startswith("127.")


def new_token():
    """生成一枚 LAN 共享 token（24 字节 ≈ 32 字符 urlsafe）。"""
    return secrets.token_urlsafe(24)


def load_relay_state():
    """读上次保存的 relay 状态；没有/损坏返回 None。"""
    try:
        with open(RELAY_STATE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("port"):
            return d
    except (OSError, ValueError):
        pass
    return None


def clear_relay_state():
    try:
        os.remove(RELAY_STATE_PATH)
    except OSError:
        pass


def save_relay_state(host, port, token=""):
    """把 relay 的 host/port/token 落盘；失败不抛（best-effort）。"""
    try:
        os.makedirs(os.path.dirname(RELAY_STATE_PATH), exist_ok=True)
        with open(RELAY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"host": host, "port": port, "token": token or ""},
                      f, ensure_ascii=False)
        # token 是凭据，别让同机其他用户读得到（Windows 上 chmod 语义有限，尽力而为）
        try:
            os.chmod(RELAY_STATE_PATH, 0o600)
        except OSError:
            pass
    except OSError:
        pass


class SkyNetRelay:
    """SkyNet 兼容的 relay server（line-JSON over TCP，守护线程常驻）。

    host 默认 127.0.0.1（只本机/adb reverse 进来的手机）；传 host="0.0.0.0"
    即 --lan 模式，允许局域网其他节点借本机出口。--lan 必须配 token（弱共享
    密钥，够拦住局域网的误连/扫描），token 为空时拒绝所有非本机请求。
    """

    def __init__(self, host="127.0.0.1", port=0, token=""):
        self.host = host
        self.port = port
        self.token = token or ""
        self._srv = None
        self._thread = None
        self._stop = threading.Event()

    def start(self, host=None, port=None, token=None):
        """起守护线程里的 TCP server，返回实际监听端口。

        host/port/token 可在此覆盖构造值（node_agent --lan 用）。
        """
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        if token is not None:
            self.token = token or ""
        # 🔴 fail-closed：绑非回环地址（--lan）而没 token ⇒ **不起**，直接抛。
        # 旧行为是照样绑 0.0.0.0 且不校验 token —— 等于把「本机出口 + 取页能力」
        # 摆在局域网上任人借用（总纲 P1-11 就是这一条）。宁可不启动，
        # 也不给一个看起来能用、其实裸奔的端口。
        if not is_loopback(self.host) and not self.token:
            raise ValueError(
                "relay 绑 %s（非回环）必须配 token；否则局域网内任何人都能借本机出口。"
                "传 token=... 或先调 new_token() 生成一枚。" % (self.host,))
        _ensure_path()                       # 确认 netforce 在，再绑端口
        from skynet import net as N        # noqa: F401（运行期才需要 net 模块）
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(64)
        # 监听套超时：让 accept 循环能定期醒一次记心跳（没连接时也算活着）。
        self._srv.settimeout(30.0)
        self.port = self._srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, args=(N,), daemon=True)
        self._thread.start()
        save_relay_state(self.host, self.port, self.token)
        return self.port

    @property
    def running(self):
        """relay 是否已 start() 并在监听。"""
        return self._srv is not None

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass

    def _accept_loop(self, N):
        try:
            from .beat import register
        except (ImportError, ValueError):
            from beat import register
        # relay 是事件驱动（没连接就一直阻塞在 accept），所以心跳记在
        # 「还在等」这件事上：stale 说明 accept 线程真的死了，而不是「没流量」。
        bt = register("relay.accept", stale_after=90.0)
        bt.beat()
        while not self._stop.is_set():
            try:
                conn, peer = self._srv.accept()
            except socket.timeout:
                # 监听套设有超时（见 start）：没连接也应定期记一次心跳，
                # 否则「静悄悄没流量」会被判成「线程死了」——两件事不能混。
                bt.beat()
                continue
            except OSError as e:
                # stop() 关掉监听会让 accept 抛 OSError —— 那是正常收尾，
                # 不算失败；只有「还在跑却 accept 失败」才是异常。
                if self._stop.is_set():
                    break
                bt.allow("accept: %s" % e)
                break
            bt.beat()
            threading.Thread(target=self._handle, args=(conn, N, peer[0] if peer else ""),
                             daemon=True).start()

    def _handle(self, conn, N, peer=""):
        buf = b""
        try:
            conn.settimeout(25.0)
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line.decode("utf-8", "ignore"))
                    except Exception:
                        conn.sendall(
                            json.dumps({"type": "error", "error": "bad json"}).encode("utf-8") + b"\n")
                        continue
                    rid = req.get("id")
                    kind = req.get("kind")
                    # token 鉴权：**按来源判**，不按「设没设 token」判。
                    #   回环来源（本机进程 / 经 adb reverse 进来的手机，在本进程眼里
                    #   就是 127.0.0.1）⇒ 免 token，零摩擦；
                    #   非回环来源（局域网他机）⇒ **一律要 token**。
                    # 这样即使 relay 绑在 0.0.0.0，LAN 侧也过不了门；
                    # 而绑回环时 LAN 侧根本连不上（双保险）。
                    # 用 compare_digest 做常量时间比较，避免按字节猜 token。
                    if not is_loopback(peer):
                        got = req.get("token") or ""
                        if not self.token or not hmac.compare_digest(str(got), str(self.token)):
                            conn.sendall(json.dumps(
                                {"type": "result", "id": rid, "ok": False,
                                 "error": "unauthorized: 局域网来源必须带正确 token"
                                          "（relay 只借给持 token 的节点）"}
                            ).encode("utf-8") + b"\n")
                            continue
                    if kind == "fetch":
                        url = req.get("url", "")
                        try:
                            res = N.fetch_text(url)
                            body = getattr(res, "data", b"") or b""
                            resp = {
                                "type": "result", "id": rid, "ok": bool(res.ok),
                                "status": getattr(res, "status", 0),
                                "body_b64": base64.b64encode(body).decode("ascii"),
                                "text": getattr(res, "text", "") or "",
                                "error": getattr(res, "error", "") or "",
                            }
                        except Exception as e:      # 取页失败，带原因回传，不断连
                            resp = {"type": "result", "id": rid, "ok": False,
                                    "error": f"{type(e).__name__}: {e}"}
                    else:
                        resp = {"type": "result", "id": rid, "ok": False,
                                "error": "unknown kind: %r" % kind}
                    conn.sendall(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


# 单例：catalog / 设备类复用同一个 relay 实例
_relay = None


def get_relay(host=None, token=None):
    """单例。host/token 仅在首次创建或显式传入时生效（node_agent --lan 用）。

    若首次创建且未显式给 host/token，尝试从持久化状态恢复，避免端口漂移。

    🔴 **不回填 host**：盘里存过 `"host":"0.0.0.0"`（上次开过 --lan），
    下一次随手 `get_relay()` 就会静默绑 0.0.0.0 —— 等于把对外端口重新打开，
    而调用方根本没表达这个意图。绑哪个网卡只认**显式参数**，默认永远回环。
    token 可以回填（只对非回环来源生效，不经 --lan 就等于没用）。
    """
    global _relay
    if _relay is None:
        saved = None
        if host is None and token is None:
            saved = load_relay_state()
        _host = host or "127.0.0.1"
        _token = token if token is not None else ((saved.get("token") if saved else "") or "")
        _relay = SkyNetRelay(host=_host, token=_token)
        if saved and saved.get("port") and host is None:
            _relay.port = int(saved["port"])
    else:
        if host is not None:
            _relay.host = host
        if token is not None:
            _relay.token = token
    return _relay


# ---------------------------------------------------------------- 自测（全局域内，不依赖外网）

def _selftest():
    import http.server as _hs
    import socketserver as _ss
    import threading as _th

    # 1) 本地文件服务（只绑 127.0.0.1，不依赖外网）
    class _H(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"hello from skynet relay target"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    file_srv = _ss.TCPServer(("127.0.0.1", 0), _H)
    file_port = file_srv.server_address[1]
    _th.Thread(target=file_srv.serve_forever, daemon=True).start()
    target_url = "http://127.0.0.1:%d/x" % file_port

    # 2) 起 relay（start() 会把 netforce 仓库注入 sys.path）
    relay = SkyNetRelay()
    rp = relay.start()

    # 确认 skynet.route 可 import（路径已在 start() 里注入）
    from skynet import route as SR  # noqa: F401

    # 3) 客户端经 relay 取页（直接 socket 线，走 SkyNet 兼容协议）
    s = socket.create_connection(("127.0.0.1", rp), timeout=10)
    rid = "r1"
    s.sendall(json.dumps({"type": "task", "id": rid, "kind": "fetch",
                          "url": target_url}).encode("utf-8") + b"\n")
    data = b""
    while b"\n" not in data:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    msg = json.loads(data.split(b"\n", 1)[0].decode("utf-8", "ignore"))
    ok = msg.get("ok") and "hello from skynet relay target" in msg.get("text", "")
    print("skynet_relay selftest:", "PASS" if ok else ("FAIL %r" % msg))
    relay.stop()
    file_srv.shutdown()
    return ok


if __name__ == "__main__":
    _selftest()
