package app.skynet.tianwang.next

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** 翻译按钮相关纯逻辑：自动判定、脚本自洽。不碰网络、不碰 Android。 */
class TranslateLogicTest {

    @Test
    fun englishPageAutoTranslates() {
        val en = listOf(
            "A practical guide to building fast applications on modern hardware",
            "The team announced a new release with better memory management",
            "Readers can download the source code from the public repository",
        )
        assertTrue(ArticleTranslator.isEnglish(en))
    }

    @Test
    fun chineseAndShortSamplesAreNotAutoTranslated() {
        val zh = listOf(
            "这是一篇关于本地模型部署的技术文章，介绍了常用工具与配置方法",
            "第二段内容继续说明推理速度优化的若干经验与注意事项",
        )
        assertFalse(ArticleTranslator.isEnglish(zh))
        assertFalse(ArticleTranslator.isEnglish(listOf("Hi")))
        assertFalse(ArticleTranslator.isEnglish(emptyList()))
    }

    @Test
    fun collectScriptInjectsAndCollectsInOneCall() {
        val inject = ArticleTranslator.injectScript()
        val collect = ArticleTranslator.collectScript()
        assertTrue(inject.contains("window.__twApply"))
        assertTrue(inject.contains("window.__twRestore"))
        assertTrue(inject.contains("window.__twCollect=function"))
        // 注入与收集合成一次调用，避免「注入还没跑完就收集」的竞态
        assertTrue(collect.startsWith(inject))
        assertTrue(collect.trimEnd().endsWith("window.__twCollect();"))
        assertEquals(1, collect.split("window.__twCollect();").size - 1)
    }

    @Test
    fun manualClickIsNotBlockedByAutoDecision() {
        // 手动路径不做语言判定：中文页面点「中文」同样该真的去翻一次。
        // 这里锁住判定的边界，防止以后又把 isEnglish 写成手动按钮的前置闸门。
        val mixed = listOf(
            "Mixed 中英 content with enough latin letters to pass the threshold here",
        )
        assertTrue(ArticleTranslator.isEnglish(mixed))
        val zh = List(6) { "中文内容占据绝大多数，只有少量 latin 词汇" }
        assertFalse(ArticleTranslator.isEnglish(zh))
    }

    @Test
    fun arabicTextIsNotEnglish() {
        // 实测事故：WebView 没带 Accept-Language，example.com 回了阿拉伯语版，
        // 被判定成「英文」硬走 en→zh 通道，结果整段阿语原样留在材料里。
        val ar = listOf(
            "هذا النطاق مُخصص للاستخدام في أمثلة التوثيق دون الحاجة إلى إذن",
            "هذه ليست خدمة، يُرجى تجنب الاعتماد عليها لأغراض الاختبار والمراقبة",
        )
        assertFalse(ArticleTranslator.isEnglish(ar))
    }
}
