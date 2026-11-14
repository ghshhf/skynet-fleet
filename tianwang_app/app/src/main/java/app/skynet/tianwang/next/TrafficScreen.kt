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
 * 网络流量：读 /proc/net/dev 看各接口累计收发字节与包数。
 * 纯本地读取，普通 App 进程就能读，不依赖 proot、不联网。
 */
@Composable
fun TrafficScreen() {
    val scope = rememberCoroutineScope()
    var rows by remember { mutableStateOf<List<TrafficRow>>(emptyList()) }
    var notice by remember { mutableStateOf("点「读取网络流量」看各接口收发字节。") }
    var busy by remember { mutableStateOf(false) }

    fun load() {
        if (busy) return
        busy = true
        notice = "读取 /proc/net/dev…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("cat /proc/net/dev", LinuxRuntime.workspace().absolutePath, 10_000)
            val list = parseNetDev(out)
            withContext(Dispatchers.Main) {
                busy = false
                rows = list
                notice = if (list.isEmpty()) "解析不到接口（/proc/net/dev 可能受限）" else "共 ${list.size} 个接口"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("网络流量", "各网络接口的累计收发字节（/proc/net/dev）。纯本地读取，不依赖 proot")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("读取网络流量" to { load() })
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(10.dp))
        FeatureSection("接口（${rows.size}）")
        rows.forEach { r ->
            FeatureCard {
                Text(r.name, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.height(2.dp))
                FeatureKV("接收", human(r.rx))
                FeatureKV("发送", human(r.tx))
                FeatureKV("收包/发包", "${r.rxPkts} / ${r.txPkts}")
            }
            Spacer(Modifier.height(4.dp))
        }
        Spacer(Modifier.height(20.dp))
    }
}

private data class TrafficRow(
    val name: String,
    val rx: Long,
    val tx: Long,
    val rxPkts: Long,
    val txPkts: Long,
)

private fun parseNetDev(out: String): List<TrafficRow> {
    val res = mutableListOf<TrafficRow>()
    out.lineSequence().forEach { line ->
        val t = line.trim()
        if (t.isEmpty()) return@forEach
        if (t.startsWith("Inter-") || t.startsWith("face")) return@forEach
        val ci = t.indexOf(':')
        if (ci < 0) return@forEach
        val name = t.substring(0, ci).trim()
        val nums = t.substring(ci + 1).trim().split(Regex("\\s+")).mapNotNull { it.toLongOrNull() }
        if (nums.size >= 16) {
            res.add(TrafficRow(name, nums[0], nums[8], nums[1], nums[9]))
        }
    }
    return res
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
