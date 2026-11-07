package app.skynet.tianwang.next

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder

/**
 * 从 HTTP(S) 链接直接下 GGUF 到本机模型目录 —— 老工程 `ModelDownloader` 的 Kotlin 版。
 *
 * 为什么不只用系统文件选择器：SAF 覆盖「电脑推 / 网盘 / 聊天软件收到」，
 * 但手机断网独立跑时，**链接是唯一不经过电脑的入口**。两条路并存，不是替换。
 *
 * 三条硬规矩（照老工程）：
 *   1. 先写 `.part`，校验通过才改名 —— 半截文件绝不能冒充可用模型；
 *   2. 有预期体积就比对 Content-Length，实际字节不足直接判失败；
 *   3. 写完验 GGUF 魔数，不是 GGUF 立刻删。
 */
object ModelDownload {
    private const val BUF = 1 shl 20               // 1MB
    private const val PROGRESS_EVERY = 16L shl 20  // 16MB 报一次，别让界面重组过密

    /** URL 末段当文件名；拿不到就按时间戳兜底。只保留 .gguf。 */
    fun filenameFromUrl(url: String): String {
        val raw = runCatching {
            val path = URL(url.trim()).path
            path.substringAfterLast('/')
        }.getOrNull().orEmpty()
        val decoded = runCatching { URLDecoder.decode(raw, "UTF-8") }.getOrDefault(raw)
            .replace('/', '_').replace('\\', '_').trim()
        val base = decoded.ifBlank { "model-${System.currentTimeMillis()}" }
        return if (base.endsWith(".gguf", ignoreCase = true)) base else "$base.gguf"
    }

    /**
     * 下载到 [dir]。`url` 必须 http/https；返回落盘后的 File。
     * [expectedBytes] > 0 时，先比 Content-Length，下载后再比实际字节。
     */
    suspend fun fetch(
        url: String,
        dir: File,
        name: String = "",
        expectedBytes: Long = 0,
        onProgress: (Long, Long) -> Unit = { _, _ -> },
    ): Result<File> = withContext(Dispatchers.IO) {
        val u = url.trim()
        val scheme = runCatching { URL(u).protocol.lowercase() }.getOrNull()
        if (scheme != "http" && scheme != "https") {
            return@withContext Result.failure(IOException("只支持 http/https 链接（当前是「$scheme」）"))
        }
        if (!dir.isDirectory && !dir.mkdirs()) {
            return@withContext Result.failure(IOException("建不了模型目录：${dir.absolutePath}"))
        }

        val fileName = name.trim().ifBlank { filenameFromUrl(u) }
        var target = File(dir, fileName)
        var n = 1
        while (target.exists()) {
            target = File(dir, fileName.removeSuffix(".gguf") + "-${n++}.gguf")
        }
        val part = File(dir, target.name + ".part")

        var c: HttpURLConnection? = null
        var committed = false
        try {
            val conn = URL(u).openConnection() as HttpURLConnection
            c = conn
            conn.instanceFollowRedirects = true
            conn.connectTimeout = 12_000
            conn.readTimeout = 60_000
            conn.setRequestProperty("Accept", "*/*")
            val code = conn.responseCode
            if (code != 200) {
                val errBody = conn.errorStream?.use { it.readBytes().toString(Charsets.UTF_8) } ?: ""
                throw IOException("HTTP $code：${errBody.take(120)}")
            }
            val total = conn.contentLengthLong
            if (expectedBytes > 0 && total > 0 && total != expectedBytes) {
                throw IOException("体积对不上：预期 $expectedBytes，服务器 $total")
            }

            var done = 0L
            var lastTick = 0L
            conn.inputStream.use { ins ->
                part.outputStream().use { out ->
                    val buf = ByteArray(BUF)
                    while (true) {
                        val k = ins.read(buf)
                        if (k < 0) break
                        out.write(buf, 0, k)
                        done += k
                        if (done - lastTick >= PROGRESS_EVERY) {
                            lastTick = done
                            onProgress(done, total)
                        }
                    }
                }
            }
            if (expectedBytes > 0 && done < expectedBytes) {
                throw IOException("下载不完整：拿到 $done / $expectedBytes 字节")
            }
            if (total > 0 && done < total) {
                throw IOException("下载不完整：拿到 $done / $total 字节")
            }

            val magic = ByteArray(4)
            val head = runCatching {
                part.inputStream().use { it.read(magic) }
                String(magic)
            }.getOrDefault("")
            if (head != "GGUF") {
                throw IOException("不是 GGUF 文件（头部魔数不对），已丢弃")
            }

            if (!part.renameTo(target)) {
                // 某些文件系统 rename 会失败：退回复制，再删临时文件
                part.inputStream().use { ins ->
                    target.outputStream().use { out -> ins.copyTo(out) }
                }
                part.delete()
            }
            committed = true
            onProgress(done, total)
            Result.success(target)
        } catch (e: Throwable) {
            part.delete()
            // 复制中途出错会留下半截目标文件；committed 之后才允许留名。
            // 目标名是我们挑的空位，删掉不会误伤已有模型。
            if (!committed) target.delete()
            Result.failure(e)
        } finally {
            c?.disconnect()
        }
    }
}
