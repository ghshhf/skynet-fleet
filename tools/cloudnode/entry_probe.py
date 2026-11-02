# -*- coding: utf-8 -*-
"""天网 · 动态入口探针（entry_probe）

为什么需要它
------------
主人 2026-11-02 指出根因：

    「IPv6 为什么没有大规模使用，是它变动太频繁了。
      所以很多时候，你会经常以为它是失效的。」

对。IPv6 地址按 RFC 4941 轮换、家宽重拨换前缀，**地址本身不是稳定标识**。
拿裸 IP 当门牌用，隔一阵就连不上，然后误判成「失效」。

天网里早就有对应解法（`fleet/rendezvous.py`），它要成立需要三件事同时在场：

    ① 一个**不变**的标识          → self_id（机器指纹，跨重启跨网络不变）
    ② 一个**会变**的当前地址       → 定期检测，变了就上报
    ③ 一个**能被外部验证**的口     → ← 本文件提供这一项

第 ③ 项之前是空的：rdv_auto 的墙外自检需要「一个本机在监听、且带端口的候选」，
没有它，自检只能报 `unverified`（测不了 ≠ 进不来）。所以这里补上：

    一个极小的常驻 HTTP 服务，监听在 [::]:PORT
    · 路径 `/`      返回一个最小 JSON（证明这是天网的入口探针）
    · 路径 `/ping`  返回 pong（墙外自检用这个最省事）
    · 路径 `/state` 返回当前 self_id / 当前地址 / 状态（对端查「现在在哪」）

它同时是**数据面**和**控制面**的锚点：
  · 数据面：wsrelay 的 WS 隧道可以指向它，外部机器经中继进来能落到本机
  · 控制面：主人从外部查 `/state`，拿到的是「self_id → 当前地址」的映射

为什么监听 [::] 而不是 0.0.0.0
---------------------------
v6 没有 NAT，同一个 socket 绑双栈就同时覆盖 v4/v6。
本机当前没拿到全球 v6 时，socket 依然绑得上（内核允许），
等路由器/热点下发前缀，地址一到就直接生效 —— **不用重起服务**。

安全边界
--------
1. 只回本机公开的状态，**不执行任何命令**、不读文件、不接受写入。
2. 只绑 v6 环回以外的全地址，但**没有秘密就查不到有用信息**，
   真正的门禁是中间那层 wsrelay 的 HMAC 鉴权。
3. 不自己改防火墙（那是 rdv_auto 的 pinhole 职责，失败也不影响这里）。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

DEFAULT_PORT = int(os.environ.get("SKYNET_ENTRY_PORT", "8791"))


def _self_id() -> str:
    """稳定的设备标识。取不到就退回环境变量，绝不每次现生成
    （现生成 = 每次重启都变成一台新设备，对端存的地址全失效）。"""
    try:
        from fleet import rendezvous as rdv
        return rdv.self_id()
    except Exception:
        return os.environ.get("SKYNET_SELF_ID", "sf-unknown")


def _current_addr() -> dict:
    """当前入口地址摘要：哪个地址现在算「当前」。
    读不出来就说读不出来 —— 这里不做网络探测（探测是 rendezvous 的活）。"""
    try:
        from fleet import rendezvous as rdv
        st = rdv.Rendezvous().state()
        return {"current": st.get("current"),
                "candidates": [c.get("ip") for c in (st.get("candidates") or [])],
                "self_id": st.get("self_id") or _self_id()}
    except Exception as e:
        return {"current": None, "candidates": [],
                "self_id": _self_id(), "err": f"{type(e).__name__}: {e}"}


def _handler_body(path: str) -> tuple:
    """返回 (status, body_bytes)。只读，绝不执行任何指令。"""
    path = (path or "/").split("?", 1)[0].rstrip("/") or "/"
    if path == "/ping":
        return 200, json.dumps({"ok": True, "pong": True,
                                "ts": round(time.time(), 2)}).encode()
    if path == "/state":
        d = _current_addr()
        d["ok"] = True
        d["ts"] = round(time.time(), 2)
        return 200, json.dumps(d, ensure_ascii=False).encode()
    if path == "/":
        return 200, json.dumps({
            "ok": True, "service": "skynet-entry-probe",
            "self_id": _self_id(),
            "routes": ["/", "/ping", "/state"],
            "ts": round(time.time(), 2),
        }).encode()
    return 404, json.dumps({"ok": False, "err": "not found"}).encode()


def serve(port: int = DEFAULT_PORT, host: str = "::", ready_cb=None):
    """阻塞式服务。返回时线程结束。

    用 `socketserver` 太重，这里手写最小 HTTP —— 依赖为零是硬要求
    （这套东西要能在任何墙外机器上跑，包括只有一个裸 python3 的沙箱）。
    """
    try:
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # IPV6_V6ONLY=0 → 同一个 socket 同时收 v4（映射地址）和 v6
        try:
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        srv.bind((host, port))
        srv.listen(64)
        real_port = srv.getsockname()[1]
        if ready_cb:
            ready_cb(real_port)
        print(f"[entry_probe] listening on [{host}]:{real_port}", flush=True)
    except OSError as e:
        print(f"[entry_probe] bind failed: {e}", flush=True)
        raise

    while True:
        try:
            conn, addr = srv.accept()
        except OSError:
            continue
        threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()


def _serve_one(conn: socket.socket):
    try:
        conn.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            d = conn.recv(4096)
            if not d:
                return
            buf += d
            if len(buf) > 8192:
                return
        head, _, _ = buf.partition(b"\r\n\r\n")
        line = head.split(b"\r\n")[0].decode("latin1", "replace")
        parts = line.split()
        method = parts[0] if parts else "GET"
        path = parts[1] if len(parts) > 1 else "/"
        status, body = _handler_body(path)
        if method not in ("GET", "HEAD"):
            status, body = 405, json.dumps({"ok": False, "err": "method not allowed"}).encode()
        conn.sendall(
            b"HTTP/1.1 " + str(status).encode() + b" " +
            (b"OK" if status == 200 else b"ERR") + b"\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    port = DEFAULT_PORT
    if argv:
        try:
            port = int(argv[0])
        except ValueError:
            pass
    serve(port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
