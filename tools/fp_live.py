"""真机验证：档案注入到底有没有生效。

判定标准只有一条 —— **headless 下不注入必然报软件渲染**，
注入了就必须报成档案里那张显卡。这一条骗不了人。

    python tools/fp_live.py              # 默认 win/CN
    python tools/fp_live.py mac US      # 换机型/地理

为什么专挑 headless 验：headless 是最脏的环境，脏环境里能干净，
有头环境（真机 GPU）自然不会更差。反过来在有头上验会得出一片绿，
但那是浏览器本身给的，不是我们给的 —— 等于没验。
"""

import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _serve():
    """起一个本地静态站。

    必须有这一步：`navigator.userAgentData`（UA-CH）**只在安全上下文暴露**，
    about:blank 和 file:// 都不算。所以早先在 about:blank 里验，UA-CH 一直是
    None，看起来像「注入没生效」，其实是那个环境里根本没人读它。
    localhost/127.0.0.1 被 Chrome 视为可信来源，正好够用。
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"<!doctype html><html><head><title>fp-live</title></head>"
                    b"<body>fp probe</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/" % srv.server_address[1]


def looks(actual, keys):
    out = {}
    for k in keys:
        v = actual.get(k)
        if isinstance(v, (list, tuple)):
            v = list(v)
        out[k] = v
    return out


def main():
    os_ = sys.argv[1] if len(sys.argv) > 1 else "win"
    geo = sys.argv[2] if len(sys.argv) > 2 else "CN"

    from fleet import fp_profile as FP
    from fleet import fp_inject as FI

    p = FP.make(os=os_, geo=geo, seed="live-%s-%s" % (os_, geo))
    bad = FP.contradictions(p)
    print("档案 %s/%s  自洽=%s" % (os_, geo, "是" if not bad else "否 %s" % bad))
    print("  期望 UA      : %s" % p["ua"])
    print("  期望 时区/语言: %s / %s" % (p["tz"], p["locale"]))
    print("  期望 屏幕/DPR : %s @%s" % (p["screen"], p["dpr"]))
    print("  期望 GPU      : %s | %s" % (p["webgl_vendor"], p["webgl_renderer"]))
    print()

    from playwright.sync_api import sync_playwright

    key = ["ua", "platform", "language", "hardware_concurrency", "device_memory",
           "webdriver", "tz", "screen", "avail", "dpr", "outer", "inner",
           "webgl_vendor", "webgl_renderer", "ua_ch", "canvas_hash"]

    srv, url = _serve()
    print("本地站: %s（安全上下文，UA-CH 只在这里才暴露）\n" % url)

    with tempfile.TemporaryDirectory(prefix="fp-live-") as tmp:
        with sync_playwright() as pw:
            # 沙箱的地基就是它：一个独立的 user_data_dir = 一份独立的历史。
            # 目录是 TemporaryDirectory，出 with 就删 —— 这就是「临时起沙箱」。
            #
            # 刻意**不加** --disable-blink-features=AutomationControlled：
            # 那个 flag 会让 navigator.webdriver 整个属性消失（'webdriver' in navigator = false），
            # 而真 Chrome 是「属性在、值为 false」。抹掉属性反而比留着更可疑。
            # 我们用 init 脚本把值改成 false，属性照旧存在 —— 这才是真机的形状。
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=os.path.join(tmp, "profile"),
                headless=True,
            )
            browser = ctx
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url)

            before = FI.readback(page)
            print("—— 注入前（裸 headless，这就是会被抓的样子）——")
            for k, v in looks(before, key).items():
                print("  %-22s %s" % (k, v))

            res = FI.apply_to_page(page, ctx, p)
            print("\n—— 注入 ——")
            print("  init 脚本: %s   CDP: %s   need_reload=%s   err=%s"
                  % (res["init"], ",".join(m.split(".")[-1] for m in res["cdp"]),
                     res["need_reload"], res["err"] or "无"))

            page.reload(wait_until="domcontentloaded")
            after = FI.readback(page)

            print("\n—— 注入后 ——")
            for k, v in looks(after, key).items():
                print("  %-22s %s" % (k, v))

            d = FI.diff(p, after)
            print("\n—— 核对：期望 vs 实际 ——")
            if not d:
                print("  零差异：档案里写的，页面报的就是它")
            else:
                for x in d:
                    print("  ✗ %-22s 期望 %r 实得 %r  %s"
                          % (x["field"], x["want"], x["got"], x.get("note") or ""))

            # 关键一条：软件渲染必须消失
            rend = str(after.get("webgl_renderer") or "")
            leaked = any(s.lower() in rend.lower()
                         for s in ("SwiftShader", "llvmpipe", "Software Rasterizer", "Mesa OffScreen"))
            print("\n软件渲染是否泄漏: %s" % ("**是 —— 没盖住**" if leaked else "否"))

            # 二次核对：噪声必须恒定。
            # 同档案再读一次，hash 必须一模一样 —— 每次访问都变等于告诉对方
            # 「这个人每次都在换身份」，比不做更可疑。
            page.reload(wait_until="domcontentloaded")
            again = FI.readback(page)
            same = again.get("canvas_hash") == after.get("canvas_hash")
            print("Canvas 噪声恒定: %s（两次 %r / %r）"
                  % ("是" if same else "**否 —— 每次都在变，反而是破绽**",
                     after.get("canvas_hash"), again.get("canvas_hash")))

            browser.close()

    srv.shutdown()
    hard_fail = [x for x in d if x["field"] in
                 ("ua", "platform", "webdriver", "tz", "webgl_renderer", "webgl_vendor")]
    print("\n结论：%d 项差异，其中关键项 %d 项" % (len(d), len(hard_fail)))
    return 0 if not d else 1


if __name__ == "__main__":
    sys.exit(main())
