"""TCP 预筛：把订阅里连 TCP 都建立不起来的节点提前剔掉。

为什么必须做：mihomo 的 url-test 会真的用协议去试每个节点。1751 个节点、
每个最多等 5 秒，串行要十几分钟，这段时间整个代理不可用。而大部分免费节点
在 TCP 层就是死的 —— 先花 30 秒用并发 TCP 探一遍，能把候选砍掉一大半。

hysteria2 走 QUIC（UDP），TCP 测不到，默认整批保留，让 mihomo 自己判。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor

SKIP_TCP = {"hysteria2", "hysteria", "tuic"}


def parse_proxies(txt: str) -> list[dict]:
    lines = txt.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^proxies:\s*$", ln):
            start = i + 1
            break
    if start is None:
        raise SystemExit("找不到 proxies:")
    body = []
    for ln in lines[start:]:
        if re.match(r"^[a-zA-Z_]", ln):
            break
        body.append(ln)

    out: list[dict] = []
    cur = None
    for ln in body:
        dash = re.match(r"^\s*-\s+(.*)$", ln)
        if dash:
            rest = dash.group(1).strip()
            if rest.startswith("{"):
                out.append(dict(re.findall(r'([\w-]+):\s*"?([^,"}]+)"?', rest)))
                cur = None
            else:
                cur = {}
                out.append(cur)
                kv = re.match(r"([\w-]+):\s*(.*)$", rest)
                if kv:
                    cur[kv.group(1)] = kv.group(2).strip().strip('"\'')
            continue
        kv = re.match(r"^\s+([\w-]+):\s*(.*)$", ln)
        if kv and cur is not None:
            cur[kv.group(1)] = kv.group(2).strip().strip('"\'')
    return [d for d in out if d.get("type")]


def tcp_ok(server: str, port: int, timeout: float) -> float | None:
    try:
        t = time.time()
        s = socket.create_connection((server, port), timeout=timeout)
        s.close()
        return round((time.time() - t) * 1000)
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="tools/cloudnode/sub/clash.yaml")
    ap.add_argument("--out", dest="out", default="tools/cloudnode/sub/alive.json")
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--workers", type=int, default=120)
    ap.add_argument("--keep-quic", action="store_true", default=True)
    a = ap.parse_args()

    txt = io.open(a.inp, encoding="utf-8").read()
    nodes = parse_proxies(txt)
    print(f"解析 {len(nodes)} 个节点")

    quic = [n for n in nodes if n.get("type", "").lower() in SKIP_TCP]
    tcp_nodes = [n for n in nodes if n.get("type", "").lower() not in SKIP_TCP]
    print(f"  QUIC 类（跳过 TCP 探测，保留）: {len(quic)}")
    print(f"  TCP 类待探测: {len(tcp_nodes)}")

    def probe(n: dict):
        srv = n.get("server", "")
        try:
            port = int(n.get("port", 0))
        except Exception:
            return n, None
        if not srv or not port:
            return n, None
        return n, tcp_ok(srv, port, a.timeout)

    t0 = time.time()
    alive: list[dict] = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for n, ms in ex.map(probe, tcp_nodes):
            if ms is not None:
                alive.append({"name": n.get("name"), "type": n.get("type"),
                              "server": n.get("server"), "port": n.get("port"),
                              "tcp_ms": ms})
    dur = round(time.time() - t0, 1)

    quic_names = [{"name": n.get("name"), "type": n.get("type"),
                   "server": n.get("server"), "port": n.get("port"),
                   "tcp_ms": -1} for n in quic]
    result = {"generated": int(time.time()), "probe_seconds": dur,
              "tcp_total": len(tcp_nodes), "tcp_alive": len(alive),
              "quic_kept": len(quic_names), "nodes": alive + quic_names}
    io.open(a.out, "w", encoding="utf-8").write(json.dumps(result, ensure_ascii=False, indent=1))

    print(f"\nTCP 存活 {len(alive)} / {len(tcp_nodes)}   用时 {dur}s")
    by_type: dict[str, int] = {}
    for x in alive:
        by_type[x["type"]] = by_type.get(x["type"], 0) + 1
    print("存活分布:", by_type)
    if alive:
        best = sorted(alive, key=lambda x: x["tcp_ms"])[:8]
        print("\n最快的几个：")
        for b in best:
            print(f"  {b['tcp_ms']:5}ms  {b['type']:9} {b['server']}:{b['port']}")


if __name__ == "__main__":
    main()
