package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 联网工具的纯逻辑测试：不联网、不碰 Android。
 *
 * 守的是一条硬线：**解析不出来要如实返回空，不许编造条目**。
 * 网页结构会变，这份测试就是「变了会红」的报警器 —— 没有它，
 * 上线后失效会表现成「搜不到东西」，看起来像网络问题，实际是解析废了。
 */
class WebLogicTest {

    /** 截自手机实抓的 cn.bing.com 结果页（2026-11-09），结构与线上一致。 */
    private val bingHtml = """
        <html><body><ol id="b_results">
        <li class="b_algo"><h2><a target="_blank" href="https://www.w3schools.com/python/" h="ID=SERP,5127.2"><strong>Python Tutorial</strong> - W3Schools</a></h2>
        <div class="b_caption"><p class="b_lineclamp4">Learn Python in bite-sized quests with XP and streaks.</p></div></li>
        <li class="b_algo b_algoBorder"><h2><a href="https://docs.python.org/3/tutorial/index.html">The Python Tutorial &mdash; Python 3.15</a></h2>
        <p>This tutorial introduces the reader informally to the language.</p></li>
        </ol></body></html>
    """.trimIndent()

    @Test
    fun parsesRealBingStructure() {
        val hits = WebLogic.parseBingResults(bingHtml, 5)
        assertEquals("应解析出 2 条，实际：$hits", 2, hits.size)

        val first = hits[0]
        assertEquals("https://www.w3schools.com/python/", first.url)
        assertEquals("Python Tutorial - W3Schools", first.title)
        assertTrue("摘要该带出来，实际：${first.snippet}", first.snippet.contains("bite-sized"))

        val second = hits[1]
        assertEquals("https://docs.python.org/3/tutorial/index.html", second.url)
        assertTrue("实体该解开，实际：${second.title}", second.title.contains("—"))
        // class="b_algo b_algoBorder" 这种带额外 class 的块也要认
        assertTrue(second.snippet.isNotBlank())
    }

    @Test
    fun emptyOrUnknownStructureReturnsEmpty() {
        assertTrue(WebLogic.parseBingResults("", 5).isEmpty())
        assertTrue(
            "结构变了必须返回空表，绝不编造条目",
            WebLogic.parseBingResults("<html>一个完全不同的页面</html>", 5).isEmpty(),
        )
    }

    @Test
    fun skipsNonHttpLinks() {
        val html = """<li class="b_algo"><h2><a href="javascript:void(0)">坏链接</a></h2><p>x</p></li>"""
        assertTrue("javascript: 链接不该出现在结果里", WebLogic.parseBingResults(html, 5).isEmpty())
    }

    @Test
    fun skipsRedirectWrapperAndOverlongUrls() {
        val long = "a".repeat(250)
        val html = """
            <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&&p=abc">跳转包装</a></h2><p>x</p></li>
            <li class="b_algo"><h2><a href="https://example.com/$long">超长链接</a></h2><p>x</p></li>
            <li class="b_algo"><h2><a href="https://ok.example.com/page">正常</a></h2><p>x</p></li>
        """.trimIndent()
        val hits = WebLogic.parseBingResults(html, 5)
        assertEquals("只该留下正常那条，实际：$hits", 1, hits.size)
        assertEquals("https://ok.example.com/page", hits[0].url)
    }

    @Test
    fun limitIsRespected() {
        val hits = WebLogic.parseBingResults(bingHtml, 1)
        assertEquals(1, hits.size)
    }

    @Test
    fun decodesEntities() {
        assertEquals("a & b", WebLogic.decodeEntities("a &amp; b"))
        assertEquals("—", WebLogic.decodeEntities("&mdash;"))
        assertEquals("中", WebLogic.decodeEntities("&#20013;"))
        assertEquals("中", WebLogic.decodeEntities("&#x4E2D;"))
        assertEquals("认不出的实体保留原样，不许吞掉", "&notareal;", WebLogic.decodeEntities("&notareal;"))
    }

