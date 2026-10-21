"""Windows 图形版的窗口壳：起本地服务 → 注入令牌 → 开 pywebview 窗口。

为什么要有壳：浏览器（WebView2）里跑的是本地 Web UI，它天生只能发 HTTP 请求，
不能直接调 Python 设备。壳就干三件事：把本地服务拉起来、把一次性令牌交给页面、
开一个窗口。仅此而已——**壳里不写任何能力逻辑**（能力全在后端走 registry.call）。

令牌怎么给页面：放在 URL 的**片段**（`#t=...`）里。片段不会随 HTTP 请求发出，
所以它既不会出现在服务端的请求日志里，也不会出现在任何 Referer 头里。
页面读出来存在内存变量里（不落 localStorage——落盘就等于把凭据留在盘上）。

没装 pywebview 时必须说清怎么办（`pip install pywebview`），不许静默失败——
一个「双击没反应」的软件是最难查的那种坏。

`--no-window`：不开窗，只跑服务本体（给无 GUI 环境/自动化用，也给门禁用）。
"""
import argparse
import os
import sys

from .approval import ApprovalBroker
from .server import FleetServer

WIN_TITLE = "设备编队"
WIN_WIDTH = 1440
WIN_HEIGHT = 900
WIN_MIN = (1080, 680)


def _smoke():
    """CI / 打包冒烟：起服务 → 带 token 探 /api/health → 退。

    不开窗、不阻塞、不需要显示器、不依赖 webview（webview 只在开窗那条路才 import）。
    冻扁包出来后也用同一个 --smoke 做「产物真能跑」的落地检查。
    """
    import json
    import urllib.request

    srv = FleetServer(port=0, approval=ApprovalBroker())
    port = srv.start()
    url = srv.page_url("light")
    token = url.split("#t=", 1)[1].split("&")[0]   # token 在片段里，抽出
    try:
        req = urllib.request.Request(
            srv.base_url + "/api/health",
            headers={"X-Fleet-Token": token})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        ok = data.get("ok") is True and "devices" in data
        print(("SMOKE OK" if ok else "SMOKE FAIL") + ": " +
              json.dumps(data, ensure_ascii=False)[:200])
        return 0 if ok else 1
    except Exception as e:
        print("SMOKE FAIL: %s: %s" % (type(e).__name__, e))
        return 1
    finally:
        srv.stop()


def _require_webview():
    """拿 pywebview；没有就报明确原因并退出，不许静默失败。"""
    try:
        import webview
    except ImportError as e:
        raise SystemExit(
            "没装 pywebview，图形窗口起不来。\n"
            "装它：pip install pywebview\n"
            "（只想跑后端服务本体的话加 --no-window）\n"
            f"原始错误：{e}")
    return webview


def main(argv=None):
    ap = argparse.ArgumentParser(description="设备编队 · Windows 图形版")
    ap.add_argument("--no-window", action="store_true",
                    help="只跑本地服务，不开窗口（无 GUI 环境用）")
    ap.add_argument("--smoke", action="store_true",
                    help="起服务探 /api/health 后退出（CI / 打包冒烟，不开窗）")
    ap.add_argument("--port", type=int, default=0,
                    help="端口；0 = 随机端口（默认，防冲突也防被外部扫到）")
    ap.add_argument("--theme", default="light", choices=["light", "dark"],
                    help="初始主题")
    args = ap.parse_args(argv)

    if args.smoke:
        return _smoke()

    # W2：注入审批中枢。有副作用的能力会被注册表拦在闸门上，挂起等主人在
    # 弹窗里点确认。判定权始终在注册表层（要不要审批、几级、要不要双确认），
    # 这里只是把「这次要问主人」这件事接上。
    srv = FleetServer(port=args.port, approval=ApprovalBroker())
    port = srv.start()
    url = srv.page_url(args.theme)
    print(f"[fleet-gui] 本地服务已起：{srv.base_url}", flush=True)
    print(f"[fleet-gui] 页面地址（片段里是一次性令牌）：{url}", flush=True)
    print("[fleet-gui] 这个服务只对本机开放（127.0.0.1 + 随机端口 + 一次性令牌）",
          flush=True)

    if args.no_window:
        print("[fleet-gui] --no-window：不开窗口，服务继续跑。Ctrl+C 结束。", flush=True)
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
        return 0

    webview = _require_webview()
    window = webview.create_window(
        WIN_TITLE, url,
        width=WIN_WIDTH, height=WIN_HEIGHT,
        min_size=WIN_MIN,
        background_color="#15171a" if args.theme == "dark" else "#f7f8fa")
    try:
        webview.start()
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
