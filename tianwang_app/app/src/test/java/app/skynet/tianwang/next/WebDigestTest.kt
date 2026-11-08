package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 「网页交给它自己解析」这条链路的纯逻辑测试。
 *
 * 守三条线：
 *  1. 链接只从文本里**捞**，不猜、不改写（维基那种带括号的也不能被切断）；
 *  2. 抽句只从原文里挑，**不改写、不生成**；
 *  3. 预算是硬约束 —— 超一点就会在回灌时被截断，最后一条永远读不全。
 */
class WebDigestTest {

    @Test
    fun extractsUrlsFromChatText() {
        val text = """
            看这几篇：
            https://example.com/a?x=1，
            https://example.com/a?x=1
            https://zh.wikipedia.org/wiki/Foo_(bar)。
            ftp://nope.example
            https://mp.weixin.qq.com/s/AbC-123
        """.trimIndent()
        val urls = WebLogic.parseUrls(text, 5)
        assertEquals(3, urls.size)
        assertEquals("https://example.com/a?x=1", urls[0])
        // 半角括号是条目名的一部分，不能当句读掐掉
        assertEquals("https://zh.wikipedia.org/wiki/Foo_(bar)", urls[1])
        assertEquals("https://mp.weixin.qq.com/s/AbC-123", urls[2])
    }

    @Test
    fun urlCountIsCapped() {
        val text = (1..9).joinToString("\n") { "https://example.com/p$it" }
        assertEquals(5, WebLogic.parseUrls(text, 5).size)
    }

    @Test
    fun noUrlMeansEmpty() {
        assertTrue(WebLogic.parseUrls("这篇文章讲的是本地模型", 5).isEmpty())
        assertTrue(WebLogic.parseUrls("", 5).isEmpty())
    }

    @Test
    fun splitsChineseAndEnglishSentences() {
        val zh = "第一句足够长可以保留下来。第二句也足够长可以保留下来！短。"
        val z = WebLogic.splitSentences(zh)
        assertEquals(2, z.size)
        assertTrue(z[0].endsWith("。"))

        // 短于 8 字符的片段（"Yes."）按导航噪音丢掉，所以第三句要写得够长
        val en = "This is the first sentence. Second one is here! Is the third one short? Yes."
        val e = WebLogic.splitSentences(en)
        assertEquals("实际切出：$e", 3, e.size)
    }

    @Test
    fun longSentenceIsSplitByCommaNotEmpty() {
        val long = "这是一个非常长的句子，" + "用来测试按逗号切分，".repeat(30) + "结束。"
        val sents = WebLogic.splitSentences(long, 100)
        assertTrue("不能把长句整条丢掉", sents.isNotEmpty())
        assertTrue("每段都不能超预算：${sents.map { it.length }}", sents.all { it.length <= 100 })
        assertTrue(sents.size > 3)
    }

    @Test
    fun segmentsKeepEverySentence() {
        val text = (1..20).joinToString("") { "第${it}句内容用于验证分段逻辑，长度大概二十来个字。" }
        val segs = WebLogic.segments(text, 100, 50)
        assertTrue(segs.isNotEmpty())
        assertTrue(segs.all { it.length <= 100 })
        assertEquals("分段只能重排、不能丢句", text, segs.joinToString(""))
    }

    @Test
    fun keySentencesPreferFocusAndStayInOrder() {
        val text = "OpenAI 今天发布了新模型。" +
            "这个模型的参数是 70B，比上一代小 30%。" +
            "价格下降了 50%，每百万 token 收费 2 美元。" +
            "发布时间是 2025 年 3 月。" +
            "这段话与主题无关，只是凑数用的，不要选它。"
        val keys = WebLogic.keySentences(text, "价格 收费", 80)

        assertTrue("命中关注点的句子必须进来", keys.any { it.contains("价格") })
        assertTrue("无关句不该占预算：$keys", keys.none { it.contains("凑数") })
        assertTrue("超预算：${keys.joinToString("").length}", keys.joinToString("").length <= 80)
        // 顺序必须与原文一致，否则一篇文章会被讲成前后颠倒
        assertEquals(keys.sortedBy { text.indexOf(it) }, keys)
    }

    @Test
    fun dropsCodeAndRuntimeNoise() {
        // 实测样本：Python 教程页抽出来整屏是 >>> 代码，about 页开头是「脚本未运行」提示。
        // 这两类必须在切句阶段就丢掉，否则它们会占满预算、让模型只会复述代码。
        val text = "注意：此页面显示回退，因为交互式脚本未运行，请启用浏览器脚本。\n" +
            ">>> 17 // 3 #地板分区丢弃小数部分\n" +
            "Python 是一种解释型语言，它可以在多种平台上运行，并且强调代码的可读性。\n" +
            "{\"a\":1,\"b\":[2,3],\"c\":{\"d\":4}}"
        val sents = WebLogic.splitSentences(text)
        assertTrue("代码行不能进材料：$sents", sents.none { it.startsWith(">>>") })
        assertTrue("运行时提示不能进材料：$sents", sents.none { it.contains("脚本未运行") })
        assertEquals("只剩那句正常正文：$sents", 1, sents.size)
    }

