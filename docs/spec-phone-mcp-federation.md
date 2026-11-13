# Spec：手机接入外部 MCP（工具面从封闭变可扩展）

状态：待批准。只改 `tianwang_app`（第二代 Android），不动网络层、`ApprovalBroker`、`MemGuard`、`persona`。

## 1. 为什么是这条（以 Operit2 代码为准）

### 1.1 Operit2 的真实骨架

浅克隆于 `e:\xmanbian\2026-11-12\operit-ref`（`git clone --depth 1`）。逐文件核对后，关键事实：

- **全仓没有 `AccessibilityService` / `AccessibilityNodeInfo`**，搜 `AccessibilityService` 零命中。它不靠无障碍点屏。
- 形态是 Flutter UI + Rust Core（`core/crates`）+ 平台 Host（`hosts/android`）+ `apps/android-module`（root/KernelSU 守护）。
- 界面之外的系统能力走 Host：`hosts/android/src/system_operation.rs` 只有 `startApp` / `getAppUsageTime` 等系统 API，重活走 shell/root。
- **扩展面是它的核心**：
  - MCP 客户端：`core/crates/tool/services/src/tools/mcp/MCPManager.rs`、`MCPServerConfig.rs`、`MCPToolExecutor.rs`；
  - 远程传输：`mcp_runtime/plugins/MCPStreamableHttp.rs`（POST + 增量响应 + 超时/取消）；
  - 本地服务：`mcp_runtime/MCPLocalServer.rs`、`MCPStarter.rs`；
  - 工具命名 `serverName:toolName`（`MCPToolExecutor.rs:338-350`），调用前要求服务已激活。
- 权限四层：App 沙箱 → Host 授权 → `AiPermissionMode`（ReadOnly/WorkspaceWrite/Full）→ 用户按调用批准（对应 Operit2 参考仓库里的 `permission-access-architecture.md`，本仓未收录）。

结论：Operit2 的差异不是「无障碍做得更好」，而是 **工具面是开放的、可扩展的**。天网手机端目前恰好相反：60 多个工具写死在一个 Kotlin 列表里，只能被外部调用，不能自己长出能力。这条差距比再补几个点屏动作更接近「我想要的」。

### 1.2 天网手机端现状（代码）

- `PhoneMcp.kt:15` `private val tools: List<McpTool>`：静态 60+ 工具；`PhoneMcp.kt:501/515` 两个 `McpCore` 在对象初始化时就绑死这份列表。
- `PhoneMcp.kt:526` `fun toolList(): List<McpTool> = tools`；`LanHub.kt:277/310` 用它查 peer 工具元数据。
- `McpCore.kt:34` `tools: List<McpTool>`，`tools/list` 与 `tools/call` 都从同一份静态列表查。
- 无 MCP 客户端：只有 `PhoneMcpLink`（把手机作为 MCP Server 往外连）与 `PhoneMcp.handleLanLine`（LAN 直连）。
- 已有可复用件：OkHttp 4.12（`app/build.gradle.kts`）、`SseParser`（`Sse.kt`）、`Prefs`、`Net` 的初始化方式。

### 1.3 差距表

| 维度 | Operit2 | 天网手机端 | 本轮 |
| --- | --- | --- | --- |
| MCP 客户端 | 有（远程 Streamable HTTP + 本地服务） | 无 | **做远程 HTTP** |
| 工具来源 | 静态 + MCP + 插件动态注册 | 纯静态 | **动态合并外部 MCP** |
| 工具命名 | `server:tool` | 下划线扁平名 | `mcp__<server>__<tool>`（MCP 名不允许冒号） |
| 权限 | 四层 + 按调用批准 | 单一「允许操作」 | 复用现有开关，外部工具一律 `sideEffect=true` |
| 本地 MCP 进程 | 有 | 无 | 后续 |
| 插件/ToolPkg | 有 | 无 | 后续 |

## 2. 设计

### 2.1 传输与协议（`McpClient.kt`，新文件）

只做 **Streamable HTTP**，不做 stdio/本地进程（Android 上起 node/python 另立项）。

- `POST <url>`，头：`Content-Type: application/json`、`Accept: application/json, text/event-stream`、可选 `Authorization: Bearer <token>`。
- 序列：`initialize` → 读响应头 `Mcp-Session-Id` → `notifications/initialized` → `tools/list`（跟随 `nextCursor`，最多 5 页）→ `tools/call`。
- 后续请求带 `Mcp-Session-Id`；遇到 400/404 且 body 提示 session 失效时，重新 `initialize` 一次再重试。
- 响应解析：
  - `Content-Type` 含 `text/event-stream` 时逐行喂 `SseParser`，取 id 匹配的 JSON-RPC 响应后结束；
  - 否则直接解析 body JSON。
- 超时：连接 5s、读 20s；单服务调用串行（`Mutex`），不并发轰对端。
- 错误统一成 `McpCallException(message)`：非 2xx 带 body 前 300 字；JSON-RPC `error.message` 原样返回；超时明确写「超时」。
- 纯协议部分（请求构造、响应解析、SSE 取帧）与 OkHttp 分开，JVM 可测。

### 2.2 配置、缓存与联邦（`McpServers.kt`，新文件）

- 存储：`SharedPreferences("tianwang_mcp")` 一个 key `servers`，JSON 数组：
  ```json
  [{"name":"pc","url":"http://192.168.1.10:8765/mcp","token":"","enabled":true}]
  ```
