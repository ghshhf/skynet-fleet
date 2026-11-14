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
import org.json.JSONObject

/**
 * 命令收藏：把常用 shell 命令存在本机（KB），一键执行、看输出、删除。
 * 配合终端 / 代码工作台使用；不联网、不依赖 proot。
 */
@Composable
fun CommandFavScreen() {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var label by remember { mutableStateOf("") }
    var cmd by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf("") }
    var runOut by remember { mutableStateOf<String?>(null) }
    var running by remember { mutableStateOf(false) }

    val favs = remember(tick) { kbList("term.fav") }

    fun save() {
        if (label.isBlank() || cmd.isBlank()) { notice = "标签和命令都要填。"; return }
        runCatching {
            KB.add("term.fav", JSONObject().put("label", label.take(40)).put("cmd", cmd.take(2000)))
        }
        notice = "已保存收藏：$label"
        label = ""; cmd = ""
        tick++
    }

    fun runCmd(c: String) {
        if (running) return
        running = true
        scope.launch(Dispatchers.IO) {
            val r = ShellRunner.run(c, LinuxRuntime.workspace().absolutePath, 15_000)
            withContext(Dispatchers.Main) {
                running = false
                runOut = "== $c ==\n$r"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("命令收藏", "常用 shell 命令存本机，一键执行 / 删除。不联网")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureField(value = label, onChange = { label = it }, label = "标签（如：看磁盘）")
            Spacer(Modifier.height(6.dp))
            FeatureField(value = cmd, onChange = { cmd = it }, label = "命令（如：df -h）")
            Spacer(Modifier.height(6.dp))
            FeatureActions("保存收藏" to { save() })
            if (notice.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
            }
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("收藏（${favs.size}）")
        if (favs.isEmpty()) {
            Text("还没有收藏。上方保存一条常用命令试试。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            favs.forEach { f ->
                val l = f["label"].orEmpty()
                val c = f["cmd"].orEmpty()
                val id = f["id"].orEmpty()
                FeatureCard {
                    Text(l.ifBlank { "(无标签)" }, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(2.dp))
                    Text(c.take(120), fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(4.dp))
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        TextButton(onClick = { runCmd(c) }) { Text("执行", fontSize = 11.sp) }
                        TextButton(onClick = {
                            runCatching { KB.remove("term.fav", id) }
                            tick++
                            notice = "已删除：$l"
                        }) { Text("删除", fontSize = 11.sp) }
                    }
                }
                Spacer(Modifier.height(4.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }

    if (runOut != null) {
        AlertDialog(
            onDismissRequest = { runOut = null },
            title = { Text("执行结果") },
            text = {
                Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())) {
                    Text(runOut!!, fontSize = 10.5.sp, lineHeight = 15.sp)
                }
            },
            confirmButton = { TextButton(onClick = { runOut = null }) { Text("关闭") } },
        )
    }
}
