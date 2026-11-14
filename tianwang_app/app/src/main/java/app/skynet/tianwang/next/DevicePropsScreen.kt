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
 * 本机属性：getprop 构建 / 运行环境 / 网络等系统属性，支持按键或值过滤。
 * 纯 shell 层，不依赖 proot。
 */
@Composable
fun DevicePropsScreen() {
    val scope = rememberCoroutineScope()
    var props by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    var filter by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf("点「读取本机属性」看系统构建与运行环境。") }
    var busy by remember { mutableStateOf(false) }

    fun load() {
        if (busy) return
        busy = true
        notice = "读取 getprop…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("getprop", LinuxRuntime.workspace().absolutePath, 15_000)
            val m = parseGetprop(out)
            withContext(Dispatchers.Main) {
                busy = false
                props = m
                notice = if (m.isEmpty()) "getprop 无输出（可能受限）" else "共 ${m.size} 条属性"
            }
        }
    }

    val groups = remember(props, filter) {
        val f = filter.trim().lowercase()
        val filtered = if (f.isEmpty()) {
            props
        } else {
            props.filter { it.key.lowercase().contains(f) || it.value.lowercase().contains(f) }
        }
        filtered.entries
            .groupBy { it.key.substringBefore('.', "other") }
            .toSortedMap()
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("本机属性", "系统构建 / 运行环境 / 网络等 getprop 属性，可搜索")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("读取本机属性" to { load() })
            Spacer(Modifier.height(6.dp))
            FeatureField(value = filter, onChange = { filter = it }, label = "过滤（键或值）")
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(10.dp))
        groups.forEach { (g, items) ->
            FeatureSection("$g（${items.size}）")
            items.sortedBy { it.key }.forEach { (k, v) ->
                FeatureKV(k, v)
            }
            Spacer(Modifier.height(6.dp))
        }
        Spacer(Modifier.height(20.dp))
    }
}

private fun parseGetprop(out: String): Map<String, String> {
    val m = LinkedHashMap<String, String>()
    val re = Regex("^\\[(.+?)\\]:\\s*\\[(.*)\\]$")
    out.lineSequence().forEach { line ->
        val t = line.trim()
        val mm = re.find(t) ?: return@forEach
        m[mm.groupValues[1]] = mm.groupValues[2]
    }
    return m
}
