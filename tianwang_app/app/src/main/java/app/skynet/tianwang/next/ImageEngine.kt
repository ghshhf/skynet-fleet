package app.skynet.tianwang.next

import android.content.ContentValues
import android.content.Context
import android.graphics.BitmapFactory
import android.os.Environment
import android.provider.MediaStore
import android.util.Base64
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * 本机出图：手机自己 exec 自带的 diffusion 引擎，自己发请求、自己存图。
 *
 * 引擎二进制是随 APK 走的 `libstable_diffusion_core.so`（jniLibs），
 * 模型/QNN 库是本机数据（外部私有目录 ld/）。全程没有第二个 App 需要先装好、
 * 先打开、先开 host mode —— 能力就在这台手机、这个 App 里。
 *
 * 直驱协议取自电脑上跑通过的 `fleet/engine.py` + `image_gen_runner.py`：
 *   POST /generate (SSE) → event: complete → data: {"type":"complete","image":"<png base64>"}
 * 分辨率不是请求参数而是启动参数，非 512x512 必须带对应 patch，否则出彩色噪声。
 */
object ImageEngine {

    const val DEFAULT_PORT = 8083
    private const val EXEC_SD = "libstable_diffusion_core.so"
    private const val PREF = "tianwang_image"

    /** 一台手机上同一时刻只能有一个 diffusion 进程（HTP 独占）。 */
    @Volatile private var process: Process? = null
    @Volatile private var started: StartKey? = null
    private val logLines = ArrayDeque<String>()
    @Volatile private var lastErr = ""

    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(300, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    private data class StartKey(val model: String, val w: Int, val h: Int, val type: String, val port: Int)

    data class ImageModel(val name: String, val dir: File, val sizes: List<Pair<Int, Int>>)

    // ---------- 配置：路径是可改的数据，不是焊死在代码里的常量 ----------

    private fun sp(ctx: Context) = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)

    fun root(ctx: Context): File {
        val saved = sp(ctx).getString("root", "") ?: ""
        return if (saved.isNotBlank()) File(saved)
        else File(ctx.getExternalFilesDir(null), "ld")
    }

    fun setRoot(ctx: Context, path: String) {
        sp(ctx).edit().putString("root", path.trim()).apply()
        stop()
    }

    fun port(ctx: Context): Int = sp(ctx).getInt("port", DEFAULT_PORT)

    fun setPort(ctx: Context, p: Int) {
        sp(ctx).edit().putInt("port", p.coerceIn(1024, 65535)).apply()
        stop()
    }

    fun type(ctx: Context): String = sp(ctx).getString("type", "sd15npu") ?: "sd15npu"

    fun setType(ctx: Context, t: String) {
        sp(ctx).edit().putString("type", t).apply()
        stop()
    }

    // ---------- 扫描本机模型 ----------

    /** 只认真正带 unet.bin 的目录；upscaler_* 不是底模，不混进来。 */
    fun scan(ctx: Context): List<ImageModel> = runCatching {
        val md = File(root(ctx), "models")
        if (!md.isDirectory) return emptyList()
        md.listFiles { f -> f.isDirectory && File(f, "unet.bin").isFile }
            ?.sortedBy { it.name }
            ?.map { d ->
                val sizes = mutableListOf(512 to 512)
                d.listFiles { f -> f.isFile && f.name.endsWith(".patch") }?.forEach { f ->
                    ImageEngineLogic.parsePatchName(f.name.removeSuffix(".patch"))?.let { sizes.add(it) }
                }
                ImageModel(d.name, d, sizes.distinct().sortedWith(compareBy({ it.first }, { it.second })))
            } ?: emptyList()
    }.getOrDefault(emptyList())

    /** QNN 库优先用 APK 解包出来的（能 dlopen）；用户自定义目录里那份也能兜底。 */
    fun qnnDir(ctx: Context): String {
        val lib = ctx.applicationInfo.nativeLibraryDir
        return if (File(lib, "libQnnHtpV69.so").isFile) lib
        else File(root(ctx), "runtime_libs").absolutePath
    }

    fun hasRuntime(ctx: Context): Boolean =
        File(qnnDir(ctx), "libQnnHtpV69.so").isFile

    // ---------- 引擎进程 ----------

    fun status(ctx: Context): String {
        val key = started
        val alive = process?.isAlive == true
        return when {
            key == null -> "引擎未启动"
            !alive -> "引擎已退出${if (lastErr.isBlank()) "" else " · $lastErr"}"
            else -> "${key.model} · ${key.w}×${key.h} · ${key.type} · :${key.port}"
        }
    }

    fun lastLog(): String = synchronized(logLines) { logLines.takeLast(12).joinToString("\n") }

