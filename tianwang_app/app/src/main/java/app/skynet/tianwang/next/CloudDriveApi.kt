package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Credentials
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder
import java.util.ArrayDeque
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * 网盘聚合的网络层：把 OpenList 服务和 WebDAV 两种后端统一成「列目录 / 搜索 / 取直链」。
 *
 * - OpenList：自带 40+ 网盘驱动（阿里/百度/夸克/115/OneDrive/...），**任意对等节点**都能跑一份
 *   （手机 / PC / NAS 都一样），各网盘账号在所选节点的管理页登录——手机用内嵌 WebView 就能完成，
 *   不要求另一台设备先登录。这里登录拿 token 后调该节点的 /api/fs 系列接口。
 * - WebDAV：原生 PROPFIND 列目录；不支持搜索。
 *
 * OpenList v4 实测要点（与 AList v3 有差异）：
 *  1) /api/fs/list 的条目**不返回 path 字段**，必须由「父目录 + name」自己拼，否则点目录进不去；
 *  2) 业务失败也走 HTTP 200，错误在 JSON 的 code/message 里，只看 HTTP 会把错误静默吞成空列表；
 *  3) /api/fs/search 默认未建索引，返回 "search not available"，需回退到目录树遍历；
 *  4) /api/fs/get 返回 raw_url 直链，其 host 由服务端生成，手机端要换回连接里填的 host。
 *
 * 凭据只在此层使用、只落 CloudDrivePrefs，不进日志。
 */
object CloudDriveApi {
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .followRedirects(true)
        .build()
    private val json = "application/json".toMediaType()
    private val xml = "application/xml; charset=utf-8".toMediaType()
    // OpenList 登录 token 内存缓存：重启失效时下次操作重新 login。
    private val olTokens = HashMap<String, String>()

