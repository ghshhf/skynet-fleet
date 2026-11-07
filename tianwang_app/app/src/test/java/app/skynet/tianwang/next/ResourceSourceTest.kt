package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 全球源解析 + 插件式翻译的纯逻辑，不联网、不需要真机。 */
class ResourceSourceTest {

    @Test
    fun awesome清单抽外链() {
        val md = """
            ## Tools
            - [LangChain](https://github.com/langchain-ai/langchain) - framework
            - [A cool dataset](https://example.com/data) great
            - [internal link](./relative.md) should be ignored
            - [docs](https://github.com/a/b/blob/master/readme.md) skipped
            - [LangChain](https://github.com/langchain-ai/langchain) duplicate
        """.trimIndent()
        val hits = ResourceCrawler.markdownLinks(md)
        assertEquals(2, hits.size)
        assertEquals("LangChain", hits[0].title)
        assertEquals("https://github.com/langchain-ai/langchain", hits[0].url)
        assertEquals("repo", hits[0].kind)
        assertEquals("tool", hits[1].kind)
        assertTrue(hits[0].tags.contains("awesome"))
    }

    @Test
    fun atom条目能出卡片() {
        val xml = """
            <feed><entry>
              <title>New AI &amp; paper</title>
              <link rel="alternate" href="https://arxiv.org/abs/2601.00001" />
              <summary>Some plain result</summary>
              <updated>2026-11-07T10:00:00Z</updated>
            </entry></feed>
        """.trimIndent()
        val hits = ResourceCrawler.parseFeedXml(xml, "论文")
        assertEquals(1, hits.size)
        assertEquals("New AI & paper", hits[0].title)
        assertEquals("https://arxiv.org/abs/2601.00001", hits[0].url)
        assertEquals("Some plain result", hits[0].summary)
        assertTrue(hits[0].publishedAt > 0.0)
    }

    @Test
    fun 无效链接与CDATA() {
        val xml = """
            <rss><item>
              <title><![CDATA[  Hello <world>  ]]></title>
              <link>/relative/path</link>
            </item></rss>
        """.trimIndent()
        assertTrue(ResourceCrawler.parseFeedXml(xml, "x").isEmpty())
        assertEquals("Hello", ResourceCrawler.stripTags("<![CDATA[  Hello <world>  ]]>"))
    }

    @Test
    fun 英文判定与切换脚本() {
        assertTrue(
            ArticleTranslator.isEnglish(
                listOf("A new study shows that large language models can reason about code.")
            )
        )
        assertFalse(ArticleTranslator.isEnglish(listOf("这是一篇中文文章，讲的是手机自己联网抓取资源。")))
        assertFalse(ArticleTranslator.isEnglish(listOf("OK")))
        val js = ArticleTranslator.injectScript()
        assertTrue(js.contains("__twCollect"))
        assertTrue(js.contains("__twApply"))
        assertTrue(js.contains("__twRestore"))
        assertTrue(js.contains("NodeFilter.SHOW_TEXT"))
    }
}
