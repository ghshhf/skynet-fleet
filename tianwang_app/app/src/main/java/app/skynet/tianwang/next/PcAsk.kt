package app.skynet.tianwang.next

import java.net.HttpURLConnection
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * 电脑 / API 算力：问题交给电脑，由电脑按自己的算力注册表决定走本机还是云端。
 *
 * 手机端**不落任何厂商 key** —— key 在电脑的 `~/.fleet/compute_keys.json`，
 * 回包也永远不带 key。这条通道同时覆盖「电脑的本地模型」和「电脑配的云端 API」，
 * 所以并行池里叫它「电脑/API」一条车道就够了。
 */
object PcAsk {
    suspend fun ask(
        prompt: String,
        maxTokens: Int = 256,
        purpose: String = "chat",
        endpoint: String = "",
        timeoutMs: Int = 60_000,
    ): ToolResult = withContext(Dispatchers.IO) {
        if (prompt.isBlank()) return@withContext ToolResult(false, err = "缺 prompt：写清要问什么")
        if (!Net.hasConfig()) {
            return@withContext ToolResult(false, err = "没配电脑地址/令牌；先在「连接」页配对")
        }
        val body = JSONObject()
            .put("prompt", prompt)
            .put("max_tokens", maxTokens.coerceIn(32, 4096))
            .put("purpose", purpose.ifBlank { "chat" })
            .put("timeout", (timeoutMs / 1000).coerceIn(5, 300))
        if (endpoint.isNotBlank()) body.put("endpoint", endpoint)

        var conn: HttpURLConnection? = null
        try {
            conn = Net.auth(Api.endpoint(Net.loadConfig().url, "/api/compute/ask")).apply {
                requestMethod = "POST"
                setRequestProperty("Content-Type", "application/json")
                readTimeout = timeoutMs + 5_000
                doOutput = true
            }
            conn.outputStream.write(body.toString().toByteArray(Charsets.UTF_8))
            val code = conn.responseCode
            val raw = runCatching {
                (if (code in 200..299) conn.inputStream else conn.errorStream)
                    ?.bufferedReader()?.readText().orEmpty()
            }.getOrDefault("")
            if (code !in 200..299) {
                return@withContext ToolResult(false, err = "电脑 HTTP $code ${raw.take(160)}")
            }
            val j = runCatching { JSONObject(raw) }.getOrNull()
                ?: return@withContext ToolResult(false, err = "电脑回的不是 JSON：${raw.take(160)}")
            if (!j.optBoolean("ok", false)) {
                return@withContext ToolResult(false, err = j.optString("err", "电脑没给原因"))
            }
            val text = j.optString("text")
            if (text.isBlank()) return@withContext ToolResult(false, err = "电脑回了空答案")
            val who = listOf(j.optString("endpoint"), j.optString("model"))
                .filter { it.isNotBlank() }.joinToString(" · ")
            ToolResult(true, text = "由电脑/API 算力（$who）回答：\n$text")
        } catch (e: Throwable) {
            ToolResult(false, err = "电脑算力调用失败：${e.message ?: e.javaClass.simpleName}")
        } finally {
            conn?.disconnect()
        }
    }
}
