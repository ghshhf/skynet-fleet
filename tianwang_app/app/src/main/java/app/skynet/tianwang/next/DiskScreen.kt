package app.skynet.tianwang.next

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 磁盘与挂载：df -h 看各分区 / 挂载点的用量。
 * 纯本地读取，普通 App 进程即可，不依赖 proot、不联网。
 */
@Composable
fun DiskScreen() {
    val scope = rememberCoroutineScope()
    var rows by remember { mutableStateOf<List<DiskRow>>(emptyList()) }
    var notice by remember { mutableStateOf("点「读取磁盘与挂载」看各分区用量。") }
    var busy by remember { mutableStateOf(false) }

    fun load() {
        if (busy) return
        busy = true
        notice = "读取 df -h…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("df -h 2>/dev/null", LinuxRuntime.workspace().absolutePath, 10_000)
            val list = parseDf(out)
            withContext(Dispatchers.Main) {
                busy = false
                rows = list
                notice = if (list.isEmpty()) "df 无输出（可能受限）" else "共 ${list.size} 个挂载点"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("磁盘与挂载", "各分区 / 挂载点的用量（df -h）。纯本地读取，不依赖 proot")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("读取磁盘与挂载" to { load() })
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(10.dp))
        FeatureSection("挂载点（${rows.size}）")
        rows.forEach { r ->
            FeatureCard {
                Text(r.mount, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                FeatureKV("文件系统", r.fs)
                FeatureKV("已用/可用", "${r.used} / ${r.avail}")
                FeatureKV("使用率", r.usePct)
            }
            Spacer(Modifier.height(4.dp))
        }
        Spacer(Modifier.height(20.dp))
    }
}

private data class DiskRow(
    val fs: String,
    val mount: String,
    val used: String,
    val avail: String,
    val usePct: String,
)

private fun parseDf(out: String): List<DiskRow> {
    val res = mutableListOf<DiskRow>()
    out.lineSequence().forEach { line ->
        val t = line.trim()
        if (t.isEmpty() || t.startsWith("Filesystem")) return@forEach
        val p = t.split(Regex("\\s+"))
        // 标准 6 列：Filesystem Size Used Avail Use% MountedOn
        if (p.size >= 6) {
            res.add(DiskRow(p[0], p[5], p[2], p[3], p[4]))
        }
    }
    return res
}
