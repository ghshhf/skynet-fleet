"""MCP over /pair 对等中继：把手机上的 MCP Server 挂进本仓。

为什么需要这一层
----------------
手机 App 自己已经把无障碍通道包成了 MCP Server（`self_status` / `self_ui_tap`
…），但手机没有公网入口。`tools/cloudnode/wsrelay.py` 的 `/pair` 让两台机器
各自**出站**连到中继，配对后由中继盲转发字节 —— 不需要 VPN / ADB / sshd。

本模块就是 PC 侧的那个端点：
    PairMCPClient ← /pair (WebSocket) → 中继 → 手机 MCP Server

手机端协议（与 `fleet/mcp/server.py` 对齐）
------------------------------------------
  · 每次发送一个 UTF-8 JSON 对象，后跟换行 ``\\n``；
  · 一个物理帧可能含一条或多条消息，也可能一条消息被拆成多帧；
  · JSON-RPC 2.0：initialize / tools/list / tools/call /
    notifications/initialized / ping；
  · 工具名固定：self_status / self_ui_dump / self_ui_find / self_ui_tap /
    self_ui_text / self_ui_scroll / self_ui_key / self_app_launch /
    self_ui_screenshot，参数都在 ``arguments`` 对象里。

三条纪律
--------
1. 零第三方依赖：只复用仓库里的零依赖 `/pair` 实现，不引 mcp SDK。
2. 不静默：缺配置、连不上、超时、id 对不上、收到坏 JSON 都要报清楚。
3. 通知不等待：没有 id 的 JSON-RPC 消息发出去就走，不等回包。
"""
import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.parse

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.cloudnode.wsrelay import connect_pair        # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "device-fleet-pair"
CLIENT_VERSION = "0.1"


class MCPError(Exception):
    """对端 MCP server 出错。消息必须说清是哪个 server、哪一步、为什么。"""


def normalize_pair_url(url):
    """`wss://host` / `wss://host/` 补成 `/pair`；已有路径原样用。

    为什么要自动补：部署方从 wsrelay 的文档里抄下来的地址通常是裸域名，
    此时直接把 WS 打到 `/` 会落到健康检查页，拿到的不是配对端点，
    现象是「握手成功但第一帧没回音」—— 极难从错误里看出是路径少了。
    """
    url = (url or "").strip()
    if not url:
        return ""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("ws", "wss", "http", "https"):
        raise MCPError(f"中继地址 {url!r} 的 scheme 不认识（要 ws:// 或 wss://）")
    if not u.netloc:
        raise MCPError(f"中继地址 {url!r} 缺主机名")
    path = u.path or ""
    if path in ("", "/"):
        path = "/pair"
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path, u.query, u.fragment))


