#!/usr/bin/env python3
"""egress_scan —— 候选出口节点可达性扫描

背景事实（已实测）：
  · YouTube / Google IP 段整段黑洞（0/133 可达）
  · HuggingFace IP 通但 SNI 被 RST
  · *.daytonaproxy01.net 通（TLS 握手 + HTTP 404，1.9s）
  ⇒ 封锁不是"全封"，是"按 IP 段 + 按 SNI 点名"。
  ⇒ 只要找到一个域名不在点名册 + IP 可达的境外服务，就能当出口节点。

本脚本按"能跑我们自己的代码 + 有固定公开域名"筛选候选，四层测量：
    DNS → TCP:443 → TLS 握手（带真名 SNI）→ HTTP 响应
TLS 握手成功是关键判据：说明 SNI 没被点名。

用法：python tools/egress_scan.py
"""
from __future__ import annotations

import json
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor

# (类别, 域名, 说明) —— 只收"能部署代码 + 域名固定"的平台
CANDIDATES = [
    # 已验证基准
    ("基准", "daytonaproxy01.net", "Daytona 沙箱预览（已实测通）"),
    ("基准", "github.com", "GitHub（IP 通/SNI 封，靠 gitport）"),
    ("基准", "baidu.com", "国内对照"),

    # 能跑容器的 PaaS
    ("PaaS", "deno.dev", "Deno Deploy"),
    ("PaaS", "deno.com", "Deno Deploy 主站"),
    ("PaaS", "vercel.app", "Vercel 部署域名"),
    ("PaaS", "vercel.com", "Vercel 主站"),
    ("PaaS", "netlify.app", "Netlify 部署域名"),
    ("PaaS", "netlify.com", "Netlify 主站"),
    ("PaaS", "pages.dev", "Cloudflare Pages"),
    ("PaaS", "workers.dev", "Cloudflare Workers"),
    ("PaaS", "onrender.com", "Render 部署域名"),
    ("PaaS", "render.com", "Render 主站"),
    ("PaaS", "fly.dev", "Fly.io"),
    ("PaaS", "railway.app", "Railway"),
    ("PaaS", "up.railway.app", "Railway 部署域名"),
    ("PaaS", "koyeb.app", "Koyeb 部署域名"),
    ("PaaS", "koyeb.com", "Koyeb 主站"),
    ("PaaS", "modal.run", "Modal 部署域名"),
    ("PaaS", "replit.app", "Replit 部署域名"),
    ("PaaS", "replit.dev", "Replit Dev"),
    ("PaaS", "pythonanywhere.com", "PythonAnywhere"),
    ("PaaS", "glitch.me", "Glitch"),
    ("PaaS", "cyclic.app", "Cyclic"),
    ("PaaS", "spacehey.com", "对照"),

    # 隧道 / 内网穿透
    ("隧道", "ngrok.io", "ngrok"),
    ("隧道", "ngrok-free.app", "ngrok 免费域名"),
    ("隧道", "ngrok.app", "ngrok 新域名"),
    ("隧道", "serveo.net", "serveo"),
    ("隧道", "localtunnel.me", "localtunnel"),
    ("隧道", "pinggy.io", "pinggy"),
    ("隧道", "bore.pub", "bore"),
    ("隧道", "telebit.io", "telebit"),
    ("隧道", "cfargotunnel.com", "Cloudflare Argo Tunnel"),
    ("隧道", "trycloudflare.com", "Cloudflare 快速隧道"),

    # 容器 / GPU 算力平台（自带域名或端口转发）
    ("算力", "runpod.ai", "RunPod"),
    ("算力", "replicate.com", "Replicate"),
    ("算力", "huggingface.co", "HF（已知 SNI 封）"),
    ("算力", "hf.space", "HF Spaces（独立域名）"),

    # 对象存储 / 静态托管（可放 payload）
    ("存储", "r2.dev", "Cloudflare R2 公开域名"),
    ("存储", "backblazeb2.com", "Backblaze B2"),
    ("存储", "digitaloceanspaces.com", "DO Spaces"),
    ("存储", "wasabisys.com", "Wasabi"),

    # 大厂云（自家域名一般不在黑名单）
    ("大厂", "aws.amazon.com", "AWS"),
    ("大厂", "cloudfront.net", "AWS CloudFront"),
    ("大厂", "azure.com", "Azure"),
    ("大厂", "azurewebsites.net", "Azure App Service"),
    ("大厂", "cloudfunctions.net", "GCP Functions"),
    ("大厂", "run.app", "Cloud Run"),
    ("大厂", "googleapis.com", "Google APIs"),
]


