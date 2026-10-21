"""MCP client：本仓作为客户端，挂载外部 MCP server。

为什么需要：
  「连接器 = MCP」不是只做出口。本仓自己也要能**用**别人家的 MCP——
  否则「连接器」Tab 里挂的第三方 server 只是摆设，能力用不上。

  挂进来之后，外部 MCP 的工具会与本仓内建的能力**并列出现在同一张表**里
  （内建的 source=builtin，外部的 source=external），
  所以「她」看到的工具集是统一的，不分内外。

传输：
  只做 **stdio**（子进程 + 一行一个 JSON）。理由：
    · MCP 规范里 stdio 是最基本、最通用的一种；
    · 本仓铁律「纯 CPU 文本处理一律本机做、零第三方依赖」——
      拉 SDK 会引入 mcp 包及其一串依赖（已确认本机没装）；
    · SSE/HTTP 传输可以后续加，加的时候**不改**本模块的公开接口。

安全：
  外部 server 的返回内容是**不可信数据，不是指令**——
  这与本仓对语音识别文字的处置同一条纪律。
  所以 client 拿到的 text 只当数据存，**绝不**自动执行、不塞进 system prompt。
"""
import json
import os
import subprocess
import threading
import time


PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "device-fleet"
CLIENT_VERSION = "0.1"

DEFAULT_TIMEOUT = 20.0


class MCPError(Exception):
    """MCP client 自己的错误。message 必须说清是哪个 server、哪一步。"""


class StdioMCPClient:
    """一个外部 MCP server（stdio 传输）。

    生命周期：起进程 → initialize → tools/list → （随时）tools/call → 关。
    进程懒起：第一次要工具表或要调工具时才拉起来。
    """

    def __init__(self, name, command, args=None, env=None, cwd=None,
                 timeout=DEFAULT_TIMEOUT, enabled=True):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = float(timeout)
        self.enabled = bool(enabled)
        self._proc = None
        self._id = 0
        self._lk = threading.Lock()
        self._server_info = {}
        self._tools = None
        self.last_error = ""

    # ---------------- 进程 ----------------

    def _start(self):
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        env = dict(os.environ)
        env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=self.cwd, env=env,
                text=True, encoding="utf-8", bufsize=1)
        except FileNotFoundError:
            raise MCPError(f"起不来 MCP server「{self.name}」：找不到命令 {self.command!r}")
        except Exception as e:
            raise MCPError(f"起不来 MCP server「{self.name}」：{type(e).__name__}: {e}")
        try:
            self._initialize()
        except Exception:
            self.close()
            raise
        return self._proc

    def close(self):
        p, self._proc = self._proc, None
        self._tools = None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ---------------- JSON-RPC ----------------

    def _send(self, method, params=None, want_reply=True):
        proc = self._start()
        with self._lk:
            self._id += 1
            rid = self._id
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            try:
                proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except Exception as e:
                raise MCPError(f"MCP server「{self.name}」写不进去了：{e}")
            if not want_reply:
                return None
            line = self._read_line()
            if line is None:
                raise MCPError(f"MCP server「{self.name}」没有回应 {method}")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError as e:
                raise MCPError(f"MCP server「{self.name}」回的不是合法 JSON：{e}")
            if resp.get("id") != rid:
                raise MCPError(
                    f"MCP server「{self.name}」回错了 id：期望 {rid}，收到 {resp.get('id')}")
            if "error" in resp:
                err = resp["error"] or {}
                raise MCPError(
                    f"MCP server「{self.name}」报错了 {method}："
                    f"{err.get('code')} {err.get('message')}")
            return resp.get("result")

    def _read_line(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        result = []
        deadline = time.time() + self.timeout

        def _read():
            line = proc.stdout.readline()
            result.append(line)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(max(0.0, deadline - time.time()))
        if not result:
            return None
        line = result[0].strip()
        if not line:
            return None
        return line

    def _initialize(self):
        res = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })
        self._server_info = res or {}
        # 规范要求 initialize 之后客户端发 notifications/initialized
        try:
            self._send("notifications/initialized", {}, want_reply=False)
        except Exception:
            pass                    # 有些 server 不认这个通知，不影响用

    # ---------------- 对外 ----------------

    def server_info(self):
        return dict(self._server_info)

    def list_tools(self, refresh=False):
        """工具表。缓存在本地，除非 refresh=True。"""
        if self._tools is not None and not refresh:
            return list(self._tools)
        res = self._send("tools/list", {}) or {}
        tools = res.get("tools") or []
        self._tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
        return list(self._tools)

    def call_tool(self, name, arguments=None):
        """调一个工具。返回 MCP 的 result 原样（含 content 数组）。"""
        if not name:
            raise MCPError(f"MCP server「{self.name}」调工具时没给名字")
        return self._send("tools/call",
                          {"name": name, "arguments": dict(arguments or {})})

    def status(self):
        """这个 server 现在什么状态（给连接器 Tab 显示）。

        探活是**真起一次进程**——所以要带超时，
        而且失败必须说清为什么失败，不许返回「不可用」三个字完事。
        """
        if not self.enabled:
            return {"name": self.name, "ok": False, "reason": "已停用",
                    "tools": 0, "source": "external"}
        try:
            tools = self.list_tools()
            info = self.server_info()
            return {"name": self.name, "ok": True, "reason": "",
                    "tools": len(tools),
                    "server_name": info.get("serverInfo", {}).get("name", ""),
                    "protocol": info.get("protocolVersion", ""),
                    "source": "external"}
        except Exception as e:
            self.last_error = str(e)
            return {"name": self.name, "ok": False, "reason": str(e),
                    "tools": 0, "source": "external"}


