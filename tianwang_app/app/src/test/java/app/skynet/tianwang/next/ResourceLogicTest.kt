package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 本机资源库的纯逻辑测试：不连任何服务端、不需要真机。
 *
 * 钉住的是「两个节点共享时不能算错的那些东西」：同一个链接必须收敛成同一个 id，
 * 同一份内容必须算出同一个指纹，否则合并会越同步越重复。
 */
class ResourceLogicTest {

    @Test
    fun `规范化链接去跟踪参数并排序 query`() {
        assertEquals(
            "https://a.example/x?a=1&b=2",
            ResourceLogic.canonUrl("http://www.a.example:80/x/?utm_source=t&b=2&a=1"),
        )
        assertEquals("https://b.example/", ResourceLogic.canonUrl("b.example"))
        assertEquals("", ResourceLogic.canonUrl("  "))
    }

    @Test
    fun `有摘要按内容去重 没摘要退回链接`() {
        val a = ResourceLogic.fingerprint("标题", "摘要", "https://a/x")
        val b = ResourceLogic.fingerprint(" 标题 ", "摘要", "https://b/y")
        assertEquals(a, b)
        assertEquals(64, a.length)
        assertEquals("r_" + a.take(16), ResourceLogic.rid(a))
        assertNotEquals(
            ResourceLogic.fingerprint("标题", "", "https://a/x"),
            ResourceLogic.fingerprint("标题", "", "https://b/y"),
        )
    }

    @Test
    fun `已爬库里塞进 summary 的 JSON 元数据翻成人话`() {
        val s = ResourceLogic.cleanSummary(
            """{"digg": 15, "comment": 1, "create_time": 1754834882, "images": 1, "author": "Healer."}""",
        )
        assertTrue(s.contains("作者 Healer."))
        assertTrue(s.contains("赞 15"))
        assertTrue(s.contains("图 1 张"))
        assertEquals("一句话摘要", ResourceLogic.cleanSummary("一句话摘要"))
    }

    @Test
    fun `同站最多两条 不够再补`() {
        val xs = listOf("a", "a", "a", "b", "b", "c")
        assertEquals(listOf("a", "a", "b", "b"), ResourceLogic.diverse(xs, 4) { it })
    }

    @Test
    fun `打分公式与电脑侧一致`() {
        val s = ResourceLogic.score(1.0, 0.5, 1.0, 1.0, 0.5, 1.0, 0.12)
        assertEquals(0.35 + 0.10 + 0.20 + 0.15 + 0.05 + 0.25 - 0.12, s, 0.0001)
        // 同一 session 的探索项是确定性的，不靠随机数
        assertEquals(
            ResourceLogic.exploreOf("r_1", "phone-1"),
            ResourceLogic.exploreOf("r_1", "phone-1"),
            0.0001,
        )
    }

    @Test
    fun `RSS 与 Atom 都能抽出条目`() {
        val rss = """<rss><channel><item><title>甲</title><link>https://x/1</link>""" +
            """<description><![CDATA[<p>正文 &amp; 更多</p>]]></description>""" +
            """</item></channel></rss>"""
        val a = ResourceCrawler.parseFeedXml(rss, "订阅")
        assertEquals(1, a.size)
        assertEquals("甲", a[0].title)
        assertEquals("https://x/1", a[0].url)
        assertEquals("正文 & 更多", a[0].summary)

        val atom = """<feed><entry><title>乙</title>""" +
            """<link href="https://y/2"/><summary>摘要</summary></entry></feed>"""
        val b = ResourceCrawler.parseFeedXml(atom, "订阅")
        assertEquals(1, b.size)
        assertEquals("https://y/2", b[0].url)
        assertEquals("摘要", b[0].summary)
    }

    @Test
    fun `标签切分去重且最多八条`() {
        val t = ResourceLogic.tagsOf("论文/工具、论文, 工具", listOf("站点"))
        assertEquals(listOf("论文", "工具", "站点"), t)
        assertEquals(3, t.size)
    }
}