    @Test
    fun abbreviationDotsAreNotSentenceEnds() {
        // 实测碎片来源：e.g. 的句点被当句号，一句被劈成「…(e.g」和「5.0, 1.6)…」两截。
        val t = "The integer numbers (e.g. 2, 4, 20) have type int, the ones with a " +
            "fractional part (e.g. 5.0, 1.6) have type float. We will see more later."
        val sents = WebLogic.splitSentences(t)
        assertEquals("缩写句点不能切句：$sents", 2, sents.size)
        assertTrue(
            "第一句必须是完整的：$sents",
            sents[0].contains("(e.g. 5.0, 1.6) have type float."),
        )
    }

    @Test
    fun joinsSoftLineBreaksInsideParagraph() {
        // 实测样本：文档站为了排版在 <p> 里硬折行，一句话被切成三截，
        // 抽出来的材料变成「2、4、20 ）的类型为int ，」这种碎片。
        val html = "<html><body><p>The integer numbers (e.g. 2, 4, 20) have type int,\n" +
            "the ones with a fractional part have type\nfloat. We will see more later.</p></body></html>"
        val text = WebLogic.htmlToText(html)
        assertTrue(
            "段落内的软换行必须收成空格：$text",
            text.contains("have type int, the ones with a fractional part have type float."),
        )
    }

    @Test
    fun blockTagsStillBreakParagraphs() {
        val html = "<html><body><p>第一段足够长的内容在这里。</p><p>第二段足够长的内容在这里。</p></body></html>"
        val text = WebLogic.htmlToText(html)
        assertTrue("块级标签之间必须分段：$text", text.contains("\n"))
    }

    @Test
    fun emptyInputYieldsNothing() {
        assertTrue(WebLogic.keySentences("", "价格", 100).isEmpty())
        assertEquals("", WebLogic.extractKey("", "价格", 100))
    }

    @Test
    fun perPageCharsNeverInvertsItsRange() {
        // 真机 bug（0.1.45）：模型传 chars=100，原实现 (1500/1).coerceIn(220, 200)
        // 上下界反过来，web_digest 抛 IllegalArgumentException，
        // 模型拿到英文异常后回用户一句「材料没提到」—— 看起来像没读到，实际是算崩了。
        for (want in listOf(-1, 0, 1, 50, 100, 200, 400, 700, 9999)) {
            for (pages in 1..5) {
                val per = WebLogic.perPageChars(want, pages, 1500, 200)
                assertTrue("want=$want pages=$pages 算出的 $per 低于下限", per >= 200)
                assertTrue("want=$want pages=$pages 算出的 $per 高于上限", per <= 1500)
            }
        }
    }

    @Test
    fun softNotFoundPageIsRecognizedAsFailure() {
        // 真机样本：模型自己拼了个文章链接，服务端回 404 页面，渲染通道不看状态码，
        // 于是「Not Found / The requested URL was not found」被当正文回灌，
        // 模型最后回用户一句「材料没提到」。
        assertTrue(WebLogic.looksLikeNotFound(
            "404 Not Found",
            "Not Found\nThe requested URL was not found on this server.",
        ))
        assertTrue(WebLogic.looksLikeNotFound("", "页面不存在，请检查链接是否正确。"))
        assertTrue(WebLogic.looksLikeNotFound("404", "您访问的页面已被删除。"))
    }

    @Test
    fun realShortArticleIsNotMistakenFor404() {
        // 判定必须保守：example.com 那种正经短文不能因为短就被判成 404
        assertFalse(WebLogic.looksLikeNotFound(
            "Example Domain",
            "This domain is for use in documentation examples without needing permission. " +
                "Avoid use in operations.",
        ))
        assertFalse(WebLogic.looksLikeNotFound(
            "阮一峰的网络日志",
            "2026年10月16日 » 科技爱好者周刊（第 415 期）：3D 版《看不见的城市》",
        ))
        // 超过 400 字的长文一律不判 404（正文里提到 404 也不该误伤）
        assertFalse(WebLogic.looksLikeNotFound("HTTP 状态码详解", "404 ".repeat(200)))
    }

    @Test
    fun navBarIsNoiseButRealListLineIsNot() {
        // 真机样本：焦点词「新闻」落在导航条里，导航条靠命中焦点词拿了最高分，
        // 把真正的正文第一段挤掉，材料第一行变成「热点追踪 | 中新真探 | …」。
        assertTrue(
            "竖线串起来的栏目条必须当噪音丢掉",
            WebLogic.looksLikeNoise("热点追踪 | 中新真探 | 新闻浮世绘 | 中新法治 | 第一现场"),
        )
        // 但正常的一行（哪怕带书名号、日期、一个 »）不能误杀：
        // 阮一峰博客列表就是这么写的，它是有用内容。
        assertFalse(
            "带一个 » 的正常标题不能当导航条丢掉",
            WebLogic.looksLikeNoise("2026年10月16日 » 科技爱好者周刊（第 415 期）：3D 版《看不见的城市》"),
        )
        assertFalse(
            "正文段落不能被误判",
            WebLogic.looksLikeNoise("临近双11，打开购物软件开始搜索想要的商品，跳出来不少同类产品，买哪个？"),
        )
    }

    @Test
    fun perPageCharsGivesTheModelWhatItAskedFor() {
        // 篇数不多时，用户/模型要多少给多少（钳在 200..700）
        assertEquals(400, WebLogic.perPageChars(400, 1, 1500, 200))
        assertEquals(700, WebLogic.perPageChars(700, 1, 1500, 200))
        // 篇数多到预算不够分：不能低于下限，否则某篇连一句完整的都没有
        assertEquals(300, WebLogic.perPageChars(700, 5, 1500, 200))
        assertEquals(200, WebLogic.perPageChars(400, 20, 1500, 200))
    }
}
