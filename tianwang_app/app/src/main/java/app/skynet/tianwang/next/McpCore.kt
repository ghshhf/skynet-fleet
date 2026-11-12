package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject
import org.json.JSONTokener

/**
 * 手机端 MCP Server 的纯协议层：不碰 Android、不碰网络，只吃 JSON、吐 JSON。
 *
 * 这样协议错误可以在 JVM 单测里当场抓住，而不是等到真机 + 中继 + 控制面
 * 三层都接上之后，靠人盯日志猜「到底谁没回包」。
 *
 * 传输层（`PhoneMcpLink`）只负责：配对、按行切帧、把这里的返回值写回去。
 */
data class McpTool(
    val name: String,
    val description: String,
    val inputSchema: JSONObject,
    /** 会改变手机界面/状态的能力。只读入网时拒调，错误必须显式。 */
    val sideEffect: Boolean = false,
) {
    fun describe(): JSONObject = JSONObject()
        .put("name", name)
        .put("description", description)
        .put("inputSchema", inputSchema)
        .put("annotations", JSONObject()
            .put("readOnlyHint", !sideEffect)
            .put("destructiveHint", false)
            .put("openWorldHint", false))
}

class McpCore(
    /** 每次现取：外部 MCP 的工具是运行时长出来的，不能在构造成员时绑死。 */
    private val tools: () -> List<McpTool>,
    private val actionsAllowed: () -> Boolean,
    private val call: (String, JSONObject) -> ToolResult,
    private val serverName: String = "phone-self",
    private val serverVersion: String = "0.1.0",
    private val protocolVersion: String = "2025-06-18",
) {
    /** 一行（或一帧）原始文本 → 要写回的一行 JSON；通知返回 null。 */
    fun handleLine(line: String): String? {
        val text = line.trim()
        if (text.isEmpty()) return null
        val value = try {
            val tok = JSONTokener(text)
            val v = tok.nextValue()
            // 不允许「一条合法 JSON 后面再拖垃圾」—— 那通常意味着对端拼帧拼错了。
            if (tok.nextClean() != 0.toChar()) throw JSONException("JSON 末尾有多余内容")
            v
        } catch (t: Throwable) {
            return error(null, -32700, "解析失败：${t.message ?: t.javaClass.simpleName}").toString()
        }
        return when (value) {
            is JSONObject -> handle(value)?.toString()
            is JSONArray -> handleBatch(value)?.toString()
            else -> error(null, -32600, "请求必须是 JSON 对象或数组").toString()
        }
    }

    /** 单个 JSON-RPC 请求；通知（无 id）返回 null。 */
    fun handle(req: JSONObject): JSONObject? {
        val id = if (req.has("id")) req.opt("id") ?: JSONObject.NULL else null
        val isNotification = id == null
        val method = req.optString("method", "")
        if (method.isBlank()) {
            return if (isNotification) null else error(id, -32600, "缺 method")
        }
        // JSON-RPC 通知没有 id：不执行、不回包。避免控制面用「通知」绕过工具审计。
        if (isNotification) return null
        val params = req.optJSONObject("params") ?: JSONObject()

        return when (method) {
            "initialize" -> ok(id, JSONObject()
                .put("protocolVersion", protocolVersion)
                .put("capabilities", JSONObject()
                    .put("tools", JSONObject().put("listChanged", false)))
                .put("serverInfo", JSONObject()
                    .put("name", serverName)
                    .put("version", serverVersion))
                .put("instructions",
                    "手机本机能力。默认只读：status/dump/find/screenshot；" +
                        "tap/text/scroll/key/launch 需要用户在手机「手机本机 → 入网」里显式打开「允许操作」。"))

            "notifications/initialized", "notifications/cancelled" -> null

            "ping" -> ok(id, JSONObject())

            "tools/list" -> {
                val arr = JSONArray()
                tools().forEach { arr.put(it.describe()) }
                ok(id, JSONObject().put("tools", arr))
            }

            "tools/call" -> {
                val name = params.optString("name", "")
                if (name.isBlank()) {
                    error(id, -32602, "tools/call 缺 name")
                } else {
                    val args = params.optJSONObject("arguments") ?: JSONObject()
                    ok(id, invoke(name, args))
                }
            }

            else -> if (isNotification) null else error(id, -32601, "未知方法：$method")
        }
    }

    private fun handleBatch(batch: JSONArray): JSONArray? {
        if (batch.length() == 0) {
            return JSONArray().put(error(null, -32600, "空 batch"))
        }
        val out = JSONArray()
        for (i in 0 until batch.length()) {
            val item = batch.optJSONObject(i)
                ?: return JSONArray().put(error(null, -32600, "batch 第 ${i + 1} 项不是对象"))
            handle(item)?.let { out.put(it) }
        }
        return if (out.length() == 0) null else out
    }

    private fun invoke(name: String, args: JSONObject): JSONObject {
        val tool = tools().firstOrNull { it.name == name }
            ?: return toolResult("未知工具：$name（先 tools/list 看可用工具）", true)
        if (tool.sideEffect && !actionsAllowed()) {
            return toolResult(
                "工具 $name 会改变手机界面/状态，但手机入网当前是只读模式。" +
                    "请在手机「手机本机 → 入网设置」里打开「允许操作」后重试。",
                true,
            )
        }
        val r = try {
            call(name, args)
        } catch (t: Throwable) {
            ToolResult(false, err = "${t.javaClass.simpleName}: ${t.message ?: "执行失败"}")
        }
        return if (r.ok) toolResult(r) else toolResult(r.err.ifBlank { "执行失败" }, true)
    }

    private fun toolResult(r: ToolResult): JSONObject {
        val content = JSONArray()
        if (r.imageBase64.isNotBlank()) {
            content.put(JSONObject()
                .put("type", "image")
                .put("data", r.imageBase64)
                .put("mimeType", r.mimeType.ifBlank { "image/jpeg" }))
        }
        // 图片和说明可以同时回：远端既能看到画面，也知道文件落在哪。
        if (r.text.isNotBlank() || content.length() == 0) {
            content.put(JSONObject().put("type", "text").put("text", r.text))
        }
        return JSONObject().put("content", content).put("isError", false)
    }

    private fun toolResult(text: String, isError: Boolean): JSONObject =
        JSONObject()
            .put("content", JSONArray().put(JSONObject().put("type", "text").put("text", text)))
            .put("isError", isError)

    private fun ok(id: Any?, result: JSONObject): JSONObject = JSONObject()
        .put("jsonrpc", "2.0")
        .put("id", id ?: JSONObject.NULL)
        .put("result", result)

    private fun error(id: Any?, code: Int, message: String): JSONObject = JSONObject()
        .put("jsonrpc", "2.0")
        .put("id", id ?: JSONObject.NULL)
        .put("error", JSONObject().put("code", code).put("message", message))
}
