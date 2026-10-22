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
import json
import socket
import threading

from .netforce_skynet import _ensure_path  # 复用 netforce 定位逻辑


class SkyNetRelay:
    """SkyNet 兼容的 relay server（line-JSON over TCP，守护线程常驻）。"""

    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self._srv = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        """起守护线程里的 TCP server，返回实际监听端口。"""
        _ensure_path()                       # 确认 netforce 在，再绑端口
        from skynet import net as N        # noqa: F401（运行期才需要 net 模块）
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, args=(N,), daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        self._stop.set()
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass

    def _accept_loop(self, N):
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, N), daemon=True).start()

    @staticmethod
    def _handle(conn, N):
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


def get_relay():
    global _relay
    if _relay is None:
        _relay = SkyNetRelay()
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
