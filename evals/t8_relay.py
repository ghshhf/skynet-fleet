"""离线门禁：经 FleetNode.invoke 走 SkyNet relay 取页（skynet.relay / skynet.relay.fetch）。

不依赖外网：本地 http.server 当取页目标，relay 经 skynet.net.fetch_text 取它，
FleetNode.invoke 当设备层入口。验三件事：
  ① skynet.relay 状态读取（relay 在跑 → ok=True 带 port）
  ② skynet.relay.fetch 取页（成功带 text，chars>0）
  ③ 一致性：relay 没启动时 fetch 必须失败带原因，而不是谎报成功
"""
import os
import sys
import http.server as _hs
import socketserver as _ss
import threading as _th
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.kinds.fleet_node import FleetNode
from fleet.skynet_relay import get_relay, SkyNetRelay


def _selftest():
    # 本地文件服务（只绑 127.0.0.1，不依赖外网）
    class _H(_hs.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"hello from skynet relay target over fleet_node.invoke"
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

    # 起 relay（本机出口），get_relay() 单例
    relay = get_relay()
    if not relay.running:
        relay.start()
    rp = relay.port

    node = FleetNode("node:local", rec={"capabilities": ["skynet.relay"]})

    # ① skynet.relay 状态
    r1 = node.invoke("skynet.relay")
    assert r1.get("ok"), "skynet.relay 应返回 ok=True（relay 在跑）"
    assert r1.get("data", {}).get("port") == rp, "skynet.relay 回报端口应与 relay.port 一致"
    print("  [1] skynet.relay 状态 OK (port=%d)" % rp)

    # ② skynet.relay.fetch 取页
    r2 = node.invoke("skynet.relay.fetch", {"url": target_url})
    assert r2.get("ok"), "skynet.relay.fetch 取页应成功：%s" % r2.get("err")
    txt = r2.get("data", {}).get("text", "")
    assert "hello from skynet relay target" in txt, "取回正文应包含目标串，实得=%r" % txt
    assert r2.get("data", {}).get("chars", 0) > 0, "chars 应>0"
    print("  [2] skynet.relay.fetch 取页 OK (chars=%d)" % r2["data"]["chars"])

    # ③ 一致性：relay 没启动时 fetch 必须失败带原因
    relay.stop()
    # 重置单例状态，确保 get_relay() 看到 stopped
    r3 = node.invoke("skynet.relay.fetch", {"url": target_url})
    assert not r3.get("ok"), "relay 停了 fetch 必须失败，不应谎报成功"
    assert r3.get("err"), "失败必须带原因，不应静默空"
    print("  [3] 一致性 OK（relay 停 → fetch 失败带原因：%s）" % r3.get("err"))

    # 收尾：把 relay 重新停干净（已是 stopped）
    relay.stop()
    file_srv.shutdown()
    print("t8_relay: PASS")
    return True


if __name__ == "__main__":
    try:
        ok = _selftest()
    except Exception as e:
        import traceback
        traceback.print_exc()
        ok = False
    sys.exit(0 if ok else 1)
