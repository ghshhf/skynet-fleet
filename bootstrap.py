"""第三原则落地：单一工具箱反向推送。

中心（Commander / 手机）起迷你 HTTP 服务，把自包含工具箱（node_agent.py + common.py
+ 安装器）以几 KB bootstrap 分发出去；目标端 fetch 后即跑，之后配置/依赖全由中心
push——目标端不再各自去公网下。
Windows/macOS 不能凭空被塞 exe 自动跑，故首脚需一次极小引导（手机出示二维码 ->
设备拉几 KB bootstrap -> 之后全 push）；Android 可 adb push 直推（见 platform_android）。
"""
import http.server
import os
import socketserver
import threading


BOOTSTRAP_SCRIPT = '''# 天网工程 bootstrap：从中心拉取工具箱并注册
import os, sys, urllib.request
BASE = os.environ.get("FLEET_BASE", "http://127.0.0.1:9100")
for fn in ("common.py", "node_agent.py", "enroll.py"):
    try:
        data = urllib.request.urlopen(BASE + "/" + fn, timeout=15).read()
        open(fn, "wb").write(data)
        print("[bootstrap] 已拉取", fn, len(data), "B")
    except Exception as e:
        print("[bootstrap] 拉取失败", fn, e); sys.exit(1)
print("[bootstrap] 工具箱就绪。两种入网方式：")
print("  ① 扫码（推荐）：install_windows.py --enroll \\"fleet://enroll?p=...\\"")
print("  ② 密码：python node_agent.py --host <CommanderIP> --port 9000 --name <本机名> --password <密码>")
'''


class _Handler(http.server.BaseHTTPRequestHandler):
    root = "."

    def do_GET(self):
        if self.path in ("/", "/bootstrap.py"):
            body = BOOTSTRAP_SCRIPT.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # 其余路径按 root 目录裸文件分发（node_agent.py / common.py / install_windows.py ...）
        rel = self.path.lstrip("/")
        fp = os.path.join(self.root, rel)
        if os.path.isfile(fp):
            body = open(fp, "rb").read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


def start_server(root, host="127.0.0.1", port=9100):
    """起分发服务（后台线程）。返回 server 对象。

    port 传 0 = 让系统随机分配（2026-10-21 实测修的偶发红）：
    固定端口在并发场景会直接撞 10048，且撞了之后报错点离真正的原因很远。
    真实端口从 httpd.server_address[1] 取。
    """
    _Handler.root = root
    httpd = socketserver.TCPServer((host, port), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def fetch_bytes(base, path):
    """目标端拉取。返回 bytes（供自测校验一致性）。"""
    import urllib.request
    return urllib.request.urlopen(base + "/" + path, timeout=15).read()


if __name__ == "__main__":
    srv = start_server(".", "127.0.0.1", 9100)
    print("bootstrap 服务已起 127.0.0.1:9100")
    print(fetch_bytes("http://127.0.0.1:9100", "bootstrap.py")[:40])
    srv.shutdown()
