package app.skynet.tianwang.next

import android.content.ClipboardManager
import android.content.ClipData
import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.*

/**
 * 剪贴板：读取 / 写入当前剪贴板，并把复制过的内容留在本地历史（只存本机，不联网、不上传）。
 * 纯 Android 能力，不需要 proot、不需要任何额外权限。
 */
@Composable
fun ClipboardScreen() {
    val ctx = LocalContext.current
    var current by remember { mutableStateOf("") }
    var input by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf("") }
    val history = remember(current, notice) { kbList("clip.history").take(50) }

    fun read() {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
        current = cm?.primaryClip?.let { if (it.itemCount > 0) it.getItemAt(0).text?.toString() else null } ?: ""
        notice = if (current.isBlank()) "剪贴板当前为空。" else "已读取剪贴板（${current.length} 字）。"
    }

    fun copy(text: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("tianwang", text))
        runCatching {
            KB.add("clip.history", JSONObject().put("text", text.take(2000)).put("at", System.currentTimeMillis()))
        }
        current = text
        notice = "已复制到剪贴板，并记录到本地历史。"
    }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("剪贴板", "读取 / 写入当前剪贴板，复制历史留在本机，不联网")
        Spacer(Modifier.height(8.dp))

        FeatureCard {
            FeatureActions(
                "读取剪贴板" to { read() },
                "复制上面输入" to { if (input.isNotBlank()) copy(input) },
            )
            Spacer(Modifier.height(6.dp))
            FeatureField(value = input, onChange = { input = it }, label = "要写入剪贴板的内容")
            Spacer(Modifier.height(6.dp))
            Text("当前剪贴板：${if (current.isBlank()) "（空）" else current.take(300)}",
                fontSize = 10.5.sp, lineHeight = 15.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (notice.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
            }
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("本地历史（${history.size}）")
        if (history.isEmpty()) {
            Text("还没有复制记录。", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            history.forEach { h ->
                val t = h["text"].orEmpty()
                val at = h["at"].orEmpty()
                FeatureCard(onClick = { copy(t) }) {
                    Text(t.take(120), fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(2.dp))
                    Text("${fmtClipAt(at)} · 点击再次复制", fontSize = 9.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(4.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}

private fun fmtClipAt(at: String): String {
    val n = at.toLongOrNull() ?: return "-"
    return if (n <= 0L) "-" else SimpleDateFormat("MM-dd HH:mm", Locale.US).format(Date(n))
}
