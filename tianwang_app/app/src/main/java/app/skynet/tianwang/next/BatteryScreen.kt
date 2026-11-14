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
 * 电量与电池：读 dumpsys battery 解析电量 / 健康 / 温度 / 电压 / 充放电状态。
 * 纯 shell 层，不依赖 proot；解析不到就如实说，不编造数据。
 */
@Composable
fun BatteryScreen() {
    val scope = rememberCoroutineScope()
    var info by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    var notice by remember { mutableStateOf("点「读取电池状态」看电量/健康/温度/电压。") }
    var busy by remember { mutableStateOf(false) }

    fun load() {
        if (busy) return
        busy = true
        notice = "读取 dumpsys battery…"
        scope.launch(Dispatchers.IO) {
            val out = ShellRunner.run("dumpsys battery", LinuxRuntime.workspace().absolutePath, 10_000)
            val m = parseBattery(out)
            withContext(Dispatchers.Main) {
                busy = false
                info = m
                notice = if (m.isEmpty()) "dumpsys battery 无输出（可能受限）" else "电池状态已读取"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("电量与电池", "电量 / 健康 / 温度 / 电压 / 充放电状态。读 dumpsys battery，不依赖 proot")
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            FeatureActions("读取电池状态" to { load() })
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(10.dp))
        if (info.isNotEmpty()) {
            FeatureSection("电池信息")
            info.forEach { (k, v) -> FeatureKV(k, v) }
        }
        Spacer(Modifier.height(20.dp))
    }
}

private fun parseBattery(out: String): Map<String, String> {
    val raw = LinkedHashMap<String, String>()
    out.lineSequence().forEach { line ->
        val t = line.trim()
        val idx = t.indexOf(':')
        if (idx <= 0) return@forEach
        val key = t.substring(0, idx).trim()
        val value = t.substring(idx + 1).trim()
        if (value.isNotEmpty()) raw[key] = value
    }
    val m = LinkedHashMap<String, String>()
    raw["level"]?.let { m["电量"] = "$it%" }
    raw["health"]?.let { m["健康"] = healthText(it.toIntOrNull() ?: 0) }
    raw["status"]?.let { m["状态"] = statusText(it.toIntOrNull() ?: 0) }
    raw["present"]?.let { m["是否在位"] = if (it == "true") "是" else "否" }
    raw["temperature"]?.let { v ->
        m["温度"] = "%.1f ℃".format(v.toIntOrNull()?.div(10.0) ?: 0.0)
    }
    raw["voltage"]?.let { v ->
        val mv = v.toIntOrNull() ?: 0
        m["电压"] = "$mv mV (${mv / 1000.0} V)"
    }
    raw["technology"]?.let { m["电池技术"] = it }
    raw["charge counter"]?.let { m["电荷计数"] = "$it μAh" }
    return m
}

private fun healthText(h: Int) = when (h) {
    1 -> "未知"; 2 -> "良好"; 3 -> "过热"; 4 -> "耗尽"
    5 -> "过压"; 6 -> "失效"; 7 -> "过冷"; else -> "代码#$h"
}

private fun statusText(s: Int) = when (s) {
    1 -> "未知"; 2 -> "充电中"; 3 -> "放电中"; 4 -> "未充电"; 5 -> "已满"; else -> "代码#$s"
}
