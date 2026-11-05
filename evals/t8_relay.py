"""离线门禁：经 FleetNode.invoke 走 SkyNet relay 取页（skynet.relay / skynet.relay.fetch）。

不依赖外网：本地 http.server 当取页目标，relay 经 skynet.net.fetch_text 取它，
FleetNode.invoke 当设备层入口。验三件事：
  ① skynet.relay 状态读取（relay 在跑 → ok=True 带 port）
  ② skynet.relay.fetch 取页（成功带 text，chars>0）
  ③ 一致性：relay 没启动时 fetch 必须失败带原因，而不是谎报成功
"""
import os
import sys
import json
import http.server as _hs
import socketserver as _ss
import threading as _th
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# t8 是离线门禁：自己造一个最小 netforce（skynet.net.fetch_text），
# 不依赖外部 netforce 仓库；这样 relay 的取页/鉴权/端口回退逻辑都能真跑。
def _install_fake_netforce():
    import tempfile

    base = tempfile.mkdtemp(prefix="fleet-t8-netforce-")
    sk = os.path.join(base, "skynet")
    os.makedirs(sk, exist_ok=True)
    with open(os.path.join(base, "resource_finder.py"), "w", encoding="utf-8") as f:
        f.write('"""t8 fake netforce marker"""\n')
    with open(os.path.join(sk, "__init__.py"), "w", encoding="utf-8") as f:
        f.write("")
    with open(os.path.join(sk, "net.py"), "w", encoding="utf-8") as f:
        f.write(
            "import urllib.request\n"
            "from dataclasses import dataclass\n\n"
            "@dataclass\n"
            "class FetchResult:\n"
            "    ok: bool = True\n"
            "    status: int = 0\n"
            "    data: bytes = b''\n"
            "    text: str = ''\n"
            "    error: str = ''\n"
            "    url: str = ''\n\n"
            "def fetch_text(url, timeout=15, **kw):\n"
            "    try:\n"
            "        req = urllib.request.Request(url, headers={'User-Agent': 'fleet-t8/1'})\n"
            "        with urllib.request.urlopen(req, timeout=timeout) as r:\n"
            "            body = r.read()\n"
            "            return FetchResult(ok=True, status=getattr(r, 'status', 200),\n"
            "                               data=body,\n"
            "                               text=body.decode('utf-8', 'replace'), url=url)\n"
            "    except Exception as e:\n"
            "        return FetchResult(ok=False, error='%s: %s' % (type(e).__name__, e),\n"
            "                           url=url)\n"
        )
    os.environ["NETFORCE_HOME"] = base
    for k in [k for k in sys.modules if k == "skynet" or k.startswith("skynet.")]:
        del sys.modules[k]