    @Test
    fun htmlToTextDropsScriptStyleNavFooter() {
        val html = """
            <html><head><title>标题</title><style>.a{color:red}</style></head>
            <body><script>var x=1;</script><nav>导航栏</nav>
            <p>第一段</p><p>第二段 &amp; 结尾</p>
            <footer>版权信息</footer></body></html>
        """.trimIndent()
        val text = WebLogic.htmlToText(html)
        assertTrue("正文该保留，实际：$text", text.contains("第一段"))
        assertTrue("实体该解开，实际：$text", text.contains("第二段 & 结尾"))
        assertFalse("脚本不该进正文，实际：$text", text.contains("var x"))
        assertFalse("样式不该进正文，实际：$text", text.contains("color:red"))
        assertFalse("页脚不该进正文，实际：$text", text.contains("版权信息"))
        assertFalse("导航不该进正文，实际：$text", text.contains("导航栏"))
    }

    @Test
    fun longArticleWinsOverBoilerplate() {
        val body = "正".repeat(600)
        val html = "<html><body><div>一堆导航</div><article>$body</article><div>推荐阅读</div></body></html>"
        val text = WebLogic.htmlToText(html)
        assertTrue("正文该在，实际：${text.take(30)}", text.startsWith("正"))
        assertFalse("选了 article 就该丢掉外面的杂物，实际：${text.take(40)}", text.contains("推荐阅读"))
    }

    @Test
    fun searchUrlEncodesChineseAndCarriesMarket() {
        val u = WebLogic.searchUrl("北京 天气", 5)
        assertTrue("必须是 bing 搜索地址，实际：$u", u.startsWith("https://cn.bing.com/search?"))
        assertFalse("中文要编码后再拼，实际：$u", u.contains("北京"))
        assertTrue("不带 mkt 会跑英文结果，实际：$u", u.contains("mkt=zh-CN"))
        assertTrue(u.contains("count=5"))
    }

    @Test
    fun searchCountIsClamped() {
        assertTrue(WebLogic.searchUrl("x", 999).contains("count=15"))
        assertTrue(WebLogic.searchUrl("x", 0).contains("count=1"))
    }

    @Test
    fun webToolsAreReadOnlyAndSchematized() {
        assertEquals(3, WebTool.tools.size)
        assertEquals("web_search", WebTool.tools[0].name)
        assertEquals("web_read", WebTool.tools[1].name)
        assertEquals("web_digest", WebTool.tools[2].name)
        for (t in WebTool.tools) {
            assertFalse("${t.name} 是只读能力，断网失败不该污染状态", t.sideEffect)
            assertTrue("${t.name} 缺 schema，模型不知道参数怎么写", t.inputSchema.length() > 0)
        }
    }

    @Test
    fun hostOfStripsWww() {
        assertEquals("w3schools.com", WebLogic.hostOf("https://www.w3schools.com/python/"))
        assertEquals("", WebLogic.hostOf("不是网址"))
    }

    @Test
    fun dropsLeadingNavNoise() {
        val noise = (1..12).joinToString("\n") { "导航$it" }
        val body = "这是真正的正文段落，讲了一件事，而且足够长，长到能被当成正文。".repeat(20)
        val out = WebLogic.dropLeadingNoise("$noise\n\n$body")
        assertFalse("开头的短行噪音该被砍掉，实际开头：${out.take(20)}", out.startsWith("导航"))
        assertTrue("正文必须还在", out.contains("真正的正文"))
    }

    @Test
    fun keepsFragmentedPageIntact() {
        // 整页都是短行（列表页 / 论坛）—— 砍了就什么都没了，必须原样返回
        val text = (1..200).joinToString("\n") { "第${it}条" }
        assertEquals(text, WebLogic.dropLeadingNoise(text))
    }

    @Test
    fun shortTextIsLeftAlone() {
        assertEquals("只有一点点内容", WebLogic.dropLeadingNoise("只有一点点内容"))
    }

    @Test
    fun doesNotCutParagraphInHalf() {
        val noise = (1..10).joinToString("\n") { "导航$it" }
        // 同一个 <p> 里的软换行：判定点落在第二行，但必须退回到第一行开头
        val l1 = "这是同一段落的第一句话，它必须被完整保留下来，不能被从中间切开，否则读起来就会断句。"
        val l2 = "第二句接着往下说，这句话自己也要足够长，长到能让两行加起来超过八十个字符的门槛。"
        val body = "$l1\n$l2\n\n" +
            "后续正文内容，也足够长，长到能让判定认为这是真正的正文段落。".repeat(20)
        val out = WebLogic.dropLeadingNoise("$noise\n\n$body")
        assertTrue(
            "段落不能被从中间切开，实际开头：${out.take(30)}",
            out.startsWith("这是同一段落的第一句话"),
        )
    }
}
