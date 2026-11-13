package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.ArrayDeque
import java.util.Date
import java.util.Locale
import kotlin.text.Charsets
import kotlinx.coroutines.launch

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
    private const val DISK_MAX = 200
    private const val FILE = "action_log.json"
    @Volatile private var loaded = false
    private val items = ArrayDeque<ActionRecord>()

    @Synchronized
    fun add(name: String, args: JSONObject, ok: Boolean, err: String, ms: Long) {
        ensureLoaded()
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
        saveDiskAsync()
    }

    /** 最新的在前。limit 限制返回条数，避免一条命令把上下文塞满。 */
    @Synchronized
    fun list(limit: Int = 20): List<ActionRecord> {
        ensureLoaded()
        return items.take(limit.coerceIn(1, MAX))
    }

    /** 把所有条目格式化成多行文本，供复制 / 导出。 */
    @Synchronized
    fun text(limit: Int = 50): String {
        ensureLoaded()
        return items.take(limit).joinToString("\n") { format(it) }
    }

    @Synchronized
    fun clear() { items.clear(); saveDiskAsync() }

    /** 进程启动后第一次访问时把磁盘历史读回内存，之后只在内存里玩。 */
    @Synchronized
    private fun ensureLoaded() {
        if (loaded) return
        loaded = true
        runCatching {
            val f = File(App.ctx.filesDir, FILE)
            if (f.isFile) {
                val arr = JSONArray(f.readText(Charsets.UTF_8))
                for (i in 0 until arr.length()) {
                    val o = arr.optJSONObject(i) ?: continue
                    items.addLast(
                        ActionRecord(
                            at = o.optLong("at", 0L),
                            name = o.optString("name"),
                            args = o.optString("args"),
                            ok = o.optBoolean("ok", true),
                            err = o.optString("err"),
                            ms = o.optLong("ms", 0L),
                        )
                    )
                }
            }
        }
    }

    /** 异步落盘：不阻塞工具执行路径。先写临时文件再改名，防写一半被杀。 */
    private fun saveDiskAsync() {
        val snapshot = synchronized(this) { items.take(DISK_MAX) }
        App.scope.launch {
            runCatching {
                val arr = JSONArray()
                for (r in snapshot) {
                    arr.put(
                        JSONObject()
                            .put("at", r.at).put("name", r.name).put("args", r.args)
                            .put("ok", r.ok).put("err", r.err).put("ms", r.ms)
                    )
                }
                val f = File(App.ctx.filesDir, FILE)
                f.parentFile?.mkdirs()
                val tmp = File(f.parentFile, "$FILE.tmp")
                tmp.writeText(arr.toString(), Charsets.UTF_8)
                if (!tmp.renameTo(f)) { f.writeText(arr.toString(), Charsets.UTF_8); tmp.delete() }
            }
        }
    }

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