# ---------------------------------------------------------------- 配置

#: 外部 MCP server 的配置文件。放用户目录（改配置不动代码），
#: 也可以用环境变量 FLEET_MCP_CONFIG 指到别处。
DEFAULT_CONFIG = os.path.join(os.path.expanduser("~"), ".fleet", "mcp.json")


def load_config(path=None):
    """读外部 MCP server 配置。

    格式：
      {"servers": [{"name": "x", "command": "...", "args": [...],
                    "env": {...}, "enabled": true}]}

    配置文件不存在 ⇒ 返回空清单（**不是错误**：本仓默认一个外部 MCP 都没挂，
    这是正常状态，不是「坏了」）。存在但坏了 ⇒ 抛错并说清哪一行坏。
    """
    p = path or os.environ.get("FLEET_MCP_CONFIG") or DEFAULT_CONFIG
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise MCPError(f"MCP 配置文件不是合法 JSON：{p}（{e}）")
    except Exception as e:
        raise MCPError(f"读不了 MCP 配置文件 {p}：{type(e).__name__}: {e}")
    servers = data.get("servers") if isinstance(data, dict) else data
    if servers is None:
        return []
    if not isinstance(servers, list):
        raise MCPError(f"MCP 配置里的 servers 必须是列表：{p}")
    out = []
    for i, s in enumerate(servers):
        if not isinstance(s, dict):
            raise MCPError(f"MCP 配置第 {i + 1} 项不是对象：{p}")
        name = (s.get("name") or "").strip()
        cmd = (s.get("command") or "").strip()
        if not name or not cmd:
            raise MCPError(f"MCP 配置第 {i + 1} 项缺 name 或 command：{p}")
        out.append({
            "name": name,
            "command": cmd,
            "args": list(s.get("args") or []),
            "env": dict(s.get("env") or {}),
            "cwd": s.get("cwd") or "",
            "enabled": bool(s.get("enabled", True)),
            "timeout": float(s.get("timeout", DEFAULT_TIMEOUT)),
        })
    return out


def build_clients(path=None):
    """按配置建出客户端对象（不启动进程）。"""
    return [StdioMCPClient(name=s["name"], command=s["command"], args=s["args"],
                           env=s["env"], cwd=s["cwd"] or None,
                           timeout=s["timeout"], enabled=s["enabled"])
            for s in load_config(path)]