    private fun note(line: String) {
        synchronized(logLines) {
            logLines.addLast(line.take(300))
            while (logLines.size > 100) logLines.removeFirst()
        }
        if (line.contains("error", true) || line.contains("failed", true) ||
            line.contains("denied", true) || line.contains("0x3", true)) {
            lastErr = line.take(200)
        }
    }

    fun stop() {
        runCatching { process?.destroyForcibly() }
        process = null
        started = null
    }

    /** 参数没变且活着就复用；变了先 kill 再拉（HTP 独占，kill 后留 1.5s）。 */
    suspend fun ensureStarted(
        ctx: Context, model: String, w: Int, h: Int,
    ): Result<String> = withContext(Dispatchers.IO) {
        val p = port(ctx)
        val t = type(ctx)
        val cur = started
        if (cur != null && cur.model == model && cur.w == w && cur.h == h &&
            cur.type == t && cur.port == p && process?.isAlive == true && health(p)) {
            return@withContext Result.success("引擎已就绪（复用）")
        }
        stop()
        Thread.sleep(1500)

        val rt = root(ctx)
        val modelDir = File(rt, "models/$model")
        if (!File(modelDir, "unet.bin").isFile) {
            return@withContext Result.failure(IllegalStateException("模型目录里没有 unet.bin：${modelDir.absolutePath}"))
        }
        val lib = ctx.applicationInfo.nativeLibraryDir
        // QNN 库随 APK 解包到 nativeLibraryDir（/sdcard 是 noexec，dlopen 会失败）；
        // 用户改了资源目录、自己放了另一套 QNN 时，退回那份。
        val qnn = qnnDir(ctx)
        val exe = File(lib, EXEC_SD)
        if (!exe.isFile) {
            return@withContext Result.failure(IllegalStateException("APK 里没有出图引擎 ${exe.name}"))
        }

        val cmd = mutableListOf(
            exe.absolutePath, "--type", t,
            "--model_dir", modelDir.absolutePath,
            "--lib_dir", qnn,
            "--port", p.toString(),
        )
        ImageEngineLogic.patchFor(modelDir, w, h)?.let { cmd += listOf("--patch", it.absolutePath) }

        lastErr = ""
        synchronized(logLines) { logLines.clear() }
        val proc = runCatching {
            val pb = ProcessBuilder(cmd)
            pb.environment()["LD_LIBRARY_PATH"] =
                listOf(lib, qnn, "/vendor/lib64", "/system/lib64").joinToString(":")
            pb.environment()["ADSP_LIBRARY_PATH"] =
                "$qnn;/vendor/dsp/cdsp;/vendor/lib/rfsa/adsp;/system/lib/rfsa/adsp;"
            pb.environment()["DSP_LIBRARY_PATH"] = "$qnn;/vendor/dsp/cdsp"
            pb.redirectErrorStream(true)
            pb.start()
        }.getOrElse {
            return@withContext Result.failure(IllegalStateException("拉不起引擎：${it.message}"))
        }
        process = proc
        started = StartKey(model, w, h, t, p)
        // 引擎日志会一直往 stdout 写；不读会把管道塞满、进程卡死。
        Thread {
            runCatching {
                proc.inputStream.bufferedReader().use { r ->
                    while (true) note(r.readLine() ?: break)
                }
            }
        }.apply { isDaemon = true; name = "tw-img-engine-log" }.start()

        // 冷启动要加载约 1.2G 权重，实测 22~25s；给到 90s。
        val deadline = System.currentTimeMillis() + 90_000
        while (System.currentTimeMillis() < deadline) {
            if (!proc.isAlive) {
                return@withContext Result.failure(
                    IllegalStateException("引擎进程退出了 · ${lastErr.ifBlank { lastLog().takeLast(200) }}"))
            }
            if (health(p)) return@withContext Result.success("引擎就绪 · ${w}×${h}")
            Thread.sleep(1000)
        }
        Result.failure(IllegalStateException("引擎 90s 内没就绪 · ${lastErr.ifBlank { lastLog().takeLast(200) }}"))
    }

    private fun health(p: Int): Boolean = runCatching {
        client.newCall(Request.Builder().url("http://127.0.0.1:$p/health").get().build())
            .execute().use { it.isSuccessful }
    }.getOrDefault(false)

    // ---------- 出图 ----------

