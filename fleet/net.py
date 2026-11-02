"""天网 · 出向请求层（所有对外 HTTP 的唯一正道）

为什么需要这个文件（2026-11-02 事故复盘）：
    karing 关掉之后，注册表里 `ProxyEnable=0` 但 `ProxyServer=127.0.0.1:3067` 还留着。
    Python 的 `urllib.request.getproxies()` **只看 ProxyServer 键存不存在，根本不看
    ProxyEnable** —— 于是所有 urllib 请求都被发往一个没人监听的端口，报
    10061「目标计算机积极拒绝」。

    同一时刻纯 socket 直连的实测结果：baidu 0.07s / github 0.18s / douyin 0.06s 全通。
    也就是说「网络坏了」是假象，坏的是请求层。
    更讽刺的是，写这个 ProxyServer 的正是天网自己的 netaccel._reg_proxy()。

所以立三条规矩（不是建议，是纪律）：
    1. 任何出向请求一律走本模块的 opener()/urlopen()，不要直接 urllib.request.urlopen。
    2. **默认直连**。系统代理设置是「别人的状态」，不能默认可信 —— 想用必须显式 proxy="auto"。
    3. 显式给了 proxy 就严格用它，连不上就报错，不许偷偷回退直连（否则「我以为走了代理」
       这种误判又会发生）。

用法：
    from .. import net
    with net.urlopen(url, headers={...}, timeout=15) as r:   # 默认直连
        body = r.read()

    net.urlopen(url, proxy="auto")                  # 用系统代理（先探测存活）
    net.urlopen(url, proxy="http://127.0.0.1:3066") # 指定代理

CLI：
    python -m fleet.net status      # 诊断：系统代理是什么、活不活、直连通不通
    python -m fleet.net clean       # 清掉注册表里的 ProxyServer 残留（治本）
    python -m fleet.net --selftest
"""
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

IS_WIN = os.name == "nt"
REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# 直连 opener：ProxyHandler({}) 显式声明「一个代理都不用」，这是绕过系统残留的唯一干净办法。
# 注意不能只靠 no_proxy 环境变量 —— 它挡不住注册表，实测无效。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


# ------------------------------------------------------------------ 系统代理状态

def system_proxy_url():
    """读系统（注册表/WinINET）代理设置。返回 (url, 说明)。

    注意：这里刻意直接调 getproxies()，因为它就是坑的来源，要看清楚它给的是什么。
    """
    try:
        p = urllib.request.getproxies()
    except Exception as e:
        return None, "读不到系统代理: %r" % (e,)
    for scheme in ("https", "http"):
        v = (p.get(scheme) or "").strip()
        if v:
            if "://" not in v:
                v = "http://" + v
            return v, "系统代理(%s)=%s" % (scheme, v)
    return None, "系统未设代理"


def reg_proxy_raw():
    """Windows 注册表原始值（ProxyEnable / ProxyServer / AutoConfigURL），非 Windows 返回 {}。"""
    if not IS_WIN:
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            for name in ("ProxyEnable", "ProxyServer", "AutoConfigURL", "ProxyOverride"):
                try:
                    out[name] = winreg.QueryValueEx(k, name)[0]
                except OSError:
                    out[name] = None
    except OSError as e:
        out["_err"] = str(e)
    return out


def parse_proxy(url):
    """'http://127.0.0.1:3066' → ('127.0.0.1', 3066)。解析不了返回 (None, None)。"""
    if not url:
        return None, None
    if "://" not in url:
        url = "http://" + url
    try:
        u = urllib.parse.urlsplit(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "https" else 80)
        return host, port
    except Exception:
        return None, None


def is_proxy_alive(url, timeout=0.5):
    """代理端口是否真的有人监听。只做 TCP 连接，不发协议内容（够判断死活了）。"""
    host, port = parse_proxy(url)
    if not host or not port:
        return False
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ opener / urlopen

