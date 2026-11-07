package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.concurrent.TimeUnit

/**
 * 手机自己联网抓全球资源。
 *
 * 手机有网是默认前提：App 自己发请求、自己解析、自己写进本机库，不经过任何
 * 必须先装好、先开着的软件。源都是公开 JSON / RSS / Atom / 开源清单，不需要 key。
 * 电脑上那份「直连全球各种资源」的清单在这里以同样的形式落地，手机不是只盯六个站。
 *
 * 抓取并发（网络是瓶颈），入库串行（同一个 SQLite 连接的事务不能并发开）。
 * 单个源挂了只记它自己一行，绝不因为一个站不通就整页空白。
 */
object ResourceCrawler {
    data class Hit(
        val title: String,
        val url: String,
        val summary: String = "",
        val kind: String = "article",
        val tags: List<String> = emptyList(),
        val quality: Double = 0.55,
        val externalId: String = "",
        val publishedAt: Double = 0.0,
        /** 抓取阶段顺手抽到的封面图（enclosure / media:content / 正文首图 / 大站规则封面）。 */
        val image: String = "",
        /** 视频地址（enclosure type=video / 视频站链接），有值即视为视频资源。 */
        val video: String = "",
    )

    private class Source(val id: String, val name: String, val fetch: () -> List<Hit>)

    private class Fetched(val s: Source, val hits: List<Hit>?, val err: String?)

    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(12, TimeUnit.SECONDS)
        .followRedirects(true)
        .build()

    private val SOURCES = listOf(
        // ---- 技术社区 / 代码 ----
        Source("hn-front", "Hacker News", ::hn),
        Source("hn-new", "HN 新帖") {
            jsonHits("https://hn.algolia.com/api/v1/search_by_date?tags=story&hitsPerPage=25", "hn")
        },
        Source("github-repos", "GitHub 热门", ::github),
        Source("github-new", "GitHub 新项目") {
            githubAt("https://api.github.com/search/repositories" +
                "?q=stars:%3E100+pushed:%3E2026-01-01&sort=stars&order=desc&per_page=20")
        },
        Source("rss-lobsters", "Lobsters") { rss("社区", "https://lobste.rs/rss") },
        Source("rss-devto", "DEV Community") { rss("开发", "https://dev.to/feed") },
        Source("rss-hn", "HN RSS") { rss("hn", "https://hnrss.org/frontpage") },

        // ---- AI / 学术 ----
        Source("hf-models", "Hugging Face 镜像", ::hf),
        Source("hf-papers", "HF Daily Papers") {
            rss("论文", "https://huggingface.co/papers/rss")
        },
        Source("arxiv-ai", "arXiv AI") {
            rss("论文", "https://export.arxiv.org/api/query" +
                "?search_query=cat:cs.AI&start=0&max_results=20&sortBy=submittedDate&sortOrder=descending")
        },
        Source("arxiv-cl", "arXiv CL") {
            rss("论文", "https://export.arxiv.org/api/query" +
                "?search_query=cat:cs.CL&start=0&max_results=20&sortBy=submittedDate&sortOrder=descending")
        },

        // ---- 全球媒体 ----
        Source("rss-bbc", "BBC 科技") { rss("国际", "https://feeds.bbci.co.uk/news/technology/rss.xml") },
        Source("rss-verge", "The Verge") { rss("国际", "https://www.theverge.com/rss/index.xml") },
        Source("rss-ars", "Ars Technica") {
            rss("国际", "https://feeds.arstechnica.com/arstechnica/technology-lab")
        },
        Source("rss-techcrunch", "TechCrunch") { rss("国际", "https://techcrunch.com/feed/") },
        Source("rss-wired", "WIRED") { rss("国际", "https://www.wired.com/feed/rss") },
        Source("rss-mittr", "MIT Tech Review") {
            rss("国际", "https://www.technologyreview.com/feed/")
        },
        Source("rss-guardian", "Guardian 科技") {
            rss("国际", "https://www.theguardian.com/technology/rss")
        },
        Source("rss-nature", "Nature") { rss("科研", "https://www.nature.com/nature.rss") },
        Source("rss-producthunt", "Product Hunt") {
            rss("产品", "https://www.producthunt.com/feed")
        },

        // ---- 中文 ----
        Source("rss-sspai", "少数派") { rss("少数派", "https://sspai.com/feed") },
        Source("rss-ruanyifeng", "阮一峰的网络日志") {
            rss("博客", "https://www.ruanyifeng.com/blog/atom.xml")
        },
        Source("rss-solidot", "Solidot") { rss("科技", "https://www.solidot.org/index.rss") },
        Source("rss-qbitai", "量子位") { rss("AI", "https://www.qbitai.com/feed") },
        Source("rss-jiqizhixin", "机器之心") { rss("AI", "https://www.jiqizhixin.com/rss") },
        Source("rss-infoq", "InfoQ 中文") { rss("开发", "https://www.infoq.cn/feed") },

        // ---- 开源清单：电脑上那批 awesome 种子的链接，手机一样自己抓 ----
        Source("awesome-lists", "开源清单", ::awesome),

        // ---- 手机软件 / APK：F-Droid 开源市场 RSS，自带截图/描述，归类「手机软件」 ----
        Source("fdroid", "F-Droid") { rss("手机软件", "https://f-droid.org/feed/") },
        Source("fdroid-zh", "F-Droid 中文") { rss("手机软件", "https://f-droid.org/feed?lang=zh") },
        // ---- 更多市场源：APKMirror（WordPress feed，稳）/ IzzyOnDroid（F-Droid 第三方仓，稳）----
        // 这两个都是应用市场生态、自带 RSS，归类「手机软件」，让分类更丰富。
        // CoolAPK 无公开 RSS/API、易被反爬污染失败日志，暂不加；要加可后续写网页解析版。
        Source("apkmirror", "APKMirror") { rss("手机软件", "https://www.apkmirror.com/feed/") },
        Source("izzyon", "IzzyOnDroid") { rss("手机软件", "https://www.izzyonandroid.de/category/apps/feed/") },
    )

