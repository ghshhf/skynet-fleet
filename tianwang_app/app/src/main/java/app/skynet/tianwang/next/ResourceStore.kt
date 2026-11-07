package app.skynet.tianwang.next

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.Locale
import kotlin.math.exp

/**
 * 手机自己的资源库。
 *
 * 随包带一份已爬完的成果（assets/resources_seed.db），首次启动复制到私有目录后就是
 * 本机 SQLite；之后自己抓、自己存、自己查、自己显示，全流程不需要别的软件先装好或先开着。
 * 别的节点只是可选共享来源：调得到就合并，调不到照常可用。
 *
 * 表结构与打分口径跟电脑侧 fleet/resource_center.py 一致 —— 对等节点之间共享时，
 * 同一条内容算出的 id 必须相同，否则越同步越重复。
 */
object ResourceStore {
    private const val DB_NAME = "resources.db"
    private const val SEED = "resources_seed.db"
    private val gate = Any()

    @Volatile private var handle: SQLiteDatabase? = null

    private class Row(
        val id: String, val url: String, val title: String, val summary: String,
        val kind: String, val domain: String, val quality: Double,
        val publishedAt: Double, val createdAt: Double,
    )

    private class Ranked(val card: ResourceCard, val score: Double)

    private val SCHEMA = arrayOf(
        "CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, name TEXT, kind TEXT DEFAULT '', config TEXT DEFAULT '{}', created_at REAL, last_seen REAL)",
        "CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, title TEXT, summary TEXT, kind TEXT DEFAULT '', domain TEXT DEFAULT '', quality REAL DEFAULT 0.5, published_at REAL, created_at REAL, updated_at REAL)",
        "CREATE INDEX IF NOT EXISTS idx_res_fp ON resources(fingerprint)",
        "CREATE INDEX IF NOT EXISTS idx_res_domain ON resources(domain)",
        "CREATE INDEX IF NOT EXISTS idx_res_created ON resources(created_at)",
        "CREATE TABLE IF NOT EXISTS origins (resource_id TEXT, source_id TEXT, external_id TEXT DEFAULT '', url TEXT, fetched_at REAL, raw TEXT, PRIMARY KEY (resource_id, source_id, external_id))",
        "CREATE TABLE IF NOT EXISTS tags (resource_id TEXT, tag TEXT, weight REAL DEFAULT 1.0, PRIMARY KEY (resource_id, tag))",
        "CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag)",
        "CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY AUTOINCREMENT, resource_id TEXT, action TEXT, text TEXT DEFAULT '', created_at REAL)",
        "CREATE INDEX IF NOT EXISTS idx_actions_res ON actions(resource_id)",
        "CREATE TABLE IF NOT EXISTS exposures (resource_id TEXT, session_id TEXT, times INTEGER DEFAULT 0, clicked INTEGER DEFAULT 0, last_at REAL, PRIMARY KEY (resource_id, session_id))",
        "CREATE TABLE IF NOT EXISTS prefs (tag TEXT PRIMARY KEY, weight REAL DEFAULT 0, updated_at REAL)",
        "CREATE TABLE IF NOT EXISTS focus (tag TEXT PRIMARY KEY, created_at REAL)",
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
    )

    fun open(): SQLiteDatabase {
        synchronized(gate) {
            handle?.let { return it }
            val ctx: Context = App.ctx
            val f = ctx.getDatabasePath(DB_NAME)
            f.parentFile?.mkdirs()
            if (!f.exists() || f.length() < 4096) copySeed(ctx, f)
            val d = SQLiteDatabase.openDatabase(f.absolutePath, null, SQLiteDatabase.OPEN_READWRITE)
            for (sql in SCHEMA) d.execSQL(sql)
            handle = d
            return d
        }
    }

