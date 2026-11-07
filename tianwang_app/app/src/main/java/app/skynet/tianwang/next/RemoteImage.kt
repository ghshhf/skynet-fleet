package app.skynet.tianwang.next

import android.content.Context
import android.graphics.BitmapFactory
import android.util.LruCache
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.ui.draw.alpha

/**
 * 卡片图片：手机自己下载、自己缓存，不引第三方图片库。
 * 内存 LRU + 私有目录磁盘缓存，重复滑动不再重复吃流量。
 */
object ImageCache {
    private const val MAX_BYTES = 2 * 1024 * 1024
    private const val TTL = 7 * 24 * 3600_000L
    private val mem = LruCache<String, ImageBitmap>(64)
    private val client = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .followRedirects(true)
        .build()
    // 同时最多下 2 张图；卡片图不该拖慢正文加载。
    private val gate = Semaphore(2)

    private fun key(url: String): String {
        val d = MessageDigest.getInstance("SHA-1").digest(url.toByteArray())
        return d.joinToString("") { "%02x".format(it) }
    }

    private fun file(ctx: Context, url: String): File =
        File(File(ctx.cacheDir, "preview_img").apply { mkdirs() }, key(url) + ".bin")

    private suspend fun download(url: String): ByteArray? = gate.withPermit {
        val req = Request.Builder().url(url)
            .header("User-Agent", "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36")
            .build()
        client.newCall(req).execute().use { r ->
            if (!r.isSuccessful) return null
            val body = r.body ?: return null
            if (body.contentLength() > MAX_BYTES) return null
            val t = body.contentType()?.toString() ?: ""
            if (t.isNotBlank() && !t.startsWith("image/")) return null
            val out = ByteArrayOutputStream()
            val buf = ByteArray(8192)
            body.byteStream().use { s ->
                var total = 0
                while (total < MAX_BYTES) {
                    val n = s.read(buf)
                    if (n < 0) break
                    out.write(buf, 0, n)
                    total += n
                }
            }
            return out.toByteArray().takeIf { it.isNotEmpty() }
        }
    }

    /** 磁盘里已有且没过期？预热靠它跳过重复下载。 */
    fun has(ctx: Context, url: String): Boolean {
        val f = file(ctx, url)
        return f.exists() && f.length() >= 64 &&
            System.currentTimeMillis() - f.lastModified() < TTL
    }

    /**
     * 预下载：只落磁盘、不解码进内存 —— 预热 20 张 800px 图若全进 LRU 会吃掉几十 MB。
     * 并发由 download() 里的 gate(2) 兜住，这里不再套一层，避免信号量嵌套死锁。
     */
    suspend fun prefetch(ctx: Context, urls: List<String>) = withContext(Dispatchers.IO) {
        coroutineScope {
            urls.distinct().filter { !has(ctx, it) }.map { u ->
                async {
                    runCatching {
                        download(u)?.let { bytes -> file(ctx, u).writeBytes(bytes) }
                    }
                }
            }.awaitAll()
        }
    }

    suspend fun load(ctx: Context, url: String): ImageBitmap? {
        mem.get(url)?.let { return it }
        val f = file(ctx, url)
        val bytes = if (f.exists() && System.currentTimeMillis() - f.lastModified() < TTL) {
            runCatching { f.readBytes() }.getOrNull()
        } else {
            download(url)?.also { runCatching { f.writeBytes(it) } }
        } ?: return null
        if (bytes.size < 64) return null
        val bound = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bound)
        val maxSide = maxOf(bound.outWidth, bound.outHeight)
        if (maxSide <= 0) return null
        var sample = 1
        while (maxSide / sample > 800) sample *= 2
        val opt = BitmapFactory.Options().apply { inSampleSize = sample }
        val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opt) ?: return null
        val ib = bmp.asImageBitmap()
        mem.put(url, ib)
        return ib
    }
}

/**
 * 有图就出图，没图/加载中就一块带站点首字母的占位，不空着也不弹错误。
 * 加载中显示「呼吸骨架」(大厂两段式加载的第一段：先占位、后淡入真图)，
 * 真图解码完成后 200ms 交叉淡入，避免突兀弹出——对齐微信/飞猪的图片体验。
 */
@Composable
fun RemoteImage(url: String, modifier: Modifier = Modifier, label: String = "") {
    val ctx = LocalContext.current
    var bmp by remember(url) { mutableStateOf<ImageBitmap?>(null) }
    var done by remember(url) { mutableStateOf(false) }
    LaunchedEffect(url) {
        done = false
        bmp = withContext(Dispatchers.IO) {
            try { ImageCache.load(ctx, url) } catch (e: Exception) { null }
        }
        done = true
    }
    Box(
        modifier.background(MaterialTheme.colorScheme.surfaceVariant),
        contentAlignment = Alignment.Center,
    ) {
        // 加载中：呼吸骨架占位（轻量 alpha 脉动，不引第三方 shimmer 库）。
        if (bmp == null && !done) {
            val tr = rememberInfiniteTransition(label = "skeleton")
            val a = tr.animateFloat(
                0.30f, 0.65f,
                infiniteRepeatable(tween(900, easing = LinearEasing), RepeatMode.Reverse),
                label = "skeletonAlpha",
            )
            Box(
                Modifier.fillMaxSize().alpha(a.value)
                    .background(MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.14f)),
            )
        }
        // 真图就位后淡入覆盖在骨架之上。
        AnimatedVisibility(
            visible = bmp != null,
            enter = fadeIn(tween(220)),
            exit = fadeOut(tween(120)),
        ) {
            bmp?.let {
                Image(
                    bitmap = it,
                    contentDescription = null,
                    modifier = Modifier.fillMaxSize(),
                    contentScale = ContentScale.Crop,
                )
            }
        }
        // 加载失败且无图：退化成站点首字母占位，不弹错误。
        if (bmp == null && done && label.isNotBlank()) {
            Text(
                label.removePrefix("www.").take(1).uppercase(),
                fontSize = 30.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
