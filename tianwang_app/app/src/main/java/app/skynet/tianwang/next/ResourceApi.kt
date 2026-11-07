package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException

/**
 * 资源流门面：本机库 + 自己联网。
 *
 * 手机是完整节点 —— 存、抓、查、显示都在本机闭环，不需要别的软件先装好、先开着。
 * 电脑或其他节点只是可选共享来源：调得到就合并一批，调不到本机库照常能刷。
 * 方法签名保持不变，界面不用关心数据是从本机库还是从共享来的。
 */
data class ResourceCard(
    val id: String,
    val title: String,
    val summary: String,
    val url: String,
    val domain: String,
    val kind: String,
    val tags: List<String>,
    val score: Double,
    val why: String,
    val at: String,
    val liked: Boolean,
    val faved: Boolean,
    val comments: Int,
    /** 抓取阶段抽到的封面图（enclosure / media / 首图 / 大站规则封面），空则用 OG 兜底。 */
    val image: String = "",
    /** 视频地址，非空即视频资源。 */
    val video: String = "",
)

data class ResourceOrigin(val source: String, val url: String, val externalId: String, val at: String)

data class ResourceAction(val action: String, val text: String, val at: String)

data class ResourceDetail(
    val card: ResourceCard,
    val origins: List<ResourceOrigin>,
    val actions: List<ResourceAction>,
)

data class ResourceFeed(val items: List<ResourceCard>, val next: Int?, val total: Int)

object ResourceApi {

    /** 进程内固定的信息流会话：预热器与资源页取到同一批，预热才不会白做。 */
    val FEED_SESSION: String = "phone-" + java.util.UUID.randomUUID().toString().take(8)

    /** App 启动时后台把首页 20 条的封面 / 摘要预先备好。 */
    suspend fun prefetchFirstPage(): Unit = ResourcePrefetch.warmFirstPage(FEED_SESSION)

    /** 预热任意一批（新一页 / 滑动补漏）；返回可立即显示的预览。 */
    suspend fun prefetch(urls: List<String>): Map<String, LinkPreview> =
        ResourcePrefetch.warm(urls)

    /** 共享接口拿到的 JSON 卡片仍走这套解析，字段名与电脑侧 /api/resources 一致。 */
    internal fun parseCard(o: JSONObject): ResourceCard {
        val tags = ArrayList<String>()
        o.optJSONArray("tags")?.let { a: JSONArray ->
            for (i in 0 until a.length()) {
                a.optString(i).takeIf { it.isNotBlank() }?.let { tags.add(it) }
            }
        }
        return ResourceCard(
            id = o.optString("id"),
            title = o.optString("title").ifBlank { o.optString("domain") },
            summary = o.optString("summary"),
            url = o.optString("url"),
            domain = o.optString("domain"),
            kind = o.optString("kind"),
            tags = tags,
            score = o.optDouble("score", 0.0),
            why = o.optString("why"),
            at = o.optString("published_at"),
            liked = o.optBoolean("liked", false),
            faved = o.optBoolean("faved", false),
            comments = o.optInt("comments", 0),
            image = o.optString("image"),
            video = o.optString("video"),
        )
    }

    internal fun parseFeed(o: JSONObject): ResourceFeed {
        if (!o.optBoolean("ok", false)) throw IOException(o.optString("err", "资源流失败"))
        val items = ArrayList<ResourceCard>()
        o.optJSONArray("items")?.let { a ->
            for (i in 0 until a.length()) a.optJSONObject(i)?.let { items.add(parseCard(it)) }
        }
        val next = if (o.isNull("next_cursor")) null else o.optInt("next_cursor", 0)
        return ResourceFeed(items, next, o.optInt("total", items.size))
    }

    suspend fun feed(topic: String, cursor: Int, session: String): ResourceFeed =
        withContext(Dispatchers.IO) { ResourceStore.feed(topic, cursor, session) }

    suspend fun detail(id: String): ResourceDetail =
        withContext(Dispatchers.IO) { ResourceStore.detail(id) }

    suspend fun act(id: String, action: String, text: String = ""): JSONObject =
        withContext(Dispatchers.IO) { ResourceStore.act(id, action, text) }

    /** 兼容旧按钮名：现在报告随包成果的落地条数，不再去别的软件里搬。 */
    suspend fun importExisting(): JSONObject =
        withContext(Dispatchers.IO) { ResourceStore.seedStatus() }

    suspend fun stats(): JSONObject =
        withContext(Dispatchers.IO) { ResourceStore.stats() }

    suspend fun mine(kind: String, cursor: Int, session: String): ResourceFeed =
        withContext(Dispatchers.IO) { ResourceStore.feed("", cursor, session, mine = kind) }

    suspend fun focus(): List<String> =
        withContext(Dispatchers.IO) { ResourceStore.focus() }

    suspend fun toggleFocus(tag: String): List<String> = withContext(Dispatchers.IO) {
        val o = ResourceStore.act("", "focus", tag)
        if (!o.optBoolean("ok", false)) {
            throw IllegalArgumentException(o.optString("err", "操作失败"))
        }
        val a = o.optJSONArray("focus") ?: JSONArray()
        (0 until a.length()).mapNotNull { a.optString(it).takeIf { s -> s.isNotBlank() } }
    }

    /** 自己联网抓一轮，成果直接进本机库。 */
    suspend fun crawl(): JSONObject = ResourceCrawler.crawl()

    /** 可选：跟登记过的对等节点共享一批；拉不到不影响本机任何功能。 */
    suspend fun syncPeers(): JSONObject = ResourceCrawler.syncPeers()

    /** 半小时内抓过就不再自动抓，避免反复吃流量。 */
    fun crawlDue(): Boolean = runCatching { ResourceStore.crawlDue() }.getOrDefault(false)

    /** 卡片预览第一段：本机缓存 + 规则封面，零请求、立刻能显示。 */
    suspend fun previewInstant(urls: List<String>): Map<String, LinkPreview> =
        withContext(Dispatchers.IO) {
            urls.distinct().mapNotNull { u -> LinkPreviewStore.instant(u)?.let { u to it } }.toMap()
        }

    /** 卡片预览第二段：手机自己联网解析 OG / Twitter Card，结果落本机库。 */
    suspend fun previewFetch(urls: List<String>): Map<String, LinkPreview> =
        LinkPreviewLoader.ensure(urls)
}
