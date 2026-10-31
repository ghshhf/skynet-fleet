"""MCP server（stdio）：把注册表变成一个 MCP server。

为什么要有它：
  「设备也是 MCP」——本仓的 9 台设备本质上就是 9 个 MCP server，
  只是我们以前没这么说、也没这么做。于是外部的 AI 客户端
  （Claude Desktop / Cursor / 别的机器上的 agent）**没法用本仓的能力**——
  本仓明明有 70 项能力、9 台设备，却只在自己家的界面里能用。

  这个模块把注册表按 MCP 规范（2025-06-18）暴露出去：
    initialize   握手，报协议版本与能力
    tools/list   列工具（工具名 = 能力名映射，带 JSON Schema）
    tools/call   调工具（走注册表 ⇒ 走闸门）

三条铁律：

1. **动手能力在 MCP 路径上照样过闸门**（本仓红线 3）。
   默认导出的工具里**没有**有副作用的能力——stdio 进程没有 GUI 弹窗可等，
   等不到人就是没人授权（fail-closed）。
   即使显式开了 include_unsafe，`registry.call` 在没有 gate 时
   也会按 fail-closed 拒绝（注册表自己就写了这条）。

2. **零依赖**。JSON-RPC 2.0 over stdio 就是「一行一个 JSON 对象」，
   不需要 SDK。本仓铁律「纯 CPU 文本处理一律本机做、零第三方依赖」。

3. **不静默**。未知方法回 -32601、未知工具回 -32602、
   工具执行失败回 `isError: true` 并带原因。
   绝不用「空结果」假装成功——那是最阴的失败形态。
"""
import json
import sys

from ..catalog import build
from .schema import ToolIndex, tool_table


PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "device-fleet"
SERVER_VERSION = "0.1"

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class _ParamError(Exception):
    """参数不合法（缺 name / arguments 不是对象）。→ JSON-RPC -32602。"""


# ---------------------------------------------------------------- 结果信封

def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message, data=None):
    e = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": e}


def _text_result(text, is_error=False):
    """MCP 的工具结果：内容是数组，不是裸值。"""
    return {"content": [{"type": "text", "text": text if isinstance(text, str) else str(text)}],
            "isError": bool(is_error)}


def _result_from_device(res):
    """DeviceResult → MCP 工具结果。

    失败一律 `isError: true` + 原因文本。
    成功时把 data 序列化；不可序列化的就退成 str()——
    **但不许丢**：`brief()` 那行摘要一定带上。
    """
    if res.ok:
        payload = res.get("data")
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            body = str(payload)
        return _text_result(body if body else res.brief())
    return _text_result(f"{res.code}: {res.err}", is_error=True)


# ---------------------------------------------------------------- server

