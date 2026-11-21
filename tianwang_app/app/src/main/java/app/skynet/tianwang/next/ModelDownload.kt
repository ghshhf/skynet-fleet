package app.skynet.tianwang.next

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder
import org.json.JSONObject

/**
 * 从 HTTP(S) 链接直接下 GGUF 到本机模型目录 —— 老工程 `ModelDownloader` 的 Kotlin 版。
 *
 * 为什么不只用系统文件选择器：SAF 覆盖「电脑推 / 网盘 / 聊天软件收到」，
 * 但手机断网独立跑时，**链接是唯一不经过电脑的入口**。两条路并存，不是替换。
 *
 * 五条硬规矩：
 *   1. 先写 `.part`，校验通过才改名 —— 半截文件绝不能冒充可用模型；
 *   2. 有预期体积就比对 Content-Length，实际字节不足直接判失败；
 *   3. 写完验 GGUF 魔数，不是 GGUF 立刻删；
 *   4. **真续传**：`.part` 已存在就发 `Range: bytes=N-`，206 追加、200 覆盖；
 *      手动跟随重定向，保证 302 到 CDN 后 Range 头不丢（HttpURLConnection 自动跟随会丢）。
 *   5. **网络错 / 主动取消都保留 `.part`**，只有「不是 GGUF」才删 ——
 *      十几个 GB 的模型，断一次网不该从头再来（主人：下载一律可续传）。
 */
