package app.skynet.tianwang.next

import android.content.ClipboardManager
import android.content.ClipData
import android.content.Context
import android.os.Environment
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.io.File
import java.text.SimpleDateFormat
import java.util.*

/**
 * 文件管理器：浏览手机可访问的存储分区（App 私有空间 + Linux 工作区/用户态 +
 * 公共下载/文档/媒体目录），查看文本、删除、复制路径。
 * 不申请额外权限、不碰系统分区；公共目录在 Android 11+ 受存储沙盒限制时可能列不出，如实显示。
 */
private data class FmItem(
    val file: File, val name: String, val isDir: Boolean, val size: Long, val mtime: Long,
)

@Composable
fun FileManagerScreen() {
    val ctx = LocalContext.current
    val roots = remember { fileRoots() }
    var cur by remember { mutableStateOf<File?>(null) }
    var refresh by remember { mutableStateOf(0) }
    val items = remember(cur, refresh) { listDir(cur, roots) }
    var viewing by remember { mutableStateOf<String?>(null) }
    var confirmDel by remember { mutableStateOf<FmItem?>(null) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("文件管理器", "浏览手机可访问的存储：查看 / 删除 / 复制路径")
        Spacer(Modifier.height(8.dp))

        FeatureCard {
            Text("位置：${cur?.absolutePath ?: "根目录（可访问分区入口）"}",
                fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(4.dp))
            if (cur != null) {
                FeatureActions("返回上级" to { cur = cur?.parentFile; viewing = null })
            } else {
                FeatureActions(*roots.map { "进入: ${it.name}" to { cur = it } }.toTypedArray())
            }
        }

        Spacer(Modifier.height(8.dp))
        items.forEach { fi ->
            val isTextFile = !fi.isDir && fi.size <= 2L * 1024 * 1024 && isText(fi.file)
            FeatureCard(onClick = {
                if (fi.isDir) cur = fi.file
                else if (isTextFile) viewing = runCatching { fi.file.readText() }.getOrNull()
            }) {
                Text(fi.name, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                Text(
                    if (fi.isDir) "目录" else "${human(fi.size)} · ${fmtDate(fi.mtime)}",
                    fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(4.dp))
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    TextButton(onClick = { copyPath(ctx, fi.file) }) { Text("复制路径", fontSize = 11.sp) }
                    if (!fi.isDir) {
                        TextButton(onClick = { viewing = runCatching { fi.file.readText() }.getOrNull() }) {
                            Text("查看", fontSize = 11.sp)
                        }
                    }
                    TextButton(onClick = { confirmDel = fi }) { Text("删除", fontSize = 11.sp) }
                }
            }
        }
        if (items.isEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text("这个位置没有可列出的文件（可能受系统存储沙盒限制，或目录为空）。",
                fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(20.dp))
    }

    if (viewing != null) {
        AlertDialog(
            onDismissRequest = { viewing = null },
            title = { Text("文件内容") },
            text = {
                Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())) {
                    Text(viewing!!, fontSize = 11.sp, lineHeight = 16.sp)
                }
            },
            confirmButton = { TextButton(onClick = { viewing = null }) { Text("关闭") } },
        )
    }

    if (confirmDel != null) {
        AlertDialog(
            onDismissRequest = { confirmDel = null },
            title = { Text("删除？") },
            text = { Text("将删除：${confirmDel!!.file.absolutePath}，不可恢复。") },
            confirmButton = {
                TextButton(onClick = {
                    confirmDel!!.file.deleteRecursively()
                    confirmDel = null
                    refresh++
                }) { Text("确认删除") }
            },
            dismissButton = { TextButton(onClick = { confirmDel = null }) { Text("取消") } },
        )
    }
}

private fun fileRoots(): List<File> {
    val list = mutableListOf<File>()
    list.add(App.ctx.filesDir)
    list.add(LinuxRuntime.workspace())
    list.add(LinuxRuntime.rootfsDir())
    for (d in listOf(
        Environment.DIRECTORY_DOWNLOADS, Environment.DIRECTORY_DOCUMENTS,
        Environment.DIRECTORY_PICTURES, Environment.DIRECTORY_MOVIES, Environment.DIRECTORY_MUSIC,
    )) {
        val f = Environment.getExternalStoragePublicDirectory(d)
        if (f != null) list.add(f)
    }
    return list.distinctBy { it.absolutePath }
}

private fun listDir(cur: File?, roots: List<File>): List<FmItem> {
    val base = cur ?: return roots.map { FmItem(it, it.name, true, 0L, it.lastModified()) }
    val files = base.listFiles() ?: return emptyList()
    return files.sortedWith(compareBy({ !it.isDirectory }, { it.name.lowercase() }))
        .map { FmItem(it, it.name, it.isDirectory, it.length(), it.lastModified()) }
}

private fun isText(f: File): Boolean {
    val n = f.name.lowercase()
    return n.endsWith(".txt") || n.endsWith(".md") || n.endsWith(".sh") || n.endsWith(".py") ||
        n.endsWith(".js") || n.endsWith(".json") || n.endsWith(".html") || n.endsWith(".xml") ||
        n.endsWith(".csv") || n.endsWith(".log") || n.endsWith(".yml") || n.endsWith(".yaml") ||
        n.endsWith(".kt") || n.endsWith(".java") || n.endsWith(".gradle")
}

private fun copyPath(ctx: Context, f: File) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
    cm.setPrimaryClip(ClipData.newPlainText("path", f.absolutePath))
}

private fun human(bytes: Long): String = when {
    bytes >= 1024 * 1024 * 1024 -> "${bytes / (1024 * 1024 * 1024)} GB"
    bytes >= 1024 * 1024 -> "${bytes / (1024 * 1024)} MB"
    bytes >= 1024 -> "${bytes / 1024} KB"
    else -> "$bytes B"
}

private fun fmtDate(t: Long): String =
    if (t <= 0L) "-" else SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).format(Date(t))
