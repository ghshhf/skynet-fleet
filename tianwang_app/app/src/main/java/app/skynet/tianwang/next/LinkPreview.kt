package app.skynet.tianwang.next

import android.content.ContentValues
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.URI
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * 链接预览：像推特卡片那样，在信息流里直接带出图、标题、摘要、站点。
 *
 * 手机自己发请求解析目标页的 OG / Twitter Card 元信息，结果落进本机 SQLite，
 * 7 天内不重复抓；大站（YouTube / GitHub）还能零请求先给一张规则封面。
 * 全程只用到手机自己的联网能力，不需要任何别的软件先装好或先开着。
 */
data class LinkPreview(
    val url: String,
    val title: String = "",
    val description: String = "",
    val image: String = "",
    val icon: String = "",
    val site: String = "",
) {
    val hasImage: Boolean get() = image.isNotBlank()
}

/** 纯解析逻辑：不碰 Android，JVM 单测直接跑。 */
object LinkPreviewLogic {

    private val META_TAG = Regex("<(meta|link)\\b[^>]*>", RegexOption.IGNORE_CASE)
    private val TITLE_TAG = Regex("<title[^>]*>([\\s\\S]*?)</title>", RegexOption.IGNORE_CASE)
    private val IMG_TAG = Regex("<img\\b[^>]*>", RegexOption.IGNORE_CASE)
    private val ATTR = Regex(
        """([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))"""
    )
    private val ENTITY = mapOf(
        "&amp;" to "&", "&quot;" to "\"", "&#39;" to "'", "&apos;" to "'",
        "&lt;" to "<", "&gt;" to ">", "&nbsp;" to " ", "&#x27;" to "'",
    )
    private val IMAGE_EXT = Regex("""\.(jpe?g|png|webp|gif)(\?.*)?$""", RegexOption.IGNORE_CASE)

    private fun attrs(tag: String): Map<String, String> {
        val out = HashMap<String, String>()
        for (m in ATTR.findAll(tag)) {
            val v = m.groupValues[2].ifEmpty { m.groupValues[3].ifEmpty { m.groupValues[4] } }
            out[m.groupValues[1].lowercase()] = v
        }
        return out
    }

    fun clean(s: String): String {
        // 先剥标签再解实体：否则 &lt;标题&gt; 解成 <标题> 后会被当成标签误删。
        var v = s.replace(Regex("<[^>]+>"), " ")
        for ((k, r) in ENTITY) v = v.replace(k, r, ignoreCase = true)
        v = v.replace(Regex("""\s+"""), " ").trim()
        return v.take(300)
    }

    /** 相对地址按页面地址补全；协议相对 //host/x 也能处理。 */
    fun absolute(base: String, ref: String): String {
        val r = clean(ref)
        if (r.isBlank() || r.startsWith("data:", true)) return ""
        return runCatching { URI(base).resolve(URI(r.replace(" ", "%20"))).toString() }
            .getOrDefault(r)
    }

    /** 规则封面：不用发请求就能先把卡片撑起来，之后 OG 抓到真图会覆盖。 */
    fun coverOf(url: String): String {
        val u = url.trim()
        if (u.isEmpty()) return ""
        if (IMAGE_EXT.containsMatchIn(u)) return u
        val uri = runCatching { URI(u) }.getOrNull() ?: return ""
        val host = (uri.host ?: "").lowercase().removePrefix("www.")
        val path = uri.path ?: ""
        when {
            host == "youtu.be" -> {
                val id = path.trim('/').substringBefore('?').take(24)
                if (id.isNotBlank()) return "https://img.youtube.com/vi/$id/hqdefault.jpg"
            }
            host.endsWith("youtube.com") -> {
                val id = Regex("""(?:^|&)v=([^&]+)""").find(uri.query ?: "")?.groupValues?.get(1)
                    ?: Regex("""/(?:shorts|embed|v)/([^/?]+)""").find(path)?.groupValues?.get(1)
                if (!id.isNullOrBlank()) return "https://img.youtube.com/vi/${id.take(24)}/hqdefault.jpg"
            }
            host == "github.com" -> {
                val seg = path.trim('/').split('/').filter { it.isNotBlank() }
                if (seg.size >= 2) {
                    val owner = seg[0]
                    val repo = seg[1].removeSuffix(".git")
                    if (owner.matches(Regex("""[A-Za-z0-9_.-]+""")) &&
                        repo.matches(Regex("""[A-Za-z0-9_.-]+"""))) {
                        return "https://opengraph.githubassets.com/1/$owner/$repo"
                    }
                }
            }
            host.endsWith("bilibili.com") -> {
                // B 站封面要走它的 API，静态推不出来；交给 OG 抓取或占位。
            }
        }
        return ""
    }