def opener(proxy=None):
    """拿一个 opener。

    proxy=None      → 直连（不吃任何系统/环境代理）—— 默认
    proxy="auto"    → 系统代理，但先探活；死了就直连（这是唯一允许回退的情形）
    proxy="http://x:port" → 严格用它；连不上直接报错，不静默回退
    """
    if proxy is None or proxy == "" or proxy is False:
        return _DIRECT
    if proxy == "auto":
        url, _why = system_proxy_url()
        if url and is_proxy_alive(url):
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": url, "https": url}))
        return _DIRECT
    if not is_proxy_alive(proxy):
        raise ConnectionError("指定的代理不可用（端口无人监听）: %s" % proxy)
    # 显式代理：同时压掉环境变量里的 no_proxy 干扰？不必 —— ProxyHandler 显式给值即优先。
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def urlopen(url, data=None, headers=None, timeout=20, proxy=None, method=None):
    """统一出口。url 可以是 str 或 Request。

    返回 response（要做 with 或 .close()）。
    """
    hdrs = {"User-Agent": DEFAULT_UA}
    if headers:
        hdrs.update(headers)
    if isinstance(url, urllib.request.Request):
        req = url
        for k, v in hdrs.items():
            if req.get_header(k) is None and req.headers.get(k) is None:
                req.add_header(k, v)
    else:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    return opener(proxy).open(req, timeout=timeout)


def get_json(url, headers=None, timeout=20, proxy=None):
    """便捷：GET 并解析 JSON。返回 (obj, err)。"""
    import json as _json
    try:
        with urlopen(url, headers=headers, timeout=timeout, proxy=proxy) as r:
            raw = r.read().decode("utf-8", "replace")
        return _json.loads(raw), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


def get_text(url, headers=None, timeout=20, proxy=None, limit=None):
    """便捷：GET 文本。返回 (text, err)。"""
    try:
        with urlopen(url, headers=headers, timeout=timeout, proxy=proxy) as r:
            raw = r.read() if limit is None else r.read(limit)
        return raw.decode("utf-8", "replace"), None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])


# ------------------------------------------------------------------ 治理：清理残留

def clean_registry_proxy(dry_run=False):
    """把注册表里 ProxyEnable=0 但 ProxyServer 仍有值的「僵尸配置」清干净。

    只清 ProxyServer / AutoConfigURL 这两个值，并且**只在 ProxyEnable=0 时**才动 ——
    代理正开着（=1）的时候绝不碰，那是用户正在用的状态。
    """
    if not IS_WIN:
        return {"ok": False, "msg": "非 Windows，无需清理"}
    raw = reg_proxy_raw()
    if raw.get("_err"):
        return {"ok": False, "msg": "读注册表失败: %s" % raw["_err"]}
    enabled = raw.get("ProxyEnable") or 0
    if enabled:
        return {"ok": True, "changed": False,
                "msg": "代理当前是开启状态（ProxyEnable=1），不动它"}
    zombies = {k: raw.get(k) for k in ("ProxyServer", "AutoConfigURL")
               if raw.get(k) not in (None, "")}
    if not zombies:
        return {"ok": True, "changed": False, "msg": "没有残留，注册表干净"}
    if dry_run:
        return {"ok": True, "changed": False, "dry_run": True,
                "msg": "待清理: %r" % (zombies,)}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
            for name in zombies:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, "")
    except OSError as e:
        return {"ok": False, "msg": "写注册表失败: %s" % e}
    return {"ok": True, "changed": True, "msg": "已清掉残留: %r" % (zombies,)}


# ------------------------------------------------------------------ 诊断

PROBE_TARGETS = (
    ("baidu", "https://www.baidu.com"),
    ("github", "https://github.com"),
    ("douyin", "https://v.douyin.com/"),
)


def diagnose(timeout=8):
    """一眼看清：系统代理是什么、活不活、直连通不通、走系统代理通不通。"""
    url, why = system_proxy_url()
    out = {
        "system_proxy": url,
        "system_proxy_note": why,
        "proxy_alive": is_proxy_alive(url) if url else None,
        "registry": reg_proxy_raw(),
        "direct": {},
        "via_system": {},
    }
    for name, tgt in PROBE_TARGETS:
        try:
            with urlopen(tgt, timeout=timeout) as r:  # 默认直连
                out["direct"][name] = "HTTP %s" % r.status
        except Exception as e:
            out["direct"][name] = "FAIL %s" % type(e).__name__
    if url and out["proxy_alive"]:
        for name, tgt in PROBE_TARGETS:
            try:
                with urlopen(tgt, timeout=timeout, proxy=url) as r:
                    out["via_system"][name] = "HTTP %s" % r.status
            except Exception as e:
                out["via_system"][name] = "FAIL %s" % type(e).__name__
    out["verdict"] = _verdict(out)
    return out


