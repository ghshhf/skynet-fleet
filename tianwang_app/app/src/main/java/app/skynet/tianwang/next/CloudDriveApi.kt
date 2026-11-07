package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Credentials
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLDecoder
import java.util.Locale
import java.util.concurrent.TimeUnit

/**
 * 网盘聚合的网络层：把 OpenList 服务和 WebDAV 两种后端统一成「列目录 / 搜索」。
 *
 * - OpenList：自带 40+ 网盘驱动（阿里/百度/夸克/115/OneDrive/...），跑在自己 NAS 上，
 *   手机端只做前端：登录拿 token → 调 /api/fs/list、/api/fs/search。
 * - WebDAV：原生 PROPFIND 列目录（各网盘原生 WebDAV 支持不一）；不支持搜索。
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
        o.optJSONObject("data")?.optJSONArray("content")?.let { arr -> parseItems(arr) }
            ?: emptyList()
    }

    suspend fun search(conn: CloudConn, kw: String): List<FileItem> = withContext(Dispatchers.IO) {
        if (conn.type == "webdav") throw Exception("WebDAV 不支持搜索，请用 OpenList 连接")
        val token = ensureToken(conn)
        val o = post(conn, "/api/fs/search",
            JSONObject().apply {
                put("parent", "/"); put("keywords", kw); put("scope", 0)
                put("page", 1); put("per_page", 200)
            }, token)
        o.optJSONObject("data")?.optJSONArray("content")?.let { arr -> parseItems(arr) }
            ?: emptyList()
    }

    private fun parseItems(arr: JSONArray): List<FileItem> {
        val out = ArrayList<FileItem>()
        for (i in 0 until arr.length()) {
            val it = arr.optJSONObject(i) ?: continue
            out.add(
                FileItem(
                    name = it.optString("name"),
                    path = it.optString("path"),
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
    private fun post(conn: CloudConn, path: String, body: JSONObject, token: String? = null): JSONObject {
        val req = Request.Builder()
            .url(conn.host.trimEnd('/') + path)
            .post(okhttp3.RequestBody.create(json, body.toString()))
            .header("User-Agent", "TianwangNext/1.0")
        token?.let { req.header("Authorization", it) }
        client.newCall(req.build()).execute().use { r ->
            val txt = r.body?.string().orEmpty()
            if (!r.isSuccessful) throw Exception("HTTP ${r.code} @${conn.host}$path")
            return JSONObject(txt)
        }
    }

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
        val xml = webdavPropfind(conn, path)
        val base = joinWebdav(conn.host, path).removeSuffix("/")
        val out = ArrayList<FileItem>()
        for (m in Regex("<D:response>(.*?)</D:response>", RegexOption.DOT_MATCHES_ALL).findAll(xml)) {
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
