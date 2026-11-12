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
import java.net.URLEncoder
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
            login(conn)
            "OpenList 连接正常"
        }
    }

    suspend fun list(conn: CloudConn, path: String): List<FileItem> = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") return@withContext webdavList(conn, path)
        val o = postAuthed(conn, "/api/fs/list",
            JSONObject().apply {
                put("path", path); put("password", ""); put("page", 1); put("per_page", 200)
            })
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
        val remote = runCatching {
            postAuthed(conn, "/api/fs/search",
                JSONObject().apply {
                    put("parent", "/"); put("keywords", kw); put("scope", 0)
                    put("page", 1); put("per_page", 200)
                })
        }.getOrNull()
        val arr = remote?.optJSONObject("data")?.optJSONArray("content")
        if (arr != null && arr.length() > 0) return@withContext parseItems(arr, "/", fromSearch = true)
        walkForName(conn, kw)
    }

    /** 取文件直链（/api/fs/get 的 raw_url），并把服务端生成的 host 换成手机能连上的 host。 */
    suspend fun fileUrl(conn: CloudConn, path: String): String = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") return@withContext joinWebdav(conn.host, path)
        val o = postAuthed(conn, "/api/fs/get",
            JSONObject().apply { put("path", path); put("password", "") })
        val raw = o.optJSONObject("data")?.optString("raw_url").orEmpty()
        if (raw.isBlank()) throw Exception("没拿到文件直链")
        rebaseHost(conn.host, raw)
    }

    /** 服务端索引不可用时的回退：广度优先遍历目录树，按文件名过滤。 */
    private fun walkForName(conn: CloudConn, kw: String): List<FileItem> {
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
                postAuthed(conn, "/api/fs/list",
                    JSONObject().apply {
                        put("path", dir); put("password", ""); put("page", 1); put("per_page", 200)
                    }).optJSONObject("data")?.optJSONArray("content")
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

    private fun ensureToken(conn: CloudConn): String =
        olTokens[conn.host] ?: login(conn)

    /** OpenList 登录并缓存 token（失败如实抛，不缓存空串冒充已登录）。 */
    private fun login(conn: CloudConn): String {
        val o = post(conn, "/api/auth/login",
            JSONObject().apply { put("username", conn.user); put("password", conn.pass) })
        val t = o.optJSONObject("data")?.optString("token").orEmpty()
        if (t.isBlank()) {
            throw Exception("登录失败：${o.optString("message").ifBlank { "无 token" }}")
        }
        olTokens[conn.host] = t
        return t
    }

    /**
     * 带 token 的业务请求：token 过期（OpenList 重启 / 服务端回收）时丢掉缓存重新登录一次。
     *
     * 旧实现只把错误翻译成「登录已过期，正在重新登录」，但**没有任何代码真的重登**，
     * 于是界面上一直显示这句话、所有操作都失败，只能杀 App 重来。
     */
    private fun postAuthed(conn: CloudConn, path: String, body: JSONObject): JSONObject {
        val token = ensureToken(conn)
        return try {
            post(conn, path, body, token)
        } catch (e: Exception) {
            if (!isAuthError(e.message.orEmpty())) throw e
            olTokens.remove(conn.host)
            post(conn, path, body, login(conn))
        }
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
        return parseWebdavXml(text, path)
    }

    /**
     * 解析 PROPFIND 的 multistatus 报文。
     *
     * 踩过的四个坑（都会表现成「WebDAV 连接正常，但目录是空的」）：
     *  1) 命名空间前缀大小写不统一：Nextcloud/ownCloud/sabre 用小写 `<d:response>`，
     *     Apache/x-net 用大写 `<D:response>`，还有不带前缀的默认命名空间；
     *  2) href 可能是绝对 URL（反代 / 不同 host），直接拼连接地址会拼出垃圾；
     *  3) href 是 URL 编码的，但**路径里的 `+` 是字面加号**，URLDecoder 会解成空格；
     *  4) 自身条目用连接地址去比，中文/空格目录名永远比不中，会被当成子项列出来。
     *
     * path 统一存**解码后的路径**（OpenList 也是逻辑路径），显示与后续拼接都一致。
     */
    internal fun parseWebdavXml(text: String, currentPath: String): List<FileItem> {
        val cur = normDavPath(currentPath)
        val out = ArrayList<FileItem>()
        for (m in RE_RESPONSE.findAll(text)) {
            val block = m.groupValues[1]
            val rawHref = RE_HREF.find(block)?.groupValues?.get(1) ?: continue
            val path = davHrefPath(rawHref)
            if (path.isEmpty()) continue
            val norm = normDavPath(path)
            if (norm == cur) continue // 跳过自身
            val name = norm.substringAfterLast('/')
            if (name.isBlank()) continue
            val isDir = path.endsWith("/") || RE_COLLECTION.containsMatchIn(block)
            val len = RE_LEN.find(block)?.groupValues?.get(1)?.toLongOrNull() ?: 0L
            out.add(FileItem(name = name, path = norm, isDir = isDir, size = len))
        }
        return out.distinctBy { it.path }
            .sortedWith(compareBy({ !it.isDir }, { it.name.lowercase(Locale.ROOT) }))
    }

    /** href → 解码后的绝对路径；绝对 URL 只取 path，`?`/`#` 之后是查询与片段。 */
    internal fun davHrefPath(href: String): String {
        var s = unescapeXml(href.trim())
        if (s.startsWith("http://", true) || s.startsWith("https://", true)) {
            s = s.replaceFirst(Regex("^[A-Za-z][A-Za-z0-9+.-]*://[^/]*"), "")
            if (!s.startsWith("/")) s = "/$s"
        }
        s = s.substringBefore('#').substringBefore('?')
        // 先保护字面 `+`（路径里的加号不是空格），再按 UTF-8 解百分号
        return runCatching { URLDecoder.decode(s.replace("+", "%2B"), "UTF-8") }.getOrDefault(s)
    }

    private fun unescapeXml(s: String): String {
        if (!s.contains('&')) return s
        return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"")
            .replace("&apos;", "'").replace("&amp;", "&")
    }

    private fun normDavPath(p: String): String {
        val t = p.trim().replace(Regex("/{2,}"), "/")
        if (t.isEmpty() || t == "/") return "/"
        val full = if (t.startsWith("/")) t else "/$t"
        return full.trimEnd('/').ifEmpty { "/" }
    }

    private val RE_RESPONSE = Regex(
        "<(?:[A-Za-z0-9_.-]+:)?response\\b[^>]*>(.*?)</(?:[A-Za-z0-9_.-]+:)?response>",
        RegexOption.DOT_MATCHES_ALL,
    )
    private val RE_HREF = Regex(
        "<(?:[A-Za-z0-9_.-]+:)?href\\b[^>]*>(.*?)</(?:[A-Za-z0-9_.-]+:)?href>",
        RegexOption.DOT_MATCHES_ALL,
    )
    private val RE_LEN = Regex(
        "<(?:[A-Za-z0-9_.-]+:)?getcontentlength\\b[^>]*>(\\d+)</(?:[A-Za-z0-9_.-]+:)?getcontentlength>",
    )
    private val RE_COLLECTION = Regex("<(?:[A-Za-z0-9_.-]+:)?collection\\b")

    /** 拼 WebDAV URL：路径段按 URL 编码，文件名里的空格/`#`/`?` 不会变成片段或查询。 */
    internal fun joinWebdav(host: String, path: String): String {
        val h = host.trimEnd('/')
        val p = if (path.startsWith("/")) path else "/$path"
        return h + p.split("/").joinToString("/") {
            URLEncoder.encode(it, "UTF-8").replace("+", "%20")
        }
    }

    /** token 失效的几种表现（HTTP 401 或 OpenList 业务码里的登录提示）。 */
    internal fun isAuthError(msg: String): Boolean {
        val m = msg.lowercase(Locale.ROOT)
        return m.contains("http 401") || m.contains("login please") ||
            m.contains("unauthorized") || m.contains("登录已过期") ||
            m.contains("token is invalid") || m.contains("invalid token")
    }

    /**
     * WebDAV 直链的 Basic 认证头。
     *
     * WebDAV 的文件 URL 本身不带凭据，缺 Authorization 一律 401。App 内 OkHttp 请求
     * 会加，但 DownloadManager（以及外部播放器）拿不到我们的 client，必须显式塞头，
     * 否则「下载到手机」只会存下一个 401 错误页。
     */
    internal fun basicAuth(conn: CloudConn): String = Credentials.basic(conn.user, conn.pass)
}
