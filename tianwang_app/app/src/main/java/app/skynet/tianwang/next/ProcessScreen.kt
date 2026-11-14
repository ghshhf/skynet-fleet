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
 * 进程查看：手机自己用 ps 看运行中的进程，按内存排序，可结束自有进程。
 * 纯 Android shell 层，不依赖 proot；对系统进程结束可能权限不足，状态如实说。
 */
private data class ProcRow(val pid: String, val name: String, val rss: String, val cpu: String)

@Composable
fun ProcessScreen() {
    val scope = rememberCoroutineScope()
    var rows by remember { mutableStateOf<List<ProcRow>>(emptyList()) }
    var notice by remember { mutableStateOf("点「刷新进程」列出当前运行进程。") }
    var busy by remember { mutableStateOf(false) }
    var confirmPid by remember { mutableStateOf<String?>(null) }

    fun refresh() {
        if (busy) return
        busy = true
        notice = "读取进程列表…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("ps -A -o PID,NAME,RSS,%CPU 2>/dev/null || ps -A",
                LinuxRuntime.workspace().absolutePath, 12_000)
            val list = parsePs(out)
            withContext(Dispatchers.Main) {
                busy = false
                rows = list
                notice = if (list.isEmpty()) "没有解析到进程（ps 输出可能为空或受限）：$out".take(400) else "共 ${list.size} 个进程，按内存降序。"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("进程查看", "看运行中的进程、按内存排序、结束自有进程。纯 shell 层，不依赖 proot")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions(
                "刷新进程" to { refresh() },
            )
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(8.dp))
        rows.forEach { r ->
            FeatureCard(onClick = { confirmPid = r.pid }) {
                Text("${r.name}  (PID ${r.pid})", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                Text("内存 ${r.rss} KB · CPU ${r.cpu}%", fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(4.dp))
        }
        Spacer(Modifier.height(20.dp))
    }

    if (confirmPid != null) {
        AlertDialog(
            onDismissRequest = { confirmPid = null },
            title = { Text("结束进程？") },
            text = { Text("将向 PID ${confirmPid} 发送 kill -9。系统进程可能权限不足而失败，属正常。") },
            confirmButton = {
                TextButton(onClick = {
                    val pid = confirmPid!!
                    confirmPid = null
                    scope.launch(Dispatchers.IO) {
                        val r = ShellRunner.run("kill -9 $pid 2>&1; echo exit=\$?",
                            LinuxRuntime.workspace().absolutePath, 8_000)
                        withContext(Dispatchers.Main) { notice = "kill $pid：$r".take(300); refresh() }
                    }
                }) { Text("确认结束") }
            },
            dismissButton = { TextButton(onClick = { confirmPid = null }) { Text("取消") } },
        )
    }
}

private fun parsePs(out: String): List<ProcRow> {
    val res = mutableListOf<ProcRow>()
    out.lineSequence().forEach { line ->
        val t = line.trim()
        if (t.isEmpty()) return@forEach
        val p = t.split(Regex("\\s+"))
        // 跳过表头与其它字段数不足的异常行
        if (p.size < 4) return@forEach
        if (p[0].equals("PID", ignoreCase = true)) return@forEach
        val pid = p[0]
        val cpu = p.last()
        val rss = p[p.size - 2]
        val name = p.subList(1, p.size - 2).joinToString(" ").take(60)
        res.add(ProcRow(pid, name, rss, cpu))
    }
    // 按 RSS（字符串数值）降序
    return res.sortedByDescending {
        it.rss.filter { c -> c.isDigit() }.toLongOrNull() ?: 0L
    }
}