def probe(host: str, timeout: float = 5.0) -> dict:
    r = {"host": host, "dns": None, "ip": None, "tcp": None, "tls": None, "http": None}
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        r["dns"] = round((time.time() - t0) * 1000)
        r["ip"] = infos[0][4][0]
    except Exception as e:
        r["dns"] = "ERR:" + type(e).__name__
        return r

    sock = None
    try:
        t1 = time.time()
        sock = socket.create_connection((r["ip"], 443), timeout=timeout)
        r["tcp"] = round((time.time() - t1) * 1000)
    except Exception as e:
        r["tcp"] = "ERR:" + type(e).__name__
        return r

    try:
        t2 = time.time()
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(sock, server_hostname=host)
        r["tls"] = round((time.time() - t2) * 1000)
        try:
            ss.sendall(
                f"HEAD / HTTP/1.1\r\nHost: {host}\r\nUser-Agent: Mozilla/5.0\r\nConnection: close\r\n\r\n".encode()
            )
            data = ss.recv(200)
            r["http"] = data.split(b"\r\n")[0].decode("latin1", "replace") if data else "EMPTY"
        except Exception as e:
            r["http"] = "ERR:" + type(e).__name__
        finally:
            try:
                ss.close()
            except Exception:
                pass
    except Exception as e:
        r["tls"] = "ERR:" + type(e).__name__ + ":" + str(e)[:40]
        try:
            sock.close()
        except Exception:
            pass
    return r


def verdict(r: dict) -> str:
    if isinstance(r["tls"], int):
        return "通"  # TLS 握手成功 = SNI 未被点名
    if isinstance(r["tcp"], int):
        return "IP通/SNI封"
    if isinstance(r["dns"], int):
        return "IP黑洞"
    return "DNS失败"


def main():
    print("扫描 %d 个候选 · 判据：TLS 握手成功 = SNI 未被点名 = 能当出口\n" % len(CANDIDATES))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda c: (c, probe(c[1])), CANDIDATES))

    buckets: dict[str, list] = {}
    rows = []
    for (cat, host, note), r in results:
        v = verdict(r)
        buckets.setdefault(v, []).append((host, note, r))
        rows.append({"cat": cat, "host": host, "note": note, "verdict": v, **{
            k: r[k] for k in ("dns", "ip", "tcp", "tls", "http")
        }})

    order = ["通", "IP通/SNI封", "IP黑洞", "DNS失败"]
    for v in order:
        items = buckets.get(v, [])
        print(f"=== {v}  ({len(items)}) ===")
        for host, note, r in sorted(items, key=lambda x: x[0]):
            detail = ""
            if v == "通":
                detail = f"tls={r['tls']}ms  {str(r['http'])[:24]}"
            elif v == "IP通/SNI封":
                detail = f"tcp={r['tcp']}ms  tls={str(r['tls'])[:34]}"
            elif v == "IP黑洞":
                detail = str(r["tcp"])[:34]
            else:
                detail = str(r["dns"])[:34]
            print(f"  {host:<28} {note:<26} {detail}")
        print()

    out = "tools/.egress_scan.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows}, f, ensure_ascii=False, indent=2)
    print(f"用时 {time.time()-t0:.1f}s · 明细写入 {out}")

    ok = [h for h, _, _ in buckets.get("通", [])]
    print(f"\n可用出口候选（{len(ok)} 个）：")
    for h in ok:
        print("  ·", h)


if __name__ == "__main__":
    main()
