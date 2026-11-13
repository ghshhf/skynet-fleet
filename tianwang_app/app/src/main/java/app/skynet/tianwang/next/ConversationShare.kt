package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
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
import androidx.compose.ui.Alignment
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
 * 对话回放、导出与分享。
 *
 * 对照 DSH Go 的 replay / share_code：整段对话能离开这台手机给人看，
 * 而且导出前有脱敏层。天网原来 `SessionStore` 只管存、抽屉只管选，
 * 存下来的东西没有任何一条出口 —— 换个设备、发给别人、回头只看不聊，都做不到。
 *
 * 两条纪律（沿用项目的诚实边界）：
 * - 回放是**只读**的，不会把历史消息喂回模型，也不会改动会话。
 * - 导出文本整段过 [Diagnostics.redact]；工具参数里可能带密钥，不能原样发出去。
 */
object ConversationShare {

    private fun stamp(at: Long): String =
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).format(Date(at))

    private fun roleName(r: ChatMsg.Role): String = when (r) {
        ChatMsg.Role.USER -> "我"
        ChatMsg.Role.ASSISTANT -> "她"
        ChatMsg.Role.SYSTEM -> "系统"
    }

    /** Markdown：模型当时真实走过的顺序（消息 + 挂在它上面的工具调用）。 */
    fun markdown(s: Session): String {
        val sb = StringBuilder()
        sb.append("# ").append(s.title.ifBlank { "新会话" }).append("\n\n")
        sb.append("- 时间：").append(stamp(s.at)).append('\n')
        sb.append("- 模型：").append(s.modelId.ifBlank { "未记录" }).append('\n')
        sb.append("- 消息：").append(s.msgs.size).append(" 条\n\n")
        for (m in s.msgs) {
            sb.append("## ").append(roleName(m.role)).append(" · ").append(stamp(m.at)).append("\n\n")
            if (m.thought.isNotBlank()) {
                sb.append("> 思考：").append(m.thought.replace("\n", "\n> ")).append("\n\n")
            }
            if (m.text.isNotBlank()) sb.append(m.text).append("\n\n")
            if (m.tools.isNotEmpty()) {
                sb.append("工具调用：\n\n")
                for (t in m.tools) {
                    sb.append("- `").append(t.name).append("` · ")
                        .append(
                            when (t.state) {
                                ToolCall.State.OK -> "完成"
                                ToolCall.State.FAIL -> "失败"
                                ToolCall.State.RUNNING -> "运行中"
                            }
                        )
                    if (t.ms > 0) sb.append(" · ").append(t.ms).append("ms")
                    sb.append('\n')
                    if (t.args.isNotBlank()) sb.append("  - 参数：").append(t.args.replace("\n", " ")).append('\n')
                    if (t.result.isNotBlank()) sb.append("  - 结果：").append(t.result.replace("\n", " ").take(400)).append('\n')
                }
                sb.append('\n')
            }
            if (m.failed.isNotBlank()) sb.append("> 失败：").append(m.failed).append("\n\n")
            if (m.media.isNotEmpty()) sb.append("（本轮附带 ").append(m.media.size).append(" 个媒体文件，导出不含文件内容）\n\n")
        }
        sb.append("---\n由天网 Next 导出 · 仅含文字与工具摘要\n")
        return Diagnostics.redact(sb.toString())
    }

    fun share(ctx: Context, s: Session) {
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_SUBJECT, s.title.ifBlank { "天网对话" })
            putExtra(Intent.EXTRA_TEXT, markdown(s))
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { ctx.startActivity(Intent.createChooser(i, "分享这段对话").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }

    fun copy(ctx: Context, s: Session) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText(s.title, markdown(s)))
    }
}

/** 一个 Overlay 里装完「选一段 → 只读回放 → 导出」。 */
@Composable
fun ReplayHostScreen() {
    var picked by remember { mutableStateOf<Session?>(null) }
    val s = picked
    if (s == null) ReplayPickerScreen(onPick = { picked = it })
    else ReplayScreen(session = s, onBack = { picked = null })
}

