"""出向网络层门禁：锁死「僵尸代理不许拖死全站」这条规矩（全本地，不碰外网）。

现场（2026-11-02）：
    运行环境按「系统代理设置」注入 HTTP_PROXY/HTTPS_PROXY=127.0.0.1:3067，
    karing 一停那个口就没人监听。urllib 默认 opener 吃这两个变量 → 全站请求
    发往死端口 → 10061「目标计算机积极拒绝」。而纯 socket 直连 baidu/github/douyin
    全在 0.2s 内通 —— 「网络坏了」是假象，坏的是请求层。

对应铁律：注释不是防线，测试才是。所以这里不用「现在机器上有没有毒」当判据，
而是**自己造一个毒**（起本地 server 当目标 + 指向死端口的假代理），完全离线可控。

覆盖：
  1. 选路三态：自有 7891 活着必走它 / 自有优先于系统 / 全死必回直连
  2. 显式给死代理必须抛 ConnectionError，不许静默回退（防「我以为走了代理」的误判）
  3. 【核心回归】注入死代理的环境下：裸 urlopen 必挂，net.urlopen 必通
  4. 【核心回归】子进程里 import fleet 后，裸 urlopen 也恢复（全局兜底真的装了）
  5. is_proxy_alive 判活：真监听报 True、死端口报 False
  6. clean_registry_proxy：ProxyEnable=0 之外的场合绝不乱动（dry_run 不写盘）
  7. 解析层不回归：短链抽取 + SM3 标准向量

为什么第 1 条要分三态（而是不是一句「默认直连」）：
  天网自己就有代理层（proxymgr 订阅择优 → proxycore 起 sing-box 在 7891）。
  把它当「系统脏数据」一律直连，等于自己家的内核起来也不认。但反过来无脑走代理
  也不行 —— 内核没起时必须回落，否则又一次全站拖死。所以三种状态各钉一条。
"""
import io
import os
import socket
import sys
import threading
import http.server

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import urllib.request

from fleet import net

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)