class MCPServer:
    """注册表 → MCP server。

    `include_unsafe=True` 时才导出有副作用的能力。默认 False。
    `gate` 是审批闸门（跟 registry.call 用的是同一个约定）：
      · 给了 ⇒ 动手能力真的能过闸（GUI 场景可以复用审批中枢）
      · 没给 ⇒ registry 自己 fail-closed 拒绝
    """

    def __init__(self, registry=None, include_unsafe=False, gate=None):
        self.registry = registry if registry is not None else build()
        self.include_unsafe = bool(include_unsafe)
        self.gate = gate
        self.index = ToolIndex(list(self.registry.capabilities()))

    # ---------------- 工具表 ----------------

    def tools(self):
        """tools/list 的 result。"""
        return {"tools": tool_table(self.registry.capabilities(),
                                    include_unsafe=self.include_unsafe)}

    def _tool_names(self):
        return set(t["name"] for t in self.tools()["tools"])

    # ---------------- 派发 ----------------

    def handle(self, req):
        """一个 JSON-RPC 请求 → 一个 JSON-RPC 响应（None 表示不需要回）。

        通知（没有 id）不回复——这是 JSON-RPC 的规矩，
        回一句反而会让客户端的 id 对不上。
        """
        # 批请求（数组）：逐条处理，全是通知就整体不回（JSON-RPC 的规矩）。
        # 放在这里而不是 HTTP 层——stdio 也可能收到数组，规则必须只有一份。
        if isinstance(req, list):
            out = [self.handle(x) for x in req]
            out = [r for r in out if r is not None]
            return out or None

        if not isinstance(req, dict):
            return _err(None, INVALID_REQUEST, "请求不是 JSON 对象")

        req_id = req.get("id")
        method = req.get("method")
        is_notification = "id" not in req

        if req.get("jsonrpc") != "2.0":
            return _err(req_id, INVALID_REQUEST,
                        f"jsonrpc 必须是 '2.0'，实际是 {req.get('jsonrpc')!r}")
        if not method:
            return _err(req_id, INVALID_REQUEST, "请求里没有 method")

        params = req.get("params") or {}
        if not isinstance(params, dict):
            return _err(req_id, INVALID_PARAMS, "params 必须是对象")

        try:
            if method == "initialize":
                return _ok(req_id, self._initialize(params))
            if method == "tools/list":
                return _ok(req_id, self.tools())
            if method == "tools/call":
                try:
                    return _ok(req_id, self._call_tool(params))
                except _ParamError as e:
                    return _err(req_id, INVALID_PARAMS, str(e))
            if method == "notifications/initialized":
                return None                    # 通知，不回
            if method == "ping":
                return _ok(req_id, {})
        except Exception as e:                  # 兜住一切，但要说清是什么错
            return _err(req_id, INTERNAL_ERROR, f"{type(e).__name__}: {e}")

        if is_notification:
            return None
        return _err(req_id, METHOD_NOT_FOUND, f"没有这个方法: {method}")

    def _initialize(self, params):
        client = params.get("clientInfo") or {}
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            # 非规范字段：把自己的身份说清楚，客户端爱不爱看随它。
            "instructions": (
                "设备编队的 MCP server。工具名 = 本仓能力名（'.' 写成 '_'，"
                "如 pc_state 即 pc.state）。只读能力可直接调用；"
                "有副作用的能力默认不导出。"
            ),
            "_client": client.get("name", ""),
        }

    def _call_tool(self, params):
        name = params.get("name")
        if not name:
            raise _ParamError("tools/call 缺 name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _ParamError("arguments 必须是对象")

        cap = self.index.resolve(name)
        # 🔴 先判「导出与否」，再判「有没有这个能力」——
        # 顺序反了会给出误导性文案（早先版本就是这样：索引建自全部能力，
        # 所以动手工具永远能被 resolve 到，用户看到的是「需要审批闸门」，
        # 而不是「这个工具根本没导出给你」——两者的处置完全不同）。
        if cap is not None and name not in self._tool_names():
            return _text_result(
                f"工具 {name} 存在但没有导出：{cap.name} 是有副作用的能力"
                f"（approval={cap.approval}, side_effect={cap.side_effect}，"
                f"destructive={cap.destructive}），默认不暴露给 MCP 客户端",
                is_error=True)
        if cap is None:
            return _text_result(f"没有这个工具: {name}", is_error=True)

        device = args.pop("__device__", "") or ""
        res = self.registry.call(cap.name, device=device, args=args, gate=self.gate)
        return _result_from_device(res)

    # ---------------- stdio 循环 ----------------

    def serve_stdio(self, stdin=None, stdout=None):
        """按行读 JSON-RPC，按行写 JSON-RPC。

        一行一个 JSON 对象（这是 stdio 传输的规定，不是我们图省事）。
        读到空行跳过；读到**不是 JSON 的行**回 -32700 并继续
        （不退出——一条脏数据不该让整个 server 死掉）。
        """
        sin = stdin if stdin is not None else sys.stdin
        sout = stdout if stdout is not None else sys.stdout
        # 🔴 stdio 传输的规约就是 UTF-8（MCP 官方定义）。中文 Windows 的
        # stdout 默认 GBK：tools/list 的描述里一个 `⇒` 就能把 server 写崩
        # （2026-11-01 实测，UnicodeEncodeError at 21551 字符）。这里强制
        # 重配两条流——宁可字符糊掉，stdio server 不许因编码死。
        for s in (sin, sout):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        for line in sin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                resp = _err(None, PARSE_ERROR, f"这一行不是合法 JSON: {e}")
            else:
                resp = self.handle(req)
            if resp is None:
                continue
            sout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sout.flush()


# ---------------------------------------------------------------- HTTP 复用

def handle_json(req, registry=None, include_unsafe=False, gate=None):
    """一个（或一批）JSON-RPC 请求 → 响应对象。给 HTTP 传输复用。

    为什么单独抽出来：stdio 是「一行一个 JSON」，HTTP 是「一个 body 一个 JSON」，
    但**协议处理完全是同一套**——抽出来是为了让两条传输共享同一份派发逻辑，
    免得以后改了 stdio 忘了改 HTTP（那是必然会发生的事）。

    批请求（数组）按 JSON-RPC 的规矩：逐条处理，**全是不需要回的通知时回 None**
    （等价于什么都不回）。
    """
    srv = MCPServer(registry=registry, include_unsafe=include_unsafe, gate=gate)
    return srv.handle(req)


# ---------------------------------------------------------------- CLI

def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m fleet.mcp.server",
        description="设备编队 · MCP server（stdio）")
    ap.add_argument("--unsafe", action="store_true",
                    help="连有副作用的能力也导出（危险：没有 GUI 弹窗可等审批）")
    ap.add_argument("--list", action="store_true",
                    help="只打印工具表然后退出（不启 stdio 循环）")
    args = ap.parse_args(argv)

    srv = MCPServer(include_unsafe=args.unsafe)
    if args.list:
        tools = srv.tools()["tools"]
        print(json.dumps({"count": len(tools), "tools": tools},
                         ensure_ascii=False, indent=2))
        return 0
    srv.serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
