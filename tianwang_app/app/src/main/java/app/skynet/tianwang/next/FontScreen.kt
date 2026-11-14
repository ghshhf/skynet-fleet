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
 * 字体清单：列出系统已装字体文件（/system/fonts）。
 * 优先 ls（玩具盒自带），失败退回 Java 列目录；纯本地读取。
 */
@Composable
fun FontScreen() {
    val scope = rememberCoroutineScope()
    var fonts by remember { mutableStateOf<List<FontItem>>(emptyList()) }
    var notice by remember { mutableStateOf("点「读取系统字体」看 /system/fonts 里的字体文件。") }
    var busy by remember { mutableStateOf(false) }

    fun load() {
        if (busy) return
        busy = true
        notice = "读取 /system/fonts…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("ls -1 /system/fonts 2>/dev/null", LinuxRuntime.workspace().absolutePath, 10_000)
            val list = if (out.isNotBlank()) {
                out.lineSequence().filter { it.isNotBlank() }
                    .map { FontItem(it.trim(), null) }.toList()
            } else {
                val dir = File("/system/fonts")
                (dir.listFiles()?.filter { it.isFile }
                    ?.map { FontItem(it.name, it.length()) } ?: emptyList())
            }
            withContext(Dispatchers.Main) {
                busy = false
                fonts = list
                notice = if (list.isEmpty()) "没读到字体（/system/fonts 不可访问）" else "共 ${list.size} 个字体文件"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("字体清单", "系统已装字体文件（/system/fonts）。纯本地读取")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("读取系统字体" to { load() })
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(10.dp))
        FeatureSection("字体（${fonts.size}）")
        fonts.forEach { f ->
            FeatureCard {
                Text(f.name, fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurface)
                if (f.size != null) {
                    Spacer(Modifier.height(2.dp))
                    Text("${f.size} 字节", fontSize = 9.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Spacer(Modifier.height(3.dp))
        }
        Spacer(Modifier.height(20.dp))
    }
}

private data class FontItem(val name: String, val size: Long?)
