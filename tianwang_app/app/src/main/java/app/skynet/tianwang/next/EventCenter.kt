package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 本地事件中心：把本机触发过的动作（含远程调用）审计集中展示。
 *
 * 对照 DSH Go 的 diagnostics / activity：用户要能回头看「这台手机刚才到底被指挥着
 * 做了什么、成没成」，而不是翻聊天记录去猜。ActionLog 已经在每次有副作用的
 * 工具执行后记一条，且参数已脱敏、重启后保留 —— 这里只负责把它摊开给人看。
 */
@Composable
fun EventCenterScreen() {
    val ctx = LocalContext.current
    var records by remember { mutableStateOf(ActionLog.list(50)) }
    var note by remember { mutableStateOf("") }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("本地事件中心", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("这台手机上触发过的动作审计（含远程调用）。参数已脱敏，密钥不出现；重启 App 也保留。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                cm?.setPrimaryClip(ClipData.newPlainText("天网事件", ActionLog.text(50)))
                note = "已复制到剪贴板"
            }, modifier = Modifier.weight(1f)) { Text("复制全部") }
            TextButton(onClick = {
                ActionLog.clear(); records = emptyList(); note = "已清空"
            }, modifier = Modifier.weight(1f)) { Text("清空") }
        }
        if (note.isNotBlank()) {
            Spacer(Modifier.height(5.dp))
            Text(note, fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(10.dp))
        if (records.isEmpty()) {
            Text("还没有记录的动做。用过会改状态的工具（如远程点击、打开应用）后会出现在这里。",
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            return@Column
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(5.dp)) {
            items(records, key = { "${it.at}-${it.name}-${it.ms}" }) { r ->
                val fmt = SimpleDateFormat("MM-dd HH:mm:ss", Locale.getDefault()).format(Date(r.at))
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(8.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .padding(10.dp),
                ) {
                    Row {
                        Text(fmt, fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Spacer(Modifier.weight(1f))
                        Text(if (r.ok) "成功" else "失败", fontSize = 10.5.sp,
                            color = if (r.ok) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.error)
                    }
                    Spacer(Modifier.height(3.dp))
                    Text(r.name, fontSize = 12.sp, fontFamily = FontFamily.Monospace,
                        fontWeight = FontWeight.Medium, color = MaterialTheme.colorScheme.onSurface)
                    if (r.args.isNotBlank()) Text(r.args, fontSize = 10.5.sp, lineHeight = 15.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (!r.ok && r.err.isNotBlank()) Text("→ ${r.err}", fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.error)
                }
            }
        }
    }
}
