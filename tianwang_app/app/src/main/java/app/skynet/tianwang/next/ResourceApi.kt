package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URLEncoder

/**
 * 资源流客户端：只跟电脑侧的 /api/resources* 说话。
 *
 * 为什么单独一个文件：手机不抓取、不做推荐、不存资源库——这些都在电脑侧
 * `resource_center.py`。手机只负责把卡片刷出来、把赞/收藏/评论送回去。
 * 这样换手机、重装 App 都不丢行为数据。
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
    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

    private fun read(c: HttpURLConnection): String {
        val stream = if (c.responseCode in 200..299) c.inputStream else c.errorStream
        return stream?.bufferedReader()?.use { it.readText() }.orEmpty()
    }

    private fun get(path: String, timeout: Int = 15_000): JSONObject {
        val c = Net.auth(Api.endpoint(Net.loadConfig().url, path))
        c.readTimeout = timeout
        try {
            val body = read(c)
            if (c.responseCode !in 200..299) {
                throw IOException("HTTP ${c.responseCode} ${body.take(120)}")
            }
            return JSONObject(body)
        } finally {
            c.disconnect()
        }
    }

    private fun post(path: String, body: JSONObject): JSONObject {
        val c = Net.auth(Api.endpoint(Net.loadConfig().url, path))
        c.requestMethod = "POST"
        c.doOutput = true
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        // 导入要读电脑侧两个 JSON，可能比普通请求慢；15s 不够时会误报「连不上」。
        c.readTimeout = 60_000
        try {
            c.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val raw = read(c)
            if (c.responseCode !in 200..299) {
                throw IOException("HTTP ${c.responseCode} ${raw.take(120)}")
            }
            return JSONObject(raw)
        } finally {
            c.disconnect()
        }
    }

    internal fun parseCard(o: JSONObject): ResourceCard {
        val tags = ArrayList<String>()
        o.optJSONArray("tags")?.let { a: JSONArray ->
            for (i in 0 until a.length()) a.optString(i).takeIf { it.isNotBlank() }?.let { tags.add(it) }
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
        withContext(Dispatchers.IO) {
            val q = StringBuilder("/api/resources?limit=20&cursor=$cursor&session=${enc(session)}")
            if (topic.isNotBlank()) q.append("&topic=${enc(topic)}")
            parseFeed(get(q.toString()))
        }

    suspend fun detail(id: String): ResourceDetail = withContext(Dispatchers.IO) {
        val o = get("/api/resources/detail?id=${enc(id)}")
        if (!o.optBoolean("ok", false)) throw IOException(o.optString("err", "详情失败"))
        val it = o.getJSONObject("item")
        val origins = ArrayList<ResourceOrigin>()
        it.optJSONArray("origins")?.let { a ->
            for (i in 0 until a.length()) a.optJSONObject(i)?.let { x ->
                origins.add(
                    ResourceOrigin(
                        x.optString("source_id"),
                        x.optString("url"),
                        x.optString("external_id"),
                        x.optString("fetched_at"),
                    )
                )
            }
        }
        val actions = ArrayList<ResourceAction>()
        it.optJSONArray("actions")?.let { a ->
            for (i in 0 until a.length()) a.optJSONObject(i)?.let { x ->
                actions.add(ResourceAction(x.optString("action"), x.optString("text"), x.optString("created_at")))
            }
        }
        ResourceDetail(parseCard(it), origins, actions)
    }

    suspend fun act(id: String, action: String, text: String = ""): JSONObject =
        withContext(Dispatchers.IO) {
            post("/api/resources/act", JSONObject().put("id", id).put("action", action).put("text", text))
        }

    suspend fun importExisting(): JSONObject = withContext(Dispatchers.IO) {
        post("/api/resources/import", JSONObject())
    }

    suspend fun stats(): JSONObject = withContext(Dispatchers.IO) {
        get("/api/resources/stats")
    }
}
