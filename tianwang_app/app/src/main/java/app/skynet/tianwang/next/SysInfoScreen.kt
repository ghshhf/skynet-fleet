package app.skynet.tianwang.next

import android.os.Build
import android.os.Environment
import android.os.StatFs
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.io.File

/**
 * 系统体检：手机自己读 /proc、/sys、StatFs 看清楚自己的硬件与状态。
 * 这一层 Android shell 就能读，不需要 proot、不需要联网、不需要任何外部 App。
 */
@Composable
fun SysInfoScreen() {
    val ctx = LocalContext.current
    var tick by remember { mutableStateOf(0) }
    val info = remember(tick) { readSysInfo() }
    val dm = ctx.resources.displayMetrics
    val screen = "${dm.widthPixels}x${dm.heightPixels} · ${dm.densityDpi}dpi · ${String.format("%.1f", dm.density)}x"

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("系统体检", "这台手机自己看清楚硬件与状态，不依赖外部 App")
        Spacer(Modifier.height(8.dp))

        FeatureCard(highlighted = true) {
            Text("${Build.MANUFACTURER} ${Build.MODEL}", fontSize = 13.sp,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(4.dp))
            FeatureKV("系统", "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})")
            FeatureKV("架构", Build.SUPPORTED_ABIS.joinToString(" / "))
            FeatureKV("处理器", info.cpu)
            FeatureKV("核心数", Runtime.getRuntime().availableProcessors().toString())
            FeatureKV("内存", info.mem)
            FeatureKV("负载", info.load)
            FeatureKV("电池", info.battery)
            FeatureKV("屏幕", screen)
            FeatureKV("存储(本机)", info.storage)
            FeatureKV("存储(App)", info.appStorage)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("说明")
        FeatureCard {
            Text("CPU / 内存 / 负载 / 电池直接从 /proc 与 /sys 读，Android shell 层即可，不需要 proot。" +
                "存储为可用空间估算：本机指内置存储根，App 指当前应用私有空间。",
                fontSize = 11.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(10.dp))
        FeatureActions("刷新" to { tick++ })
        Spacer(Modifier.height(20.dp))
    }
}

private data class SysInfoData(
    val cpu: String, val mem: String, val load: String,
    val battery: String, val storage: String, val appStorage: String,
)

private fun readSysInfo(): SysInfoData {
    val cpu = runCatching {
        File("/proc/cpuinfo").readText().lineSequence()
            .firstOrNull { it.startsWith("Hardware") || it.contains("model name") }
            ?.substringAfter(":")?.trim()
    }.getOrNull() ?: "未知"
    val mem = runCatching {
        val t = File("/proc/meminfo").readText()
        val total = t.lineSequence().firstOrNull { it.startsWith("MemTotal") }
            ?.substringAfter(":")?.trim()
        val avail = t.lineSequence().firstOrNull { it.startsWith("MemAvailable") }
            ?.substringAfter(":")?.trim()
        "${avail ?: "?"} / ${total ?: "?"}"
    }.getOrNull() ?: "未知"
    val load = runCatching { File("/proc/loadavg").readText().substringBefore(" ") }.getOrNull() ?: "未知"
    val battery = readBattery()
    val storage = runCatching {
        val fs = StatFs(Environment.getExternalStorageDirectory().path)
        val avail = fs.blockSizeLong * fs.availableBlocksLong
        val total = fs.blockSizeLong * fs.blockCountLong
        "${human(avail)} / ${human(total)}"
    }.getOrNull() ?: "未知"
    val appStorage = runCatching {
        val fs = StatFs(App.ctx.filesDir.path)
        val avail = fs.blockSizeLong * fs.availableBlocksLong
        "${human(avail)}"
    }.getOrNull() ?: "未知"
    return SysInfoData(cpu, mem, load, battery, storage, appStorage)
}

private fun readBattery(): String {
    for (p in listOf(
        "/sys/class/power_supply/battery/capacity",
        "/sys/class/power_supply/Battery/capacity",
    )) {
        val v = runCatching { File(p).readText().trim() }.getOrNull()
        if (!v.isNullOrBlank()) {
            val n = v.toIntOrNull() ?: continue
            if (n in 0..100) return "$n%"
        }
    }
    return "未知"
}

private fun human(bytes: Long): String {
    val mb = bytes / (1024 * 1024)
    return if (mb >= 1024) "${mb / 1024} GB" else "$mb MB"
}