    fun domainOf(url: String): String = runCatching {
        (URI(url).host ?: "").lowercase().removePrefix("www.")
    }.getOrDefault("")

    /** 从 HTML 头部解析 OG / Twitter Card / 常规 meta。 */
    fun parse(html: String, pageUrl: String): LinkPreview {
        // 只看 head，正文里的 <img> 不作为兜底来源时也不用读全篇。
        val head = html.take(200_000).let { s ->
            val end = s.indexOf("</head>", ignoreCase = true)
            if (end > 0) s.substring(0, end) else s
        }
        var title = ""
        var desc = ""
        var image = ""
        var icon = ""
        var site = ""
        var imgFallback = ""
        for (m in META_TAG.findAll(head)) {
            val a = attrs(m.value)
            val key = (a["property"] ?: a["name"] ?: a["itemprop"] ?: "").lowercase()
            val rel = (a["rel"] ?: "").lowercase()
            val value = a["content"] ?: a["href"] ?: ""
            if (value.isBlank()) continue
            when {
                key == "og:title" && title.isBlank() -> title = clean(value)
                key == "twitter:title" && title.isBlank() -> title = clean(value)
                key == "og:description" && desc.isBlank() -> desc = clean(value)
                key == "twitter:description" && desc.isBlank() -> desc = clean(value)
                key == "description" && desc.isBlank() -> desc = clean(value)
                key == "og:site_name" && site.isBlank() -> site = clean(value)
                (key == "og:image" || key == "og:image:url" ||
                    key == "og:image:secure_url" || key == "twitter:image" ||
                    key == "twitter:image:src") && image.isBlank() ->
                    image = absolute(pageUrl, value)
                (rel.contains("apple-touch-icon") || rel == "icon" ||
                    rel == "shortcut icon" || rel == "image_src") -> {
                    if (rel == "image_src" && image.isBlank()) image = absolute(pageUrl, value)
                    if (icon.isBlank() && !rel.contains("image_src")) icon = absolute(pageUrl, value)
                }
            }
        }
        if (title.isBlank()) TITLE_TAG.find(head)?.let { title = clean(it.groupValues[1]) }
        if (image.isBlank()) {
            for (m in IMG_TAG.findAll(head)) {
                val src = (attrs(m.value)["src"] ?: "").trim()
                if (src.isBlank() || src.startsWith("data:", true)) continue
                val w = attrs(m.value)["width"]?.toIntOrNull() ?: 0
                if (w in 1..64) continue
                imgFallback = absolute(pageUrl, src)
                break
            }
        }
        return LinkPreview(
            url = pageUrl,
            title = title,
            description = desc,
            image = image.ifBlank { imgFallback },
            icon = icon.ifBlank {
                if (pageUrl.startsWith("http")) absolute(pageUrl, "/favicon.ico") else ""
            },
            site = site.ifBlank { domainOf(pageUrl) },
        )
    }
}

/** 预览缓存：跟着本机资源库走，不另开一份数据库。 */
object LinkPreviewStore {
    private const val TTL = 7 * 24 * 3600_000L
    private const val FAIL_TTL = 24 * 3600_000L
    @Volatile private var ready = false

    private fun db(): android.database.sqlite.SQLiteDatabase {
        val d = ResourceStore.open()
        if (!ready) synchronized(this) {
            if (!ready) {
                d.execSQL(
                    "CREATE TABLE IF NOT EXISTS previews (" +
                        "url TEXT PRIMARY KEY, title TEXT DEFAULT '', description TEXT DEFAULT '', " +
                        "image TEXT DEFAULT '', icon TEXT DEFAULT '', site TEXT DEFAULT '', " +
                        "fetched_at REAL, failed INTEGER DEFAULT 0)"
                )
                ready = true
            }
        }
        return d
    }

    /** 命中且没过期才返回；解析失败的站点一天内不再重试。 */
    fun get(url: String): LinkPreview? {
        val now = System.currentTimeMillis()
        return db().rawQuery(
            "SELECT title,description,image,icon,site,fetched_at,failed FROM previews WHERE url=?",
            arrayOf(url),
        ).use { c ->
            if (!c.moveToFirst()) return null
            val at = (c.getDouble(5) * 1000).toLong()
            val failed = c.getInt(6) != 0
            if (now - at > if (failed) FAIL_TTL else TTL) return null
            LinkPreview(
                url = url,
                title = c.getString(0) ?: "", description = c.getString(1) ?: "",
                image = c.getString(2) ?: "", icon = c.getString(3) ?: "",
                site = c.getString(4) ?: "",
            )
        }
    }

