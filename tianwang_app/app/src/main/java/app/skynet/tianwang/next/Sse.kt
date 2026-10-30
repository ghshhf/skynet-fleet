package app.skynet.tianwang.next

/** 一条 SSE 事件。event 为事件名（天网用 step / status / answer），data 是它的 JSON。 */
data class SseEvent(val event: String, val data: String)

/**
 * 逐行喂、整事件吐的 SSE 解析器。
 *
 * 原来这套逻辑嵌在 `Net.streamAgent` 的网络循环里，跟着 IO 一起跑，测不了。
 * 拆出来之后，「多行 data 该怎么拼」「末尾没有空行要不要 flush」这些都能在
 * JVM 单测里断言，不用架一个假 SSE 服务端。
 *
 * 只实现天网用得到的部分：`event:` / `data:` / 空行分隔。`id:`、`retry:`、
 * 以 `:` 开头的注释行按规范都该忽略，这里直接丢掉。
 */
class SseParser {

    private var event = ""
    private val data = StringBuilder()

    /** 喂一行（不含行尾换行符）。凑齐一个事件就返回它，否则返回 null。 */
    fun feed(line: String): SseEvent? = when {
        line.startsWith("event:") -> {
            event = line.substring(6).trim()
            null
        }

        line.startsWith("data:") -> {
            // 多行 data 按规范用 \n 连接。原来直接 append，会把两行粘成一个
            // 非法 JSON，然后在 dispatch 里被 runCatching 静默吞掉。
            if (data.isNotEmpty()) data.append('\n')
            data.append(line.substring(5).trim())
            null
        }

        line.isEmpty() -> take()

        // id: / retry: / ":comment"，天网不用，跳到下一行
        else -> null
    }

    /** 流结束时调用：最后一条事件后面可能没有空行，不 flush 就会丢。 */
    fun flush(): SseEvent? = take()

    private fun take(): SseEvent? {
        if (data.isEmpty()) {
            // 空行但没有 data：只有 event: 没有 data 的事件不合法，丢掉并复位
            event = ""
            return null
        }
        val e = SseEvent(event, data.toString())
        event = ""
        data.clear()
        return e
    }
}