# ---------------------------------------------------------------- 本地目标 server

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"NET_GATE_OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _free_port():
    """拿一个当前确实没人监听的端口（绑一下立刻放掉）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    # 起本地目标 server
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    target = "http://127.0.0.1:%d/" % srv_port

    # 造一个「僵尸代理」：拿到没人听的端口
    dead_proxy = "http://127.0.0.1:%d" % _free_port()

    # ---- 1. 选路三态（这是「代理层面半步圆满」在出向层的兑现点）
    #     用临时替换 is_proxy_alive 模拟「内核起了/没起」，不真起 sing-box：
    #     门禁不许依赖外部二进制，否则一换环境就红。
    mine = net.self_proxy_url()
    real_alive = net.is_proxy_alive

    # 1a. 自有活着 → 默认必须用它。这条红了就说明「内核起来也白起」。
    try:
        net.is_proxy_alive = lambda url, timeout=0.3: url == mine
        check(net._proxy_target_of(net.opener(None)) == mine,
              "自有代理活着时 opener(None) 没走它（内核起了出向不认 = 白起）")
        check(net._proxy_target_of(net.opener("self")) == mine,
              "opener('self') 没走自有代理")
    finally:
        net.is_proxy_alive = real_alive

    # 1b. 自有优先于系统：两个都活也必须选自有的（不是「有代理就行」）
    try:
        net.is_proxy_alive = lambda url, timeout=0.3: True
        check(net._proxy_target_of(net.opener(None)) == mine,
              "自有与系统代理同时活着时，默认没优先用自有的")
    finally:
        net.is_proxy_alive = real_alive

    # 1c. 全死 → 必须直连（不能被系统残留拖死；这也是「不靠代理也能用」）
    try:
        net.is_proxy_alive = lambda url, timeout=0.3: False
        check(net.is_direct_opener(net.opener(None)),
              "所有代理都死时 opener(None) 不是直连（会被系统残留拖死）")
        check(net.is_direct_opener(net.opener("self")),
              "所有代理都死时 opener('self') 不是直连")
    finally:
        net.is_proxy_alive = real_alive

    # 1d. 自有死、系统活 → 回落系统代理（不是直接掉到直连）
    sys_url, _ = net.system_proxy_url()
    if sys_url:
        try:
            net.is_proxy_alive = lambda url, timeout=0.3: url == sys_url
            check(net._proxy_target_of(net.opener(None)) == sys_url,
                  "自有没起、系统代理活着时，默认没回落到系统代理")
        finally:
            net.is_proxy_alive = real_alive

    # ---- 2. 死代理必须报错，不许静默回退
    try:
        net.opener(dead_proxy)
        check(False, "显式给了死代理却没报错（静默回退会导致「我以为走了代理」的误判）")
    except ConnectionError:
        pass

    # ---- 3. 核心回归：注入死代理的环境下，裸 urlopen 挂 / net.urlopen 通
    old_env = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                                              "http_proxy", "https_proxy", "no_proxy")}
    try:
        for k in ("http_proxy", "https_proxy", "no_proxy"):
            os.environ.pop(k, None)
        os.environ["HTTP_PROXY"] = dead_proxy
        os.environ["HTTPS_PROXY"] = dead_proxy
        os.environ["NO_PROXY"] = ""          # 连 127.0.0.1 也要走代理，才能测出毒

        # 3a. 确认这个环境真的是「毒」的（否则后面两条断言没意义）
        bare_poisoned = False
        try:
            # 用 build_opener() 默认行为（读环境变量），模拟裸 urlopen
            urllib.request.build_opener().open(target, timeout=5).read(32)
        except Exception:
            bare_poisoned = True
        check(bare_poisoned, "注入死代理后裸 opener 竟然还能通 —— 造毒失败，本条门禁无意义")

        # 3b. net.urlopen 必须照样通
        try:
            with net.urlopen(target, timeout=5) as r:
                got = r.read()
            check(r.status == 200 and got == b"NET_GATE_OK",
                  "net.urlopen 在死代理环境下取不到内容（status=%r body=%r）" % (r.status, got[:40]))
        except Exception as e:
            check(False, "net.urlopen 在死代理环境下失败: %s: %s" % (type(e).__name__, e))

        # 3c. 子进程里 import fleet 后，裸 urlopen 也恢复（全局兜底真的装了）
        import subprocess
        code = (
            "import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen(%r, timeout=5).read(32)\n"
            "    print('BEFORE_OK')\n"
            "except Exception:\n"
            "    print('BEFORE_FAIL')\n"
            "import fleet\n"
            "try:\n"
            "    r = urllib.request.urlopen(%r, timeout=5)\n"
            "    print('AFTER_OK', r.status)\n"
            "except Exception as e:\n"
            "    print('AFTER_FAIL', type(e).__name__)\n"
        ) % (target, target)
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=90, cwd=ROOT, env=env)
        out = (p.stdout or "") + (p.stderr or "")
        check("BEFORE_FAIL" in out, "子进程造毒失败（导入 fleet 前就通了）：%s" % out[-200:])
        check("AFTER_OK 200" in out, "import fleet 的全局兜底没生效：%s" % out[-200:])
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- 4. 判活：真监听 → True，死口 → False
    probe_srv = socket.socket()
    probe_srv.bind(("127.0.0.1", 0))
    probe_srv.listen(1)
    live_port = probe_srv.getsockname()[1]
    try:
        check(net.is_proxy_alive("http://127.0.0.1:%d" % live_port, timeout=1.0),
              "is_proxy_alive 对真监听端口判 False")
    finally:
        probe_srv.close()
    check(not net.is_proxy_alive(dead_proxy, timeout=0.5),
          "is_proxy_alive 对死端口判 True")

    # ---- 5. 清理器不越权：dry_run 不写盘
    r = net.clean_registry_proxy(dry_run=True)
    check("ok" in r, "clean_registry_proxy(dry_run=True) 返回结构异常: %r" % (r,))

    # ---- 6. 解析层不回归（轻量，不外网）
    from fleet.kinds.parse import extract_url, _sm3
    check(extract_url("复制打开抖音，看看 https://v.douyin.com/6zGANWG_NBA/") ==
          "https://v.douyin.com/6zGANWG_NBA/", "短链抽取回归")
    check(_sm3("abc") == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0",
          "SM3 标准向量回归")

    srv.shutdown()

    if FAIL:
        print("FAIL: %d 项" % len(FAIL))
        for f in FAIL:
            print("  - " + f)
        return 1
    print("PASS: 出向网络层全过（选路三态/死代理必报错/毒环境下 net.urlopen 仍通/全局兜底生效/判活准确/解析层无回归）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