class PairMCPClient:
    """一个跑在 /pair 对面的 MCP server（手机）。

    生命周期：懒连接 → initialize → tools/list → （随时）tools/call → close。
    """

    def __init__(self, url, room, secret, name="phone-self", timeout=20.0):
        self.name = name
        self.room = (room or "").strip()
        self.secret = (secret or "").strip()
        self.url = normalize_pair_url(url)
        self.timeout = float(timeout)
        self._sock = None
        self._reader_t = None
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._id = 0
        self._send_lk = threading.Lock()        # 同一时刻只允许一组请求/响应
        self._conn_lk = threading.RLock()       # initialize 里会再进 _ensure
        self._server_info = {}
        self._tools = None
        self.last_error = ""

    # ---------------- 连接 ----------------

    def _config_error(self):
        missing = []
        if not self.url:
            missing.append("url（--url / SKYNET_RELAY_URL）")
        if not self.room:
            missing.append("room（--room / SKYNET_PAIR_ROOM）")
        if not self.secret:
            missing.append("secret（--secret / SKYNET_PAIR_SECRET）")
        if missing:
            return (f"MCP server「{self.name}」配置不全：缺 "
                    + "、".join(missing))
        return ""

    def _connect_socket(self):
        """只建连接、起读线程，不握手。给 --serve-stdio 的裸转发用。"""
        err = self._config_error()
        if err:
            raise MCPError(err)
        if self._sock is not None:
            return self._sock
        with self._conn_lk:
            if self._sock is not None:
                return self._sock
            try:
                sock = connect_pair(self.url, self.room, self.secret,
                                    timeout=self.timeout)
            except Exception as e:
                raise MCPError(
                    f"连不上手机端 MCP server「{self.name}」：{self.url}"
                    f"（room={self.room[:12]}）：{type(e).__name__}: {e}")
            self._sock = sock
            self._stop.clear()
            self._reader_t = threading.Thread(
                target=self._reader_loop, args=(sock,), daemon=True)
            self._reader_t.start()
            return sock

    def _ensure(self):
        """懒连接：第一次要工具/调工具时才连，并完成 initialize 握手。"""
        if self._sock is not None:
            return self._sock
        with self._conn_lk:
            if self._sock is not None:
                return self._sock
            self._connect_socket()
            try:
                self._initialize()
            except Exception:
                self.close()
                raise
            return self._sock

    def _reader_loop(self, sock):
        """把对端字节按 \\n 切成消息塞进队列。

        队列里放的是**原始行**而不是解析后的对象：解析失败时要能把原文
        报给调用方，否则只会看到一句「JSON 坏了」，不知道坏在哪。
        """
        buf = b""
        try:
            while not self._stop.is_set():
                data = sock.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    line = line.strip()
                    if line:
                        self._q.put(line)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self._q.put(None)          # 唤醒还在等响应的调用方

    # ---------------- JSON-RPC ----------------

    def _initialize(self):
        res = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })
        self._server_info = res or {}
        # 规范要求 initialize 之后发 notifications/initialized。
        # 它是通知：没有 id，也不等回包（手机端按约定只需读掉）。
        self._send("notifications/initialized", {}, want_reply=False)

    def _send(self, method, params=None, want_reply=True):
        sock = self._ensure()
        with self._send_lk:
            if want_reply:
                self._id += 1
                rid = self._id
            else:
                rid = None
            msg = {"jsonrpc": "2.0", "method": method}
            if want_reply:
                msg["id"] = rid
            if params is not None:
                msg["params"] = params
            raw = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                sock.sendall(raw)
            except Exception as e:
                raise MCPError(
                    f"手机端 MCP server「{self.name}」写不进去了"
                    f"（{method}）：{type(e).__name__}: {e}")
            if not want_reply:
                return None

            deadline = time.time() + self.timeout
            skipped = 0
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    raise MCPError(
                        f"手机端 MCP server「{self.name}」等 {method} 回应超时"
                        f"（{self.timeout:g}s，id={rid}，room={self.room[:12]}，"
                        f"url={self.url}；期间跳过 {skipped} 条非本请求的消息）")
                try:
                    item = self._q.get(timeout=remain)
                except queue.Empty:
                    raise MCPError(
                        f"手机端 MCP server「{self.name}」等 {method} 回应超时"
                        f"（{self.timeout:g}s，id={rid}）")
                if item is None:
                    self._q.put(None)      # 后面的调用也要能立刻知道断了
                    why = f"：{self.last_error}" if self.last_error else ""
                    raise MCPError(
                        f"手机端 MCP server「{self.name}」连接已断开，"
                        f"没等到 {method} 的回应{why}")
                try:
                    resp = json.loads(item.decode("utf-8"))
                except Exception as e:
                    raise MCPError(
                        f"手机端 MCP server「{self.name}」发来的不是合法 JSON："
                        f"{item[:120]!r}（{e}）")
                if not isinstance(resp, dict):
                    raise MCPError(
                        f"手机端 MCP server「{self.name}」发来的 JSON 不是对象："
                        f"{str(resp)[:120]}")
                if "id" not in resp:
                    skipped += 1          # 通知：与本次请求无关
                    continue
                if resp.get("id") != rid:
                    skipped += 1          # 别的请求的回包：绝不能错配
                    continue
                if "error" in resp:
                    err = resp.get("error") or {}
                    raise MCPError(
                        f"手机端 MCP server「{self.name}」在 {method} 报错："
                        f"{err.get('code')} {err.get('message')}")
                return resp.get("result")

    # ---------------- 对外 ----------------

    def server_info(self):
        return dict(self._server_info)

    def list_tools(self, refresh=False):
        """工具表。缓存；refresh=True 才重新问手机。"""
        if self._tools is not None and not refresh:
            return list(self._tools)
        res = self._send("tools/list", {}) or {}
        tools = res.get("tools") or []
        self._tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return list(self._tools)

    def call_tool(self, name, arguments=None):
        """调一个工具。返回 MCP 的 result 原样（含 content 数组）。"""
        if not name:
            raise MCPError(f"手机端 MCP server「{self.name}」调工具时没给名字")
        return self._send("tools/call",
                          {"name": name, "arguments": dict(arguments or {})})

    def status(self):
        """连接/握手/工具表都真跑一遍。失败必须带原因，不许只说「不可用」。"""
        try:
            tools = self.list_tools()
            info = self._server_info
            return {"ok": True, "reason": "",
                    "tools": len(tools),
                    "server_name": (info.get("serverInfo") or {}).get("name", ""),
                    "protocol": info.get("protocolVersion", "")}
        except Exception as e:
            self.last_error = str(e)
            return {"ok": False, "reason": str(e),
                    "tools": 0, "server_name": "", "protocol": ""}

    def close(self):
        sock, self._sock = self._sock, None
        self._tools = None
        self._stop.set()
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------- CLI