@Composable
private fun ReplayPickerScreen(onPick: (Session) -> Unit) {
    val ctx = LocalContext.current
    val sessions = remember { runCatching { SessionStore.load() }.getOrDefault(emptyList()) }
    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("对话回放", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("只读地重看存下来的对话，不喂回模型、不改动历史。可直接把一段导出成文字。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        if (sessions.isEmpty()) {
            Text("还没有历史会话。说过的话会自动存到手机本地，重开 App 也还在。",
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            return@Column
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(7.dp)) {
            items(sessions, key = { it.id }) { s ->
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .clickable { onPick(s) }
                        .padding(12.dp),
                ) {
                    Text(s.title.ifBlank { "新会话" }, fontSize = 13.5.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.height(3.dp))
                    Text(
                        SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(s.at)) +
                            " · ${s.msgs.size} 条消息 · ${s.msgs.sumOf { it.tools.size }} 次工具调用",
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Row(horizontalArrangement = Arrangement.End, modifier = Modifier.fillMaxWidth()) {
                        TextButton(onClick = { ConversationShare.share(ctx, s) }) { Text("导出这段") }
                    }
                }
            }
        }
    }
}

@Composable
private fun ReplayScreen(session: Session, onBack: () -> Unit) {
    val ctx = LocalContext.current
    Column(Modifier.fillMaxSize().padding(horizontal = 14.dp, vertical = 10.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            TextButton(onClick = onBack) { Text("← 全部会话") }
            Spacer(Modifier.weight(1f))
            TextButton(onClick = {
                ConversationShare.copy(ctx, session)
            }) { Text("复制") }
            TextButton(onClick = { ConversationShare.share(ctx, session) }) { Text("分享") }
        }
        Text(session.title.ifBlank { "新会话" }, fontSize = 16.sp,
            fontWeight = FontWeight.SemiBold, color = MaterialTheme.colorScheme.onSurface)
        Text(
            SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault()).format(Date(session.at)) +
                " · ${session.msgs.size} 条 · 只读回放",
            fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(10.dp))
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(session.msgs, key = { it.id }) { m -> ReplayMsg(m) }
        }
    }
}

@Composable
private fun ReplayMsg(m: ChatMsg) {
    var open by remember { mutableStateOf(false) }
    val mine = m.role == ChatMsg.Role.USER
    val align = if (mine) Alignment.End else Alignment.Start
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(
                if (m.role == ChatMsg.Role.SYSTEM) MaterialTheme.colorScheme.surfaceVariant
                else MaterialTheme.colorScheme.surface,
            )
            .clickable { open = !open }
            .padding(11.dp),
        horizontalAlignment = align,
    ) {
        Text(
            when (m.role) {
                ChatMsg.Role.USER -> "我"
                ChatMsg.Role.ASSISTANT -> "她"
                ChatMsg.Role.SYSTEM -> "系统"
            } + " · " + SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(m.at)),
            fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(4.dp))
        if (m.text.isNotBlank()) {
            Text(m.text, fontSize = 12.5.sp, lineHeight = 18.sp,
                color = MaterialTheme.colorScheme.onSurface)
        }
        if (m.failed.isNotBlank()) {
            Text("失败：${m.failed}", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.error)
        }
        if (m.media.isNotEmpty()) {
            Text("（${m.media.size} 个媒体文件，回放不加载图片）", fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (m.tools.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            for (t in m.tools) {
                Text(
                    "· ${t.name} · " + when (t.state) {
                        ToolCall.State.OK -> "完成"
                        ToolCall.State.FAIL -> "失败"
                        ToolCall.State.RUNNING -> "运行中"
                    } + if (t.ms > 0) " ${t.ms}ms" else "",
                    fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                    color = if (t.state == ToolCall.State.FAIL) MaterialTheme.colorScheme.error
                    else MaterialTheme.colorScheme.primary,
                )
            }
        }
        if (open) {
            Spacer(Modifier.height(6.dp))
            if (m.thought.isNotBlank()) {
                Text("思考：${m.thought}", fontSize = 10.5.sp, lineHeight = 15.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            for (t in m.tools) {
                if (t.args.isNotBlank()) {
                    Text("参数 ${t.name}：${t.args}", fontSize = 10.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (t.result.isNotBlank()) {
                    Text("结果 ${t.name}：${t.result.take(500)}", fontSize = 10.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}
