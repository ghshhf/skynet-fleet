"""测「免费计算平台的部署域名」能否作为自己的门牌号。

判断依据（2026-10-31 已证）：封锁是按域名点名，不是按 IP 段（Google 除外）。
所以只要「平台根域名 + 随机子域名」还能 TLS 握手，就说明这个门牌号没被点名，
可以把一台墙外机器挂在后面 —— 这才是「自己给自己分配节点」的真实基础。

为什么用随机子域名测：
  根域名通不代表子域名通。vercel.com 通但 vercel.app 黑洞就是例子。
  而 TLS 握手发生在 HTTP 之前，握手成功 = SNI 没被拦，比看 HTTP 状态码更接近真相。

注意：这里只测「门牌号」，不测「房子里有没有人」。
  握手成功但 404 是正常的（子域名不存在），依然算通。
"""
import random
import socket
import ssl
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# (平台名, 部署域名后缀, 说明)
PLATFORMS = [
    ("Cloudflare Workers", "workers.dev", "免费 10 万请求/天，支持 WebSocket + TCP connect()"),
    ("Cloudflare Pages", "pages.dev", "静态托管，支持 Functions"),
    ("Deno Deploy", "deno.dev", "免费边缘函数，支持 WebSocket"),
    ("Render", "onrender.com", "免费 web service，512MB，15 分钟休眠"),
    ("Koyeb", "koyeb.app", "免费 1 个 web service"),
    ("Fly.io", "fly.dev", "按量免费额度"),
    ("Railway", "up.railway.app", "试用额度"),
    ("HuggingFace Spaces", "hf.space", "免费 CPU，禁代理（ToS）"),
    ("Glitch", "glitch.me", "免费 Node 托管"),
    ("Replit", "replit.app", "免费额度"),
    ("Surge", "surge.sh", "静态托管"),
    ("Vercel", "vercel.app", "免费，子域实测被封（对照）"),
    ("Netlify", "netlify.app", "免费，子域实测被封（对照）"),
    ("GitHub Pages", "github.io", "静态托管"),
    ("PythonAnywhere", "pythonanywhere.com", "免费账号"),
    ("Oracle Cloud", "oraclecloud.com", "永久免费 ARM 实例（需信用卡）"),
]


def rand_label(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def probe(item):
    name, suffix, note = item
    host = f"{rand_label()}.{suffix}"
    t0 = time.time()
    try:
        s = socket.create_connection((host, 443), timeout=7)
        tcp = round((time.time() - t0) * 1000)
    except Exception as e:
        return name, suffix, host, None, None, f"TCP:{type(e).__name__}"
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=host)
        ver = ss.version()
        ss.close()
        return name, suffix, host, tcp, ver, None
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        return name, suffix, host, tcp, None, f"TLS:{type(e).__name__}"


def main():
    with ThreadPoolExecutor(max_workers=10) as ex:
        rows = list(ex.map(probe, [(n, s, no) for n, s, no in PLATFORMS]))

    notes = {s: no for _, s, no in PLATFORMS}
    rows.sort(key=lambda r: (r[4] is None, r[3] or 9999))

    ok, bad = [], []
    print(f"{'平台':<22}{'域名后缀':<24}{'判定':<10}{'TCP':>7}  TLS")
    print("-" * 96)
    for name, suffix, host, tcp, ver, err in rows:
        if ver:
            ok.append((name, suffix))
            flag = "通"
        else:
            bad.append((name, suffix))
            flag = "封" if err and err.startswith("TLS") else "黑洞"
        tcp_s = f"{tcp}ms" if tcp is not None else "-"
        print(f"{name:<22}{suffix:<24}{flag:<10}{tcp_s:>7}  {ver or (err or '-')}")

    print()
    print(f"门牌号可用 {len(ok)} 个：")
    for name, suffix in ok:
        print(f"  · {name:<22} *.{suffix:<20} {notes.get(suffix, '')}")
    print()
    print(f"不可用 {len(bad)} 个：{', '.join(s for _, s in bad)}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