def _serve_stdio(client):
    """stdin 的每一行原样转发给手机，手机响应按行写回 stdout。

    这样 `fleet/mcp/client.py` 可以直接把它当 stdio MCP server 挂上：
        {"name": "phone", "command": "python",
         "args": ["-m", "fleet.mcp.pair", "--serve-stdio"], "env": {...}}

    注意：这里**不替客户端做 initialize**——stdio 客户端自己会握手。
    只补连接，然后把字节原样搬过去，语义完全由两端自己谈。
    """
    for s in (sys.stdin, sys.stdout):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    try:
        sock = client._connect_socket()
    except Exception as e:
        sys.stderr.write(f"{e}\n")
        return 1

    errs = []
    done = threading.Event()
    write_lk = threading.Lock()

    def writer():
        while not done.is_set():
            try:
                item = client._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                msg = json.loads(item.decode("utf-8"))
            except Exception as e:
                errs.append(f"手机端发来的不是合法 JSON：{item[:120]!r}（{e}）")
                done.set()
                break
            if not isinstance(msg, dict) or "id" not in msg:
                continue                  # 通知不回写
            with write_lk:
                sys.stdout.write(item.decode("utf-8") + "\n")
                sys.stdout.flush()

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()
    rc = 0
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)          # 只验形状，不改一个字节
            except json.JSONDecodeError as e:
                sys.stderr.write(f"stdin 这一行不是合法 JSON：{line[:120]!r}（{e}）\n")
                rc = 1
                break
            try:
                sock.sendall(line.encode("utf-8") + b"\n")
            except Exception as e:
                sys.stderr.write(f"写给手机失败：{type(e).__name__}: {e}\n")
                rc = 1
                break
    finally:
        # 给最后一批响应一点时间落盘，再收摊
        if rc == 0:
            done.wait(0.2)
        done.set()
        client.close()
    if errs and rc == 0:
        sys.stderr.write(errs[0] + "\n")
        return 1
    return rc


def _build_client(a):
    return PairMCPClient(a.url, a.room, a.secret, name=a.name, timeout=a.timeout)


def _main(argv=None):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        prog="python -m fleet.mcp.pair",
        description="手机端 MCP Server 的 /pair 桥（出站 WebSocket，不经 adb）")
    ap.add_argument("--url", default=os.environ.get("SKYNET_RELAY_URL", ""),
                    help="中继地址（也可用 SKYNET_RELAY_URL）；裸域名自动补 /pair")
    ap.add_argument("--room", default=os.environ.get("SKYNET_PAIR_ROOM", ""),
                    help="配对的房间号（也可用 SKYNET_PAIR_ROOM）")
    ap.add_argument("--secret", default=os.environ.get("SKYNET_PAIR_SECRET", ""),
                    help="配对密钥（也可用 SKYNET_PAIR_SECRET）")
    ap.add_argument("--name", default="phone-self", help="显示用的 server 名")
    ap.add_argument("--timeout", type=float, default=20.0, help="单次请求等待秒数")
    ap.add_argument("--status", action="store_true", help="打印连接/工具表状态")
    ap.add_argument("--list", action="store_true", help="打印工具表")
    ap.add_argument("--call", metavar="NAME", default="", help="调一个工具")
    ap.add_argument("--args", default="{}", help="工具参数（JSON 对象）")
    ap.add_argument("--serve-stdio", action="store_true",
                    help="把 stdin 的 JSON-RPC 原样转发给手机（给 stdio MCP 挂载用）")
    a = ap.parse_args(argv)

    client = _build_client(a)
    if a.serve_stdio:
        rc = _serve_stdio(client)
        client.close()
        return rc

    if not (a.status or a.list or a.call):
        ap.print_help()
        return 2

    try:
        if a.status:
            st = client.status()
            print(json.dumps(st, ensure_ascii=False, indent=2))
            if not st.get("ok"):
                client.close()
                return 1
        if a.list:
            print(json.dumps(client.list_tools(), ensure_ascii=False, indent=2))
        if a.call:
            try:
                args = json.loads(a.args or "{}")
            except json.JSONDecodeError as e:
                raise MCPError(f"--args 不是合法 JSON：{e}")
            if not isinstance(args, dict):
                raise MCPError("--args 必须是 JSON 对象")
            res = client.call_tool(a.call, args)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    except MCPError as e:
        sys.stderr.write(f"{e}\n")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(_main())
