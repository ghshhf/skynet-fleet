package app.skynet.tianwang.next

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject
import java.net.MalformedURLException
import java.net.URL

/**
 * 外部 MCP 服务器的配置、缓存与联邦目录。
 *
 * 关键约束：`cachedTools()` **只读缓存、绝不联网**。手机自己的 `tools/list`
 * 不能被某个外部服务拖住 —— 外部服务挂了，本机 60 个工具必须照常可用。
 *
 * 命名用 `mcp__<server>__<tool>`：MCP 规范不允许工具名带冒号，
 * 所以不能照搬 Operit2 的 `server:tool`，但保留同样的「服务 + 原名」映射，
 * 调用时用原始工具名，绝不靠反解字符串。
 */
object McpServers {
    private const val SP = "tianwang_mcp"
    private const val KEY = "servers"
    private const val MAX_TOOL_NAME = 64
    private const val FAIL_BACKOFF_MS = 60_000L

    data class Server(
        val name: String,
        val url: String,
        val token: String = "",
        val enabled: Boolean = true,
    )

    data class Status(
        val server: Server,
        val tools: List<RemoteTool> = emptyList(),
        val fetchedAt: Long = 0,
        val error: String = "",
        val failedAt: Long = 0,
    )

    private lateinit var sp: SharedPreferences
    private val lock = Any()
    private val statuses = LinkedHashMap<String, Status>()
    private val clients = HashMap<String, McpClient>()

    fun init(ctx: Context) {
        sp = ctx.applicationContext.getSharedPreferences(SP, Context.MODE_PRIVATE)
        synchronized(lock) { syncLocked(decode(sp.getString(KEY, "[]").orEmpty())) }
    }

    /** 当前配置（不含缓存与错误）。 */
    fun servers(): List<Server> = synchronized(lock) { statuses.values.map { it.server } }

    fun add(name: String, url: String, token: String = ""): ToolResult {
        if (!::sp.isInitialized) return ToolResult(false, err = "MCP 配置未初始化")
        val cleanName = name.trim()
        val cleanUrl = url.trim().trimEnd('/')
        if (cleanName.isBlank()) return ToolResult(false, err = "缺 name")
        if (!validUrl(cleanUrl)) return ToolResult(false, err = "url 必须是 http(s):// 且带主机名")
        synchronized(lock) {
            if (statuses.containsKey(cleanName)) {
                return ToolResult(false, err = "同名服务已存在：$cleanName")
            }
            writeLocked(statuses.values.map { it.server } + Server(cleanName, cleanUrl, token.trim()))
        }
        return ToolResult(true, text = refreshOne(cleanName))
    }

    fun remove(name: String): ToolResult {
        if (!::sp.isInitialized) return ToolResult(false, err = "MCP 配置未初始化")
        synchronized(lock) {
            if (!statuses.containsKey(name)) return ToolResult(false, err = "没有这个服务：$name")
            writeLocked(statuses.values.map { it.server }.filterNot { it.name == name })
            clients.remove(name)
        }
        return ToolResult(true, text = "已移除 $name")
    }

    /** 刷新全部（name 为空）或单个服务，返回可读结果。 */
    fun refresh(name: String?): String {
        val targets = synchronized(lock) {
            statuses.values.map { it.server }.filter { it.enabled && (name.isNullOrBlank() || it.name == name) }
        }
        if (targets.isEmpty()) return if (name.isNullOrBlank()) "还没有启用的外部 MCP" else "没有这个服务：$name"
        return targets.joinToString("\n") { refreshOne(it.name) }
    }

    /** 联邦目录：只读缓存，供 `tools/list` 与本地模型使用。 */
    fun cachedTools(): List<McpTool> = synchronized(lock) {
        if (!::sp.isInitialized) return emptyList()
        indexLocked().map { (exposed, pair) ->
            val (server, tool) = pair
            McpTool(
                name = exposed,
                description = "[外部MCP:${server.name}] ${tool.description}".trim(),
                inputSchema = tool.inputSchema,
                sideEffect = true,
            )
        }
    }

    /** 调一个联邦工具。 */
    fun call(exposed: String, args: JSONObject): ToolResult {
        val target = synchronized(lock) { indexLocked()[exposed] }
            ?: return ToolResult(false, err = "未知外部工具：$exposed（先 self_mcp_refresh）")
        val (server, tool) = target
        val serverName = server.name
        val st = synchronized(lock) { statuses[serverName] }
            ?: return ToolResult(false, err = "服务不存在：$serverName")
        if (!st.server.enabled) return ToolResult(false, err = "服务已停用：$serverName")
        val ago = System.currentTimeMillis() - st.failedAt
        if (st.error.isNotBlank() && ago in 0 until FAIL_BACKOFF_MS) {
            return ToolResult(false, err = "服务 $serverName 刚失败过，60 秒内不自动重试：${st.error}")
        }
        val client = synchronized(lock) {
            clients.getOrPut(serverName) { McpClient(st.server.url, st.server.token) }
        }
        return try {
            client.callTool(tool.name, args)
        } catch (t: Throwable) {
            val msg = t.message ?: t.javaClass.simpleName
            synchronized(lock) {
                statuses[serverName] = st.copy(error = msg, failedAt = System.currentTimeMillis())
            }
            ToolResult(false, err = "外部 MCP 调用失败：$msg")
        }
    }