def _verdict(d):
    direct_ok = sum(1 for v in d["direct"].values() if v.startswith("HTTP"))
    if direct_ok == len(d["direct"]):
        if d["system_proxy"] and not d["proxy_alive"]:
            return "直连正常；系统代理是僵尸配置（端口没人听），urllib 会被它坑 → 跑 clean"
        return "直连正常"
    if d["system_proxy"] and d["proxy_alive"]:
        return "直连不通但代理活着 → 用 proxy='auto' 或显式指定代理"
    return "直连不通且代理不可用 → 真·网络故障（查网卡/网关/DNS）"


# ------------------------------------------------------------------ 自测

def is_direct_opener(op):
    """判断一个 opener 是不是「一个代理都不用」。

    为什么这么绕：urllib 的 ProxyHandler **只在 proxies 非空时才 setattr('http_open')**，
    而 add_handler 会把方法名 'proxy_open' 之外的登记进去。空 proxies 的 ProxyHandler
    什么都不注册 → 根本不会出现在 op.handlers 里。
    所以「手上没有 ProxyHandler」== 直连，这是唯一可靠的判据（实测确认，别再改回去）。
    """
    return not any(isinstance(h, urllib.request.ProxyHandler) for h in op.handlers)


def _selftest():
    out = {"ok": False}
    # 1. 默认必须是直连 opener（不能被系统残留带跑）
    out["default_is_direct"] = is_direct_opener(opener(None))
    # 2. proxy='auto' 在僵尸代理下必须回退直连
    url, _ = system_proxy_url()
    if url and not is_proxy_alive(url, timeout=0.3):
        out["auto_falls_back_when_dead"] = is_direct_opener(opener("auto"))
    else:
        out["auto_falls_back_when_dead"] = True  # 没有僵尸代理，这条不适用
    # 3. 显式给死代理必须报错，不许静默回退
    try:
        opener("http://127.0.0.1:1")  # 1 号口不可能有人听
        out["dead_proxy_raises"] = False
    except ConnectionError:
        out["dead_proxy_raises"] = True
    # 4. 行为证据（这一条才是真凭据，前三条只是结构检查）
    #    僵尸代理存在时：裸 urlopen 必挂、net.urlopen 必通 → 一正一反证明修复真的有用。
    if url and not is_proxy_alive(url, timeout=0.3):
        try:
            urllib.request.urlopen("https://www.baidu.com", timeout=8).read(64)
            out["bare_urlopen_blocked"] = False
        except Exception as e:
            out["bare_urlopen_blocked"] = type(e).__name__
    else:
        out["bare_urlopen_blocked"] = "n/a(无僵尸代理)"
    try:
        with urlopen("https://www.baidu.com", timeout=8) as r:
            out["direct_probe"] = "HTTP %s" % r.status
            out["direct_ok"] = r.status == 200
    except Exception as e:
        out["direct_probe"] = "FAIL %s" % type(e).__name__
        out["direct_ok"] = False
    out["ok"] = (out["default_is_direct"] and out["auto_falls_back_when_dead"]
                 and out["dead_proxy_raises"] and out["direct_ok"])
    return out


# ------------------------------------------------------------------ CLI

def _cmd_status(_args):
    d = diagnose()
    print("系统代理 : %s" % (d["system_proxy"] or "(无)"))
    print("  说明   : %s" % d["system_proxy_note"])
    print("  端口活 : %s" % d["proxy_alive"])
    reg = d["registry"]
    if reg:
        print("  注册表 : ProxyEnable=%s ProxyServer=%r" % (reg.get("ProxyEnable"), reg.get("ProxyServer")))
    print("直连探测 : %s" % ", ".join("%s=%s" % kv for kv in d["direct"].items()))
    if d["via_system"]:
        print("走系统代理: %s" % ", ".join("%s=%s" % kv for kv in d["via_system"].items()))
    print("判定     : %s" % d["verdict"])
    return 0


def _cmd_clean(_args):
    r = clean_registry_proxy()
    print(("[OK] " if r["ok"] else "[FAIL] ") + r["msg"])
    if r.get("changed"):
        print("提示：已开的进程（含本进程）持有的旧 opener 不会自动更新，重启即可。")
    return 0 if r["ok"] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "status":
        return _cmd_status(argv[1:])
    if cmd == "clean":
        return _cmd_clean(argv[1:])
    if cmd in ("--selftest", "selftest"):
        import json
        r = _selftest()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    print("未知命令: %s（可用: status / clean / --selftest）" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