    /** assets 是压缩的，不能直接 openDatabase，先流式落到私有目录再打开。 */
    private fun copySeed(ctx: Context, target: File) {
        val tmp = File(target.parentFile, "resources_seed.tmp")
        try {
            ctx.assets.open(SEED).use { input ->
                tmp.outputStream().buffered().use { out -> input.copyTo(out, 64 * 1024) }
            }
            if (target.exists()) target.delete()
            if (!tmp.renameTo(target)) {
                tmp.copyTo(target, overwrite = true)
                tmp.delete()
            }
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }

    // ---------------------------------------------------------------- 信息流

    fun feed(
        topic: String, cursor: Int, session: String, limit: Int = 20, mine: String = "",
        recordExposure: Boolean = true,
    ): ResourceFeed {
        val d = open()
        val now = System.currentTimeMillis() / 1000.0
        val conds = ArrayList<String>()
        val args = ArrayList<String>()
        if (topic.isNotBlank()) {
            conds.add("(r.title LIKE ? OR r.summary LIKE ? OR EXISTS(" +
                "SELECT 1 FROM tags t WHERE t.resource_id=r.id AND t.tag LIKE ?))")
            val like = "%$topic%"
            args.add(like); args.add(like); args.add(like)
        }
        if (mine == "favorite" || mine == "like") {
            conds.add("EXISTS(SELECT 1 FROM actions a WHERE a.resource_id=r.id AND a.action=?)")
            args.add(mine)
        }
        val where = if (conds.isEmpty()) "" else "WHERE " + conds.joinToString(" AND ")
        val rows = ArrayList<Row>()
        d.rawQuery(
            "SELECT r.id,r.canonical_url,r.title,r.summary,r.kind,r.domain,r.quality," +
                "r.published_at,r.created_at FROM resources r $where " +
                "ORDER BY r.created_at DESC LIMIT 500",
            args.toTypedArray(),
        ).use { c ->
            while (c.moveToNext()) {
                rows.add(
                    Row(
                        c.getString(0), c.getString(1) ?: "", c.getString(2) ?: "",
                        c.getString(3) ?: "", c.getString(4) ?: "", c.getString(5) ?: "",
                        c.getDouble(6), c.getDouble(7), c.getDouble(8),
                    )
                )
            }
        }
        if (rows.isEmpty()) return ResourceFeed(emptyList(), null, 0)

        val tagMap = HashMap<String, MutableList<String>>()
        for (chunk in rows.map { it.id }.chunked(400)) {
            val ph = chunk.joinToString(",") { "?" }
            d.rawQuery("SELECT resource_id,tag FROM tags WHERE resource_id IN ($ph)",
                chunk.toTypedArray()).use { c ->
                while (c.moveToNext()) {
                    tagMap.getOrPut(c.getString(0)) { ArrayList() }.add(c.getString(1))
                }
            }
        }
        val prefs = HashMap<String, Double>()
        d.rawQuery("SELECT tag,weight FROM prefs", null).use { c ->
            while (c.moveToNext()) prefs[c.getString(0)] = c.getDouble(1)
        }
        val expTimes = HashMap<String, Int>()
        d.rawQuery("SELECT resource_id,times FROM exposures WHERE session_id=?",
            arrayOf(session)).use { c ->
            while (c.moveToNext()) expTimes[c.getString(0)] = c.getInt(1)
        }
        // 同站看得越多越往后压
        val domSeen = HashMap<String, Int>()
        for (r in rows) {
            val t = expTimes[r.id] ?: 0
            if (t > 0 && r.domain.isNotBlank()) {
                domSeen[r.domain] = (domSeen[r.domain] ?: 0) + maxOf(1, t)
            }
        }

        val ranked = ArrayList<Ranked>(rows.size)
        for (r in rows) {
            val rt: List<String> = tagMap[r.id] ?: emptyList()
            var pref = 0.0
            for (t in rt) pref += prefs[t] ?: 0.0
            val interest = (pref / 3.0).coerceIn(0.0, 1.0)
            val ts = when {
                r.publishedAt > 0.0 -> r.publishedAt
                r.createdAt > 0.0 -> r.createdAt
                else -> now
            }
            val ageDays = ((now - ts) / 86400.0).coerceAtLeast(0.0)
            val fresh = exp(-ageDays / 14.0)
            val times = expTimes[r.id] ?: 0
            val novel = 1.0 / (1.0 + times)
            val match = if (topic.isNotBlank() &&
                (r.title.contains(topic, true) || r.summary.contains(topic, true) ||
                    rt.any { it.contains(topic, true) })) 1.0 else 0.0
            val explore = ResourceLogic.exploreOf(r.id, session)
            val domainPen = (0.12 * (domSeen[r.domain] ?: 0)).coerceAtMost(0.36)
            val sc = ResourceLogic.score(interest, r.quality, fresh, novel, explore, match, domainPen)
            val why = ArrayList<String>()
            if (interest > 0) why.add("兴趣 +%.2f".format(0.35 * interest))
            if (match > 0) why.add("主题匹配")
            if (times == 0) why.add("没刷过")
            if (fresh > 0.8) why.add("新鲜")
            if (domainPen > 0) why.add("同站已多看")
            ranked.add(
                Ranked(
                    ResourceCard(
                        id = r.id,
                        title = r.title.ifBlank { r.domain },
                        summary = ResourceLogic.cleanSummary(r.summary),
                        url = r.url,
                        domain = r.domain,
                        kind = r.kind,
                        tags = rt,
                        score = Math.round(sc * 10000.0) / 10000.0,
                        why = why.take(2).joinToString(" · ").ifBlank { "随机看看" },
                        at = ResourceLogic.isoTs(ts),
                        liked = false, faved = false, comments = 0,
                    ),
                    sc,
                )
            )
        }
        ranked.sortWith(compareByDescending<Ranked> { it.score }.thenBy { it.card.id })

        val window = if (cursor < ranked.size) {
            ranked.subList(cursor, minOf(ranked.size, cursor + limit * 2)).toList()
        } else emptyList()
        val page = ResourceLogic.diverse(window, limit) { it.card.domain }
        if (page.isEmpty()) return ResourceFeed(emptyList(), null, ranked.size)

        val marks = HashMap<String, MutableMap<String, Int>>()
        for (chunk in page.map { it.card.id }.chunked(400)) {
            val ph = chunk.joinToString(",") { "?" }
            d.rawQuery(
                "SELECT resource_id,action,COUNT(*) FROM actions WHERE resource_id IN ($ph) " +
                    "GROUP BY resource_id,action", chunk.toTypedArray(),
            ).use { c ->
                while (c.moveToNext()) {
                    marks.getOrPut(c.getString(0)) { HashMap() }[c.getString(1)] = c.getInt(2)
                }
            }
        }
        val cards = page.map {
            val m: Map<String, Int> = marks[it.card.id] ?: emptyMap()
            it.card.copy(
                liked = m.containsKey("like"),
                faved = m.containsKey("favorite"),
                comments = m["comment"] ?: 0,
            )
        }
        // 预热器取同一批 URL 时不能写曝光：写了会把首页这 20 条按「已看过」压后，
        // 页面真正打开时拿到的就是另一批，预热全白做。
        if (recordExposure) for (c in cards) {
            val cv = ContentValues().apply {
                put("times", (expTimes[c.id] ?: 0) + 1)
                put("clicked", 0)
                put("last_at", now)
            }
            val n = d.update("exposures", cv, "resource_id=? AND session_id=?",
                arrayOf(c.id, session))
            if (n == 0) {
                val ins = ContentValues().apply {
                    put("resource_id", c.id); put("session_id", session)
                    put("times", 1); put("clicked", 0); put("last_at", now)
                }
                d.insert("exposures", null, ins)
            }
        }
        val next = if (ranked.size > cursor + page.size) cursor + page.size else null
        return ResourceFeed(cards, next, ranked.size)
    }

    // ---------------------------------------------------------------- 详情 / 行为

    fun detail(id: String): ResourceDetail {
        val d = open()
        var card: ResourceCard? = null
        d.rawQuery(
            "SELECT id,canonical_url,title,summary,kind,domain,quality,published_at,created_at " +
                "FROM resources WHERE id=?", arrayOf(id),
        ).use { c ->
            if (c.moveToFirst()) {
                val ts = if (c.getDouble(7) > 0.0) c.getDouble(7) else c.getDouble(8)
                val tags = ArrayList<String>()
                d.rawQuery("SELECT tag FROM tags WHERE resource_id=?", arrayOf(id)).use { t ->
                    while (t.moveToNext()) tags.add(t.getString(0))
                }
                val acts = actionRows(d, id)
                card = ResourceCard(
                    id = c.getString(0),
                    title = (c.getString(2) ?: "").ifBlank { c.getString(5) ?: "" },
                    summary = ResourceLogic.cleanSummary(c.getString(3) ?: ""),
                    url = c.getString(1) ?: "",
                    domain = c.getString(5) ?: "",
                    kind = c.getString(4) ?: "",
                    tags = tags,
                    score = c.getDouble(6),
                    why = "",
                    at = ResourceLogic.isoTs(ts),
                    liked = acts.any { it.action == "like" },
                    faved = acts.any { it.action == "favorite" },
                    comments = acts.count { it.action == "comment" },
                )
            }
        }
        val base = card ?: throw IllegalArgumentException("没有这条资源：$id")
        val origins = ArrayList<ResourceOrigin>()
        d.rawQuery(
            "SELECT source_id,url,external_id,fetched_at FROM origins WHERE resource_id=? LIMIT 20",
            arrayOf(id),
        ).use { c ->
            while (c.moveToNext()) {
                origins.add(
                    ResourceOrigin(
                        c.getString(0), c.getString(1) ?: "", c.getString(2) ?: "",
                        ResourceLogic.isoTs(c.getDouble(3)),
                    )
                )
            }
        }
        return ResourceDetail(base, origins, actionRows(d, id))
    }

    private fun actionRows(d: SQLiteDatabase, id: String): List<ResourceAction> {
        val out = ArrayList<ResourceAction>()
        d.rawQuery(
            "SELECT action,text,created_at FROM actions WHERE resource_id=? ORDER BY id DESC LIMIT 50",
            arrayOf(id),
        ).use { c ->
            while (c.moveToNext()) {
                out.add(ResourceAction(c.getString(0), c.getString(1) ?: "",
                    ResourceLogic.isoTs(c.getDouble(2))))
            }
        }
        return out
    }

    fun act(id: String, action: String, text: String = ""): JSONObject {
        val d = open()
        val a = action.trim().lowercase(Locale.ROOT)
        val t = text.trim().take(500)
        val now = System.currentTimeMillis() / 1000.0
        if (a == "focus") {
            val tag = t.take(24)
            if (tag.isEmpty()) return JSONObject().put("ok", false).put("err", "关注主题不能为空")
            val removed = d.delete("focus", "tag=?", arrayOf(tag))
            val added = removed == 0
            if (added) {
                val cv = ContentValues().apply { put("tag", tag); put("created_at", now) }
                d.insert("focus", null, cv)
            }
            return JSONObject().put("ok", true).put("action", "focus")
                .put("added", added).put("focus", focusArray(d))
        }
        if (a != "like" && a != "favorite" && a != "comment") {
            return JSONObject().put("ok", false).put("err", "不支持的操作：$a")
        }
        if (a == "comment" && t.isEmpty()) {
            return JSONObject().put("ok", false).put("err", "评论不能为空")
        }
        val exists = d.rawQuery("SELECT 1 FROM resources WHERE id=?", arrayOf(id))
            .use { it.moveToFirst() }
        if (!exists) return JSONObject().put("ok", false).put("err", "没有这条资源：$id")
        // 再点一次就取消：赞/收藏是开关，不是一次性写入。
        if (a == "like" || a == "favorite") {
            val old = d.rawQuery(
                "SELECT id FROM actions WHERE resource_id=? AND action=? LIMIT 1",
                arrayOf(id, a),
            ).use { if (it.moveToFirst()) it.getLong(0) else -1L }
            if (old >= 0L) {
                d.delete("actions", "id=?", arrayOf(old.toString()))
                // 撤掉这次行为加过的兴趣权重，别让取消过的赞继续影响排序
                val back = if (a == "like") 1.0 else 2.0
                d.rawQuery("SELECT tag FROM tags WHERE resource_id=?", arrayOf(id)).use { c ->
                    while (c.moveToNext()) {
                        d.execSQL("UPDATE prefs SET weight=MAX(weight-?, 0), updated_at=? WHERE tag=?",
                            arrayOf<Any>(back, now, c.getString(0)))
                    }
                }
                return JSONObject().put("ok", true).put("removed", true)
                    .put("active", false).put("id", id).put("action", a)
            }
        }
        val cv = ContentValues().apply {
            put("resource_id", id); put("action", a); put("text", t); put("created_at", now)
        }
        d.insert("actions", null, cv)
        val gain = when (a) { "like" -> 1.0; "favorite" -> 2.0; else -> 0.5 }
        // minSdk 28 的 SQLite 不支持 UPSERT，用「先累加、没有再插」两步。
        d.rawQuery("SELECT tag FROM tags WHERE resource_id=?", arrayOf(id)).use { c ->
            while (c.moveToNext()) {
                val tag = c.getString(0)
                d.execSQL("UPDATE prefs SET weight=weight+?, updated_at=? WHERE tag=?",
                    arrayOf<Any>(gain, now, tag))
                val ins = ContentValues().apply {
                    put("tag", tag); put("weight", gain); put("updated_at", now)
                }
                d.insertWithOnConflict("prefs", null, ins, SQLiteDatabase.CONFLICT_IGNORE)
            }
        }
        d.execSQL("UPDATE exposures SET clicked=1 WHERE resource_id=?", arrayOf<Any>(id))
        return JSONObject().put("ok", true).put("removed", false)
            .put("active", true).put("id", id).put("action", a)
    }

    fun focus(): List<String> {
        val d = open()
        val out = ArrayList<String>()
        d.rawQuery("SELECT tag FROM focus ORDER BY created_at DESC", null).use { c ->
            while (c.moveToNext()) out.add(c.getString(0))
        }
        return out
    }

    private fun focusArray(d: SQLiteDatabase): JSONArray {
        val a = JSONArray()
        d.rawQuery("SELECT tag FROM focus ORDER BY created_at DESC", null).use { c ->
            while (c.moveToNext()) a.put(c.getString(0))
        }
        return a
    }

    fun stats(): JSONObject {
        val d = open()
        fun one(sql: String): Int = d.rawQuery(sql, null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
        val prefs = JSONArray()
        d.rawQuery("SELECT tag,weight FROM prefs ORDER BY weight DESC LIMIT 10", null).use { c ->
            while (c.moveToNext()) {
                prefs.put(JSONObject().put("tag", c.getString(0)).put("weight", c.getDouble(1)))
            }
        }
        return JSONObject().put("ok", true)
            .put("resources", one("SELECT COUNT(*) FROM resources"))
            .put("sources", one("SELECT COUNT(*) FROM sources"))
            .put("origins", one("SELECT COUNT(*) FROM origins"))
            .put("actions", one("SELECT COUNT(*) FROM actions"))
            .put("tags", one("SELECT COUNT(DISTINCT tag) FROM tags"))
            .put("prefs", prefs)
            .put("focus", focusArray(d))
    }

    // ---------------------------------------------------------------- 入库 / 去重

    /** 把一批标准资源写进本机库：链接规范化 + 内容指纹两层去重，来源与标签一并记。 */
    fun ingest(sourceId: String, items: JSONArray, sourceName: String = "",
               sourceKind: String = "crawler"): JSONObject {
        val d = open()
        val now = System.currentTimeMillis() / 1000.0
        upsertSource(d, sourceId, sourceName, sourceKind, now)
        var new = 0
        var merged = 0
        var skipped = 0
        d.beginTransaction()
        try {
            for (i in 0 until items.length()) {
                val it = items.optJSONObject(i)
                if (it == null) { skipped++; continue }
                val rawUrl = it.optString("url").trim()
                if (rawUrl.isBlank()) { skipped++; continue }
                val canonical = ResourceLogic.canonUrl(rawUrl)
                if (canonical.isBlank()) { skipped++; continue }
                val domain = ResourceLogic.domainOf(canonical)
                val title = it.optString("title").trim().ifBlank { domain }
                val summary = it.optString("summary").ifBlank { it.optString("desc") }.trim()
                val kind = it.optString("kind").ifBlank { it.optString("type") }.ifBlank { "article" }
                val quality = it.optDouble("quality", 0.5)
                val fp = ResourceLogic.fingerprint(title, summary, canonical)
                var rid = d.rawQuery(
                    "SELECT id FROM resources WHERE canonical_url=? OR fingerprint=? LIMIT 1",
                    arrayOf(canonical, fp),
                ).use { c -> if (c.moveToFirst()) c.getString(0) else "" }
                if (rid.isNotEmpty()) {
                    d.execSQL(
                        "UPDATE resources SET updated_at=?, " +
                            "title=CASE WHEN COALESCE(title,'')='' THEN ? ELSE title END, " +
                            "summary=CASE WHEN COALESCE(summary,'')='' THEN ? ELSE summary END " +
                            "WHERE id=?",
                        arrayOf<Any>(now, title, summary, rid),
                    )
                    merged++
                } else {
                    rid = ResourceLogic.rid(fp)
                    val cv = ContentValues().apply {
                        put("id", rid); put("canonical_url", canonical); put("fingerprint", fp)
                        put("title", title); put("summary", summary); put("kind", kind)
                        put("domain", domain); put("quality", quality)
                        put("published_at", it.optDouble("published_at", 0.0))
                        put("created_at", now); put("updated_at", now)
                    }
                    if (d.insertWithOnConflict("resources", null, cv,
                            SQLiteDatabase.CONFLICT_IGNORE) < 0) {
                        skipped++; continue
                    }
                    new++
                }
                val ext = it.optString("id").ifBlank { it.optString("external_id") }
                    .ifBlank { canonical }.take(200)
                val ocv = ContentValues().apply {
                    put("resource_id", rid); put("source_id", sourceId)
                    put("external_id", ext); put("url", canonical)
                    put("fetched_at", now); put("raw", it.toString().take(4000))
                }
                d.insertWithOnConflict("origins", null, ocv, SQLiteDatabase.CONFLICT_IGNORE)

                val tagList = ArrayList<String>()
                val arr = it.optJSONArray("tags")
                if (arr != null) {
                    for (t in 0 until arr.length()) {
                        arr.optString(t).takeIf { it.isNotBlank() }?.let { tagList.add(it) }
                    }
                } else {
                    tagList.addAll(ResourceLogic.tagsOf(it.optString("category")))
                }
                val seenTags = HashSet<String>()
                for (raw in tagList) {
                    val tag = raw.trim().take(24)
                    if (tag.isEmpty() || !seenTags.add(tag)) continue
                    val tcv = ContentValues().apply {
                        put("resource_id", rid); put("tag", tag); put("weight", 1.0)
                    }
                    d.insertWithOnConflict("tags", null, tcv, SQLiteDatabase.CONFLICT_IGNORE)
                }
            }
            d.setTransactionSuccessful()
        } finally {
            d.endTransaction()
        }
        return JSONObject().put("ok", true).put("new", new).put("merged", merged)
            .put("skipped", skipped).put("total", new + merged)
    }

    private fun upsertSource(d: SQLiteDatabase, sid: String, name: String, kind: String,
                             now: Double) {
        val up = ContentValues().apply {
            put("last_seen", now)
            put("name", name.ifBlank { sid })
        }
        val n = d.update("sources", up, "id=?", arrayOf(sid))
        if (n == 0) {
            val cv = ContentValues().apply {
                put("id", sid); put("name", name.ifBlank { sid }); put("kind", kind)
                put("config", "{}"); put("created_at", now); put("last_seen", now)
            }
            d.insertWithOnConflict("sources", null, cv, SQLiteDatabase.CONFLICT_IGNORE)
        }
    }

    /** 随包成果的落地情况；首次启动复制失败时这里会是 0，界面能直接看出来。 */
    fun seedStatus(): JSONObject {
        val d = open()
        val n = d.rawQuery("SELECT COUNT(*) FROM resources", null).use {
            if (it.moveToFirst()) it.getInt(0) else 0
        }
        return JSONObject().put("ok", true).put("scanned", n)
            .put("new", 0).put("merged", n)
    }

    /** 自动抓取节流：半小时一次，不反复吃流量，也不做后台定时任务。 */
    fun crawlDue(): Boolean {
        val d = open()
        val last = d.rawQuery("SELECT v FROM meta WHERE k='last_crawl'", null).use {
            if (it.moveToFirst()) it.getString(0).toLongOrNull() ?: 0L else 0L
        }
        return System.currentTimeMillis() - last > 30 * 60 * 1000L
    }

    fun markCrawl() {
        val d = open()
        val cv = ContentValues().apply {
            put("k", "last_crawl"); put("v", System.currentTimeMillis().toString())
        }
        d.insertWithOnConflict("meta", null, cv, SQLiteDatabase.CONFLICT_REPLACE)
    }
}
