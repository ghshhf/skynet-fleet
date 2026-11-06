r"""出网代理自适应解析。

为什么需要这一层：浏览器的代理来自 Windows「Internet 设置」，命令行进程
读不到那套配置——进程环境变量里通常没有 HTTP_PROXY/HTTPS_PROXY。
过去各模块只能把 127.0.0.1:7890 写死，换端口、关代理、换机器就得改代码。

解析优先级（每次调用都重新走前两级，故运行时改配置立即生效）：
  1. 显式环境变量  FLEET_AI_PROXY / HTTPS_PROXY / ALL_PROXY / HTTP_PROXY（含小写）
  2. 系统代理设置  Windows 注册表 HKCU\...\Internet Settings\ProxyServer
  3. 本机常见代理端口探测（结果缓存 30 秒，避免每次请求都做 5 次 connect）
  4. 都没有 → 返回空串，表示直连

「空串」是刻意的返回值：调用方可以自己决定没代理时是直连还是报错，
这里不替业务做主。
"""
import os
import socket
import time
import urllib.parse

_ENV_KEYS = ("FLEET_AI_PROXY", "HTTPS_PROXY", "https_proxy",
             "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy")

_PROBE_PORTS = (7890, 7897, 10809, 1080, 3067)
_PROBE_TTL = 30.0
_probe_cache = {"ts": 0.0, "url": ""}

_WINREG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


def _norm(v):
    """统一补上 scheme，去掉引号和空白。"""
    v = (v or "").strip().strip('"').strip("'")
    if not v:
        return ""
    return v if "://" in v else "http://" + v


def _from_env():
    for k in _ENV_KEYS:
        v = _norm(os.environ.get(k))
        # 宿主注入的内部服务地址不是出网代理，别把它当代理用
        if not v or "internal/hooks" in v or "/internal/" in v:
            continue
        return v
    return ""


