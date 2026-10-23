"""t6：MCP server 协议验证（用注入式假 registry，不依赖全量 catalog.build）。

为什么用假 registry：本机 embeddable Python 没有 pywin32，跑不动 catalog.build()
（它会 import WindowsPC 等真设备）。但 MCP 协议逻辑与具体设备无关，用桩验证：
  · initialize 握手报正确协议版本
  · tools/list（默认）只暴露只读能力；副作用能力（ai.chat / image.gen）默认不导出
  · tools/list（--unsafe）才包含副作用能力
  · tools/call 走 registry.call，只读能力成功、未导出能力给 isError 且带原因
  · 错误码：未知方法 -32601、缺 name -32602、未知工具带原因
  · notifications/initialized 不回包
重点是证明「image.gen 已进 catalog 真能力表 → tools/list 能列出来（--unsafe 下）」，
以及整体协议健壮（不静默、错误码正确）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet.core.registry import Capability, Param
from fleet.mcp import server as S


class FakeResult:
    def __init__(self, ok, data=None, err="", code="ok"):
        self.ok = ok
        self.data = data or {}
        self.err = err
        self.code = code

    def get(self, k, d=None):
        # 与真实 DeviceResult.get 对齐：「data」返回整段 payload，其余查子字段
        if k == "data":
            return self.data
        return self.data.get(k, d)

    def brief(self):
        return self.err or "ok"


class FakeReg:
    """最小 registry 桩：3 条能力（1 只读 + 2 副作用），call 返回确定性结果。"""

    def __init__(self):
        self._caps = [
            Capability("ai.list", "列出后端", side_effect=False),
            Capability("ai.chat", "对话", side_effect=True, approval="auto"),
            Capability("image.gen", "出图", side_effect=True,
                       params=(Param("prompt", "str", True, "画面描述"),)),
        ]

    def capabilities(self):
        return list(self._caps)

    def call(self, cap, device="", args=None, gate=None):
        if cap == "ai.list":
            return FakeResult(True, {"backends": [{"id": "pollinations"}]})
        if cap == "ai.chat":
            return FakeResult(True, {"text": "hi"})
        if cap == "image.gen":
            return FakeResult(False, err="后端未配置", code="unavailable")
        return FakeResult(False, err="no such", code="unsupported")


FAIL = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [X]  ") + msg)
    if not cond:
        FAIL.append(msg)


def rpc(srv, method, params=None, rid=1):
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        req["params"] = params
    return srv.handle(req)


# ---------------------------------------------------------------- 默认（不含副作用）
print("① tools/list 默认只暴露只读能力")
srv = S.MCPServer(registry=FakeReg())
r = rpc(srv, "initialize", {"protocolVersion": "2025-06-18",
                           "clientInfo": {"name": "test"}})
check(r["result"]["protocolVersion"] == S.PROTOCOL_VERSION,
      "initialize 回正确协议版本 %s" % S.PROTOCOL_VERSION)
tools = rpc(srv, "tools/list")["result"]["tools"]
names = {t["name"] for t in tools}
check("ai_list" in names, "默认含只读 ai_list")
check("ai_chat" not in names, "默认不含副作用 ai_chat")
check("image_gen" not in names, "默认不含副作用 image_gen")

# ---------------------------------------------------------------- --unsafe 含副作用
print("② tools/list --unsafe 含副作用能力（含 image.gen）")
srv_u = S.MCPServer(registry=FakeReg(), include_unsafe=True)
tools_u = rpc(srv_u, "tools/list")["result"]["tools"]
names_u = {t["name"] for t in tools_u}
check("ai_chat" in names_u, "--unsafe 含 ai_chat")
check("image_gen" in names_u, "--unsafe 含 image_gen（已进 catalog 真能力表）")
ig = next(t for t in tools_u if t["name"] == "image_gen")
check(ig["inputSchema"]["required"] == ["prompt"], "image_gen 工具 schema 必填 prompt")
check("本仓能力名 image.gen" in ig["description"], "工具描述带真能力名便于回溯")

# ---------------------------------------------------------------- tools/call
print("③ tools/call 行为")
r = rpc(srv, "tools/call", {"name": "ai_list", "arguments": {}})
check(r["result"].get("isError") is not True, "只读 ai_list 调用成功")
check("pollinations" in r["result"]["content"][0]["text"], "回包含后端数据")

r = rpc(srv, "tools/call", {"name": "image_gen", "arguments": {"prompt": "猫"}})
check(r["result"].get("isError") is True, "默认未导出 image_gen → isError=True")
check("没有导出" in r["result"]["content"][0]["text"], "未导出时明确说「没导出」而非假装成功")

r = rpc(srv_u, "tools/call", {"name": "image_gen", "arguments": {"prompt": "猫"}})
check(r["result"].get("isError") is True, "image_gen 调用失败（后端未配置）仍 isError=True")
check("未配置" in r["result"]["content"][0]["text"], "失败带原因（未配置）不静默")

# ---------------------------------------------------------------- 错误码
print("④ 错误码与边界")
r = rpc(srv, "bogus/method", {})
check(r["error"]["code"] == S.METHOD_NOT_FOUND, "未知方法 → -32601")
r = rpc(srv, "tools/call", {"arguments": {}})
check(r["error"]["code"] == S.INVALID_PARAMS, "缺 name → -32602")
r = rpc(srv, "tools/call", {"name": "nope_x", "arguments": {}})
check(r["result"].get("isError") is True and "没有这个工具" in r["result"]["content"][0]["text"],
      "未知工具带原因而非静默")
r = rpc(srv, "notifications/initialized", {})
check(r is None, "通知不回包")

# ---------------------------------------------------------------- 收尾
print("t6 mcp:", "PASS" if not FAIL else "FAIL (%d 项)" % len(FAIL))
sys.exit(1 if FAIL else 0)