    /** 测试连接：OpenList 走登录，WebDAV 走 PROPFIND 根。 */
    suspend fun probe(conn: CloudConn): String = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") {
            webdavPropfind(conn, "/")
            "WebDAV 连接正常"
        } else {
            val o = post(conn, "/api/auth/login",
                JSONObject().apply { put("username", conn.user); put("password", conn.pass) })
            val t = o.optJSONObject("data")?.optString("token").orEmpty()
            if (t.isBlank()) {
                throw Exception("登录失败：${o.optString("message").ifBlank { "无 token" }}")
            }
            olTokens[conn.host] = t
            "OpenList 连接正常"
        }
    }

    suspend fun list(conn: CloudConn, path: String): List<FileItem> = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") return@withContext webdavList(conn, path)
        val token = ensureToken(conn)
        val o = post(conn, "/api/fs/list",
            JSONObject().apply {
                put("path", path); put("password", ""); put("page", 1); put("per_page", 200)
            }, token)
        o.optJSONObject("data")?.optJSONArray("content")?.let { parseItems(it, path) }
            ?: emptyList()
    }

    /**
     * 搜索：优先用 OpenList 服务端全文索引；服务端没建索引时（默认），
     * 回退为目录树遍历（深度 3 / 最多 40 个目录），保证「搜得到」。
     */
    suspend fun search(conn: CloudConn, kw: String): List<FileItem> = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") throw Exception("WebDAV 不支持搜索，请用 OpenList 连接")
        if (kw.isBlank()) return@withContext emptyList()
        val token = ensureToken(conn)
        val remote = runCatching {
            post(conn, "/api/fs/search",
                JSONObject().apply {
                    put("parent", "/"); put("keywords", kw); put("scope", 0)
                    put("page", 1); put("per_page", 200)
                }, token)
        }.getOrNull()
        val arr = remote?.optJSONObject("data")?.optJSONArray("content")
        if (arr != null && arr.length() > 0) return@withContext parseItems(arr, "/", fromSearch = true)
        walkForName(conn, token, kw)
    }

    /** 取文件直链（/api/fs/get 的 raw_url），并把服务端生成的 host 换成手机能连上的 host。 */
    suspend fun fileUrl(conn: CloudConn, path: String): String = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") return@withContext joinWebdav(conn.host, path)
        val token = ensureToken(conn)
        val o = post(conn, "/api/fs/get",
            JSONObject().apply { put("path", path); put("password", "") }, token)
        val raw = o.optJSONObject("data")?.optString("raw_url").orEmpty()
        if (raw.isBlank()) throw Exception("没拿到文件直链")
        rebaseHost(conn.host, raw)
    }

    /** 服务端索引不可用时的回退：广度优先遍历目录树，按文件名过滤。 */
    private fun walkForName(conn: CloudConn, token: String, kw: String): List<FileItem> {
        val out = ArrayList<FileItem>()
        val seen = HashSet<String>()
        val queue = ArrayDeque<Pair<String, Int>>()
        queue.add("/" to 0)
        var visited = 0
        while (queue.isNotEmpty() && visited < 40) {
            val (dir, depth) = queue.pollFirst() ?: break
            if (!seen.add(dir)) continue
            visited++
            val arr = runCatching {
                post(conn, "/api/fs/list",
                    JSONObject().apply {
                        put("path", dir); put("password", ""); put("page", 1); put("per_page", 200)
                    }, token).optJSONObject("data")?.optJSONArray("content")
            }.getOrNull() ?: continue
            for (f in parseItems(arr, dir)) {
                if (f.name.contains(kw, ignoreCase = true)) out.add(f)
                if (f.isDir && depth < 3) queue.add(f.path to depth + 1)
            }
        }
        return out.distinctBy { it.path }
            .sortedWith(compareBy({ !it.isDir }, { it.name.lowercase(Locale.ROOT) }))
    }

    private fun parseItems(arr: JSONArray, base: String, fromSearch: Boolean = false): List<FileItem> {
        val out = ArrayList<FileItem>()
        val root = base.trimEnd('/')
        for (i in 0 until arr.length()) {
            val it = arr.optJSONObject(i) ?: continue
            val name = it.optString("name")
            if (name.isBlank()) continue
            val explicit = it.optString("path").orEmpty()
            val parent = if (fromSearch) it.optString("parent").orEmpty() else ""
            val path = when {
                explicit.startsWith("/") -> explicit
                parent.isNotBlank() -> parent.trimEnd('/') + "/" + name
                else -> root + "/" + name
            }
            out.add(
                FileItem(
                    name = name,
                    path = path,
                    isDir = it.optBoolean("is_dir"),
                    size = it.optLong("size", 0L),
                )
            )
        }
        return out.sortedWith(compareBy({ !it.isDir }, { it.name.lowercase(Locale.ROOT) }))
    }

    private suspend fun ensureToken(conn: CloudConn): String {
        olTokens[conn.host]?.let { return it }
        probe(conn)
        return olTokens[conn.host].orEmpty()
    }

    // ---------------------------------------------------------------- OpenList HTTP
    /** 同时校验 HTTP 状态与业务 code，避免「HTTP 200 + code 500」被当成空数据静默吞掉。 */
    private fun post(conn: CloudConn, path: String, body: JSONObject, token: String? = null): JSONObject {
        val req = Request.Builder()
            .url(conn.host.trimEnd('/') + path)
            .post(okhttp3.RequestBody.create(json, body.toString()))
            .header("User-Agent", "TianwangNext/1.0")
        token?.let { req.header("Authorization", it) }
        client.newCall(req.build()).execute().use { r ->
            val txt = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw Exception("HTTP ${r.code} @$path")
            val o = runCatching { JSONObject(txt) }
                .getOrElse { throw Exception("返回不是 JSON：$path (${txt.take(60)})") }
            val code = o.optInt("code", 200)
            if (code != 200) throw Exception(friendly(o.optString("message"), code))
            return o
        }
    }

    private fun friendly(msg: String, code: Int): String = when {
        msg.contains("please add a storage", true) ->
            "这个节点还没挂载任何网盘，点「在本机登录 / 管理网盘」用手机内嵌页面添加即可"
        msg.contains("search not available", true) -> "服务端搜索未启用，已用目录遍历代替"
        msg.contains("Invalid username or password", true) -> "账号或密码不对"
        msg.contains("login please", true) -> "登录已过期，正在重新登录"
        msg.isBlank() -> "OpenList 返回错误 code=$code"
        else -> msg
    }

    /** raw_url 里的 host 换成手机填的连接地址（USB 转发 127.0.0.1 / 局域网 IP 都能通）。 */
    private fun rebaseHost(host: String, raw: String): String = runCatching {
        val u = URI(raw)
        val suffix = (u.rawPath ?: "") + (u.rawQuery?.let { "?$it" }.orEmpty())
        host.trimEnd('/') + suffix
    }.getOrDefault(raw)

    // ---------------------------------------------------------------- WebDAV HTTP
    private fun webdavPropfind(conn: CloudConn, path: String): String {
        val req = Request.Builder()
            .url(joinWebdav(conn.host, path))
            .method(
                "PROPFIND",
                okhttp3.RequestBody.create(
                    xml,
                    "<D:propfind xmlns:D=\"DAV:\"><D:prop><D:resourcetype/>" +
                        "<D:getcontentlength/></D:prop></D:propfind>",
                ),
            )
            .header("User-Agent", "TianwangNext/1.0")
            .header("Depth", "1")
            .header("Authorization", Credentials.basic(conn.user, conn.pass))
        client.newCall(req.build()).execute().use { r ->
            val txt = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw Exception("WebDAV HTTP ${r.code}")
            return txt
        }
    }

    private fun webdavList(conn: CloudConn, path: String): List<FileItem> {
        val text = webdavPropfind(conn, path)
        val base = joinWebdav(conn.host, path).removeSuffix("/")
        val out = ArrayList<FileItem>()
        for (m in Regex("<D:response>(.*?)</D:response>", RegexOption.DOT_MATCHES_ALL).findAll(text)) {
            val block = m.groupValues[1]
            val href = Regex("<D:href>(.*?)</D:href>", RegexOption.DOT_MATCHES_ALL)
                .find(block)?.groupValues?.get(1)?.let { decode(it) } ?: continue
            if (href == base || href == "$base/") continue // 跳过自身
            val name = href.removeSuffix("/").substringAfterLast("/")
            if (name.isBlank()) continue
            val isDir = Regex("<D:collection>").containsMatchIn(block)
            val len = Regex("<D:getcontentlength>(\\d+)</D:getcontentlength>")
                .find(block)?.groupValues?.get(1)?.toLongOrNull() ?: 0L
            out.add(FileItem(name = name, path = href, isDir = isDir, size = len))
        }
        return out.sortedWith(compareBy({ !it.isDir }, { it.name.lowercase(Locale.ROOT) }))
    }

    private fun joinWebdav(host: String, path: String): String {
        val h = host.trimEnd('/')
        val p = if (path.startsWith("/")) path else "/$path"
        return h + p
    }

    private fun decode(s: String): String = runCatching { URLDecoder.decode(s, "UTF-8") }.getOrDefault(s)
}
