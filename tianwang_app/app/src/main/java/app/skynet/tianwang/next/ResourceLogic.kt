package app.skynet.tianwang.next

import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder
import java.net.URLEncoder
import java.security.MessageDigest
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 资源流的纯逻辑：URL 规范化、内容指纹、摘要清洗、打分、同站限流。
 *
 * 不碰 Android、数据库、网络，所以 JVM 单测能直接跑。口径与电脑侧
 * `fleet/resource_center.py` 一致：两个节点各存各的库，但同一条内容必须算出
 * 同一个 id、同一个分，共享合并时才会判成同一条，不会越同步越重复。
 */
object ResourceLogic {
    private val TRACK = setOf(
        "fbclid", "gclid", "yclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
        "spm", "scm", "from", "share_token", "share_source", "utm_medium",
    )
    private const val HEX = "0123456789abcdef"

    /** 规范链接：补协议、去 www/默认端口、去跟踪参数、路径归一、query 排序。 */
    fun canonUrl(raw: String): String {
        var s = raw.trim()
        if (s.isEmpty()) return ""
        if (!s.contains("://")) s = "https://$s"
        return runCatching {
            val u = URI(s)
            var host = (u.host ?: "").lowercase(Locale.ROOT)
            if (host.isBlank()) {
                host = (u.rawAuthority ?: "").lowercase(Locale.ROOT)
                    .substringAfterLast('@').substringBefore(':')
            }
            if (host.startsWith("www.")) host = host.substring(4)
            if (host.isBlank()) return@runCatching ""
            var port = u.port
            if (port == 80 || port == 443) port = -1
            var path = (u.rawPath ?: "/").replace(Regex("/+"), "/").trimEnd('/')
            if (path.isEmpty()) path = "/"
            val q = queryPairs(u.rawQuery).filterNot { (k, _) ->
                val key = k.lowercase(Locale.ROOT)
                key.startsWith("utm_") || key in TRACK
            }.sortedWith(compareBy({ it.first }, { it.second }))
                .joinToString("&") { (k, v) -> "${enc(k)}=${enc(v)}" }
            buildString {
                append("https://").append(host)
                if (port > 0) append(':').append(port)
                append(if (path.startsWith("/")) path else "/$path")
                if (q.isNotEmpty()) append('?').append(q)
            }
        }.getOrElse { "" }
    }

    private fun queryPairs(raw: String?): List<Pair<String, String>> {
        if (raw.isNullOrBlank()) return emptyList()
        return raw.split('&').mapNotNull { seg ->
            if (seg.isBlank()) return@mapNotNull null
            val i = seg.indexOf('=')
            val k = if (i < 0) seg else seg.substring(0, i)
            val v = if (i < 0) "" else seg.substring(i + 1)
            dec(k) to dec(v)
        }
    }

    private fun dec(s: String): String = runCatching {
        URLDecoder.decode(s.replace("+", "%20"), "UTF-8")
    }.getOrDefault(s)

    private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

    fun domainOf(canonical: String): String = runCatching {
        (URI(canonical).host ?: "").lowercase(Locale.ROOT).removePrefix("www.")
    }.getOrDefault("")

    fun sha256(s: String): String {
        val b = MessageDigest.getInstance("SHA-256").digest(s.toByteArray(Charsets.UTF_8))
        val out = StringBuilder(b.size * 2)
        for (x in b) {
            val v = x.toInt() and 0xff
            out.append(HEX[v ushr 4]).append(HEX[v and 0x0f])
        }
        return out.toString()
    }

    /** 有摘要按「标题+摘要」跨站去重，没摘要退回规范链接。 */
    fun fingerprint(title: String, summary: String, canonical: String): String {
        val t = title.trim().lowercase(Locale.ROOT)
        val sm = summary.trim().lowercase(Locale.ROOT)
        return sha256(if (sm.isNotEmpty()) t + "\n" + sm.take(800) else canonical)
    }

    fun rid(fp: String): String = "r_" + fp.take(16)

    /**
     * 已爬库里有一批抖音条目把元数据 JSON 直接塞进了 summary。
     * 那种字符串不能原样摆到卡片上，这里翻成人能读的一行。
     */
    fun cleanSummary(s: String, limit: Int = 220): String {
        val t = s.trim()
        if (t.isEmpty()) return ""
        if (t.startsWith("{")) {
            val o = runCatching { JSONObject(t) }.getOrNull()
            if (o != null) {
                val bits = ArrayList<String>()
                o.optString("author").takeIf { it.isNotBlank() }?.let { bits.add("作者 $it") }
                if (o.has("digg")) bits.add("赞 ${o.optInt("digg")}")
                if (o.has("comment")) bits.add("评论 ${o.optInt("comment")}")
                if (o.optInt("images") > 0) bits.add("图 ${o.optInt("images")} 张")
                if (bits.isNotEmpty()) return bits.joinToString(" · ")
            }
        }
        val flat = t.replace(Regex("\\s+"), " ")
        return if (flat.length > limit) flat.take(limit) + "…" else flat
    }

    fun tagsOf(raw: String, extra: List<String> = emptyList()): List<String> {
        val out = LinkedHashSet<String>()
        (raw.split(Regex("[/、,，|]+")) + extra).forEach {
            val t = it.trim().take(24)
            if (t.isNotEmpty()) out.add(t)
        }
        return out.toList().take(8)
    }

    fun isoTs(ts: Double): String {
        if (ts <= 0.0) return ""
        return runCatching {
            SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault())
                .format(Date((ts * 1000.0).toLong()))
        }.getOrDefault("")
    }

    /** 与电脑侧同一套透明打分：兴趣 / 质量 / 新鲜 / 没刷过 / 探索 / 主题匹配 / 同站惩罚。 */
    fun score(
        interest: Double, quality: Double, fresh: Double, novel: Double,
        explore: Double, match: Double, domainPen: Double,
    ): Double = 0.35 * interest + 0.20 * quality + 0.20 * fresh + 0.15 * novel +
        0.10 * explore + 0.25 * match - domainPen

    /** 确定性探索，不靠随机数：同一个 session 每次打开顺序一致。 */
    fun exploreOf(id: String, session: String): Double =
        (sha256(id + session).take(4).toInt(16) % 100) / 100.0

    /** 同站最多 2 条，不够再从剩余里补，保证一屏不全是同一个站。 */
    fun <T> diverse(items: List<T>, limit: Int, domain: (T) -> String): List<T> {
        val picked = ArrayList<T>()
        val rest = ArrayList<T>()
        val per = HashMap<String, Int>()
        for (it in items) {
            val d = domain(it)
            val n = per[d] ?: 0
            if (n < 2) {
                per[d] = n + 1
                picked.add(it)
                if (picked.size >= limit) return picked
            } else rest.add(it)
        }
        for (it in rest) {
            if (picked.size >= limit) break
            picked.add(it)
        }
        return picked
    }
}
