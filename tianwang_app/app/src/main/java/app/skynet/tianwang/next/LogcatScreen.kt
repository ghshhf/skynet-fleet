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
import java.io.File

/**
 * 本机日志：抓取 logcat 缓冲，按关键词过滤，可保存与清空。
 * Android 11+ 默认只能看到本应用日志，属正常；纯 shell 层，不依赖 proot。
 */
@Composable
fun LogcatScreen() {
    val scope = rememberCoroutineScope()
    var log by remember { mutableStateOf("点「抓取 logcat」读取本应用日志缓冲。") }
    var keyword by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var notice by remember { mutableStateOf("") }

    val shown = remember(log, keyword) {
        if (keyword.isBlank()) log
        else log.lineSequence().filter { it.contains(keyword, ignoreCase = true) }.joinToString("\n")
    }

    fun capture() {
        if (busy) return
        busy = true
        notice = "抓取中…"
        scope.launch(Dispatchers.IO) {
            val r = ShellRunner.run("logcat -d -v brief *:V 2>&1 | tail -n 2000",
                LinuxRuntime.workspace().absolutePath, 15_000)
            withContext(Dispatchers.Main) {
                busy = false
                log = if (r.isBlank()) "（空，或本应用暂无日志）" else r
                notice = "已抓取 ${log.lineSequence().count()} 行。"
            }
        }
    }

    fun save() {
        if (log.isBlank()) { notice = "没有可保存的日志。"; return }
        val f = File(LinuxRuntime.workspace(), "logcat_${System.currentTimeMillis()}.txt")
        runCatching { f.writeText(log, Charsets.UTF_8) }
        notice = "已保存到：${f.absolutePath}"
    }

    fun clear() {
        scope.launch(Dispatchers.IO) {
            val r = ShellRunner.run("logcat -c 2>&1; echo exit=\$?",
                LinuxRuntime.workspace().absolutePath, 8_000)
            withContext(Dispatchers.Main) { notice = "清空缓冲：$r".take(200) }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("本机日志", "抓取 logcat 缓冲、按关键词过滤、保存或清空。Android 11+ 仅本应用日志")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions(
                "抓取 logcat" to { capture() },
                "保存" to { save() },
                "清空缓冲" to { clear() },
            )
            Spacer(Modifier.height(6.dp))
            FeatureField(value = keyword, onChange = { keyword = it }, label = "过滤关键词（留空显示全部）")
            if (notice.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
            }
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("日志（${shown.lineSequence().count()} 行）")
        FeatureCard {
            Text(shown.take(8000), fontSize = 9.5.sp, lineHeight = 13.sp,
                color = MaterialTheme.colorScheme.onSurface)
        }
        Spacer(Modifier.height(20.dp))
    }
}
