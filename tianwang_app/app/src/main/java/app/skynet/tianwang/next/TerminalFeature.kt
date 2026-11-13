package app.skynet.tianwang.next

import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * 终端：对照 DSH Go 的 terminal_page/terminal_screen + 本机运行环境。
 *
 * 天网本来就有 [ShellRunner]（真在手机 shell 身份下执行命令），缺的是一个
 * 「人能敲命令、能看输出、能翻历史」的界面。这里不新造执行器，直接复用它；
 * 历史落本机，命令不自动发、不联网，点一次跑一次。
 */
object TerminalHistory {
    const val KEY = "term_history"
    private const val MAX = 60

    fun add(cmd: String, cwd: String, out: String) {
        KB.add(KEY, JSONObject()
            .put("cmd", cmd)
            .put("cwd", cwd)
            .put("out", out.take(8000)))
        val arr = KB.array(KEY)
        if (arr.length() > MAX) {
            val trimmed = org.json.JSONArray()
            for (i in (arr.length() - MAX) until arr.length()) {
                trimmed.put(arr.optJSONObject(i) ?: continue)
            }
            KB.put(KEY, trimmed)
        }
    }

    fun clear() = KB.put(KEY, org.json.JSONArray())
}

@Composable
fun TerminalScreen() {
    val scope = rememberCoroutineScope()
    var cmd by remember { mutableStateOf("") }
    var cwd by remember { mutableStateOf("") }
    var running by remember { mutableStateOf(false) }
    var lastOut by remember { mutableStateOf("（还没跑过命令）") }
    var tick by remember { mutableStateOf(0) }
    val history = remember(tick) { kbList(TerminalHistory.KEY).asReversed() }

    LazyColumn(Modifier.fillMaxSize().padding(14.dp)) {
        item {
            FeatureHeader("终端", "在手机 shell 身份下敲命令、看输出、翻历史。复用本机执行器，不联网、不自动发。")
            Spacer(Modifier.height(8.dp))
        }
        item {
            FeatureCard {
                FeatureField(cmd, { cmd = it }, "命令，如 ls / getprop / pm list packages")
                Spacer(Modifier.height(6.dp))
                FeatureField(cwd, { cwd = it }, "工作目录（可空）")
                Spacer(Modifier.height(6.dp))
                FeatureActions(
                    (if (running) "执行中…" else "执行") to {
                        if (cmd.isNotBlank() && !running) {
                            running = true
                            lastOut = "执行中…"
                            val c = cmd; val d = cwd
                            scope.launch(Dispatchers.IO) {
                                val out = runCatching { ShellRunner.run(c, d, 15_000) }
                                    .getOrElse { "执行失败：${it.message}" }
                                TerminalHistory.add(c, d, out)
                                withContext(Dispatchers.Main) {
                                    lastOut = out
                                    running = false
                                    tick++
                                }
                            }
                        }
                    },
                    "清空历史" to { TerminalHistory.clear(); tick++ },
                    "填示例" to { cmd = "getprop ro.product.model" },
                )
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("最近一次输出")
            FeatureCard {
                Text(lastOut, fontSize = 11.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurface)
            }
            Spacer(Modifier.height(10.dp))
        }
        item {
            FeatureSection("历史（${history.size}）")
        }
        history.forEach { h ->
            item {
                FeatureCard {
                    Text("$ ${h["cmd"].orEmpty()}", fontSize = 11.5.sp,
                        color = MaterialTheme.colorScheme.primary)
                    if ((h["cwd"].orEmpty()).isNotBlank()) {
                        Text("cwd: ${h["cwd"]}", fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Spacer(Modifier.height(3.dp))
                    Text(h["out"].orEmpty().take(400), fontSize = 10.5.sp, lineHeight = 15.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(7.dp))
            }
        }
        item { Spacer(Modifier.height(20.dp)) }
    }
}