- `init(ctx)` 与 `Net.init` 同处调用。
- 纯函数（可单测）：`encode(list)` / `decode(text)` / `slug(s)` / `exposedName(server, tool)`。
- 命名：`mcp__<slug(server)>__<slug(tool)>`，总长 ≤64；冲突追加 `__2`、`__3`。内存索引 `exposed -> (server, originalTool)`，调用时用原名，绝不靠反解字符串。
- 缓存：每个服务一份 `tools` + `fetchedAt`；`cachedTools()` **只读缓存、绝不联网**，保证 `tools/list` 不被外部服务拖住。
- 刷新：`refresh(name)` 才联网；`self_mcp_add` 成功后自动刷新该服务；缓存超过 10 分钟时由 `self_mcp_refresh` 刷新，不在协议路径上做。
- 失败隔离：某服务连不上 → 该服务贡献 0 个工具，错误记在状态里；本地 60 个工具不受影响。
- 调用映射：`call(exposed, args)` → 找到服务与原工具名 → `tools/call` → 把 MCP `content` 数组转成 `ToolResult`：
  - 文本项拼接；
  - 图片项取第一张写 `imageBase64`/`mimeType`；
  - `isError=true` → `ToolResult(false, err=文本)`；
  - 结果文本超 4000 字截断并注明。

### 2.3 接入手机工具面

1. `McpCore.kt`
   - `tools: List<McpTool>` → `tools: () -> List<McpTool>`；`tools/list` 与 `invoke` 每次现取。
2. `PhoneMcp.kt`
   - `fun toolList(): List<McpTool> = tools + McpServers.cachedTools()`。
   - `core` / `lanCore` 传 `tools = { toolList() }`。
   - `executeTool` 开头：`if (mcpName.startsWith("mcp__")) return McpServers.call(mcpName, args)`。
   - 审计保持不变（外部工具 `sideEffect=true`，会进 `ActionLog`）。
3. 新增 4 个管理工具（写进静态 `tools` 列表，`self_mcp_servers` 只读，其余 `sideEffect=true`）：
   - `self_mcp_servers`：列出名称/URL/启用/工具数/最近错误；
   - `self_mcp_add`：`name`、`url`、`token`(可选) → 保存并立刻刷新；
   - `self_mcp_remove`：`name`；
   - `self_mcp_refresh`：`name`(可选，空=全部) → 返回各服务工具数。
4. `ToolScope.kt`
   - 新增 `EXT("外部工具", words=["mcp","外部工具","插件","扩展"], prefixes=["mcp__"], core=[])`，让本地模型说「用 MCP 查一下」时能拿到外部工具；`domainLabel` 测试同步补一条。
5. `LocalAgent` / `LanHub` 不改逻辑：它们都通过 `PhoneMcp.toolList()` 现取，自动获得外部工具；只读模式下外部工具因 `sideEffect=true` 被现有闸门挡住。

### 2.4 安全边界

- 外部工具一律 `sideEffect=true`：必须打开「允许操作」才可调，与现有权限口径一致。
- URL 只接受 `http://` / `https://`，拒绝空 host；`token` 只用于 `Authorization` 头，**不写日志、不回显**（`self_mcp_servers` 只显示「已配置/未配置」）。
- 存储为明文 SharedPreferences（与 `PairToken` 现状一致）；不新增加密依赖。
- 不做自动重试风暴：每个调用最多重连一次；服务失败后 60 秒内不再自动尝试。

## 3. 文件与改动清单

| 文件 | 动作 |
| --- | --- |
| `app/src/main/java/.../McpClient.kt` | 新增：JSON-RPC + OkHttp + SSE + 超时 |
| `app/src/main/java/.../McpServers.kt` | 新增：配置/命名/缓存/联邦调用 |
| `app/src/main/java/.../McpCore.kt` | `tools` 改为 provider |
| `app/src/main/java/.../PhoneMcp.kt` | 动态 `toolList()`、`mcp__` 分派、4 个管理工具 |
| `app/src/main/java/.../ToolScope.kt` | 新增 EXT 域 |
| `app/src/main/java/.../MainActivity.kt` | `McpServers.init(this)`（与 `Net.init` 同处） |
| `app/src/test/.../McpClientTest.kt` | 新增：JDK `HttpServer` 假服务端 |
| `app/src/test/.../McpServersTest.kt` | 新增：编码/命名/冲突/截断 |
| `app/src/test/.../McpCoreTest.kt` | 适配 provider；加「动态出现」用例 |
| `app/src/test/.../ToolScopeTest.kt` | 加 EXT 域用例 |

## 4. 验收标准

1. `self_mcp_add` 一个可达服务后，`self_mcp_servers` 能看到工具数；`tools/list` 出现 `mcp__<server>__*`。
2. 调 `mcp__<server>__<tool>` 能拿到文本；图片类结果回 `image` content。
3. 只读模式（未开「允许操作」）下外部工具被显式拒绝，错误说明与现有写操作一致。
4. 服务不可达时：`self_mcp_servers` 报最近错误；本地工具与 `tools/list` 不受影响、不卡顿。
5. `Mcp-Session-Id` 被回传；SSE 响应能解析；20s 超时可控。
6. 单测全绿；`git diff --check` 干净。

## 5. 验证命令

```powershell
# tianwang_app 目录
.\gradlew.bat :app:testDebugUnitTest --tests "app.skynet.tianwang.next.*"

# 仓库根
git diff --check
python tools\check_catalog.py
```

不跑全量 `run_selftests.py`（用户明确要求）。

## 6. 提交

`feat(phone): 接入外部 MCP 服务器，工具面从封闭变可扩展`

## 7. 明确不做

- 不做本地 MCP 进程（stdio/node/python）、不做插件/ToolPkg、不做按调用批准、不做管理界面（本轮用 `self_mcp_*` 工具管理）。
- 不改 LAN「口令即授权」、不改无障碍执行逻辑、不动网络层。
