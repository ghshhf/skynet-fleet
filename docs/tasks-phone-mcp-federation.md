# Tasks：手机接入外部 MCP

依据：`docs/spec-phone-mcp-federation.md`

## T1 协议层 `McpClient.kt`
- [x] JSON-RPC 请求构造（initialize / notifications/initialized / tools/list / tools/call）。
- [x] OkHttp POST，`Mcp-Session-Id` 回传，session 失效重连一次。
- [x] `text/event-stream` 用 `SseParser` 取 id 匹配响应；JSON body 直解。
- [x] 超时（5s/20s）、单服务串行、错误归一成 `McpCallException`。

验证：`McpClientTest` 全绿（Android 单测类路径看不到 `com.sun.net.httpserver`，改用裸 `ServerSocket` 假服务端）。

## T2 配置与联邦 `McpServers.kt`
- [x] `encode/decode/slug/exposedName` 纯函数。
- [x] SharedPreferences 持久化 + `init(ctx)`。
- [x] 服务状态缓存、`cachedTools()` 只读不联网、`refresh()` 联网。
- [x] `call(exposed, args)` → `ToolResult`（文本/图片/isError/截断）。
- [x] 失败隔离与 60 秒退避。

验证：`McpServersTest` 全绿。

## T3 接入工具面
- [x] `McpCore` 改 `tools: () -> List<McpTool>`。
- [x] `PhoneMcp.toolList()` 合并外部工具；`core`/`lanCore` 用 provider。
- [x] `executeTool` 增加 `mcp__` 分派（含 ActionLog 审计）。
- [x] 新增 `self_mcp_servers` / `self_mcp_add` / `self_mcp_remove` / `self_mcp_refresh`。
- [x] `ToolScope` 新增 EXT 域；`MainActivity` 调 `McpServers.init`。

验证：`McpCoreTest`、`ToolScopeTest` 全绿。

## T4 收尾
- [x] `gradlew.bat :app:testDebugUnitTest --tests "app.skynet.tianwang.next.*"`（BUILD SUCCESSFUL）。
- [x] `git diff --check`；`python tools\check_catalog.py`（PASS）。
- [ ] 单条 commit：`feat(phone): 接入外部 MCP 服务器，工具面从封闭变可扩展`。
- [ ] 汇报：改了什么、证据、commit、剩余风险与下一步（本地 MCP 进程 / 插件运行时 / 按调用批准）。
