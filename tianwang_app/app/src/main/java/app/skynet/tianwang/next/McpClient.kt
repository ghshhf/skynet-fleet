package app.skynet.tianwang.next

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** 外部 MCP 调用失败的统一异常。消息直接可展示给模型/用户。 */
class McpCallException(message: String) : Exception(message)

/** 外部 MCP 服务器上的一个工具（协议层的原始形态，未加本地前缀）。 */
data class RemoteTool(
    val name: String,
    val description: String,
    val inputSchema: JSONObject,
)

/**
 * Streamable HTTP 版 MCP 客户端。
 *
 * 只依赖 OkHttp + org.json，不碰 Android，JVM 单测里可以对着
 * `com.sun.net.httpserver.HttpServer` 的假服务端跑完整握手。
 *
 * 设计取舍：
 * - 一个实例对应一个服务器，调用串行（[lock]），不并发轰对端；
 * - `tools/list` 只做协议，不缓存；缓存由 [McpServers] 负责；
 * - SSE 响应按行读，拿到 id 匹配的 JSON-RPC 响应就停，不等服务端关流。
 */
class McpClient(
    private val url: String,
    private val token: String = "",
    private val client: OkHttpClient = defaultClient(),
) {
    companion object {
        const val PROTOCOL_VERSION = "2025-06-18"
        const val CLIENT_NAME = "tianwang-phone"

        private val JSON = "application/json; charset=utf-8".toMediaType()

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(20, TimeUnit.SECONDS)
            .callTimeout(25, TimeUnit.SECONDS)
            .build()
    }

    private val lock = Any()
    private var nextId = 0
    private var sessionId = ""
    private var initialized = false

    /** 握过一次手并拿到工具列表。 */
    fun listTools(): List<RemoteTool> = synchronized(lock) {
        ensureInitialized()
        val out = ArrayList<RemoteTool>()
        var cursor = ""
        var pages = 0
        do {
            val params = JSONObject()
            if (cursor.isNotBlank()) params.put("cursor", cursor)
            val result = rpc("tools/list", params)
                ?: throw McpCallException("tools/list 没有 result")
            val arr = result.optJSONArray("tools") ?: JSONArray()
            for (i in 0 until arr.length()) {
                val t = arr.optJSONObject(i) ?: continue
                val name = t.optString("name")
                if (name.isBlank()) continue
                out.add(
                    RemoteTool(
                        name = name,
                        description = t.optString("description"),
                        inputSchema = t.optJSONObject("inputSchema")
                            ?: JSONObject().put("type", "object"),
                    )
                )
            }
            cursor = result.optString("nextCursor")
            pages++
        } while (cursor.isNotBlank() && pages < 5)
        out
    }

    /** 调一个外部工具。 */
    fun callTool(name: String, args: JSONObject): ToolResult = synchronized(lock) {
        ensureInitialized()
        val result = rpc(
            "tools/call",
            JSONObject().put("name", name).put("arguments", args),
        ) ?: throw McpCallException("tools/call 没有 result")
        toToolResult(result)
    }

    private fun ensureInitialized() {
        if (initialized) return
        val id = ++nextId
        val body = JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", id)
            .put("method", "initialize")
            .put("params", JSONObject()
                .put("protocolVersion", PROTOCOL_VERSION)
                .put("capabilities", JSONObject())
                .put("clientInfo", JSONObject()
                    .put("name", CLIENT_NAME)
                    .put("version", "1")))
        // 这里不能再走带重连的 rpc()：initialize 本身被拒就没有「重连」可言。
        val response = exchange(body, id)
            ?: throw McpCallException("initialize 被服务端拒绝（HTTP 400/404）")
        response.optJSONObject("error")?.let {
            throw McpCallException(it.optString("message").ifBlank { "initialize 失败" })
        }
        // 初始化后的通知失败不应把整条通道判死，但要让对端知道握手完成。
        runCatching {
            send(JSONObject()
                .put("jsonrpc", "2.0")
                .put("method", "notifications/initialized"))
        }
        initialized = true
    }

    /** 发一条 JSON-RPC 请求并取 result；错误统一抛 [McpCallException]。 */
    private fun rpc(method: String, params: JSONObject): JSONObject? {
        val id = ++nextId
        val body = JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", id)
            .put("method", method)
            .put("params", params)
        var response = exchange(body, id)
        // session 过期：服务端常回 400/404，重来一次握手再试。
        if (response == null) {
            resetSession()
            ensureInitialized()
            val retryId = ++nextId
            response = exchange(
                JSONObject()
                    .put("jsonrpc", "2.0")
                    .put("id", retryId)
                    .put("method", method)
                    .put("params", params),
                retryId,
            ) ?: throw McpCallException("$method 在重连后仍无响应")
        }
        response.optJSONObject("error")?.let {
            throw McpCallException(it.optString("message").ifBlank { "MCP 返回错误" })
        }
        return response.optJSONObject("result")
    }

    private fun resetSession() {
        sessionId = ""
        initialized = false
    }

    /**
     * 一次 POST。返回 null 表示「这个 session 已失效，值得重连一次」。
     * 其余失败（超时/解析/HTTP 错误）直接抛。
     */
    private fun exchange(body: JSONObject, expectId: Int): JSONObject? {
        val req = Request.Builder()
            .url(url)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json, text/event-stream")
            .apply { if (token.isNotBlank()) header("Authorization", "Bearer $token") }
            .apply { if (sessionId.isNotBlank()) header("Mcp-Session-Id", sessionId) }
            .post(body.toString().toRequestBody(JSON))
            .build()
        try {
            client.newCall(req).execute().use { resp ->
                resp.header("Mcp-Session-Id")?.takeIf { it.isNotBlank() }?.let { sessionId = it }
                if (resp.code == 400 || resp.code == 404) {
                    resp.body?.string()
                    return null
                }
                if (!resp.isSuccessful) {
                    val snippet = resp.body?.string().orEmpty().take(300)
                    throw McpCallException("HTTP ${resp.code}：$snippet")
                }
                val type = resp.header("Content-Type").orEmpty()
                if (type.contains("text/event-stream", ignoreCase = true)) {
                    return readSse(resp.body!!.source(), expectId)
                }
                val text = resp.body?.string().orEmpty()
                return parseResponse(text, expectId)
                    ?: throw McpCallException("MCP 回包 id 不匹配（期望 $expectId）")
            }
        } catch (e: McpCallException) {
            throw e
        } catch (e: IOException) {
            val why = if (e.message?.contains("timeout", true) == true) "超时" else "网络错误"
            throw McpCallException("MCP $why：${e.message ?: e.javaClass.simpleName}")
        }
    }

    private fun send(body: JSONObject) {
        val req = Request.Builder()
            .url(url)
            .header("Content-Type", "application/json")
            .header("Accept", "application/json, text/event-stream")
            .apply { if (token.isNotBlank()) header("Authorization", "Bearer $token") }
            .apply { if (sessionId.isNotBlank()) header("Mcp-Session-Id", sessionId) }
            .post(body.toString().toRequestBody(JSON))
            .build()
        runCatching { client.newCall(req).execute().close() }
    }

    private fun readSse(source: okio.BufferedSource, expectId: Int): JSONObject? {
        val parser = SseParser()
        while (!source.exhausted()) {
            val line = source.readUtf8Line() ?: break
            val event = parser.feed(line) ?: continue
            parseResponse(event.data, expectId)?.let { return it }
        }
        parser.flush()?.let { event ->
            parseResponse(event.data, expectId)?.let { return it }
        }
        throw McpCallException("SSE 流已结束，但没有收到 id=$expectId 的响应")
    }

    private fun parseResponse(text: String, expectId: Int): JSONObject? {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return null
        val value = try {
            org.json.JSONTokener(trimmed).nextValue()
        } catch (t: Throwable) {
            throw McpCallException("MCP 回包不是合法 JSON：${t.message ?: t.javaClass.simpleName}")
        }
        return when (value) {
            is JSONObject -> if (value.optInt("id", -1) == expectId) value else null
            is JSONArray -> (0 until value.length())
                .mapNotNull { value.optJSONObject(it) }
                .firstOrNull { it.optInt("id", -1) == expectId }
            else -> null
        }
    }

    private fun toToolResult(result: JSONObject): ToolResult {
        val isError = result.optBoolean("isError", false)
        val content = result.optJSONArray("content") ?: JSONArray()
        val texts = ArrayList<String>()
        var image = ""
        var mime = ""
        for (i in 0 until content.length()) {
            val item = content.optJSONObject(i) ?: continue
            when (item.optString("type")) {
                "text" -> {
                    val t = item.optString("text")
                    if (t.isNotBlank()) texts.add(t)
                }
                "image" -> if (image.isBlank()) {
                    image = item.optString("data")
                    mime = item.optString("mimeType")
                }
                "resource" -> {
                    val r = item.optJSONObject("resource") ?: continue
                    val t = r.optString("text")
                    if (t.isNotBlank()) texts.add(t)
                }
            }
        }
        val text = texts.joinToString("\n")
        return if (isError) {
            ToolResult(false, err = text.ifBlank { "外部 MCP 返回错误" })
        } else {
            ToolResult(true, text = clip(text), imageBase64 = image, mimeType = mime.ifBlank { "image/png" })
        }
    }

    private fun clip(s: String): String =
        if (s.length <= 4000) s else s.take(4000) + "\n…（已截断，原始结果共 ${s.length} 字）"
}