    /** `self_mcp_servers` 的可读输出。 */
    fun describe(): String = synchronized(lock) {
        if (statuses.isEmpty()) return "还没有配置外部 MCP。用 self_mcp_add 添加，例如 name=pc、url=http://192.168.1.10:8765/mcp"
        buildString {
            append("外部 MCP（${statuses.size} 个）：\n")
            statuses.values.forEach { st ->
                append("  [").append(if (st.server.enabled) "启用" else "停用").append("] ")
                    .append(st.server.name).append(" · ").append(st.server.url)
                    .append(" · 工具 ").append(st.tools.size)
                    .append(if (st.server.token.isNotBlank()) " · 凭据已配置" else " · 无凭据")
                if (st.error.isNotBlank()) append(" · 最近错误：").append(st.error)
                append('\n')
            }
        }
    }

    // ---- 内部 ----

    private fun refreshOne(name: String): String {
        val server = synchronized(lock) { statuses[name]?.server } ?: return "没有这个服务：$name"
        if (!server.enabled) return "$name 已停用"
        val client = synchronized(lock) { clients.getOrPut(name) { McpClient(server.url, server.token) } }
        return try {
            val tools = client.listTools()
            synchronized(lock) {
                statuses[name] = Status(server, tools, System.currentTimeMillis(), "", 0)
            }
            "$name：${tools.size} 个工具"
        } catch (t: Throwable) {
            val msg = t.message ?: t.javaClass.simpleName
            synchronized(lock) {
                statuses[name] = Status(server, emptyList(), 0, msg, System.currentTimeMillis())
            }
            "$name：刷新失败 —— $msg"
        }
    }

    /** 必须持有 [lock]。 */
    private fun indexLocked(): LinkedHashMap<String, Pair<Server, RemoteTool>> {
        val out = LinkedHashMap<String, Pair<Server, RemoteTool>>()
        for (st in statuses.values) {
            if (!st.server.enabled) continue
            for (tool in st.tools) {
                val name = uniqueName(exposedName(st.server.name, tool.name), out.keys)
                out[name] = st.server to tool
            }
        }
        return out
    }

    /** 必须持有 [lock]。 */
    private fun syncLocked(list: List<Server>) {
        val keep = list.associateBy { it.name }
        val it = statuses.keys.iterator()
        while (it.hasNext()) {
            if (!keep.containsKey(it.next())) it.remove()
        }
        for (s in list) {
            val old = statuses[s.name]
            if (old == null) {
                statuses[s.name] = Status(s)
            } else if (old.server != s) {
                statuses[s.name] = Status(s)
                clients.remove(s.name)
            }
        }
    }

    /** 必须持有 [lock]。 */
    private fun writeLocked(list: List<Server>) {
        sp.edit().putString(KEY, encode(list)).apply()
        syncLocked(list)
    }

    // ---- 纯函数（JVM 单测直接覆盖） ----

    fun encode(list: List<Server>): String {
        val arr = JSONArray()
        list.forEach { s ->
            arr.put(JSONObject()
                .put("name", s.name)
                .put("url", s.url)
                .put("token", s.token)
                .put("enabled", s.enabled))
        }
        return arr.toString()
    }

    fun decode(text: String): List<Server> {
        val arr = try {
            JSONArray(text.ifBlank { "[]" })
        } catch (t: Throwable) {
            return emptyList()
        }
        val out = ArrayList<Server>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val name = o.optString("name").trim()
            val url = o.optString("url").trim().trimEnd('/')
            if (name.isBlank() || !validUrl(url)) continue
            out.add(Server(name, url, o.optString("token"), o.optBoolean("enabled", true)))
        }
        return out
    }

    fun validUrl(url: String): Boolean {
        if (!url.startsWith("http://") && !url.startsWith("https://")) return false
        return try {
            URL(url).host?.isNotBlank() == true
        } catch (e: MalformedURLException) {
            false
        }
    }

    /** 只保留 ASCII 字母数字、下划线、连字符；空串折成 `x`。 */
    fun slug(s: String): String = buildString {
        s.lowercase().forEach { c ->
            append(
                if (c in 'a'..'z' || c in '0'..'9' || c == '_' || c == '-') c else '_'
            )
        }
    }.trim('_').ifBlank { "x" }

    /** `mcp__<server>__<tool>`，总长不超过 64。 */
    fun exposedName(server: String, tool: String): String {
        val prefix = "mcp__${slug(server).take(24)}__"
        val room = MAX_TOOL_NAME - prefix.length
        return prefix + slug(tool).take(room.coerceAtLeast(1))
    }

    /** 同名前缀撞车时追加 `__2`、`__3`，并保持总长不超过 64。 */
    internal fun uniqueName(base: String, taken: Set<String>): String {
        if (base !in taken) return base
        var n = 2
        while (true) {
            val candidate = withSuffix(base, n)
            if (candidate !in taken) return candidate
            n++
        }
    }

    private fun withSuffix(base: String, n: Int): String {
        val suffix = "__$n"
        return base.take(MAX_TOOL_NAME - suffix.length) + suffix
    }
}