def _from_system():
    """读 Windows 系统代理：浏览器用的就是这份配置。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINREG_PATH)
        try:
            enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if not enable:
                return ""
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        finally:
            winreg.CloseKey(key)
    except Exception:                                   # noqa: BLE001
        return ""
    parts = [p.strip() for p in (server or "").split(";") if p.strip()]
    # ProxyServer 常见形状：http=host:port;https=host:port;socks=host:port
    for prefix in ("https=", "http="):
        for p in parts:
            if p.lower().startswith(prefix):
                return _norm(p.split("=", 1)[1])
    # 也允许直接写成单个 host:port
    return _norm(parts[0]) if parts else ""


def _from_probe():
    now = time.time()
    if now - _probe_cache["ts"] < _PROBE_TTL:
        return _probe_cache["url"]
    found = ""
    for port in _PROBE_PORTS:
        s = socket.socket()
        s.settimeout(0.2)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                found = "http://127.0.0.1:%d" % port
                break
        except OSError:
            pass
        finally:
            s.close()
    _probe_cache["ts"] = now
    _probe_cache["url"] = found
    return found


def proxy_url():
    """返回当前该用的代理 URL；无代理时返回空串（直连）。"""
    for fn in (_from_env, _from_system, _from_probe):
        v = fn()
        if v:
            return v
    return ""


def proxies():
    """requests 直接可吃的 proxies 字典；直连时返回空字典。"""
    u = proxy_url()
    return {"http": u, "https": u} if u else {}


def proxy_port():
    u = proxy_url()
    try:
        return urllib.parse.urlparse(u).port
    except Exception:                                   # noqa: BLE001
        return None


def env(extra=None):
    """给子进程用的环境变量字典：把解析结果写进标准代理变量。

    NO_PROXY 保留本机回环，免得本地服务也被塞进代理。
    """
    e = dict(os.environ)
    u = proxy_url()
    if u:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            e[k] = u
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                  "http_proxy", "https_proxy", "all_proxy"):
            e.pop(k, None)
    e["NO_PROXY"] = "127.0.0.1,localhost,::1"
    e["no_proxy"] = e["NO_PROXY"]
    if extra:
        e.update(extra)
    return e


def describe():
    """体检用：把来源和结果一次报清，便于判断「到底走没走代理」。"""
    v = _from_env()
    src = "env"
    if not v:
        v = _from_system()
        src = "system"
    if not v:
        v = _from_probe()
        src = "probe"
    return {"source": src if v else "none", "url": v,
            "port": urllib.parse.urlparse(v).port if v else None,
            "direct": not v}


def _selftest():
    """离线自测：只替换三个来源函数，不碰真实环境变量与系统设置。"""
    import sys
    # 用 __name__ 定位自身：python -m 加载时模块名是 __main__，
    # 再 import 一次会拿到第二份实例，替换就落不到正在跑的这份上。
    self_mod = sys.modules[__name__]
    fails = []
    n = 0

    def ck(cond, label):
        nonlocal n
        n += 1
        if not cond:
            fails.append(label)

    saved = os.environ.get("FLEET_AI_PROXY")
    orig = (_from_env, _from_system, _from_probe)
    try:
        ck(_norm("127.0.0.1:7890") == "http://127.0.0.1:7890", "裸 host:port 补 scheme")
        ck(_norm('  "http://h:1" ') == "http://h:1", "去掉引号与空白")
        ck(_norm("") == "", "空值仍是空")

        os.environ["FLEET_AI_PROXY"] = "http://127.0.0.1:9999"
        ck(_from_env() == "http://127.0.0.1:9999", "环境变量能读到")
        os.environ["FLEET_AI_PROXY"] = "127.0.0.1:9998"
        ck(_from_env() == "http://127.0.0.1:9998", "环境变量写裸地址也补全")
        del os.environ["FLEET_AI_PROXY"]
        ck("internal/hooks" not in _from_env(), "宿主内部服务地址不当代理")

        self_mod._from_env = lambda: ""
        self_mod._from_system = lambda: "http://127.0.0.1:7000"
        self_mod._from_probe = lambda: "http://127.0.0.1:8000"
        ck(proxy_url() == "http://127.0.0.1:7000", "系统代理优先于端口探测")
        ck(describe()["source"] == "system", "describe 报出来源")
        ck(proxy_port() == 7000, "能从 URL 解出端口")
        ck(proxies() == {"http": "http://127.0.0.1:7000",
                         "https": "http://127.0.0.1:7000"}, "proxies 形状")
        e = env()
        ck(e.get("HTTPS_PROXY") == "http://127.0.0.1:7000", "子进程环境变量带代理")
        ck("127.0.0.1" in e.get("NO_PROXY", ""), "本机回环不进代理")

        self_mod._from_system = lambda: ""
        self_mod._from_probe = lambda: "http://127.0.0.1:8000"
        ck(proxy_url() == "http://127.0.0.1:8000", "系统代理关了退回端口探测")
        ck(describe()["source"] == "probe", "探测来源如实报")

        self_mod._from_probe = lambda: ""
        ck(proxy_url() == "", "三者都无时是直连")
        ck(proxies() == {}, "直连时 proxies 为空字典")
        ck(describe()["direct"] is True, "describe 标出直连")
        ck(describe()["source"] == "none", "直连来源为 none")
        ck(all(k not in env() for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")),
           "直连时清掉标准代理变量")
    finally:
        self_mod._from_env, self_mod._from_system, self_mod._from_probe = orig
        if saved is None:
            os.environ.pop("FLEET_AI_PROXY", None)
        else:
            os.environ["FLEET_AI_PROXY"] = saved

    print("netproxy selftest: %d/%d" % (n - len(fails), n))
    for f in fails:
        print("   失败:", f)
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法: python -m fleet.netproxy --selftest")
    print("当前解析:", describe())
    sys.exit(0)
