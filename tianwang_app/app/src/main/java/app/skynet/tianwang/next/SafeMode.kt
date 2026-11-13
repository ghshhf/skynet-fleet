package app.skynet.tianwang.next

import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 安全模式：一个应急总闸，把「别人能操作这台手机」的所有入口一次关掉。
 *
 * 与 `Prefs.mcpAllowActions` 的分工：
 * - 后者是平时的权限档位（默认只读，用户自己打开）；
 * - 前者是「现在立刻全停」，关掉之后不自动恢复，必须由人再打开。
 *
 * 对照 DSH Go 的产品承诺：手机操控要用户开启权限，且「可随时停止」。
 * 天网原来只有散在二级页里的开关，没有一个能一眼看到、一按到底的地方。
 *
 * 纪律：只影响**远控与写操作**，不碰本机对话、本地模型、资源库、出图 ——
 * 手机仍然是完整对等节点，不会被这一下变成需要谁先开机的终端。
 */
object SafeMode {

    fun on(): Boolean = Prefs.safeMode

    fun set(on: Boolean) {
        Prefs.safeMode = on
    }

    /** 平时那档动作权限，叠加安全模式总闸。所有调用方都走这里，别各处自己拼。 */
    fun allowsActions(): Boolean = Prefs.mcpAllowActions && !Prefs.safeMode

    /** 局域网直连的口令即授权，但总闸仍然优先。 */
    fun allowsLanActions(): Boolean = !Prefs.safeMode

    /**
     * 一键停：断开入网、停局域网服务、停前台壳、停 DDNS 监听、收回动作权限。
     * 逐项 try，任何一项失败都不影响其余项 —— 应急按钮不能因为一处异常整个失灵。
     * 返回的每一行都是真的执行过（或明确失败），不写「应该已停止」这种话。
     */
    fun stopAll(ctx: Context): String {
        val lines = mutableListOf<String>()

        runCatching { PhoneMcpLink.stop() }
            .onSuccess { lines.add("入网链接：已断开") }
            .onFailure { lines.add("入网链接：断开失败 ${it.message ?: ""}") }

        runCatching { LanHub.stop(ctx) }
            .onSuccess { lines.add("局域网直连服务：已停") }
            .onFailure { lines.add("局域网直连服务：停止失败 ${it.message ?: ""}") }

        runCatching { PhoneLinkService.stop(ctx) }
            .onSuccess { lines.add("常驻前台服务：已停") }
            .onFailure { lines.add("常驻前台服务：停止失败 ${it.message ?: ""}") }

        runCatching { Ddns.stopWatch() }
            .onSuccess { lines.add("动态域名监听：已停") }
            .onFailure { lines.add("动态域名监听：停止失败 ${it.message ?: ""}") }

        Prefs.mcpAllowActions = false
        Prefs.mcpEnabled = false
        Prefs.safeMode = true
        Prefs.safeStoppedAt = System.currentTimeMillis()
        lines.add("动作权限：已收回（下次开机也不会自动连回来）")
        return lines.joinToString("\n")
    }

    /** 当前几条通道的真实状态，只读。 */
    fun statusLines(): List<Pair<String, String>> = listOf(
        "安全模式" to if (on()) "已开启 · 全部远控被挡" else "未开启",
        "允许操作" to if (Prefs.mcpAllowActions) "已打开" else "默认只读",
        "入网链接" to if (PhoneMcpLink.connected) "在线" else "未连接",
        "局域网直连" to if (LanHub.running) "运行中" else "未启动",
        "动作闸门" to if (allowsActions()) "放行写操作" else "拒绝写操作",
    )
}

@Composable
fun SafeModeScreen() {
    val ctx = LocalContext.current
    var on by remember { mutableStateOf(SafeMode.on()) }
    var report by remember { mutableStateOf("") }
    var status by remember { mutableStateOf(SafeMode.statusLines()) }

    LazyColumn(
        Modifier.fillMaxSize().padding(14.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item {
            Text("安全模式", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface)
            Text("一键停止所有远控。本机对话、本地模型、资源库、出图照常可用 —— 手机还是完整节点。",
                fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(8.dp))
        }
        item {
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(14.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("总闸", fontSize = 14.sp, fontWeight = FontWeight.Medium,
                            color = MaterialTheme.colorScheme.onSurface)
                        Text(
                            if (on) "所有写操作与远控入口都被拒绝"
                            else "平时默认只读；打开总闸则一律拒绝",
                            fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Switch(checked = on, onCheckedChange = {
                        on = it
                        SafeMode.set(it)
                        status = SafeMode.statusLines()
                    })
                }
                if (Prefs.safeStoppedAt > 0L) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "上次一键停止：" + SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault())
                            .format(Date(Prefs.safeStoppedAt)),
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
        item {
            Button(
                onClick = {
                    report = SafeMode.stopAll(ctx)
                    on = SafeMode.on()
                    status = SafeMode.statusLines()
                },
                modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.error,
                ),
            ) { Text("立即停止全部远控") }
        }
        if (report.isNotBlank()) {
            item {
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .padding(13.dp),
                ) {
                    Text("这次真的停了什么", fontSize = 12.5.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(6.dp))
                    Text(report, fontSize = 11.sp, lineHeight = 17.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            Text("当前状态", fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface)
        }
        items(status.size) { i ->
            val (k, v) = status[i]
            Row(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(8.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(horizontal = 12.dp, vertical = 9.dp),
            ) {
                Text(k, fontSize = 12.sp, modifier = Modifier.weight(1f),
                    color = MaterialTheme.colorScheme.onSurface)
                Text(v, fontSize = 12.sp,
                    color = if (v.contains("拒绝") || v.contains("在线") || v.contains("运行中"))
                        MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        item {
            Spacer(Modifier.height(6.dp))
            Text("注意：总闸不会自动恢复。要重新允许远控，回来手动关掉开关，再到「设备与权限」打开允许操作。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}
