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
 * 网络诊断：手机自己用 /system/bin 里的 ping / ip / cat /etc/resolv.conf 看清楚
 * 自己的网络状况。纯 Android shell 层，不需要 proot、不需要联网（ping 本身是测连通性）。
 */
@Composable
fun NetDiagScreen() {
    val scope = rememberCoroutineScope()
    var host by remember { mutableStateOf("8.8.8.8") }
    var out by remember { mutableStateOf("点上面的按钮开始诊断。") }
    var busy by remember { mutableStateOf(false) }

    fun run(label: String, cmd: String) {
        if (busy) return
        busy = true
        out = "执行：$label …"
        val c = cmd
        scope.launch(Dispatchers.IO) {
            val r = ShellRunner.run(c, LinuxRuntime.workspace().absolutePath, 20_000)
            withContext(Dispatchers.Main) {
                busy = false
                out = "== $label ==\n$r"
            }
        }
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("网络诊断", "手机自己看清楚连通性 / 网关 / DNS / 接口，不依赖外部 App")
        Spacer(Modifier.height(8.dp))

        FeatureCard {
            FeatureField(value = host, onChange = { host = it }, label = "目标主机 / 域名（用于自定义 Ping）")
            Spacer(Modifier.height(8.dp))
            FeatureActions(
                "Ping 公共 DNS" to { run("Ping 8.8.8.8", "ping -c 4 8.8.8.8") },
                "Ping 网关" to { run("Ping 网关", "ip route 2>/dev/null || cat /proc/net/route") },
            )
            Spacer(Modifier.height(6.dp))
            FeatureActions(
                "Ping 自定义" to { run("Ping $host", "ping -c 4 $host") },
                "DNS 解析" to { run("DNS 解析 $host", "nslookup $host 2>/dev/null || getent hosts $host || cat /etc/resolv.conf") },
            )
            Spacer(Modifier.height(6.dp))
            FeatureActions(
                "接口列表" to { run("网络接口", "ip addr 2>/dev/null || cat /proc/net/dev") },
                "路由表" to { run("路由表", "ip route 2>/dev/null || cat /proc/net/route") },
            )
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("结果")
        FeatureCard {
            Text(out, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurface)
        }
        Spacer(Modifier.height(20.dp))
    }
}
