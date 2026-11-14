package app.skynet.tianwang.next

import android.os.Environment
import android.os.StatFs
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/**
 * 存储分析：看各目录体积 / 文件数，定位大文件与可清缓存。
 * 应用自有目录用 Kotlin 受限遍历（封顶文件数，避免卡死）；外部媒体目录走 du（玩具盒自带）。
 * 只提供「清空本应用自己的缓存」这一条确定性清理，绝不碰系统与其它 App。
 */
@Composable
fun StorageAnalysisScreen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    var rows by remember { mutableStateOf<List<StorageRow>>(emptyList()) }
    var internal by remember { mutableStateOf<Pair<Long, Long>?>(null) }
    var notice by remember { mutableStateOf("点「扫描存储占用」看各目录体积与文件数。") }
    var busy by remember { mutableStateOf(false) }
    var confirmClear by remember { mutableStateOf(false) }
    var freedMsg by remember { mutableStateOf<String?>(null) }

    fun scan() {
        if (busy) return
        busy = true
        notice = "扫描中…"
        scope.launch(Dispatchers.IO) {
            val data = ctx.filesDir
            val cache = ctx.cacheDir
            val extCache = ctx.externalCacheDir
            val ws = LinuxRuntime.workspace()

            val dirs = mutableListOf<Pair<String, File?>>()
            dirs.add("应用数据(filesDir)" to data)
            dirs.add("应用缓存(cacheDir)" to cache)
            dirs.add("外部缓存(externalCacheDir)" to extCache)
            dirs.add("工作区(workspace)" to ws)

            val extRoot = Environment.getExternalStorageDirectory()
            val media = listOf("DCIM", "Download", "Music", "Movies", "Pictures", "Android/data")
                .map { File(extRoot, it) }
            val duArgs = media.joinToString(" ") { "\"${it.absolutePath}\"" }
            val duOut = ShellRunner.run("du -sh $duArgs 2>/dev/null", ws.absolutePath, 20_000)
            val duMap = parseDu(duOut)

            val list = mutableListOf<StorageRow>()
            dirs.forEach { (name, f) ->
                if (f != null && f.exists()) {
                    val (b, c) = walkSize(f)
                    list.add(StorageRow(name, f.absolutePath, b, c, true))
                }
            }
            media.forEach { f ->
                val size = duMap[f.absolutePath]
                list.add(StorageRow(f.name, f.absolutePath, size ?: -1L, -1, size != null))
            }

            val fs = runCatching {
                val s = StatFs(data.absolutePath)
                s.availableBytes to s.totalBytes
            }.getOrNull()

            withContext(Dispatchers.Main) {
                busy = false
                rows = list
                internal = fs
                notice = "扫描完成：${list.size} 项。"
            }
        }
    }

    fun clearCache() {
        val targets = listOfNotNull(ctx.cacheDir, ctx.externalCacheDir)
        var freed = 0L
        targets.forEach { dir -> freed += runCatching { deleteRecursive(dir) }.getOrDefault(0L) }
        freedMsg = "已清空本应用缓存，释放约 ${human(freed)}。"
        confirmClear = false
        scan()
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("存储分析", "看各目录体积/文件数，定位大文件与可清缓存。仅能清理本应用自己的缓存")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("扫描存储占用" to { scan() })
            Spacer(Modifier.height(4.dp))
            if (internal != null && internal!!.second > 0) {
                FeatureKV("内部可用", human(internal!!.first))
                FeatureKV("内部总容", human(internal!!.second))
                FeatureKV(
                    "已用占比",
                    String.format("%.0f%%", (1 - internal!!.first.toDouble() / internal!!.second) * 100),
                )
            }
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("清空本应用缓存" to { confirmClear = true })
            Text("只删 App 自己的 cache 目录，不碰系统和别的 App。",
                fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (freedMsg != null) {
            Spacer(Modifier.height(6.dp))
            Text(freedMsg!!, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("目录占用（${rows.size}）")
        rows.forEach { r ->
            FeatureCard {
                Text(r.name, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                Text(r.path.take(80), fontSize = 9.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(3.dp))
                val sizeTxt = if (r.size >= 0) human(r.size) else "du 不可用"
                val cntTxt = if (r.count >= 0) "${r.count} 个文件" else ""
                Text("$sizeTxt  $cntTxt".trim(), fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.primary)
            }
            Spacer(Modifier.height(4.dp))
        }
        Spacer(Modifier.height(20.dp))
    }

    if (confirmClear) {
        AlertDialog(
            onDismissRequest = { confirmClear = false },
            title = { Text("清空本应用缓存") },
            text = { Text("会删除 App 的 cache 目录（含外部缓存）。不影响你的数据文件与工作区。确定继续？") },
            confirmButton = { TextButton(onClick = { clearCache() }) { Text("清空") } },
            dismissButton = { TextButton(onClick = { confirmClear = false }) { Text("取消") } },
        )
    }
}

private data class StorageRow(
    val name: String,
    val path: String,
    val size: Long,
    val count: Int,
    val duOk: Boolean,
)

private fun walkSize(dir: File, cap: Int = 8000): Pair<Long, Int> {
    var bytes = 0L
    var n = 0
    if (!dir.isDirectory) return bytes to n
    try {
        val seq = dir.walkTopDown()
        for (p in seq) {
            if (p.isFile) {
                bytes += p.length()
                n++
                if (n >= cap) break
            }
        }
    } catch (_: Throwable) {
    }
    return bytes to n
}

private fun deleteRecursive(dir: File): Long {
    var freed = 0L
    dir.listFiles()?.forEach { f ->
        if (f.isDirectory) freed += deleteRecursive(f)
        else {
            freed += f.length()
            runCatching { f.delete() }
        }
    }
    return freed
}

private fun parseDu(out: String): Map<String, Long> {
    val m = mutableMapOf<String, Long>()
    out.lineSequence().forEach { line ->
        val t = line.trim()
        if (t.isEmpty()) return@forEach
        val parts = t.split(Regex("\\s+"), limit = 2)
        if (parts.size < 2) return@forEach
        val path = parts[1].trim().removeSurrounding("\"")
        m[path] = parseSize(parts[0])
    }
    return m
}

private fun parseSize(s: String): Long {
    val u = s.takeLast(1)
    val num = s.dropLast(1).toDoubleOrNull() ?: return -1L
    return when (u) {
        "K" -> (num * 1024).toLong()
        "M" -> (num * 1024 * 1024).toLong()
        "G" -> (num * 1024 * 1024 * 1024).toLong()
        "T" -> (num * 1024 * 1024 * 1024 * 1024).toLong()
        else -> num.toLong()
    }
}

private fun human(b: Long): String {
    if (b < 0) return "—"
    val u = arrayOf("B", "KB", "MB", "GB", "TB")
    var v = b.toDouble()
    var i = 0
    while (v >= 1024 && i < u.size - 1) {
        v /= 1024
        i++
    }
    return "%.1f %s".format(v, u[i])
}
