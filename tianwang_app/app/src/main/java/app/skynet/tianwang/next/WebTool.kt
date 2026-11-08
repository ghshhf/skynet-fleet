package app.skynet.tianwang.next

import java.net.URLEncoder
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject

/** 一条搜索结果。 */
data class SearchHit(val title: String, val url: String, val snippet: String)

/**
 * 联网工具的纯逻辑层：URL 组装、结果页解析、正文抽取。
 *
 * 全部是**不碰 Android 的纯函数** —— 网页结构会变，这块必须被单测钉死，
 * 否则改一次线上就静默失效一次，而且失效时看起来像「没搜到」而不是「坏了」。
 */
object WebLogic {

    private val BLOCK = Regex("<li[^>]*class=\"[^\"]*b_algo[^\"]*\"", RegexOption.IGNORE_CASE)
    private val H2_LINK = Regex(
        "<h2[^>]*>\\s*<a[^>]*?href=\"([^\"]+)\"[^>]*>([\\s\\S]*?)</a>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    private val PARA = Regex(
        "<p[^>]*>([\\s\\S]*?)</p>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    private val TAG = Regex("<[^>]+>")

    /**
     * 整块删除：这些标签里的内容不该出现在正文里。
     *
     * `pre` 必须在内：文档站的代码块是 `<pre>`，抽进来全是 `>>> 17/3`、
     * `spam = 1`。小模型读不懂代码，这些行还会因为「含数字」在打分里胜出，
     * 把真正的讲解句全挤掉 —— 实测 Python 教程就是这样整篇变成代码片段。
     */
    private val DROP_BLOCK = Regex(
        "<(script|style|noscript|svg|iframe|head|nav|footer|form|aside|template|pre)\\b[^>]*>[\\s\\S]*?</\\1>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    /** 块级标签换成换行，保住段落结构。 */
    private val BLOCK_TAG = Regex(
        "</?(p|div|br|li|tr|td|h[1-6]|section|article|header|blockquote|pre|hr)\\b[^>]*>",
        RegexOption.IGNORE_CASE,
    )
    private val ARTICLE = Regex(
        "<article\\b[^>]*>([\\s\\S]*?)</article>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    private val MAIN = Regex(
        "<main\\b[^>]*>([\\s\\S]*?)</main>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    private val BODY = Regex(
        "<body\\b[^>]*>([\\s\\S]*?)</body>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )

    private val ENTITY = Regex("&(#[xX]?[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]{1,31});")

    private val NAMED = mapOf(
        "amp" to "&", "lt" to "<", "gt" to ">", "quot" to "\"", "apos" to "'",
        "nbsp" to " ", "mdash" to "—", "ndash" to "–", "hellip" to "…",
        "middot" to "·", "ldquo" to "“", "rdquo" to "”", "lsquo" to "‘",
        "rsquo" to "’", "laquo" to "«", "raquo" to "»", "copy" to "©",
        "reg" to "®", "trade" to "™", "times" to "×", "divide" to "÷",
        "deg" to "°", "plusmn" to "±", "permil" to "‰", "euro" to "€",
        "pound" to "£", "yen" to "¥", "sect" to "§", "para" to "¶",
    )

    /** 搜一句要用的地址。`mkt=zh-CN` 决定返回中文结果，不带会跑到英文站。 */
    fun searchUrl(query: String, count: Int): String {
        val q = URLEncoder.encode(query, "UTF-8")
        val n = count.coerceIn(1, 15)
        return "https://cn.bing.com/search?q=$q&count=$n&mkt=zh-CN"
    }

    /** 剥标签 + 解实体 + 收空白，得到一行干净文字。 */
    fun inlineText(raw: String): String =
        decodeEntities(TAG.replace(raw, " ")).replace(Regex("\\s+"), " ").trim()

    /**
     * 解析 Bing 结果页。结构与样本一致时返回条目；结构变了就返回空表，
     * 由上层显式报「没解析到结果」，**绝不返回编造的条目**。
     */
    fun parseBingResults(html: String, limit: Int): List<SearchHit> {
        if (html.isBlank()) return emptyList()
        val parts = BLOCK.split(html)
        if (parts.size < 2) return emptyList()
        val out = ArrayList<SearchHit>(limit)
        for (i in 1 until parts.size) {
            if (out.size >= limit) break
            val block = parts[i]
            val h2 = H2_LINK.find(block) ?: continue
            val url = decodeEntities(h2.groupValues[1]).trim()
            val title = inlineText(h2.groupValues[2])
            if (url.isBlank() || title.isBlank()) continue
            // 只留 http(s)，挡掉 javascript: / data: 这类脏链接
            if (!url.startsWith("http://") && !url.startsWith("https://")) continue
            // Bing 的跳转包装：模型拿它去 web_read 会绕一圈，而且经常失效
            if (url.contains("bing.com/ck/")) continue
            // 超长 URL 基本都是跟踪参数，模型抄不全反而读不了 —— 宁可少给一条
            if (url.length > 200) continue
            val snippet = PARA.find(block)
                ?.let { inlineText(it.groupValues[1]) }
                .orEmpty()
            out.add(SearchHit(title = title, url = url, snippet = snippet))
        }
        return out
    }

    /**
     * 网页 → 正文纯文本。
     *
     * 优先取 `<article>` / `<main>`，但要求它确实有内容（>500 字），
     * 否则回落到 `<body>` —— 有些站把 article 用在卡片列表上，取它会只剩一行标题。
     */
    fun htmlToText(raw: String): String {
        if (raw.isBlank()) return ""
        val candidates = listOf(ARTICLE, MAIN).mapNotNull { re ->
            re.find(raw)?.groupValues?.get(1)
        }
        val chosen = candidates.firstOrNull { it.length > 500 }
            ?: BODY.find(raw)?.groupValues?.get(1)
            ?: raw
        var s = DROP_BLOCK.replace(chosen, " ")
        // 🔴 先收软换行，再打块边界 —— 顺序不能反。
        //    HTML 源码里 `<p>` 内部为了排版会硬折行，这些换行不是句子边界；
        //    先按块标签换成 \n 的话，两者混在一起就分不开了，结果一句话被切成
        //    三四个碎片（「the ones with a fractional part have type」/「float .」），
        //    抽句时它们还会因为短而显得「像是列表项」，把正文挤掉。
        s = s.replace("\r\n", "\n").replace('\r', '\n')
        s = s.replace('\n', ' ')
        s = BLOCK_TAG.replace(s, "\n")
        s = TAG.replace(s, " ")
        s = decodeEntities(s)
        s = s.replace('\u00A0', ' ').replace('\u3000', ' ')
        s = Regex("[ \\t]+").replace(s, " ")
        s = Regex(" *\\n *").replace(s, "\n")
        s = Regex("\\n{3,}").replace(s, "\n\n")
        return dropLeadingNoise(s.trim())
    }

    /**
     * 砍掉开头那段导航噪音。
     *
     * 真实页面（Python 文档就是这样）在正文前有一串「Navigation / index / modules /
     * Theme」这类短行。它们不是正文，却稳稳占掉小模型窗口的前 10%。
     *
     * 判据刻意保守：只有**开头连续出现一串短行**、且砍完还剩大半正文时才动手；
     * 页面本身就很碎（列表页、论坛）时原样返回 —— 宁可不砍，也不误伤。
     */
    fun dropLeadingNoise(text: String): String {
        if (text.length < 500) return text
        val lines = text.split('\n')
        var consumed = 0
        var acc = 0
        var cutAt = -1
        for (i in lines.indices) {
            val line = lines[i].trim()
            consumed += line.length + 1
            // 到达第一个「真正的段落」：连续短行到此结束
            if (line.length >= 40) {
                acc += line.length
                if (acc >= 80) {
                    cutAt = i
                    break
                }
            }
            if (consumed > text.length * 2 / 5) return text
        }
        if (cutAt <= 0) return text
        // 回溯到最近的空行：一刀砍在句子中间会把第一段读成半截。
        // 但最多回望 8 行 —— 整页没有空行分隔时，继续退只会一路退到文件头，
        // 那就从「砍掉一点噪音」变成「什么都没砍」。
        var start = cutAt
        var back = 0
        while (start > 0 && lines[start - 1].isNotBlank() && back < 8) {
            start--
            back++
        }
        if (start == 0) return text
        val rest = lines.drop(start).joinToString("\n").trim()
        // 砍完只剩小半，说明刚才那串不是噪音而是正文本身
        return if (rest.length * 2 < text.length) text else rest
    }

    fun decodeEntities(raw: String): String {
        if (!raw.contains('&')) return raw
        return ENTITY.replace(raw) { m ->
            val body = m.groupValues[1]
            when {
                body.startsWith("#x") || body.startsWith("#X") ->
                    body.drop(2).toIntOrNull(16)?.let { cp(it) } ?: m.value
                body.startsWith("#") ->
                    body.drop(1).toIntOrNull()?.let { cp(it) } ?: m.value
                else -> NAMED[body.lowercase()] ?: m.value
            }
        }
    }

    private fun cp(code: Int): String? =
        if (code in 1..0x10FFFF && (code < 0xD800 || code > 0xDFFF)) {
            String(Character.toChars(code))
        } else {
            null
        }

    /** 域名，用于展示「来自哪个站」，比整条 URL 省 token。 */
    fun hostOf(url: String): String = runCatching {
        val u = java.net.URI(url)
        (u.host ?: "").removePrefix("www.")
    }.getOrDefault("")

    /**
     * 从一段自然语言里抽出链接。
     *
     * 为什么不让模型直接给数组：1.7B 抄 URL 经常多一个引号、少一段路径，
     * 数组 JSON 一坏整条消息就废。让它把带链接的那句话原样贴过来，
     * 由代码按正则捞 —— 捞不到就明确报「没找到链接」，不会猜。
     *
     * 只掐英文句读和全角标点，**不掐半角括号**：维基百科的条目名里就带括号，
     * 掐掉等于把链接改坏。
     */
    fun parseUrls(text: String, max: Int = 5): List<String> {
        if (text.isBlank()) return emptyList()
        val re = Regex("https?://[^\\s\"'<>（）【】\\[\\]，。；、]+", RegexOption.IGNORE_CASE)
        val seen = LinkedHashSet<String>()
        for (m in re.findAll(text)) {
            val u = m.value.trimEnd('.', ',', ';', ':', '!', '?',
                '。', '，', '；', '：', '！', '？', '、', '）', '】')
            if (u.length < 12) continue
            seen.add(u)
            if (seen.size >= max) break
        }
        return seen.toList()
    }

    /**
     * 句子边界。**全部是零宽断言**：切分点不能吃掉字符。
     *
     * 用 `\.` 直接匹配会把句号本身从正文里删掉（Java 的 split 丢弃分隔符），
     * 表现成「每句结尾都没有句号」—— 翻译接口拿到的就是半截句子。
     *
     * 中文句末（。！？；）直接切；英文的 `!?` 必须后面跟空白（否则 `a?x=1` 会被劈断）；
     * 半角句点要求前面不是数字（3.14）且后面是空白。缩写（e.g. / i.e. / U.S.）
     * 在切分前用哨兵字符保护，见 [ABBREV]。
     */
    private val SENT_BREAK = Regex(
        "(?<=[。！？；])|(?<=[!?])(?=\\s)|(?<=[^0-9]\\.)(?=\\s|$)|\\n+",
    )

    /** 缩写句点：`e.g.` `i.e.` `U.S.`。切句前换成哨兵，切完还原。 */
    private val ABBREV = Regex("\\b([A-Za-z])\\.([A-Za-z])\\.")

    private const val SENTINEL = '\u0001'

    /** 代码行/命令行的开头特征。 */
    private val CODE_HINT = Regex(
        "^(>>>|\\.\\.\\.|\\$ |/\\*|//|#include|import |from |def |class |function )",
    )

    /**
     * 明显不是「一句话」的行。
     *
     * 三类：代码行（`>>> 17/3`）、符号密度过高的行（表格/公式/JSON）、
     * 运行时提示（「注意：此页面显示回退，因为交互式脚本未运行」）。
     *
     * 为什么必须在切句时就丢掉：它们会被原样送进翻译接口、再原样出现在材料里，
     * 模型看到一屏代码只会复述代码，用户要的「讲清楚」就没了。
     */
    fun looksLikeNoise(s: String): Boolean {
        if (s.length < 12) return true
        if (CODE_HINT.containsMatchIn(s)) return true
        val symbols = s.count { it in "{}[]<>|=+*/\\_#`;" }
        if (symbols * 5 > s.length) return true
        // 导航/栏目条：用竖线类分隔符串起来的一串短词，比如
        // 「热点追踪 | 中新真探 | 新闻浮世绘 | 中新法治 | 第一现场」。
        //
        // 🔴 真机实测踩过：这类串里恰好含焦点词（用户要「新闻」，串里就有「新闻浮世绘」），
        //    打分时它命中焦点词拿了最高分，把真正的正文第一段压了下去 ——
        //    材料第一行变成导航条，模型只能据此说「报道了国内外热点」这种空话。
        //    正文里不会连用三个竖线，所以这条判据很安全。
        val bars = s.count { it == '|' || it == '｜' || it == '»' }
        if (bars >= 3) return true
        if (bars >= 2) {
            val items = s.split('|', '｜', '»').map { it.trim() }.filter { it.isNotEmpty() }
            if (items.isNotEmpty() && items.sumOf { it.length } / items.size <= 8) return true
        }
        val low = s.lowercase()
        val lead = low.startsWith("注意") || low.startsWith("提示") ||
            low.startsWith("note") || low.startsWith("warning")
        if (lead && (low.contains("脚本") || low.contains("javascript") ||
                low.contains("浏览器") || low.contains("browser") || low.contains("fallback"))
        ) return true
        return false
    }

    /**
     * 切句：翻译和抽句都以「句」为单位。
     *
     * 太短、像代码、像运行时提示的片段直接丢（见 [looksLikeNoise]）；
     * 超长句再按逗号劈开，否则一条 500 字的句子会独占整个预算。
     */
    fun splitSentences(text: String, maxLen: Int = 200): List<String> {
        if (text.isBlank()) return emptyList()
        // 先把 e.g. / i.e. 的句点藏起来，否则它们会被当成句末，一句话从中间劈开。
        val safe = ABBREV.replace(text) { m ->
            "${m.groupValues[1]}$SENTINEL${m.groupValues[2]}$SENTINEL"
        }
        val out = ArrayList<String>()
        for (raw in safe.split(SENT_BREAK)) {
            val s = raw.trim().replace(SENTINEL, '.')
            if (looksLikeNoise(s)) continue
            if (s.length <= maxLen) {
                out.add(s)
                continue
            }
            // 长句按逗号/顿号劈，劈完仍然超长就硬切 —— 宁可断在逗号，不可整条丢弃。
            var buf = StringBuilder()
            for (part in s.split(Regex("(?<=[，,、])"))) {
                if (buf.isNotEmpty() && buf.length + part.length > maxLen) {
                    out.add(buf.toString().trim())
                    buf = StringBuilder()
                }
                buf.append(part)
            }
            val rest = buf.toString().trim()
            if (rest.isNotEmpty()) {
                if (rest.length <= maxLen) out.add(rest)
                else rest.chunked(maxLen).forEach { out.add(it) }
            }
        }
        return out
    }

    /**
     * 把正文切成可直接送翻译接口的段（每段 ≤ [maxLen]）。
     *
     * 公开翻译端点对单次请求有长度上限（实测 450 字左右稳定），
     * 所以按句聚合而不是硬切：切在句子中间，译出来就是半句话。
     */
    fun segments(text: String, maxLen: Int = 450, limit: Int = 24): List<String> {
        val sents = splitSentences(text)
        val out = ArrayList<String>()
        var buf = StringBuilder()
        for (s in sents) {
            if (buf.isNotEmpty() && buf.length + s.length > maxLen) {
                out.add(buf.toString())
                buf = StringBuilder()
                if (out.size >= limit) return out
            }
            buf.append(s)
        }
        if (buf.isNotEmpty() && out.size < limit) out.add(buf.toString())
        return out
    }

    /**
     * 软 404 判定：很多站把「找不到」渲染成一个正常的 200 页面，光看状态码判不出来。
     *
     * 为什么必须判 —— 真机实测：1.7B 读一篇博客列表时自己拼了个文章链接
     * （`…/2026-10-16-tech-weekly-415.html`），服务端回 404 页面，
     * 而渲染通道不看状态码，于是「Not Found / The requested URL was not found」
     * 被当成正文回灌。模型把它当材料，最后跟用户说「材料没提到」。
     * **工具把失败包装成成功，比直接失败更有害**：模型没有任何线索去换路。
     *
     * 判定刻意保守：正文短 **且** 标题/开头出现明确的找不到字样才算，
     * 免得把一篇正常短文误判成 404。
     */
    fun looksLikeNotFound(title: String, body: String): Boolean {
        if (body.length > 400) return false
        val head = (title + "\n" + body.take(240)).lowercase()
        return NOT_FOUND_HINTS.any { head.contains(it) }
    }

    private val NOT_FOUND_HINTS = listOf(
        "404", "not found", "page not found", "does not exist", "no longer available",
        "页面不存在", "页面找不到了", "找不到页面", "页面未找到", "您访问的页面",
        "無法找到", "无法找到该页",
    )

    /**
     * 每篇保留多少字：把「模型想要的」和「总预算 / 篇数」收成一个安全值。
     *
     * 抽成纯函数是有原因的 —— 这里踩过一次真机 bug：原实现写的是
     * `(total / pages).coerceIn(220, want)`，下界写死 220、上界却来自参数，
     * 模型传 `chars=100` 时 want 被钳到 200，于是 `coerceIn(220, 200)` 上下界反过来，
     * 整个工具抛 `Cannot coerce value to an empty range`。模型看不懂英文异常，
     * 转头跟用户说「材料没提到」—— 用户看到的是「它没读到」，实际是代码算崩了。
     *
     * @param want 调用方想要的字数（可能离谱，先钳到 [floor]..[cap]）
     * @param pages 篇数
     * @param total 材料总预算
     */
    fun perPageChars(
        want: Int,
        pages: Int,
        total: Int = 1500,
        floor: Int = 200,
        cap: Int = 700,
    ): Int {
        val w = want.coerceIn(floor, cap)
        val share = total / pages.coerceAtLeast(1)
        // 取大：篇数多到预算不够分时，宁可总量超一点，也不能让某篇连一句完整的都没有。
        return maxOf(floor, minOf(w, share))
    }

    /**
     * 抽取式要点：**只挑原文里已有的句子，不改写、不生成**。
     *
     * 为什么不交给模型总结：1.7B 读 8000 字再总结，实测会漏掉大半并掺入原文没有的
     * 说法。机械活（挑句）交给代码是确定性的，模型只负责最后把这几条串成话。
     *
     * 打分三档：命中关注词最高；带数字/日期/百分比的句子次之（事实密度高）；
     * 靠前的句子再加一点（导语通常就是结论）。选完**按原文顺序**输出，
     * 否则一篇文章会被讲成前后颠倒。
     */
    fun keySentences(text: String, focus: String, budget: Int): List<String> {
        if (text.isBlank()) return emptyList()
        val sents = splitSentences(text)
        if (sents.isEmpty()) return listOf(text.take(budget).trim())
        val words = focus.lowercase()
            .split(Regex("[\\s,，、;；]+"))
            .filter { it.length >= 2 }
        val scored = sents.mapIndexed { i, s ->
            val low = s.lowercase()
            var sc = 0
            if (words.isNotEmpty()) {
                val hit = words.count { low.contains(it) }
                if (hit > 0) sc += 60 + hit * 10
            }
            // 完整句比碎片可信：以句末标点收尾的才是成句的话。
            if (s.last() in "。！？.!?") sc += 10
            if (s.any { it.isDigit() }) sc += 6
            // 30～180 字是「一句有内容的话」的典型区间；太短没信息，太长读不动。
            if (s.length in 30..180) sc += 8
            // 前四分之一算导语区
            if (i * 4 < sents.size) sc += 4
            Triple(i, s, sc)
        }
        val picked = ArrayList<Triple<Int, String, Int>>()
        var used = 0
        for (t in scored.sortedByDescending { it.third }) {
            if (used + t.second.length > budget) continue
            picked.add(t)
            used += t.second.length
            if (used >= budget * 9 / 10) break
        }
        if (picked.isEmpty()) return listOf(text.take(budget).trim())
        return picked.sortedBy { it.first }.map { it.second }
    }

    /** 上面那版的整段形式，给「只要一段话」的调用方。 */
    fun extractKey(text: String, focus: String, budget: Int): String =
        keySentences(text, focus, budget).joinToString("\n")
}

/**
 * 出站联网：手机第一个「能看外面」的能力。
 *
 * 为什么不走 PhoneSelf：联网不依赖无障碍通道，不该被无障碍开关挡住
 * （与网盘聚合同一个理由）。
 *
 * 为什么不用搜索 API：不引第三方 key —— 注册、额度、失效都是长期负担。
 * 改用 Bing 结果页解析，实测手机直连 7.66ms、中英文都稳定回 10 条。
 * **解析不出来就显式报失败，绝不返回编造条目。**
 *
 * 两个工具都只读（sideEffect = false），断网或解析失败也不会污染任何状态。
 */
object WebTool {
    private const val UA =
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    /** 单页最多读 8MB：超过基本是视频/图片直链，读了也用不上，还白烧流量。 */
    private const val MAX_BYTES = 8L * 1024 * 1024

    private val TITLE = Regex(
        "<title[^>]*>([\\s\\S]*?)</title>",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )

    private val client = OkHttpClient.Builder()
        .connectTimeout(12, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .callTimeout(35, TimeUnit.SECONDS)
        .followRedirects(true)
        .followSslRedirects(true)
        .build()

    val tools: List<McpTool> = listOf(
        McpTool(
            "web_search",
            "联网搜索网页，返回标题、链接和摘要。查新闻、天气、价格、文档、事实这类本机没有的信息时用它。",
            obj(
                mapOf(
                    "query" to prop("string", "要搜的关键词"),
                    "count" to prop("integer", "返回几条，默认 5，上限 10"),
                ),
                listOf("query"),
            ),
        ),
        McpTool(
            "web_read",
            "打开一个网址，抽取正文纯文本；长文可用 offset 续读。搜索结果里要深入了解哪条，就用它打开那条。",
            obj(
                mapOf(
                    "url" to prop("string", "完整网址，必须 http:// 或 https:// 开头"),
                    "offset" to prop("integer", "从正文第几个字符开始读，默认 0"),
                    "limit" to prop("integer", "本次最多返回多少字符，默认 1500，上限 4000"),
                    "render" to prop("boolean", "直连读不到正文时设 true，用无头浏览器渲染后再读（更慢）"),
                ),
                listOf("url"),
            ),
        ),
        McpTool(
            "web_digest",
            "把网址直接交给它自己读完再回答：自动抓正文、外文自动翻成中文、按关注点抽取要点。" +
                "用户说「这几篇给我讲讲」「这链接说了啥」「帮我看看这篇文章」时用它。" +
                "能一次处理多个链接（最多 5 个）；**单篇也直接用它，不用先 web_read**——" +
                "它抓完就翻好中文并挑出要点，反复 web_read 只会多花几步。",
            obj(
                mapOf(
                    "urls" to prop("string", "一个或多个网址，换行、空格或逗号分隔"),
                    "focus" to prop("string", "想重点看什么，如「价格」「风险」「发布时间」；不填按全文重要性抽"),
                    "chars" to prop("integer", "每篇保留多少字，默认 400，范围 200～700"),
                    "render" to prop("boolean", "直连读不到正文时设 true，用无头浏览器渲染后再读（更慢）"),
                ),
                listOf("urls"),
            ),
        ),
    )

    suspend fun run(name: String, args: JSONObject): ToolResult = withContext(Dispatchers.IO) {
        try {
            when (name) {
                "web_search" -> search(args)
                "web_read" -> read(args)
                "web_digest" -> digest(args)
                else -> ToolResult(false, err = "未知联网工具：$name")
            }
        } catch (t: Throwable) {
            ToolResult(false, err = netError(t))
        }
    }

    private fun search(args: JSONObject): ToolResult {
        val q = args.optString("query").trim()
        if (q.isBlank()) return ToolResult(false, err = "缺 query")
        val count = args.optInt("count", 5).coerceIn(1, 10)
        val url = WebLogic.searchUrl(q, count)
        val html = fetch(url).getOrElse {
            return ToolResult(false, err = "搜索请求失败：${it.message ?: it.javaClass.simpleName}")
        }
        val hits = WebLogic.parseBingResults(html, count)
        if (hits.isEmpty()) {
            return ToolResult(
                false,
                err = "搜「$q」没解析到结果（页面结构可能变了，或这个词确实没有结果）",
            )
        }
        val text = buildString {
            append("搜「").append(q).append("」得到 ").append(hits.size).append(" 条：\n")
            hits.forEachIndexed { i, h ->
                append(i + 1).append(". ").append(clip(h.title, 60)).append('\n')
                // URL 要给完整的：截断后模型照着抄会直接读不到
                append("   ").append(h.url).append('\n')
                if (h.snippet.isNotBlank()) append("   ").append(clip(h.snippet, 120)).append('\n')
            }
        }
        return ToolResult(true, text = text)
    }

    private suspend fun read(args: JSONObject): ToolResult {
        val url = args.optString("url").trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            return ToolResult(false, err = "url 必须是 http:// 或 https:// 开头")
        }
        val offset = args.optInt("offset", 0).coerceAtLeast(0)
        val limit = args.optInt("limit", 1500).coerceIn(200, 4000)
        val render = args.optBoolean("render", false)
        val page = loadPage(url, render)
        if (page.err != null) return ToolResult(false, err = page.err)
        val body = page.body
        if (offset >= body.length) {
            return ToolResult(false, err = "offset=$offset 已超过正文长度 ${body.length}")
        }
        val end = minOf(offset + limit, body.length)
        val piece = body.substring(offset, end)
        val head = buildString {
            if (page.title.isNotBlank()) append("标题：").append(clip(page.title, 90)).append('\n')
            append("地址：").append(clip(url, 120))
            if (page.rendered) append("（浏览器渲染）")
            append('\n')
        }
        val tail = if (end < body.length) {
            "\n\n[已显示 $offset–$end / 共 ${body.length} 字；续读加 offset=$end]"
        } else {
            "\n\n[正文共 ${body.length} 字，已读完]"
        }
        return ToolResult(true, text = head + "正文：\n" + piece + tail)
    }

    /** 一篇文章抓下来、抽成正文后的样子。`err != null` 表示这篇没救回来。 */
    private class Page(
        val url: String,
        val title: String,
        val body: String,
        val rendered: Boolean,
        val err: String? = null,
    )

    /**
     * 抓一页，直连优先、浏览器兜底。
     *
     * 判「没救」的不是 HTTP 状态，而是**正文长度**：SPA 的首包会 200 但只有几百字
     * 导航，这种在用户眼里等于没读到。所以正文 <300 字且有渲染权限时就换通道。
     */
    private suspend fun loadPage(url: String, allowRender: Boolean): Page {
        val htmlResult = fetch(url)
        var html = htmlResult.getOrDefault("")
        var rendered = false
        var body = if (html.isBlank()) "" else WebLogic.htmlToText(html)
        if (body.length < 300 && allowRender) {
            val r = WebRender.html(url)
            val h2 = r.getOrNull()
            if (h2 != null) {
                val b2 = WebLogic.htmlToText(h2)
                if (b2.length > body.length) {
                    html = h2
                    body = b2
                    rendered = true
                }
            } else if (html.isBlank()) {
                return Page(url, "", "", false,
                    "打开 $url 失败：${r.exceptionOrNull()?.message ?: "未知原因"}")
            }
        }
        if (html.isBlank()) {
            return Page(url, "", "", false,
                "打开 $url 失败：${htmlResult.exceptionOrNull()?.message ?: "未知原因"}")
        }
        val title = TITLE.find(html)?.let { WebLogic.inlineText(it.groupValues[1]) }.orEmpty()
        if (body.isBlank()) {
            return Page(url, title, "", rendered,
                "打开 $url 没抽到正文（可能是纯前端渲染的页面，可加 render=true 再试）")
        }
        // 状态码 200 但内容是「找不到」：必须当失败报，不能把 404 页面当材料。
        if (WebLogic.looksLikeNotFound(title, body)) {
            return Page(url, title, body, rendered,
                "打开 $url 得到的是「找不到页面」（${clip(title.ifBlank { body.take(30) }, 40)}）。" +
                    "链接可能拼错了：不要自己拼 URL，只用用户给的、或搜索结果里原样出现的链接。")
        }
        return Page(url, title, body, rendered)
    }

    /** 单次回灌给模型的材料总预算：与 [LocalAgent] 的截断线对齐，避免刚说完就被砍。 */
    private const val DIGEST_TOTAL = 1500

    /**
     * 每篇至少保留多少字。
     *
     * 两个作用，缺一个就会出问题：
     *  1. 模型传 `chars=50` 这类过小的值时不至于只剩半句；
     *  2. 和 [DIGEST_TOTAL] 的均分结果取大 —— 篇数多到预算不够分时，
     *     宁可总量超一点，也不能让某篇连一个完整句子都没有。
     */
    private const val DIGEST_MIN_PER = 200

    private const val DIGEST_MAX_PAGES = 5

    /**
     * 多篇 → 一份材料：抓正文 → 判语种 → 抽句 → 只把抽出的句子翻成中文。
     *
     * 为什么不整篇翻译：一篇 8000 字的英文文章按 450 字一段要 18 次请求，
     * 而最后给模型看的只有 400 字要点。抽完再翻，请求数从 18 降到 3~4，
     * 时间从十几秒压到一两秒，翻译通道也不容易被限流。
     *
     * 每篇的失败**单独记账**，一篇挂了不影响其它篇 —— 用户给了五条链接，
     * 四条有结果也比整批失败有用。
     */
    private suspend fun digest(args: JSONObject): ToolResult = coroutineScope {
        val urls = WebLogic.parseUrls(args.optString("urls"), DIGEST_MAX_PAGES)
        if (urls.isEmpty()) {
            return@coroutineScope ToolResult(false, err =
                "没从参数里找到链接。请把 http:// 或 https:// 开头的网址原样贴进来（最多 5 个）")
        }
        val focus = args.optString("focus").trim()
        val render = args.optBoolean("render", false)
        // 篇数多就自动压薄：五篇各 300 字，比五篇各 700 字被截断强。
        // 上下界打架会导致整批失败，算法全部收在 WebLogic.perPageChars 里（有单测守着）。
        val per = WebLogic.perPageChars(
            want = args.optInt("chars", 400),
            pages = urls.size,
            total = DIGEST_TOTAL,
            floor = DIGEST_MIN_PER,
        )

        val pages = urls.map { u -> async { loadPage(u, render) } }.awaitAll()

        val ok = pages.count { it.err == null }
        val sb = StringBuilder()
        sb.append("读了 ").append(urls.size).append(" 篇，成功 ").append(ok).append(" 篇")
        sb.append("（每篇摘 ").append(per).append(" 字以内）\n")

        pages.forEachIndexed { i, p ->
            sb.append("\n【").append(i + 1).append("】")
            if (p.err != null) {
                sb.append("失败：").append(p.err).append('\n')
                return@forEachIndexed
            }
            val keys = WebLogic.keySentences(p.body, focus, per)
            // 真机可查：正文里还剩几行代码、切出几句、第一句长什么样。
            // 抽取质量出问题时，没有这行只能靠猜（分不清是抓错了、还是切碎了）。
            val codeLines = p.body.split('\n').count { it.trimStart().startsWith(">>>") }
            android.util.Log.i("TianwangDigest",
                "url=${p.url} body=${p.body.length} codeLines=$codeLines " +
                    "sents=${keys.size} first=${keys.firstOrNull()?.take(50)}")
            val english = ArticleTranslator.isEnglish(keys.take(12))
            var lines = keys
            var translated = false
            if (english && keys.isNotEmpty()) {
                // 🔴 缓存键必须带内容指纹。
                //    翻译缓存是「url → {序号: 译文}」，抽句规则一改，同样的 url 会抽出
                //    完全不同的句子；只按 url 命中就会把旧译文贴到新句子上 ——
                //    实测出现过「正文第一句是讲整数类型，译文却是上一版抽到的代码行」。
                //    这种错误看起来像模型胡说，实际是缓存串了。
                val fingerprint = Integer.toHexString(keys.joinToString("\u0000").hashCode())
                val tr = ArticleTranslator.translate("${p.url}#digest:$fingerprint", keys)
                tr.getOrDefault(emptyMap()).let { m ->
                    if (m.size * 2 >= keys.size) {
                        lines = keys.mapIndexed { k, s -> m[k] ?: s }
                        translated = true
                    }
                }
            }
            sb.append(clip(p.title.ifBlank { WebLogic.hostOf(p.url) }, 80))
            sb.append("（").append(WebLogic.hostOf(p.url))
            if (p.rendered) sb.append("，浏览器渲染")
            if (english) sb.append(if (translated) "，外文已译成中文" else "，外文（翻译暂不可达）")
            sb.append("，正文 ").append(p.body.length).append(" 字）\n")
            lines.forEach { sb.append("- ").append(it).append('\n') }
        }
        return@coroutineScope ToolResult(true, text = sb.toString().trim())
    }

    private fun fetch(url: String): Result<String> = runCatching {
        val req = Request.Builder()
            .url(url)
            .header("User-Agent", UA)
            .header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            .header("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.6")
            .build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) throw java.io.IOException("HTTP ${resp.code}")
            val len = resp.body?.contentLength() ?: -1
            if (len > MAX_BYTES) {
                throw java.io.IOException("页面太大（${len / 1024 / 1024}MB），不读")
            }
            resp.body?.string().orEmpty()
        }
    }

    /** 截断一律补省略号：模型看到「…」才知道后面还有，不会以为那就是全部。 */
    private fun clip(s: String, n: Int): String =
        if (s.length <= n) s else s.take(n - 1) + "…"

    private fun netError(t: Throwable): String = when (t) {
        is java.net.UnknownHostException ->
            "域名解析不了（${t.message}）：手机可能没联网，或该站在当前网络不可达"
        is java.net.SocketTimeoutException -> "请求超时：网络慢或对方没响应"
        is javax.net.ssl.SSLException -> "TLS 握手失败：${t.message}"
        is java.io.IOException -> "网络错误：${t.message}"
        else -> "${t.javaClass.simpleName}: ${t.message}"
    }

    private fun prop(type: String, desc: String): JSONObject =
        JSONObject().put("type", type).put("description", desc)

    private fun obj(props: Map<String, JSONObject>, required: List<String>): JSONObject {
        val p = JSONObject()
        props.forEach { (k, v) -> p.put(k, v) }
        return JSONObject()
            .put("type", "object")
            .put("properties", p)
            .put("required", JSONArray(required))
            .put("additionalProperties", false)
    }
}
