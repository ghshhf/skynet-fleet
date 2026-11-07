package app.skynet.tianwang.next

import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Date
import java.util.Locale

/** 一条已执行/被拒绝的动作。只留最近若干条，进程重启即清空 —— 不写盘、不偷存敏感参数。 */
data class ActionRecord(
    val at: Long,
    val name: String,
    val args: String,
    val ok: Boolean,
    val err: String,
    val ms: Long,
)

/**
 * 远程动作审计。MCP 端每调一次有副作用的工具就记一条，
 * 出问题时能回答「刚才到底点了什么、成没成」，而不是只能翻聊天记录猜。
 */
object ActionLog {
    private const val MAX = 50
    private val items = ArrayDeque<ActionRecord>()

    @Synchronized
    fun add(name: String, args: JSONObject, ok: Boolean, err: String, ms: Long) {
        items.addFirst(
            ActionRecord(
                at = System.currentTimeMillis(),
                name = name,
                args = summarize(args),
                ok = ok,
                err = err.take(120),
                ms = ms,
            )
        )
        while (items.size > MAX) items.removeLast()
    }

    /** 最新的在前。limit 限制返回条数，避免一条命令把上下文塞满。 */
    @Synchronized
    fun list(limit: Int = 20): List<ActionRecord> =
        items.take(limit.coerceIn(1, MAX))

    @Synchronized
    fun clear() = items.clear()

    /** 参数摘要：正文只报字数，疑似密钥直接隐藏，长值截断。 */
    fun summarize(args: JSONObject): String {
        if (args.length() == 0) return "（无参数）"
        val keys = args.keys()
        val parts = mutableListOf<String>()
        while (keys.hasNext()) {
            val k = keys.next()
            val v = args.opt(k)
            val shown = when {
                k.contains("secret", ignoreCase = true) ||
                    k.contains("token", ignoreCase = true) -> "<已隐藏>"
                k.contains("text", ignoreCase = true) && v is String -> "${v.length} 字"
                v is String && v.length > 40 -> v.take(40) + "…"
                else -> v?.toString() ?: "null"
            }
            parts.add("$k=$shown")
        }
        return parts.joinToString(", ").take(160)
    }

    private val clock = SimpleDateFormat("HH:mm:ss", Locale.US)

    @Synchronized
    fun format(r: ActionRecord): String {
        val mark = if (r.ok) "✓" else "✗"
        val ms = "${r.ms}ms"
        return "${clock.format(Date(r.at))} $mark ${r.name}（$ms）${r.args}" +
            if (r.ok || r.err.isBlank()) "" else " → ${r.err}"
    }
}
