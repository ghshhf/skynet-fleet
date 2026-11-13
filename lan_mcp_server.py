#!/usr/bin/env python3
"""天网·电脑侧局域网 MCP 服务端（零依赖，讲手机 LanServer 协议）。

目的：让**手机**能在局域网里**直接**调电脑的能力——不绕云中继、不走 HTTP API 翻译。
手机那端的 LanClient / self_lan_call / self_peer_call 发的是一行 JSON-RPC（tools/call），
首帧 hello 带 token；这台服务完全照这套协议回，于是手机把电脑当成一个普通 peer 就能控。

鉴权原则与手机一致：**token 即授权**。拿到 token 就有权调本机全部能力（含写操作），
所以 token 不要明文进群 / 进截图；要收回就换 token（--token 或删 ~/.fleet/lan_token）。

能力来源：fleet.catalog.build(with_nodes=False)。它返回 Registry，cap_names() 是能力清单，
call(cap, args, gate=...) 回 DeviceResult（as_dict() 里有 ok/code/data/err/ms 等）。
手机侧 self.* 工具名是 phone 内部映射；电脑侧直接用 catalog 的 domain.verb 原名，
两者不是一套名字——手机端远程控制台会原样列出本服务暴露的能力名。

mDNS：可选。装了 zeroconf 就广播 _skynet._tcp.，手机端 mDNS 发现会自动看到这台电脑；
没装就跳过（手动在手机上加 host:port:token 照样能用）。

用法：
    python lan_mcp_server.py                      # 默认端口 18765，token 从 ~/.fleet/lan_token 或随机生成
    python lan_mcp_server.py --port 18766 --token 你的口令
    python lan_mcp_server.py --readonly            # 只放行只读能力（覆盖 token=授权 的默认）
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_PORT = 18765
SERVICE_TYPE = "_skynet._tcp.local."
TOKEN_PATH = os.path.expanduser("~/.fleet/lan_token")


def _load_or_make_token(explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            t = f.read().strip()
            if t:
                return t
    except OSError:
        pass
    # 32 字节随机十六进制
    import secrets
    t = secrets.token_hex(16)
    try:
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(t)
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return t


class LanMcpServer:
    def __init__(self, token: str, port: int, readonly: bool):
        self.token = token
        self.port = port
        self.readonly = readonly
        self.server: socket.socket | None = None
        self.bound_port = 0
        self.connections = 0
        self.last_error = ""
        self._reg = None
        self._caps: list[str] = []
        self._lock = threading.Lock()

    # ---------- 能力注册表（懒加载，避免导入期重 IO） ----------
    def _ensure_reg(self):
        if self._reg is not None:
            return
        from fleet.catalog import build
        self._reg = build(with_nodes=False)
        self._caps = self._reg.cap_names()

    def _gate(self, cap, dev, args, level):
        # token 即授权：默认放行一切（含副作用）。--readonly 时只放行非副作用能力。
        if self.readonly:
            cap_obj = self._reg.capability(cap)
            if cap_obj is not None and getattr(cap_obj, "side_effect", False):
                return False, "readonly 模式不放行写操作"
        return True, "lan-auto-approve"

    def _call_cap(self, cap: str, args: dict) -> dict:
        self._ensure_reg()
        if cap not in self._caps:
            return {"ok": False, "code": "unsupported", "err": f"本机没有能力：{cap}"}
        try:
            res = self._reg.call(cap, args or {}, gate=self._gate)
        except Exception as e:  # 能力内部炸了，不能让连接断
            return {"ok": False, "code": "error", "err": f"{e.__class__.__name__}: {e}"}
        return res.as_dict()

    # ---------- 协议 ----------
    @staticmethod
    def _hello_payload(server: "LanMcpServer") -> dict:
        server._ensure_reg()
        import platform
        return {
            "lan": "ok",
            "name": "skynet-pc",
            "proto": 1,
            "platform": f"{platform.system()} {platform.release()}",
            "android": "",
            "caps": len(server._caps),
            "battery": -1,
            "charging": False,
            "screenOn": False,
            "tools": server._caps,
        }

    def _handle(self, conn: socket.socket):
        conn.settimeout(30.0)
        try:
            first = self._read_line(conn)
            if not first:
                return
            try:
                head = json.loads(first)
            except Exception:
                self._write_line(conn, json.dumps({"lan": "error", "err": "首帧不是 JSON"}))
                return
            if head.get("token") != self.token:
                self._write_line(conn, json.dumps({"lan": "error", "err": "口令不对"}))
                return
            lan = head.get("lan")
            if lan == "hello":
                self._write_line(conn, json.dumps(self._hello_payload(self)))
            elif lan == "speed":
                # 简单自环测速：本服务主要做控制，speed 直接回 ok 不传数据
                self._write_line(conn, json.dumps(
                    {"lan": "speed", "ok": True, "bytes": head.get("bytes", 0),
                     "ms": 0.0, "human": "0.0 MB/s（服务端未实现大块测速）"}))
                return
            else:
                self._write_line(conn, json.dumps({"lan": "error", "err": "首帧必须是 hello 或 speed"}))
                return

            while True:
                line = self._read_line(conn)
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    req = json.loads(line)
                except Exception:
                    self._write_line(conn, json.dumps(
                        {"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "解析失败"}}))
                    continue
                if req.get("lan") == "speed":
                    self._write_line(conn, json.dumps(
                        {"lan": "speed", "ok": True, "bytes": req.get("bytes", 0),
                         "ms": 0.0, "human": "0.0 MB/s"}))
                    return
                method = req.get("method")
                rid = req.get("id")
                if method != "tools/call":
                    self._write_line(conn, json.dumps(
                        {"jsonrpc": "2.0", "id": rid,
                         "error": {"code": -32601, "message": f"不支持的方法：{method}"}}))
                    continue
                params = req.get("params") or {}
                cap = params.get("name", "")
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, dict):
                    arguments = {}
                started = time.time()
                d = self._call_cap(cap, arguments)
                d["ms"] = round((time.time() - started) * 1000.0, 1)
                text = self._result_to_text(d)
                reply = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"content": [{"type": "text", "text": text}]},
                }
                if not d.get("ok"):
                    reply = {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32000, "message": d.get("err") or d.get("code", "error")},
                    }
                self._write_line(conn, json.dumps(reply, ensure_ascii=False))
        except socket.timeout:
            pass
        except Exception as e:
            self.last_error = f"{e.__class__.__name__}: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _result_to_text(d: dict) -> str:
        ok = d.get("ok")
        code = d.get("code", "")
        data = d.get("data") or {}
        err = d.get("err", "")
        # 把结果压成一句人话：成功给 data 摘要，失败给原因。
        if ok:
            if data:
                try:
                    body = json.dumps(data, ensure_ascii=False)
                except Exception:
                    body = str(data)
                return f"[{code}] {body}"
            return f"[{code}] 完成"
        return f"[{code}] {err}"

    # ---------- 传输（与手机 LanServer 同款：一行一个 JSON，UTF-8，\n 结尾） ----------
    @staticmethod
    def _read_line(conn: socket.socket) -> str | None:
        buf = bytearray()
        while True:
            b = conn.recv(1)
            if not b:
                return buf.decode("utf-8", "replace") if buf else None
            if b[0] == 0x0A:
                return buf.decode("utf-8", "replace")
            if len(buf) < 256 * 1024:
                buf += b

    @staticmethod
    def _write_line(conn: socket.socket, s: str):
        conn.sendall((s + "\n").encode("utf-8"))

    # ---------- 生命周期 ----------
    def start(self) -> bool:
        try:
            self.server = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(("::", self.port))
        except Exception:
            try:
                self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.server.bind(("0.0.0.0", self.port))
            except Exception as e:
                self.last_error = f"端口 {self.port} 绑定失败：{e}"
                return False
        self.server.listen(32)
        self.bound_port = self.server.getsockname()[1]
        self._ensure_reg()
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        return True

    def _accept_loop(self):
        assert self.server is not None
        while True:
            try:
                conn, _ = self.server.accept()
            except Exception:
                break
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def stop(self):
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None


def _maybe_mdns(port: int, token: str):
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except Exception:
        print("[lan-mcp] 未装 zeroconf，跳过 mDNS 广播（手机仍可手动添加 host:port:token）", flush=True)
        return None
    try:
        zc = Zeroconf()
        info = ServiceInfo(
            SERVICE_TYPE,
            "skynet-pc._skynet._tcp.local.",
            addresses=[socket.inet_aton("0.0.0.0")] if False else None,
            port=port,
            properties={"token": token[:8], "role": "pc"},
        )
        # zeroconf 需要真实地址；用本机 IPv4 列表注册
        import netifaces  # 可能也没装
        addrs = []
        for iface in netifaces.interfaces():
            for a in netifaces.ifaddresses(iface).get(netifaces.AF_INET, []):
                ip = a["addr"]
                if ip != "127.0.0.1":
                    addrs.append(socket.inet_aton(ip))
        if not addrs:
            addrs = [socket.inet_aton("127.0.0.1")]
        info.addresses = addrs
        zc.register_service(info)
        print(f"[lan-mcp] mDNS 已广播 {SERVICE_TYPE} 端口 {port}", flush=True)
        return zc
    except Exception as e:
        print(f"[lan-mcp] mDNS 广播失败（不影响直连）：{e}", flush=True)
        return None


def main():
    ap = argparse.ArgumentParser(description="天网电脑侧局域网 MCP 服务端（手机直连控制电脑）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 18765）")
    ap.add_argument("--token", type=str, default=None, help="共享口令；不填则读 ~/.fleet/lan_token 或随机生成")
    ap.add_argument("--readonly", action="store_true", help="只放行只读能力，写操作拒绝")
    args = ap.parse_args()

    token = _load_or_make_token(args.token)
    srv = LanMcpServer(token=token, port=args.port, readonly=args.readonly)
    if not srv.start():
        print(f"[lan-mcp] 启动失败：{srv.last_error}", file=sys.stderr, flush=True)
        return 1

    print(f"[lan-mcp] 监听端口 {srv.bound_port}  token={token}", flush=True)
    if args.readonly:
        print("[lan-mcp] 只读模式：写操作将被拒绝", flush=True)
    print("[lan-mcp] 手机端在「远程控制台 / 双机互联」里填 host:port:token 即可直连控制本机", flush=True)
    print("[lan-mcp] 口令即授权：拿到 token 就能调本机全部能力，勿外泄", flush=True)

    _maybe_mdns(srv.bound_port, token)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
        print("[lan-mcp] 已停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
