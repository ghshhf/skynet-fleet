package app.skynet.tianwang.next

import android.content.Intent
import android.provider.Settings
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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 权限矩阵可视化：把 [Perms.rows] 里那份「能力 → 通道 → 状态 → 下一步」摊到界面上。
 *
 * 不重新判权限、不另造表，只读现成的 [Perms.snapshot] / [Perms.rows]。
 * 原来这份矩阵只有 `self.perm.matrix` 工具能吐一段文字，人装完 App
 * 根本不知道自己少了哪条通道 —— 状态躺在代码里等于没有状态。
 */
@Composable
fun PermissionMatrixScreen() {
    val ctx = LocalContext.current
    var env by remember { mutableStateOf(Perms.snapshot(ctx)) }
    val rows = Perms.rows(env)
    val ready = rows.count { it.state == PermState.READY }
    val pending = rows.count { it.state == PermState.NEEDS_GRANT }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("权限矩阵", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("能力上限由通道决定，不由模型决定。可用 $ready 条 / 待授权 $pending 条 · 缺授权时工具如实报「没开」，不重试、不退回假数据。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { env = Perms.snapshot(ctx) }, modifier = Modifier.weight(1f)) {
                Text("刷新")
            }
            Button(onClick = {
                runCatching {
                    ctx.startActivity(
                        Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    )
                }
            }, modifier = Modifier.weight(1f)) { Text("去无障碍设置") }
        }
        Spacer(Modifier.height(6.dp))
        Button(onClick = {
            runCatching {
                ctx.startActivity(
                    Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            }
        }, modifier = Modifier.fillMaxWidth()) { Text("去通知使用权设置") }
        Spacer(Modifier.height(10.dp))
        LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            item {
                ChannelOverview(env)
                Spacer(Modifier.height(6.dp))
            }
            items(rows, key = { it.id }) { r -> PermRowCard(r) }
        }
    }
}

@Composable
private fun ChannelOverview(env: PermEnv) {
    val pairs = listOf(
        "无障碍" to when {
            env.accessibilityConnected -> "已连接"
            env.accessibilityEnabled -> "已授权未连接"
            else -> "未开启"
        },
        "通知使用权" to if (env.notificationGranted) "已授权" else "未授权",
        "Shizuku" to when {
            env.shizukuAuthorized -> "已授权"
            env.shizukuRunning -> "运行中未授权"
            else -> "未运行"
        },
        "设备所有者" to if (env.deviceOwner) "已就绪" else "未就绪",
        "局域网直连" to if (env.lanRunning) "运行中" else "未启动",
        "动态域名" to when {
            env.ddnsPublished -> "已发布"
            env.ddnsConfigured -> "已配置未发布"
            else -> "未配置"
        },
        "Root" to if (env.rootAvailable) "检测到 su" else "未检测到",
    )
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(12.dp),
    ) {
        Text("通道总览", fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.height(6.dp))
        for ((k, v) in pairs) {
            Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
                Text(k, fontSize = 11.sp, modifier = Modifier.weight(1f),
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(v, fontSize = 11.sp,
                    color = if (v.startsWith("已") || v == "运行中") MaterialTheme.colorScheme.primary
                    else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

@Composable
private fun PermRowCard(r: PermRow) {
    val tone = when (r.state) {
        PermState.READY -> MaterialTheme.colorScheme.primary
        PermState.UNSUPPORTED, PermState.NEEDS_ROOT -> MaterialTheme.colorScheme.onSurfaceVariant
        else -> MaterialTheme.colorScheme.error
    }
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(11.dp),
    ) {
        Row {
            Text(r.capability, fontSize = 12.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f))
            Text(r.state.label, fontSize = 10.5.sp, color = tone)
        }
        Spacer(Modifier.height(3.dp))
        Text("${r.id} · ${r.channel}", fontSize = 10.sp,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(3.dp))
        Text("→ ${r.next}", fontSize = 10.5.sp, lineHeight = 15.sp, color = tone)
    }
}
