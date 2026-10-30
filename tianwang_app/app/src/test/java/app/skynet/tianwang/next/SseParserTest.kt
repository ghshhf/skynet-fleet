package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * SSE 事件切分。
 *
 * 原来这段逻辑长在 `Net.streamAgent` 的网络循环里，只能靠「对话不响应」来
 * 间接发现它坏了。拆出来之后这里能直接断言边界。
 */
class SseParserTest {

    @Test
    fun `一个标准事件`() {
        val p = SseParser()
        assertNull(p.feed("event: answer"))
        assertNull(p.feed("""data: {"answer":"好的"}"""))
        val e = p.feed("")
        assertEquals("answer", e?.event)
        assertEquals("""{"answer":"好的"}""", e?.data)
    }

    @Test
    fun `多行 data 用换行连接 —— 直接 append 会拼成非法 JSON`() {
        val p = SseParser()
        p.feed("event: step")
        p.feed("""data: {"cap":"image.gen",""")
        p.feed("""data: "ok":true}""")
        val e = p.feed("")
        // 按 SSE 规范多行 data 用 \n 连接（不是直接粘成一行）
        assertEquals("{\"cap\":\"image.gen\",\n\"ok\":true}", e?.data)
    }

    @Test
    fun `连续多个事件按顺序产出`() {
        val p = SseParser()
        val out = mutableListOf<SseEvent>()
        listOf(
            "event: step", """data: {"cap":"a"}""", "",
            "event: step", """data: {"cap":"b"}""", "",
            "event: answer", """data: {"answer":"done"}""", "",
        ).forEach { line -> p.feed(line)?.let { out.add(it) } }

        assertEquals(3, out.size)
        assertEquals(listOf("step", "step", "answer"), out.map { it.event })
        assertEquals("""{"answer":"done"}""", out[2].data)
    }

    @Test
    fun `流末尾没有空行时 flush 不能吞掉最后一条`() {
        val p = SseParser()
        p.feed("event: answer")
        p.feed("""data: {"answer":"last"}""")
        // 这里不喂空行，模拟连接被对端直接关掉
        val e = p.flush()
        assertEquals("answer", e?.event)
        assertEquals("""{"answer":"last"}""", e?.data)
    }

    @Test
    fun `没有 data 的空行不产出事件`() {
        val p = SseParser()
        assertNull(p.feed(""))
        assertNull(p.feed("event: status"))
        assertNull(p.feed(""))
        assertNull(p.flush())
    }

    @Test
    fun `忽略注释行与 id retry 行`() {
        val p = SseParser()
        p.feed(": keep-alive")
        p.feed("id: 7")
        p.feed("retry: 3000")
        p.feed("event: answer")
        p.feed("""data: {"answer":"x"}""")
        val e = p.feed("")
        assertEquals("answer", e?.event)
    }

    @Test
    fun `冒号后的空格被裁掉，data 前的单空格不影响内容`() {
        val p = SseParser()
        p.feed("event:   step  ")
        p.feed("data:   {\"cap\":\"x\"}")
        val e = p.feed("")
        assertEquals("step", e?.event)
        assertEquals("""{"cap":"x"}""", e?.data)
    }
}
