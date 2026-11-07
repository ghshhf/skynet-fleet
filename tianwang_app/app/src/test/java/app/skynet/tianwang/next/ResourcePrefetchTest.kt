package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 首屏预加载的纯规则：不联网、不碰数据库，JVM 直接验。 */
class ResourcePrefetchTest {

    @Test
    fun `只留可联网的地址并去重限量`() {
        val urls = (1..25).map { "https://a.cn/$it" } + listOf(
            "https://a.cn/1", "  https://b.cn/x  ", "file:///sdcard/a.jpg", "", "not-a-url",
        )
        val out = ResourcePrefetchLogic.normalize(urls)
        assertEquals(20, out.size)
        assertEquals("https://a.cn/1", out.first())
        assertTrue(out.all { it.startsWith("http") })
        assertEquals(out.size, out.distinct().size)
        assertTrue("https://b.cn/x" !in out) // 超出首页 20 条的不预下载
    }

    @Test
    fun `首屏前四条优先其余后台补`() {
        val urls = (1..20).map { "https://a.cn/$it" }
        val (head, tail) = ResourcePrefetchLogic.split(urls)
        assertEquals(4, head.size)
        assertEquals(16, tail.size)
        assertEquals(urls, head + tail)
        // 不足 4 条时不能把 tail 弄成负数
        val (h2, t2) = ResourcePrefetchLogic.split(urls.take(2))
        assertEquals(2, h2.size)
        assertTrue(t2.isEmpty())
    }

    @Test
    fun `元数据按六条一批补`() {
        val urls = (1..16).map { "https://a.cn/$it" }
        val b = ResourcePrefetchLogic.batches(urls)
        assertEquals(listOf(6, 6, 4), b.map { it.size })
        assertEquals(urls, b.flatten())
    }

    @Test
    fun `提前八条续页且没有下一页时不拉`() {
        assertTrue(ResourcePrefetchLogic.due(last = 12, size = 20, hasNext = true))
        assertFalse(ResourcePrefetchLogic.due(last = 11, size = 20, hasNext = true))
        assertFalse(ResourcePrefetchLogic.due(last = 19, size = 20, hasNext = false))
        assertFalse(ResourcePrefetchLogic.due(last = 0, size = 0, hasNext = true))
    }
}