_install_fake_netforce()

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

    # ④ fail-closed：绑非回环却不给 token ⇒ 必须拒绝启动
    #   （旧行为是照样绑 0.0.0.0 且不校验，等于把本机出口摆在局域网上任人借用）
    import fleet.skynet_relay as SR
    lan = SkyNetRelay(host="0.0.0.0", token="")
    try:
        lan.start()
        lan.stop()
        raise AssertionError("绑 0.0.0.0 且无 token 竟然起成功了（fail-open，应当拒绝）")
    except ValueError as e:
        assert "token" in str(e), "拒绝理由要说明是缺 token：%r" % str(e)
        print("  [4] fail-closed OK（0.0.0.0 无 token 拒绝启动：%s）" % str(e)[:46])

    # ⑤ 鉴权按**来源**判：局域网来源必须带 token，回环来源免 token
    #   本机没有第二块网卡也能测：直接喂 _handle 一个假连接 + 伪造 peer。
    class _FakeConn:
        def __init__(self, payload):
            self._in = payload
            self.out = b""

        def settimeout(self, *_a):
            pass

        def recv(self, _n):
            d, self._in = self._in, b""
            return d

        def sendall(self, b):
            self.out += b

        def close(self):
            pass

    def _probe(peer, token):
        req = json.dumps({"type": "task", "id": "z", "kind": "fetch",
                          "url": target_url, "token": token}).encode("utf-8") + b"\n"
        c = _FakeConn(req)
        r2._handle(c, _N(), peer)
        line = [x for x in c.out.split(b"\n") if x.strip()][0]
        return json.loads(line.decode("utf-8", "ignore"))

    def _N():
        from skynet import net as N
        return N

    tok = SR.new_token()
    r2 = SkyNetRelay(host="127.0.0.1", token=tok)
    r2.start()
    try:
        a = _probe("192.168.1.50", "")             # LAN 来源 + 无 token → 拒
        assert not a.get("ok") and "unauthorized" in (a.get("error") or ""), a
        b = _probe("192.168.1.50", "wrong-token")  # LAN 来源 + 错 token → 拒
        assert not b.get("ok") and "unauthorized" in (b.get("error") or ""), b
        c1 = _probe("192.168.1.50", tok)           # LAN 来源 + 对 token → 放行
        assert c1.get("ok"), "持正确 token 的局域网节点应能借出口：%r" % (c1,)
        d1 = _probe("127.0.0.1", "")               # 回环来源免 token
        assert d1.get("ok"), "回环来源（本机/adb reverse）应免 token：%r" % (d1,)
        print("  [5] 按来源鉴权 OK（LAN 无/错 token 拒、对 token 放行、回环免 token）")
    finally:
        r2.stop()

    # ⑥ 回归：盘里存过 host=0.0.0.0 时，随手 get_relay() **不许**静默绑 0.0.0.0
    import tempfile
    old_path, old_single = SR.RELAY_STATE_PATH, SR._relay
    try:
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"host": "0.0.0.0", "port": 45678, "token": "keep-me"}, f)
        SR.RELAY_STATE_PATH = tmp
        SR._relay = None
        g = SR.get_relay()
        assert g.host == "127.0.0.1", "get_relay 竟回填了盘里的 0.0.0.0（会静默对外开端口）"
        assert g.token == "keep-me", "token 应可回填（只对非回环来源生效）"
        print("  [6] 状态回填 OK（host 只认显式参数，绝不回填 0.0.0.0）")
    finally:
        SR.RELAY_STATE_PATH, SR._relay = old_path, old_single
        try:
            os.remove(tmp)
        except OSError:
            pass

    # ⑦ 回归：盘里存下来的端口**现在绑不上**时，必须退回系统分配，不许直接死
    #   2026-11-02 实测踩到：上次 port=0 拿到 49194 落盘，这次开机 Windows 把
    #   49152–49251 划进保留段（Hyper-V/WinNAT 每次开机重划），bind 抛 WinError 10013。
    #   原来那条路是「照搬盘里的端口」→ 中继永远起不来，而报错长得像环境问题。
    #   注入方式：子类只在「传了非 0 端口」时抛 OSError —— 不真去占保留段端口。
    class _StalePort(SR.SkyNetRelay):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.tries = []

        def _bind(self, port):
            self.tries.append(port)
            if port:
                raise OSError(10013, "以一种访问权限不允许的方式做了一个访问套接字的尝试")
            return super()._bind(port)

    st = _StalePort(host="127.0.0.1", port=49194)
    try:
        got = st.start()
        assert st.tries == [49194, 0], "应当先试存下来的端口、失败后退回 0（实得 %r）" % (st.tries,)
        assert got and got != 49194, "应换到一个真正能用的端口（实得 %r）" % (got,)
        assert st.port_fallback and st.port_fallback[0] == 49194, \
            "要把「从哪个端口退下来的」记下来给上层看（实得 %r）" % (st.port_fallback,)
        print("  [7] 存下的端口绑不上 → 退回系统分配 OK（%d → %d）"
              % (st.port_fallback[0], st.port_fallback[1]))
    finally:
        st.stop()

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