    /** 缓存里没有但能靠规则立刻出图的，先给一张「即时预览」，不发请求。 */
    fun instant(url: String): LinkPreview? {
        val cover = LinkPreviewLogic.coverOf(url)
        val hit = get(url)
        // 页面解析失败但大站能推封面时，别把卡片钉死在纯文字上。
        if (hit != null) return if (hit.hasImage || cover.isBlank()) hit else hit.copy(image = cover)
        if (cover.isBlank()) return null
        return LinkPreview(url, image = cover, site = LinkPreviewLogic.domainOf(url))
    }

    fun put(p: LinkPreview, failed: Boolean = false) {
        val cv = ContentValues().apply {
            put("url", p.url)
            put("title", p.title)
            put("description", p.description)
            put("image", p.image)
            put("icon", p.icon)
            put("site", p.site)
            put("fetched_at", System.currentTimeMillis() / 1000.0)
            put("failed", if (failed) 1 else 0)
        }
        db().insertWithOnConflict("previews", null, cv,
            android.database.sqlite.SQLiteDatabase.CONFLICT_REPLACE)
    }

    fun isFresh(url: String): Boolean {
        val now = System.currentTimeMillis()
        return db().rawQuery("SELECT fetched_at,failed FROM previews WHERE url=?",
            arrayOf(url)).use { c ->
            if (!c.moveToFirst()) false
            else now - (c.getDouble(0) * 1000).toLong() <
                if (c.getInt(1) != 0) FAIL_TTL else TTL
        }
    }
}

/** 手机自己联网解析目标页；限并发、限读大小，失败也不影响卡片显示。 */
object LinkPreviewLoader {
    private val client = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .followRedirects(true)
        .followSslRedirects(true)
        .build()
    private val inflight = ConcurrentHashMap.newKeySet<String>()
    // 低并发：卡片预览是锦上添花，不能把文章/抓取的带宽挤没。
    private val gate = Semaphore(2)

    private fun http(url: String): LinkPreview? {
        val req = Request.Builder().url(url)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36")
            .header("Accept", "text/html,application/xhtml+xml")
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            val body = r.body ?: return null
            val type = body.contentType()?.toString() ?: ""
            if (type.isNotBlank() && !type.contains("html") && !type.contains("xml") &&
                !type.contains("text")) return null
            // 只读前 256 KB：元信息都在 head，别为了一个卡片把整页扛回来。
            val stream = body.byteStream()
            val out = java.io.ByteArrayOutputStream()
            val buf = ByteArray(8192)
            var left = 128 * 1024
            while (left > 0) {
                val n = stream.read(buf, 0, minOf(buf.size, left))
                if (n < 0) break
                out.write(buf, 0, n)
                left -= n
            }
            val cs = runCatching { body.contentType()?.charset(Charsets.UTF_8) }
                .getOrNull() ?: Charsets.UTF_8
            val html = String(out.toByteArray(), cs)
            val p = LinkPreviewLogic.parse(html, url)
            return p.copy(image = p.image.ifBlank { LinkPreviewLogic.coverOf(url) })
        }
    }

    /** 只补没抓过的，并发 2 个；同一条链接不会被重复请求。 */
    suspend fun ensure(urls: List<String>): Map<String, LinkPreview> = withContext(Dispatchers.IO) {
        // 先限量再登记 inflight：否则第 7 个之后的链接会被永久标记成「正在抓」。
        val todo = urls.filter {
            it.startsWith("http", true) && !LinkPreviewStore.isFresh(it)
        }.take(6).filter { inflight.add(it) }
        if (todo.isEmpty()) return@withContext emptyMap()
        val out = HashMap<String, LinkPreview>()
        try {
            coroutineScope {
                val jobs = todo.map { u ->
                    async {
                        gate.withPermit {
                            val p = runCatching { http(u) }.getOrNull()
                            if (p != null) LinkPreviewStore.put(p) else LinkPreviewStore.put(
                                LinkPreview(u, site = LinkPreviewLogic.domainOf(u)), failed = true
                            )
                            u to p
                        }
                    }
                }
                for (j in jobs) {
                    val (u, p) = j.await()
                    if (p != null) out[u] = p
                }
            }
        } finally {
            todo.forEach { inflight.remove(it) }
        }
        out
    }
}
