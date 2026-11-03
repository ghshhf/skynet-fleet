"""t78 · 出向层自适应（默认直连优先、网络失败才回退代理）。

不依赖真网：全程 monkeypatch `_DIRECT` / `_build` / `resolve_proxy` 验证选路顺序。
"""
import sys
sys.path.insert(0, '.')
from fleet import net
import urllib.error


def chk(cond, msg):
    print(('  ✓ ' if cond else '  ✗ FAIL: ') + msg)
    if not cond:
        raise SystemExit(1)


class FakeResp:
    def __init__(self, status=200):
        self.status = status

    def read(self):
        return b'ok'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, fail=None):
        self.fail = fail
        self.called = 0

    def open(self, req, timeout=None):
        self.called += 1
        if self.fail:
            raise self.fail
        return FakeResp()


# 1) 默认直连优先：直连失败 → 回退代理成功
print("[1] 默认直连优先、失败回退代理")
direct = FakeOpener(fail=urllib.error.URLError("conn reset"))
proxy_op = FakeOpener()
net._DIRECT = direct
net._build = lambda u: proxy_op
net.resolve_proxy = lambda allow_system=True: ("http://p:1", "fallback")
r = net.urlopen("http://x", proxy=None)
chk(direct.called == 1, "直连先被调用一次")
chk(proxy_op.called == 1, "直连失败后回退代理被调用一次")
chk(r.status == 200, "回退后拿到响应")

# 2) 直连成功 → 不碰代理（随处可达时不浪费一次代理）
print("[2] 直连成功时不触碰代理")
direct2 = FakeOpener()
proxy2 = FakeOpener(fail=urllib.error.URLError("proxy dead"))
net._DIRECT = direct2
net._build = lambda u: proxy2
net.resolve_proxy = lambda allow_system=True: ("http://p:1", "fallback")
r = net.urlopen("http://x", proxy=None)
chk(direct2.called == 1, "直连成功时只调直连")
chk(proxy2.called == 0, "直连成功时不调用代理（随处可达路径不碰代理）")

# 3) HTTP 层错误不回退（业务结论 vs 网络问题）
print("[3] HTTPError 不回退，直接抛出")
direct3 = FakeOpener(fail=urllib.error.HTTPError("http://x", 404, "nf", {}, None))
proxy3 = FakeOpener()
net._DIRECT = direct3
net._build = lambda u: proxy3
try:
    net.urlopen("http://x", proxy=None)
    chk(False, "HTTPError 应抛出")
except urllib.error.HTTPError:
    chk(True, "HTTPError 不回退、直接抛出")

# 4) fallback=False 关闭回退
print("[4] fallback=False 关闭回退")
direct4 = FakeOpener(fail=urllib.error.URLError("conn reset"))
proxy4 = FakeOpener()
net._DIRECT = direct4
net._build = lambda u: proxy4
try:
    net.urlopen("http://x", proxy=None, fallback=False)
    chk(False, "fallback=False 应抛出")
except urllib.error.URLError:
    chk(True, "fallback=False 不回退、抛出直连错误")

# 5) 显式 direct 永远直连，不回退
print("[5] proxy='direct' 强制直连")
direct5 = FakeOpener()
proxy5 = FakeOpener(fail=urllib.error.URLError("p"))
net._DIRECT = direct5
net._build = lambda u: proxy5
r = net.urlopen("http://x", proxy="direct")
chk(direct5.called == 1 and proxy5.called == 0, "direct 模式只直连")

print("\nALL PASS · t78")
