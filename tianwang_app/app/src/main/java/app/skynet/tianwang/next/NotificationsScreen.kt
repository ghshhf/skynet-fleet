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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 通知中心：把本机读到的系统通知摊开给人看。
 *
 * 🔴 对照「手机是对等节点、能自我监控」：通知监听已经在跑了（NotificationWatch），
 * `self.notif.list` 工具也能读，但机主自己从前在手机上没有入口看 —— 等于让别的设备
 * 比主人更先看到这台手机收到的验证码 / 到账提醒 / 聊天。
 *
 * 纪律：通知只存本机内存、进程被杀就清空（不悄悄落盘、不进备份、不进日志）；
 * 这里只展示 + 清空，权限要用户手动去系统设置给，绝不自动尝试。
 */
@Composable
fun NotificationsScreen() {
    val ctx = androidx.compose.ui.platform.LocalContext.current
    var enabled by remember { mutableStateOf(NotificationWatch.enabled(ctx)) }
    var list by remember { mutableStateOf(NotificationWatch.list(40)) }
    var note by remember { mutableStateOf("") }

    fun refresh() {
        enabled = NotificationWatch.enabled(ctx)
        list = NotificationWatch.list(40)
    }
    LaunchedEffect(Unit) { refresh() }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("通知中心", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("这台手机读到的系统通知（验证码 / 到账 / 聊天）。只留本机内存，关进程就清空。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))

        if (!enabled) {
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(16.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.Notifications, null,
                        tint = MaterialTheme.colorScheme.error, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.size(10.dp))
                    Text("通知使用权未开启", fontSize = 14.sp, fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onSurface)
                }
                Spacer(Modifier.height(8.dp))
                Text("读通知需要你在系统设置里把「通知使用权」给这台手机。开启后，验证码、到账、聊天会直接出现在这里，不用再点进 App。",
                    fontSize = 11.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(10.dp))
                Button(onClick = {
                    if (NotificationWatch.openSettings(ctx)) {
                        note = "已跳到系统设置；给完权限回来这里会自动刷新"
                    } else {
                        note = "打不开系统设置，请手动进入 设置 → 通知使用权"
                    }
                }) { Text("去开启") }
            }
            if (note.isNotBlank()) {
                Spacer(Modifier.height(8.dp))
                Text(note, fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
            }
            return@Column
        }

        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("最近 ${list.size} 条", fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.weight(1f))
            TextButton(onClick = {
                NotificationWatch.clear(); refresh(); note = "已清空"
            }) { Text("清空", color = MaterialTheme.colorScheme.error) }
        }
        if (note.isNotBlank()) {
            Spacer(Modifier.height(4.dp))
            Text(note, fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(8.dp))

        if (list.isEmpty()) {
            Text("还没有读到的通知。开启使用权后，新来的通知会实时出现在这里。",
                fontSize = 12.sp, lineHeight = 17.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            LazyColumn(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(list, key = { it.key }) { n ->
                    val fmt = runCatching {
                        SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(n.timeMs))
                    }.getOrDefault("")
                    Column(
                        Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(MaterialTheme.colorScheme.surface)
                            .padding(12.dp),
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(n.app.ifBlank { n.pkg }, fontSize = 12.5.sp,
                                fontWeight = FontWeight.Medium,
                                color = MaterialTheme.colorScheme.onSurface,
                                modifier = Modifier.weight(1f))
                            Text(fmt, fontSize = 10.sp, fontFamily = FontFamily.Monospace,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        if (n.title.isNotBlank()) {
                            Spacer(Modifier.height(3.dp))
                            Text(n.title, fontSize = 12.5.sp,
                                color = MaterialTheme.colorScheme.onSurface)
                        }
                        if (n.text.isNotBlank()) {
                            Spacer(Modifier.height(2.dp))
                            Text(n.text, fontSize = 11.5.sp, lineHeight = 16.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        if (n.ongoing) {
                            Spacer(Modifier.height(3.dp))
                            Text("常驻通知（如音乐 / 导航）", fontSize = 10.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }
        }
    }
}
