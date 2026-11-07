package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 卡片预览纯解析逻辑：不联网、不需要真机。 */
class LinkPreviewTest {

    @Test
    fun og全字段能解析() {
        val html = """
            <html><head>
            <title>兜底标题</title>
            <meta property="og:site_name" content="少数派">
            <meta property="og:title" content="一篇 &amp; 好文章">
            <meta property="og:description" content="讲了 &quot;预览&quot; 怎么落库">
            <meta property="og:image" content="https://cdn.sspai.com/a.jpg">
            <meta name="description" content="不该被采用">
            </head><body><p>正文</p></body></html>
        """.trimIndent()
        val p = LinkPreviewLogic.parse(html, "https://sspai.com/post/123")
        assertEquals("少数派", p.site)
        assertEquals("一篇 & 好文章", p.title)
        assertEquals("讲了 \"预览\" 怎么落库", p.description)
        assertEquals("https://cdn.sspai.com/a.jpg", p.image)
        assertTrue(p.hasImage)
    }

    @Test
    fun twitter卡片与相对图片地址() {
        val html = """
            <head>
            <meta name="twitter:title" content="T 卡标题">
            <meta name="twitter:description" content="T 卡摘要">
            <meta name="twitter:image" content="/img/cover.png">
            <link rel="icon" href="//static.example.com/fav.ico">
            </head>
        """.trimIndent()
        val p = LinkPreviewLogic.parse(html, "https://example.com/a/b")
        assertEquals("T 卡标题", p.title)
        assertEquals("T 卡摘要", p.description)
        assertEquals("https://example.com/img/cover.png", p.image)
        assertEquals("https://static.example.com/fav.ico", p.icon)
        assertEquals("example.com", p.site)
    }

    @Test
    fun 没有OG时退回title与首张图() {
        val html = """
            <head><title>  普通 &lt;页面&gt;   </title>
            <meta name="description" content="一句  带换行的  描述">
            <img src="data:image/png;base64,AAAA" width="1" height="1">
            <img src="pic/logo.gif" width="32" height="32">
            <img src="pic/real.webp" width="800">
            </head>
        """.trimIndent()
        val p = LinkPreviewLogic.parse(html, "https://foo.cn/post/x/")
        assertEquals("普通 <页面>", p.title)
        assertEquals("一句 带换行的 描述", p.description)
        assertEquals("https://foo.cn/post/x/pic/real.webp", p.image)
    }

    @Test
    fun 空页面也不炸() {
        val p = LinkPreviewLogic.parse("", "https://foo.cn/x")
        assertEquals("", p.title)
        assertEquals("", p.image)
        assertEquals("foo.cn", p.site)
        assertEquals("https://foo.cn/favicon.ico", p.icon)
    }

    @Test
    fun 大站零请求封面() {
        assertEquals(
            "https://img.youtube.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
            LinkPreviewLogic.coverOf("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3"),
        )
        assertEquals(
            "https://img.youtube.com/vi/abc123/hqdefault.jpg",
            LinkPreviewLogic.coverOf("https://youtu.be/abc123"),
        )
        assertEquals(
            "https://opengraph.githubassets.com/1/ggml-org/llama.cpp",
            LinkPreviewLogic.coverOf("https://github.com/ggml-org/llama.cpp"),
        )
        assertEquals(
            "https://cdn.x.com/raw.jpg?w=800",
            LinkPreviewLogic.coverOf("https://cdn.x.com/raw.jpg?w=800"),
        )
        assertEquals("", LinkPreviewLogic.coverOf("https://news.ycombinator.com/item?id=1"))
    }

    @Test
    fun 地址补全与清洗() {
        assertEquals(
            "https://a.cn/static/x.png",
            LinkPreviewLogic.absolute("https://a.cn/p/1", "/static/x.png"),
        )
        assertEquals(
            "https://a.cn/p/x.png",
            LinkPreviewLogic.absolute("https://a.cn/p/1", "x.png"),
        )
        assertEquals("", LinkPreviewLogic.absolute("https://a.cn", "data:image/png;base64,AA"))
        assertEquals("a.cn", LinkPreviewLogic.domainOf("https://www.a.cn:8443/x"))
        assertEquals("短 文本", LinkPreviewLogic.clean("<b>短&nbsp; 文本</b>"))
        assertTrue(LinkPreviewLogic.clean("x".repeat(500)).length == 300)
    }
}