object ModelDownload {
    private const val BUF = 1 shl 20               // 1MB
    private const val PROGRESS_EVERY = 16L shl 20  // 16MB 报一次，别让界面重组过密
    private const val MAX_REDIRECT = 6

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
     * 打开连接并**手动**跟随重定向，每一跳都重新带上 Range。
     * 自动跟随（instanceFollowRedirects=true）在 hf-mirror → CDN 的 302 上会把 Range 丢掉，
     * 于是服务器回 200 全量，续传名存实亡。
     */
    private fun open(u: String, resume: Long): HttpURLConnection {
        var url = u
        for (hop in 0 until MAX_REDIRECT) {
            val c = URL(url).openConnection() as HttpURLConnection
            c.instanceFollowRedirects = false
            c.connectTimeout = 15_000
            c.readTimeout = 60_000
            c.requestMethod = "GET"
            c.setRequestProperty("Accept", "*/*")
            if (resume > 0) c.setRequestProperty("Range", "bytes=$resume-")
            val code = c.responseCode
            if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
                val loc = c.getHeaderField("Location")
                c.disconnect()
                if (loc.isNullOrBlank()) throw IOException("重定向没给 Location（HTTP $code）")
                url = URL(URL(url), loc).toString()
                continue
            }
            return c
        }
        throw IOException("重定向超过 $MAX_REDIRECT 跳，已停止")
    }

    /** 从 `Content-Range: bytes 123-456/789` 里取整文件长度；解析不出退回 Content-Length + 起点。 */
    private fun totalOf(c: HttpURLConnection, code: Int, resume: Long): Long {
        if (code == 206) {
            val cr = c.getHeaderField("Content-Range")
            val t = cr?.substringAfterLast('/', "")?.toLongOrNull()
            if (t != null && t > 0) return t
        }
        val len = c.contentLengthLong
        return if (len > 0) len + if (code == 206) resume else 0L else -1L
    }

    /**
     * 下载到 [dir]。`url` 必须 http/https；返回落盘后的 File。
     * [expectedBytes] > 0 时，先比整文件长度，下载后再比实际字节。
     * [isCancelled] 返回 true 时中止，但**保留 `.part`** 供下次续传。
     */
    suspend fun fetch(
        url: String,
        dir: File,
        name: String = "",
        expectedBytes: Long = 0,
        onProgress: (Long, Long) -> Unit = { _, _ -> },
        isCancelled: () -> Boolean = { false },
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
        val resume = if (part.isFile) part.length() else 0L

        var c: HttpURLConnection? = null
        var committed = false
        try {
            val conn = open(u, resume)
            c = conn
            val code = conn.responseCode
            if (code != 200 && code != 206 && code != 416) {
                val errBody = conn.errorStream?.use { it.readBytes().toString(Charsets.UTF_8) } ?: ""
                throw IOException("HTTP $code：${errBody.take(120)}")
            }
            // 服务器说「范围已满足」= 之前已经下完了，直接走校验 + 改名。
            if (code == 416) {
                val done = part.length()
                if (done <= 0) throw IOException("服务器返回 416 但本地没有 .part")
            } else {
                val total = totalOf(conn, code, resume)
                if (expectedBytes > 0 && total > 0 && total != expectedBytes) {
                    throw IOException("体积对不上：预期 $expectedBytes，服务器 $total")
                }
                // 带了 Range 但服务器回 200：它不支持续传，只能从头覆盖写。
                val append = code == 206
                var done = if (append) resume else 0L
                var lastTick = done
                if (append) {
                    Log.i("TianwangModel", "续传 $fileName：从 ${done / 1024 / 1024} MB 接上")
                }
                conn.inputStream.use { ins ->
                    FileOutputStream(part, append).use { out ->
                        val buf = ByteArray(BUF)
                        while (true) {
                            if (isCancelled()) throw IOException("已取消（.part 保留，可续传）")
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
                onProgress(done, total)
                if (expectedBytes > 0 && done < expectedBytes) {
                    throw IOException("下载不完整：拿到 $done / $expectedBytes 字节")
                }
                if (total > 0 && done < total) {
                    throw IOException("下载不完整：拿到 $done / $total 字节（.part 保留，可续传）")
                }
            }

            val magic = ByteArray(4)
            val head = runCatching {
                part.inputStream().use { it.read(magic) }
                String(magic, Charsets.US_ASCII)
            }.getOrDefault("")
            if (head != "GGUF") {
                part.delete()   // 只有「根本不是 GGUF」才删；网络问题一律保留
                throw IOException("不是 GGUF 文件（头部魔数不对），已丢弃")
            }

            if (!part.renameTo(target)) {
                part.inputStream().use { ins ->
                    target.outputStream().use { out -> ins.copyTo(out) }
                }
                part.delete()
            }
            committed = true
            Result.success(target)
        } catch (e: Throwable) {
            // 🔴 不删 .part：断网、超时、主动取消、发版杀进程，下次都能接着下。
            if (!committed) target.delete()
            Result.failure(e)
        } finally {
            c?.disconnect()
        }
    }
}

/**
 * 无头后台模型下载器：手机自己下，不经过电脑，也不依赖任何界面按钮。
 *
 * 为什么单独一层：界面里的下载挂在 Compose 的 rememberCoroutineScope 上，
 * 页面一退（或发版覆盖安装杀进程）就断；而十几个 GB 的 MoE 必须能跨进程存活。
 * 这里把任务挂在 [App.scope]，进度只写内存，真正的「断点」是磁盘上的 `.part` ——
 * 进程重启后状态丢了没关系，重新发一条 `self_model_dl_start` 会从 `.part` 自动续上。
 *
 * 双通道：hf-mirror 失败自动换 huggingface.co（同一文件、同一 `.part`），反之亦然。
 */
object ModelFetch {
    private const val TAG = "TianwangModel"

    data class State(
        val phase: String = "idle",     // idle / running / done / failed
        val url: String = "",
        val name: String = "",
        val done: Long = 0,
        val total: Long = 0,
        val resumed: Long = 0,
        val error: String = "",
        val at: Long = 0,
        val file: String = "",
    )

    private val lock = Any()
    @Volatile private var job: Job? = null
    @Volatile private var cancelFlag = false
    @Volatile private var state = State()

    private fun modelDir(): File {
        if (Demo.modelDir.isBlank()) {
            Demo.modelDir = File(App.ctx.getExternalFilesDir(null), "models").absolutePath
        }
        return File(Demo.modelDir).apply { mkdirs() }
    }

    private fun human(b: Long): String = when {
        b >= 1024L * 1024 * 1024 * 1024 -> "%.2f TB".format(b / 1024.0 / 1024 / 1024 / 1024)
        b >= 1024L * 1024 * 1024 -> "%.2f GB".format(b / 1024.0 / 1024 / 1024)
        b >= 1024L * 1024 -> "%.1f MB".format(b / 1024.0 / 1024)
        else -> "$b B"
    }

    /** 同一文件的备用通道；不认识的原样返回，只走一条。 */
    private fun channels(u: String): List<String> {
        val alt = when {
            u.contains("hf-mirror.com") -> u.replace("hf-mirror.com", "huggingface.co")
            u.contains("huggingface.co") -> u.replace("huggingface.co", "hf-mirror.com")
            else -> ""
        }
        return if (alt.isBlank() || alt == u) listOf(u) else listOf(u, alt)
    }

    private fun partOf(name: String): File = File(modelDir(), "$name.part")

    /** PhoneMcp 入口：三条工具都立即返回，绝不把下载耗时挂在广播回执上。 */
    fun run(name: String, args: JSONObject): ToolResult = when (name) {
        "self_model_dl_start" -> start(
            args.optString("url"),
            args.optString("name"),
            args.optLong("expected_bytes", 0L),
        )
        "self_model_dl_status" -> ToolResult(true, text = status())
        "self_model_dl_cancel" -> cancel()
        // 下完 ≠ 用上：self_model_ask 优先读 Prefs.lastModelId，不给无头通道
        // 一个「选模型」的入口，手机自己下完大模型还是会被旧模型顶着。
        "self_model_list" -> list()
        "self_model_select" -> select(args.optString("name"), args.optString("id"))
        else -> ToolResult(false, err = "未知模型下载工具：$name")
    }

    private fun ggufs(): List<File> {
        val dir = modelDir()
        return dir.listFiles()
            ?.filter { it.isFile && it.name.endsWith(".gguf", ignoreCase = true) }
            ?.sortedByDescending { it.length() } ?: emptyList()
    }

    private fun list(): ToolResult {
        val fs = ggufs()
        if (fs.isEmpty()) return ToolResult(true, text = "本机还没有 GGUF 模型")
        val cur = Prefs.lastModelId
        return ToolResult(true, text = buildString {
            append("本机模型 ").append(fs.size).append(" 个（大的在前）：\n")
            fs.forEach { f ->
                append(if (f.absolutePath == cur) "● 已选中 " else "○ ").append(f.name)
                    .append(" · ").append(human(f.length())).append('\n')
                append("  id=").append(f.absolutePath).append('\n')
            }
            append("当前选中：").append(cur.ifBlank { "（没设，默认取列表第一个）" })
        }.trimEnd())
    }

    private fun select(name: String, id: String): ToolResult {
        val fs = ggufs()
        val hit = when {
            id.isNotBlank() -> fs.firstOrNull { it.absolutePath == id }
            name.isNotBlank() -> fs.firstOrNull { it.name.contains(name, ignoreCase = true) }
            else -> null
        } ?: return ToolResult(false, err = "没找到匹配的 GGUF：给 name 子串或 id 全路径")
        Prefs.lastModelId = hit.absolutePath
        return ToolResult(true, text = buildString {
            append("已选中：").append(hit.name).append("（").append(human(hit.length())).append("）\n")
            append("id=").append(hit.absolutePath).append('\n')
            append("下一步 self_model_ask 会用这个模型")
        })
    }

    private fun start(url: String, rawName: String, expectedBytes: Long): ToolResult {
        val u = url.trim()
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            return ToolResult(false, err = "缺 url，且只收 http/https")
        }
        val fileName = rawName.trim().ifBlank { ModelDownload.filenameFromUrl(u) }
            .let { if (it.endsWith(".gguf", ignoreCase = true)) it else "$it.gguf" }
        val target = File(modelDir(), fileName)
        if (target.isFile && target.length() > 0) {
            state = state.copy(phase = "done", name = fileName, file = target.absolutePath,
                done = target.length(), total = target.length())
            return ToolResult(true, text = "已经在本地了，无需重复下载：$fileName（${human(target.length())}）")
        }
        synchronized(lock) {
            val cur = job
            if (cur != null && cur.isActive && state.name == fileName) {
                return ToolResult(true, text = "已在后台下载中：${status()}")
            }
        }
        val part = partOf(fileName)
        val resume = if (part.isFile) part.length() else 0L
        cancelFlag = false
        state = State(
            phase = "running", url = u, name = fileName,
            done = resume, resumed = resume, at = System.currentTimeMillis(),
        )
        val chans = channels(u)
        val j = App.scope.launch {
            var lastErr = ""
            for ((i, ch) in chans.withIndex()) {
                val r = ModelDownload.fetch(
                    url = ch, dir = modelDir(), name = fileName,
                    expectedBytes = expectedBytes,
                    onProgress = { d, t ->
                        state = state.copy(done = d, total = t, url = ch)
                    },
                    // 只认 cancelFlag：job 在 launch 返回后才赋值，用 isActive 判断会
                    // 误读到上一轮已结束的 Job，把新一轮下载刚起步就判成「取消」。
                    isCancelled = { cancelFlag },
                )
                r.fold(
                    onSuccess = { f ->
                        state = state.copy(phase = "done", file = f.absolutePath,
                            done = f.length(), total = f.length(), url = ch, at = System.currentTimeMillis())
                        Log.i(TAG, "下载完成：${f.name} ${human(f.length())}")
                        return@launch
                    },
                    onFailure = { e ->
                        lastErr = e.message ?: e.javaClass.simpleName
                        Log.w(TAG, "通道 ${i + 1}/${chans.size} 失败：$lastErr（.part 保留）")
                    },
                )
                if (cancelFlag) break
            }
            state = state.copy(
                phase = if (cancelFlag) "idle" else "failed",
                error = if (cancelFlag) "已取消，.part 保留，重新 start 可续传" else lastErr,
                at = System.currentTimeMillis(),
            )
        }
        job = j
        return ToolResult(true, text = buildString {
            append("已在后台启动，手机自己下，不经过电脑、不依赖界面。\n")
            append("文件：").append(fileName).append('\n')
            append("通道：").append(chans.size).append(" 条（").append(chans.joinToString(" → ") { URL(it).host }).append("）\n")
            if (resume > 0) append("续传起点：").append(human(resume)).append('\n')
            append("目标：").append(target.absolutePath).append('\n')
            append("查进度：self_model_dl_status")
        })
    }

    private fun status(): String {
        val s = state
        val part = if (s.name.isNotBlank()) partOf(s.name) else null
        val partLen = part?.takeIf { it.isFile }?.length() ?: 0L
        return buildString {
            append("阶段：").append(
                when (s.phase) {
                    "running" -> "下载中"
                    "done" -> "已完成"
                    "failed" -> "失败（.part 保留，可重新 start 续传）"
                    else -> "空闲"
                }
            ).append('\n')
            if (s.name.isNotBlank()) append("文件：").append(s.name).append('\n')
            if (s.phase == "running" || s.phase == "failed") {
                append("进度：").append(human(s.done))
                if (s.total > 0) append(" / ").append(human(s.total))
                    .append("（%.1f%%）".format(s.done * 100.0 / s.total))
                append('\n')
                if (s.resumed > 0) append("本次续传起点：").append(human(s.resumed)).append('\n')
                val sec = (System.currentTimeMillis() - s.at) / 1000.0
                if (sec > 1 && s.done > s.resumed) {
                    append("均速：").append(human(((s.done - s.resumed) / sec).toLong())).append("/s\n")
                }
                append("已跑：").append("%.0f".format(sec)).append(" 秒\n")
            }
            if (s.phase == "done") {
                append("落盘：").append(s.file.ifBlank { "—" }).append('\n')
                append("体积：").append(human(s.done)).append('\n')
            }
            if (partLen > 0) append("磁盘 .part：").append(human(partLen)).append('\n')
            if (s.error.isNotBlank()) append("最后错误：").append(s.error).append('\n')
            if (s.url.isNotBlank()) append("当前源：").append(runCatching { URL(s.url).host }.getOrDefault(s.url))
        }.trimEnd()
    }

    private fun cancel(): ToolResult {
        cancelFlag = true
        job?.cancel()
        return ToolResult(true, text = "已发取消信号；网络读会尽快退出，.part 保留，重新 start 可续传。")
    }
}
