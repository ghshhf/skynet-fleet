"""测 Invidious / Piped 这类「YouTube 镜像前端」域名在国内的可达性。

为什么测这个，而不是继续折腾代理：
  YouTube 直连是 ③ 类封锁（TLS 阶段按 IP 段整段切断），本地怎么弄都没用。
  但 Invidious 这类开源自托管前端的架构是：
      浏览器 → 镜像站（墙外机器，域名可能没被封）→ youtube.com
  视频流也由镜像站中转（local=true），不落到 googlevideo.com。
  ⇒ 只要镜像站的域名没被封，本机不需要任何代理就能看。

  这跟「网上下载器不挂代理」是同一个机制：拉取在对方机器上完成。

判据（三层，缺一不可）：
  ① TCP 能连
  ② TLS 握手成功（SNI 没被点名）
  ③ 真能返回内容（不是 502/超时）——很多镜像站活着但自己连不上 YouTube
"""
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# 候选：公共 Invidious 实例 + Piped + 官方索引
HOSTS = [
    # Invidious 公共实例（社区维护，域名各异）
    "yewtu.be",
    "inv.nadeko.net",
    "invidious.nerdvpn.de",
    "yt.artemislena.eu",
    "invidious.privacyredirect.com",
    "invidious.f5.si",
    "iv.melmac.space",
    "invidious.jing.rocks",
    "invidious.reallyaweso.me",
    "invidious.dhusch.de",
    # 官方实例索引
    "invidious.io",
    "redirect.invidious.io",
    "api.invidious.io",
    # Piped
    "piped.video",
    "pipedapi.kavin.rocks",
    "pipedapi.adminforge.de",
    "api.piped.private.coffee",
    # 对照：确认封锁仍然存在
    "www.youtube.com",
    "i.ytimg.com",
]


def probe(host: str):
    """返回 (host, tcp_ms|None, tls|None, http_line|None, err|None)。"""
    t0 = time.time()
    try:
        s = socket.create_connection((host, 443), timeout=7)
        tcp = round((time.time() - t0) * 1000)
    except Exception as e:
        return host, None, None, None, f"TCP:{type(e).__name__}"

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ss = ctx.wrap_socket(s, server_hostname=host)
        ver = ss.version()
        ss.sendall(
            f"GET / HTTP/1.1\r\nHost: {host}\r\n"
            f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)\r\n"
            f"Accept: */*\r\nConnection: close\r\n\r\n".encode()
        )
        ss.settimeout(10)
        buf = b""
        while len(buf) < 400:
            chunk = ss.recv(400 - len(buf))
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
        ss.close()
        return host, tcp, ver, line, None
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        return host, tcp, None, None, f"TLS:{type(e).__name__}"


def verdict(tcp, tls, line):
    if tcp is None:
        return "黑洞", 0
    if tls is None:
        return "SNI封", 1
    if not line:
        return "无响应", 2
    code = line.split()[1] if len(line.split()) > 1 else "?"
    if code.startswith(("2", "3")):
        return f"通 {code}", 3
    if code in ("401", "403", "429"):
        return f"通 {code}", 3
    return f"{code}", 2


def main():
    with ThreadPoolExecutor(max_workers=12) as ex:
        rows = list(ex.map(probe, HOSTS))

    rows.sort(key=lambda r: (verdict(r[1], r[2], r[3])[1], r[1] or 9999), reverse=True)

    print(f"{'域名':<38}{'判定':<12}{'TCP':>7}  {'TLS':<9}首行")
    print("-" * 108)
    ok = []
    for host, tcp, tls, line, err in rows:
        v, rank = verdict(tcp, tls, line)
        if rank >= 3:
            ok.append(host)
        tcp_s = f"{tcp}ms" if tcp is not None else "-"
        tls_s = tls or (err or "-")
        print(f"{host:<38}{v:<12}{tcp_s:>7}  {tls_s:<9}{(line or '')[:44]}")

    print()
    print(f"可用镜像站 {len(ok)} 个：{', '.join(ok) if ok else '（无）'}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
