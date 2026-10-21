"""fleet.mcp —— MCP 协议层。

**设备也是 MCP**：本仓的 9 台设备本质上就是 9 个 MCP server，
只是以前没这么说、也没这么做。这个包把那件事真正做出来：

  schema.py   能力清单 → MCP 工具表（工具名映射 + JSON Schema）
  server.py   注册表 → MCP server（stdio，JSON-RPC 2.0）
  client.py   本仓作为 MCP client，挂载外部 MCP server

为什么是「一切皆 MCP」而不是「设备是一种设备、连接器是另一种」：
  两者在协议层是同一件事——**一个暴露 tool 的 server**。
  区别只是来源：内建（本仓的设备）还是外接（用户挂的第三方 MCP）。
  统一成一张表之后，「连接器」这个 Tab 才不是空壳。
"""

__all__ = ["schema", "server", "client"]
