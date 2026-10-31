#!/usr/bin/env python3
"""cloudnode_probe —— 云端节点可行性探针

目的：回答一个问题 —— Daytona 沙箱能不能当"没被墙的出口节点"？

三组测量：
  A. 沙箱出网：能到 YouTube / Google / Twitter 吗？（白名单实测）
  B. 入向可达：preview 域名从国内能不能真连到沙箱里起的服务？
  C. 端到端：把它当 HTTP CONNECT 代理用，真实吞吐多少？

用法：
  python tools/cloudnode_probe.py a     # 只跑出网
  python tools/cloudnode_probe.py all
"""
from __future__ import annotations

import json
import sys
import time

API_URL = "https://app.daytona.io/api"


def _load_env():
    """从 tools/.cloudnode.env 读凭据（本地私有，已 gitignore）。"""
    import os
    from pathlib import Path
    env = Path(__file__).with_name(".cloudnode.env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    key = os.environ.get("DAYTONA_API_KEY", "")
    if not key:
        raise SystemExit("缺少 DAYTONA_API_KEY：请写入 tools/.cloudnode.env")
    return key, os.environ.get("DAYTONA_API_URL", API_URL)


API_KEY, API_URL = _load_env()

# 出网目标：分三档
#   对照档 = 已知可通（pypi/github）
#   疑点档 = 之前实测被 reset（cloudflare/ipify）
#   天王山 = 封锁核心目标（youtube/google/twitter）
PROBE_TARGETS = [
    ("对照-pypi", "https://pypi.org/simple/"),
    ("对照-github", "https://github.com"),
    ("疑点-cloudflare", "https://1.1.1.1"),
    ("疑点-ipify", "https://api.ipify.org"),
    ("天王山-youtube", "https://www.youtube.com"),
    ("天王山-google", "https://www.google.com"),
    ("天王山-twitter", "https://x.com"),
    ("天王山-instagram", "https://www.instagram.com"),
    ("天王山-telegram", "https://web.telegram.org"),
    ("天王山-wikipedia", "https://en.wikipedia.org"),
    ("天王山-huggingface", "https://huggingface.co"),
    ("天王山-openai", "https://api.openai.com"),
]


def _client():
    from daytona import Daytona, DaytonaConfig
    return Daytona(DaytonaConfig(api_key=API_KEY, api_url=API_URL))


def _create(dt):
    from daytona import CreateSandboxParams
    try:
        return dt.create(CreateSandboxParams(language="python"))
    except TypeError:
        # 老版本 SDK 是位置参数
        return dt.create()


def _exec(sb, cmd: str, timeout: int = 60) -> str:
    resp = sb.process.exec(cmd, timeout=timeout)
    out = getattr(resp, "result", None)
    if out is None:
        out = getattr(resp, "output", None)
    if out is None:
        out = str(resp)
    return out


def probe_egress(sb) -> list[dict]:
    """A. 沙箱出网：逐目标测 TCP + TLS + HTTP 三层。"""
    script = r'''
import socket, ssl, sys, time
targets = %s
for name, url in targets:
    host = url.split("//")[1].split("/")[0]
    row = {"name": name, "host": host}
    # 1) TCP
    try:
        t0 = time.time()
        s = socket.create_connection((host, 443), timeout=6)
        row["tcp"] = round((time.time() - t0) * 1000)
        # 2) TLS
        try:
            ctx = ssl.create_default_context()
            ss = ctx.wrap_socket(s, server_hostname=host)
            row["tls"] = ss.version()
            # 3) HTTP
            ss.sendall(("GET / HTTP/1.1\r\nHost: %s\r\nUser-Agent: probe\r\nConnection: close\r\n\r\n" % host).encode())
            data = ss.recv(200)
            first = data.split(b"\r\n")[0].decode("latin1", "replace")
            row["http"] = first
            ss.close()
        except Exception as e:
            row["tls"] = "ERR " + type(e).__name__ + ":" + str(e)[:60]
        finally:
            try: s.close()
            except Exception: pass
    except Exception as e:
        row["tcp"] = "ERR " + type(e).__name__ + ":" + str(e)[:60]
    print(json.dumps(row, ensure_ascii=False))
''' % json.dumps(PROBE_TARGETS)
    # 写进沙箱再跑，避开 shell 转义地狱
    import base64
    b64 = base64.b64encode(script.encode()).decode()
    cmd = f"python3 -c \"import base64;exec(base64.b64decode('{b64}'))\""
    out = _exec(sb, cmd, timeout=180)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def probe_ingress(sb) -> str:
    """B. 在沙箱起一个 HTTP 服务，返回 preview URL 供本地 curl。"""
    _exec(sb, "mkdir -p /tmp/probe && echo CLOUDNODE_OK > /tmp/probe/index.html")
    # 后台起服务；沙箱里 nohup 是常规做法
    _exec(sb, "pkill -f 'http.server 8899' || true", timeout=20)
    _exec(sb, "cd /tmp/probe && nohup python3 -m http.server 8899 --bind 0.0.0.0 >/tmp/probe/srv.log 2>&1 & echo started", timeout=20)
    time.sleep(2)
    check = _exec(sb, "curl -sS --max-time 5 http://127.0.0.1:8899/ || echo SELF_FAIL", timeout=20)
    print(f"  [沙箱内自测] {check.strip()[:80]}")
    url = None
    try:
        link = sb.get_preview_link(8899)
        url = getattr(link, "url", None) or str(link)
    except Exception as e:
        print(f"  [!] get_preview_link 失败: {e}")
    return url or ""


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    t0 = time.time()
    dt = _client()
    print(f"[1] 创建沙箱 ...")
    sb = _create(dt)
    sid = getattr(sb, "id", "?")
    print(f"    沙箱 id = {sid}   用时 {time.time()-t0:.1f}s")

    result = {"sandbox_id": sid}

    if mode in ("a", "all"):
        print(f"\n[2] A 组 · 沙箱出网实测")
        print(f"    {'目标':<22} {'TCP':>8}  {'TLS':<10} HTTP")
        print("    " + "-" * 70)
        rows = probe_egress(sb)
        for r in rows:
            tcp = str(r.get("tcp", "-"))
            tls = str(r.get("tls", "-"))[:9]
            http = str(r.get("http", "-"))[:28]
            mark = ""
            if isinstance(r.get("tcp"), int) and r.get("http", "").startswith("HTTP/1.1 2"):
                mark = "  <== 通"
            print(f"    {r['name']:<22} {tcp:>8}  {tls:<10} {http}{mark}")
        result["egress"] = rows

    if mode in ("b", "all"):
        print(f"\n[3] B 组 · preview 入向可达")
        url = probe_ingress(sb)
        print(f"    preview URL = {url}")
        result["preview_url"] = url
        with open("tools/.cloudnode_probe.json", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n    已写入 tools/.cloudnode_probe.json")
        print(f"    接下来本地执行：curl -sS --max-time 10 {url}")
        # 不销毁，留给本地 curl 用
        print(f"\n[保留] 沙箱 {sid} 未销毁，测完手动删")
        return

    with open("tools/.cloudnode_probe.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[4] 总用时 {time.time()-t0:.1f}s · 结果已写入 tools/.cloudnode_probe.json")
    try:
        dt.delete(sb)
        print("[5] 沙箱已销毁")
    except Exception as e:
        print(f"[5] 销毁失败（可忽略）：{e}")


if __name__ == "__main__":
    main()
