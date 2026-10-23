"""天网 可观测性端点：把 fleet.metrics.render() 挂到 127.0.0.1 的 /metrics。

为什么独立成一个小服务（而不是塞进 GUI server）：
  ① 关注点分离——指标是给 Prometheus/Grafana 拉的，不是给人点的界面，不该跟
     鉴权 token、审批弹窗混在同一个 attack surface 里。
  ② 安全默认沿用项目纪律：默认只绑 127.0.0.1（本机拉取）；Prometheus 在另一台
     机器时，由它用 `scrape` 配上 `proxy` 或用 `cloudflared` 之类隧道把 127.0.0.1
     暴露出去——本服务自己不主动绑 0.0.0.0，不给自己留「忘了改回环」的口子。
  ③ 零依赖：threading http.server 标准库就够，不引第三方。

启动：python -m fleet.metrics_server [--port 9797] [--host 127.0.0.1] [--once]
  --once  只把当前指标渲染到 stdout 然后退出（验证用，不常驻）。
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import metrics as M


class _Handler(BaseHTTPRequestHandler):
    server_version = "FleetMetrics/0.1"

    def log_message(self, fmt, *args):
        pass                            # 指标拉取很频繁，默认静默

    def _metrics(self):
        body = M.render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/metrics", "/"):
            self._metrics()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"not found")


def start(host=None, port=None):
    """起一个守护线程里的 HTTP 服务，返回 (httpd, port)。

    不阻塞调用方——指标服务本来就是后台常驻的。
    """
    host = host or os.environ.get("FLEET_METRICS_HOST", "127.0.0.1")
    port = int(port or os.environ.get("FLEET_METRICS_PORT") or 9797)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.fleet_metrics = True
    t = threading.Thread(target=httpd.serve_forever,
                         kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    return httpd, port


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m fleet.metrics_server",
        description="天网 可观测性 /metrics 端点（127.0.0.1）")
    ap.add_argument("--host", default=os.environ.get("FLEET_METRICS_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("FLEET_METRICS_PORT") or 9797))
    ap.add_argument("--once", action="store_true",
                    help="只渲染一次指标到 stdout 然后退出（不常驻）")
    args = ap.parse_args(argv)

    if args.once:
        sys.stdout.write(M.render())
        return 0

    httpd, port = start(args.host, args.port)
    sys.stdout.write("metrics on http://%s:%d/metrics  (Ctrl+C 退出)\n"
                     % (args.host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