    /** 电脑侧 resource_finder 里的种子清单，抽里面的外链就是一大批资源。 */
    private val AWESOME = listOf(
        "https://raw.githubusercontent.com/josephmisiti/awesome-machine-learning/master/readme.md",
        "https://raw.githubusercontent.com/awesomedata/awesome-public-datasets/master/README.md",
        "https://raw.githubusercontent.com/eugeneyan/awesome-llm.md/master/README.md",
        "https://raw.githubusercontent.com/awesome-selfhosted/awesome-selfhosted/master/README.md",
        "https://raw.githubusercontent.com/keon/awesome-nlp/master/README.md",
    )

    private fun get(url: String): String {
        val req = Request.Builder().url(url)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 13) TianwangNext/1.0")
            .header("Accept", "*/*")
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) throw IOException("HTTP ${r.code} $url")
            return r.body?.string().orEmpty()
        }
    }

    // ---------------------------------------------------------------- 各源解析

    private fun hn(): List<Hit> =
        jsonHits("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=25", "hn")

    /** Algolia 的 search / search_by_date 同形状，一个函数通吃。 */
    private fun jsonHits(url: String, tag: String): List<Hit> {
        val arr = JSONObject(get(url)).optJSONArray("hits") ?: return emptyList()
        val out = ArrayList<Hit>()
        for (i in 0 until arr.length()) {
            val h = arr.optJSONObject(i) ?: continue
            val title = h.optString("title").ifBlank { h.optString("story_title") }
            if (title.isBlank()) continue
            val oid = h.optString("objectID")
            val link = h.optString("url").ifBlank {
                "https://news.ycombinator.com/item?id=$oid"
            }
            out.add(
                Hit(
                    title = title, url = link,
                    summary = ResourceLogic.cleanSummary(stripTags(h.optString("story_text"))),
                    kind = "story", tags = listOf(tag, "技术"),
                    quality = 0.60 + (h.optInt("points", 0) / 1000.0).coerceAtMost(0.30),
                    externalId = oid, publishedAt = isoEpoch(h.optString("created_at")),
                )
            )
        }
        return out
    }

    private fun github(): List<Hit> = githubAt(
        "https://api.github.com/search/repositories" +
            "?q=stars:%3E500&sort=updated&order=desc&per_page=25")

    private fun githubAt(url: String): List<Hit> {
        val arr = JSONObject(get(url)).optJSONArray("items") ?: return emptyList()
        val out = ArrayList<Hit>()
        for (i in 0 until arr.length()) {
            val r = arr.optJSONObject(i) ?: continue
            val full = r.optString("full_name")
            val link = r.optString("html_url")
            if (full.isBlank() || link.isBlank()) continue
            val tags = ArrayList<String>()
            tags.add("github"); tags.add("repo")
            r.optString("language").takeIf { it.isNotBlank() }?.let { tags.add(it) }
            out.add(
                Hit(
                    title = full, url = link,
                    summary = ResourceLogic.cleanSummary(r.optString("description")),
                    kind = "repo", tags = tags,
                    quality = 0.62 + (r.optInt("stargazers_count", 0) / 100000.0).coerceAtMost(0.28),
                    image = "https://opengraph.githubassets.com/1/$full",
                    externalId = full, publishedAt = isoEpoch(r.optString("pushed_at")),
                )
            )
        }
        return out
    }

    /** 国内可达的镜像；返回数组或 {items:[]} 两种形状都接。 */
    private fun hf(): List<Hit> {
        val raw = get("https://hf-mirror.com/api/models?limit=25&sort=lastModified&direction=-1")
        val arr = if (raw.trim().startsWith("[")) JSONArray(raw)
        else JSONObject(raw).optJSONArray("items") ?: JSONArray()
        val out = ArrayList<Hit>()
        for (i in 0 until arr.length()) {
            val m = arr.optJSONObject(i) ?: continue
            val id = m.optString("modelId").ifBlank { m.optString("id") }
            if (id.isBlank()) continue
            out.add(
                Hit(
                    title = id, url = "https://hf-mirror.com/$id",
                    summary = "Hugging Face 模型 · 下载 ${m.optLong("downloads", 0)}",
                    kind = "model", tags = listOf("hf", "模型"), quality = 0.58,
                    externalId = id, publishedAt = isoEpoch(m.optString("lastModified")),
                )
            )
        }
        return out
    }

    private fun awesome(): List<Hit> {
        val out = ArrayList<Hit>()
        val seen = HashSet<String>()
        for (u in AWESOME) {
            runCatching { markdownLinks(get(u)) }.onSuccess { hits ->
                for (h in hits) if (seen.add(h.url)) out.add(h)
            }
            if (out.size >= 60) break
        }
        return out.take(60)
    }

    /** 从 awesome 风格 README 里抽外链；纯逻辑，JVM 单测直接跑。 */
    fun markdownLinks(md: String): List<Hit> {
        val re = Regex("""\[([^\[\]]{2,120})\]\((https?://[^\s)]+)\)""")
        val out = ArrayList<Hit>()
        val seen = HashSet<String>()
        for (m in re.findAll(md)) {
            val title = stripTags(m.groupValues[1]).trim()
            val url = m.groupValues[2].trim()
            if (title.isBlank() || !seen.add(url)) continue
            if (url.contains("github.com", true) &&
                (url.contains("/blob/") || url.contains("/tree/"))) continue
            out.add(
                Hit(
                    title = title, url = url,
                    summary = "开源清单收录 · ${ResourceLogic.domainOf(url)}",
                    kind = if (url.contains("github.com", true)) "repo" else "tool",
                    tags = listOf("awesome", "清单"), quality = 0.56,
                    externalId = url,
                )
            )
            if (out.size >= 40) break
        }
        return out
    }

    private fun rss(tag: String, url: String): List<Hit> = parseFeedXml(get(url), tag)

    private val BLOCK = Regex(
        "<item[\\s\\S]*?</item>|<entry[\\s\\S]*?</entry>", RegexOption.IGNORE_CASE)

    fun parseFeedXml(xml: String, tag: String): List<Hit> {
        val out = ArrayList<Hit>()
        for (m in BLOCK.findAll(xml)) {
            val b = m.value
            val title = stripTags(pick(b, "title"))
            if (title.isBlank()) continue
            val href = Regex("<link[^>]*href=[\"']([^\"']+)[\"']", RegexOption.IGNORE_CASE)
                .find(b)?.groupValues?.get(1).orEmpty()
            val link = href.ifBlank { stripTags(pick(b, "link")) }
                .ifBlank { stripTags(pick(b, "id")) }
            if (!link.startsWith("http", true)) continue
            val desc = pick(b, "description").ifBlank { pick(b, "summary") }
                .ifBlank { pick(b, "content") }
            val (image, video) = feedMedia(b, link)
            val baseKind = when {
                video.isNotBlank() -> "video"
                image.isNotBlank() && stripTags(desc).isBlank() -> "image"
                else -> "article"
            }
            // 手机软件源（F-Droid 等）无 enclosure，统一标成「应用」便于分类筛选。
            val kind = if (baseKind == "article" && tag == "手机软件") "app" else baseKind
            val tags = ArrayList<String>()
            tags.add(tag); tags.add("订阅")
            tags.addAll(feedCategories(b))
            if (video.isNotBlank()) tags.add("视频")
            if (kind == "image") tags.add("图片")
            out.add(
                Hit(
                    title = title, url = link,
                    summary = ResourceLogic.cleanSummary(stripTags(desc)),
                    kind = kind, tags = tags.distinct(), quality = 0.55,
                    image = image, video = video, externalId = link,
                    publishedAt = isoEpoch(stripTags(pick(b, "pubDate").ifBlank { pick(b, "updated") })),
                )
            )
            if (out.size >= 20) break
        }
        return out
    }

    /** 单条 feed 里的媒体：enclosure / media:content / media:thumbnail / 正文首图 / 视频站链接。 */
    private fun feedMedia(block: String, link: String): Pair<String, String> {
        var image = ""
        var video = ""
        Regex("<enclosure\\b[^>]*>", RegexOption.IGNORE_CASE).find(block)?.let { enc ->
            val a = tagAttrs(enc.value)
            val u = a["url"].orEmpty()
            val t = a["type"].orEmpty().lowercase()
            if (u.isNotBlank()) when {
                t.startsWith("video") -> video = u
                t.startsWith("image") -> image = u
                looksLikeVideo(u) -> video = u
                looksLikeImage(u) -> image = u
            }
        }
        for (m in Regex("<media:(?:content|thumbnail)\\b[^>]*>", RegexOption.IGNORE_CASE).findAll(block)) {
            val a = tagAttrs(m.value)
            val u = a["url"].orEmpty()
            if (u.isBlank()) continue
            val medium = a["medium"].orEmpty().lowercase()
            val t = a["type"].orEmpty().lowercase()
            when {
                medium == "video" || t.startsWith("video") || looksLikeVideo(u) ->
                    if (video.isBlank()) video = u
                medium == "image" || t.startsWith("image") || looksLikeImage(u) ->
                    if (image.isBlank()) image = u
            }
        }
        if (image.isBlank()) {
            val desc = pick(block, "description").ifBlank { pick(block, "content:encoded") }
                .ifBlank { pick(block, "content") }
            Regex("<img\\b[^>]*>", RegexOption.IGNORE_CASE).find(desc)?.let { img ->
                val a = tagAttrs(img.value)
                val src = (a["src"] ?: a["data-src"]).orEmpty()
                val w = a["width"]?.toIntOrNull() ?: 0
                if (src.isNotBlank() && !src.startsWith("data:", true) && w != 1) image = src
            }
        }
        // 链接本身是视频站（YouTube/B站等）：给缩略图，卡片能直接出图
        if (video.isBlank() && looksLikeVideo(link)) {
            video = link
            if (image.isBlank()) image = LinkPreviewLogic.coverOf(link)
        }
        return image to video
    }

    /** 一条 feed 里的 <category> 也收进标签，让分类更贴合内容而不是只有源名。 */
    private fun feedCategories(block: String): List<String> {
        val out = ArrayList<String>()
        for (m in Regex("<category\\b[^>]*>([\\s\\S]*?)</category>", RegexOption.IGNORE_CASE).findAll(block)) {
            val c = stripTags(m.groupValues[1]).trim().take(24)
            if (c.isNotBlank() && out.size < 4 && !out.contains(c)) out.add(c)
        }
        return out
    }

    private fun looksLikeImage(u: String): Boolean =
        Regex("""\.(jpe?g|png|webp|gif|avif)(\?.*)?$""", RegexOption.IGNORE_CASE).containsMatchIn(u)

    private fun looksLikeVideo(u: String): Boolean =
        Regex("""\.(mp4|webm|m4v|mov|avi)(\?.*)?$""", RegexOption.IGNORE_CASE).containsMatchIn(u) ||
            u.contains("youtube.com", true) || u.contains("youtu.be", true) ||
            u.contains("vimeo.com", true) || u.contains("bilibili.com", true)

    private fun tagAttrs(tag: String): Map<String, String> {
        val out = HashMap<String, String>()
        for (m in Regex("""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""").findAll(tag)) {
            val v = m.groupValues[2].ifEmpty { m.groupValues[3].ifEmpty { m.groupValues[4] } }
            out[m.groupValues[1].lowercase()] = v
        }
        return out
    }

    private fun pick(block: String, name: String): String {
        val m = Regex("<$name[^>]*>([\\s\\S]*?)</$name>", RegexOption.IGNORE_CASE)
            .find(block) ?: return ""
        return m.groupValues[1]
    }

    fun stripTags(s: String): String {
        var t = s.replace(Regex("<!\\[CDATA\\[([\\s\\S]*?)\\]\\]>")) { it.groupValues[1] }
        t = t.replace(Regex("<[^>]+>"), " ")
        t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", "\"").replace("&#39;", "'").replace("&nbsp;", " ")
            .replace("&#x27;", "'").replace("&apos;", "'")
        t = t.replace(Regex("&#(\\d+);")) {
            it.groupValues[1].toIntOrNull()?.toChar()?.toString() ?: ""
        }
        return t.replace(Regex("\\s+"), " ").trim()
    }

    /** ISO8601 / RFC2822 都尽量接，解析不了就返回 0（打分时退回 created_at）。 */
    private fun isoEpoch(s: String): Double {
        if (s.isBlank()) return 0.0
        val patterns = listOf(
            "yyyy-MM-dd'T'HH:mm:ss'Z'", "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'",
            "yyyy-MM-dd'T'HH:mm:ssXXX", "yyyy-MM-dd HH:mm:ss",
            "EEE, dd MMM yyyy HH:mm:ss Z",
        )
        for (p in patterns) {
            val r = runCatching {
                val f = SimpleDateFormat(p, Locale.US)
                f.timeZone = TimeZone.getTimeZone("UTC")
                f.parse(s)?.time?.div(1000.0) ?: 0.0
            }
            if (r.isSuccess && r.getOrNull() ?: 0.0 > 0.0) return r.getOrDefault(0.0)
        }
        return 0.0
    }

    private fun hitsJson(hits: List<Hit>): JSONArray {
        val a = JSONArray()
        for (h in hits) {
            val tags = JSONArray()
            for (t in h.tags) tags.put(t)
            a.put(
                JSONObject()
                    .put("url", h.url).put("title", h.title).put("summary", h.summary)
                    .put("kind", h.kind).put("tags", tags).put("quality", h.quality)
                    .put("image", h.image).put("video", h.video)
                    .put("id", h.externalId).put("published_at", h.publishedAt)
            )
        }
        return a
    }

    // ---------------------------------------------------------------- 动作

    /** 自己抓一轮：并发取、串行存；每个源独立成败，成果直接进本机库。 */
    suspend fun crawl(): JSONObject = withContext(Dispatchers.IO) {
        val gate = Semaphore(6)
        val fetched: List<Fetched> = coroutineScope {
            SOURCES.map { s ->
                async {
                    gate.withPermit {
                        runCatching { s.fetch() }.fold(
                            onSuccess = { Fetched(s, it, null) },
                            onFailure = {
                                Fetched(s, null,
                                    s.name + "：" + (it.message ?: it.javaClass.simpleName).take(70))
                            },
                        )
                    }
                }
            }.awaitAll()
        }
        val failed = JSONArray()
        var new = 0
        var merged = 0
        var ok = 0
        // 入库串行：同一个 SQLite 连接上不能并发开事务。
        for (f in fetched) {
            if (f.err != null) { failed.put(f.err); continue }
            try {
                val r = ResourceStore.ingest(f.s.id, hitsJson(f.hits ?: emptyList()),
                    f.s.name, "crawler")
                new += r.optInt("new")
                merged += r.optInt("merged")
                ok++
            } catch (e: Exception) {
                failed.put(f.s.name + "：" + (e.message ?: "入库失败").take(70))
            }
        }
        ResourceStore.markCrawl()
        JSONObject().put("ok", true)
            .put("sources", ok).put("sources_total", SOURCES.size)
            .put("new", new).put("merged", merged).put("total", new + merged)
            .put("failed", failed)
    }

    /**
     * 对等节点共享：登记过别的节点就顺手拉一批合并，没有就跳过。
     * 这是「任何节点可以调用其他节点」，不是前提 —— 拉不到本机库照常能用。
     */
    suspend fun syncPeers(): JSONObject = withContext(Dispatchers.IO) {
        val out = JSONObject().put("ok", true).put("new", 0).put("merged", 0)
        if (!Net.hasConfig()) {
            out.put("skipped", "没有登记的对等节点")
            return@withContext out
        }
        val c = Net.auth(Api.endpoint(Net.loadConfig().url, "/api/resources?limit=50"))
        c.readTimeout = 15_000
        try {
            if (c.responseCode !in 200..299) {
                out.put("skipped", "节点 HTTP ${c.responseCode}")
                return@withContext out
            }
            val body = c.inputStream.bufferedReader().use { it.readText() }
            val arr = JSONObject(body).optJSONArray("items") ?: JSONArray()
            val r = ResourceStore.ingest("peer-node", arr, "对等节点共享", "peer")
            out.put("new", r.optInt("new")).put("merged", r.optInt("merged"))
        } catch (e: Exception) {
            out.put("skipped", (e.message ?: "节点没回包").take(70))
        } finally {
            c.disconnect()
        }
        out
    }
}