    suspend fun generate(
        ctx: Context,
        prompt: String,
        negative: String,
        w: Int, h: Int,
        steps: Int,
        cfg: Float,
        seed: Long,
        onProgress: (String) -> Unit = {},
    ): Result<ByteArray> = withContext(Dispatchers.IO) {
        val body = ImageEngineLogic.payload(prompt, negative, w, h, steps, cfg, seed)
        val req = Request.Builder()
            .url("http://127.0.0.1:${port(ctx)}/generate")
            .post(body.toRequestBody("application/json".toMediaType()))
            .header("Accept", "text/event-stream")
            .build()
        runCatching {
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) {
                    error("引擎 HTTP ${resp.code} · ${resp.body?.string()?.take(200).orEmpty()}")
                }
                val src = resp.body?.source() ?: error("引擎返回空响应")
                var event = ""
                var image: String? = null
                var meta = JSONObject()
                while (image == null) {
                    val line = src.readUtf8Line() ?: break
                    val r = ImageEngineLogic.sseLine(line, event)
                    event = r.first
                    val obj = r.second ?: continue
                    if (obj.optString("type") == "error" || event == "error") {
                        error("引擎报错：${obj.optString("message").ifBlank { "未知" }}")
                    }
                    if (obj.optString("type") == "complete" || event == "complete") {
                        image = obj.optString("image")
                        meta = obj
                    } else {
                        val step = obj.optInt("step", -1)
                        val total = obj.optInt("total", -1)
                        if (step > 0) onProgress("出图中 · $step/$total 步")
                    }
                }
                if (image.isNullOrBlank()) error("SSE 流结束但没收到图像 · ${lastErr.ifBlank { lastLog().takeLast(200) }}")
                val bytes = Base64.decode(image, Base64.DEFAULT)
                if (bytes.size < 1024) error("图像数据异常（${bytes.size} 字节）")
                bytes
            }
        }
    }

    fun decode(bytes: ByteArray): ImageBitmap? = runCatching {
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size)?.asImageBitmap()
    }.getOrNull()

    /** 保存到系统相册；失败就退回 App 私有 Pictures 目录，不让图丢。 */
    fun save(ctx: Context, bytes: ByteArray): Result<String> = runCatching {
        val name = "tianwang_${System.currentTimeMillis()}.png"
        val values = ContentValues().apply {
            put(MediaStore.Images.Media.DISPLAY_NAME, name)
            put(MediaStore.Images.Media.MIME_TYPE, "image/png")
            put(MediaStore.Images.Media.RELATIVE_PATH,
                Environment.DIRECTORY_PICTURES + "/Tianwang")
        }
        val uri = ctx.contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values)
        if (uri != null) {
            ctx.contentResolver.openOutputStream(uri)?.use { it.write(bytes) }
                ?: error("打不开相册输出流")
            "相册/Pictures/Tianwang/$name"
        } else {
            val dir = File(ctx.getExternalFilesDir(Environment.DIRECTORY_PICTURES), "Tianwang")
            dir.mkdirs()
            File(dir, name).writeBytes(bytes)
            dir.absolutePath + "/$name"
        }
    }
}

/** 纯逻辑：patch 规则、请求体、SSE 行解析，全部可 JVM 单测。 */
object ImageEngineLogic {

    /** "768" → 768×768；"512x768" → 512×768；别的返回 null。 */
    fun parsePatchName(stem: String): Pair<Int, Int>? {
        val s = stem.trim()
        val parts = s.split('x', 'X')
        return when {
            parts.size == 1 && parts[0].toIntOrNull() != null ->
                parts[0].toInt() to parts[0].toInt()
            parts.size == 2 && parts[0].toIntOrNull() != null && parts[1].toIntOrNull() != null ->
                parts[0].toInt() to parts[1].toInt()
            else -> null
        }
    }

    /** 512×512 用模型自带的 unet.bin，不需要 patch；其余按文件挑，没有就返回 null（宁可不出噪声图）。 */
    fun patchFor(modelDir: File, w: Int, h: Int): File? {
        if (w == 512 && h == 512) return null
        val exact = File(modelDir, "${w}x${h}.patch")
        if (exact.isFile) return exact
        if (w == h) {
            val square = File(modelDir, "$w.patch")
            if (square.isFile) return square
        }
        return null
    }

    fun payload(
        prompt: String, negative: String, w: Int, h: Int,
        steps: Int, cfg: Float, seed: Long,
    ): String = JSONObject().apply {
        put("prompt", prompt)
        put("negative_prompt", negative)
        put("steps", steps.coerceIn(1, 50))
        put("cfg", cfg)
        put("width", w)
        put("height", h)
        put("output_format", "png")
        put("show_diffusion_process", false)
        if (seed >= 0) put("seed", seed)
    }.toString()

    /**
     * 解析一行 SSE。返回 (当前 event, data 里的 JSON 或 null)。
     * 非 data 行也要把 event 带下去，complete/error 靠 event 名判断。
     */
    fun sseLine(line: String, event: String): Pair<String, JSONObject?> {
        val s = line.trimEnd('\r')
        return when {
            s.startsWith("event:") -> s.substring(6).trim() to null
            !s.startsWith("data:") -> event to null
            else -> {
                val raw = s.substring(5).trim()
                if (raw.isBlank() || raw == "[DONE]") event to null
                else event to runCatching { JSONObject(raw) }.getOrNull()
            }
        }
    }
}
