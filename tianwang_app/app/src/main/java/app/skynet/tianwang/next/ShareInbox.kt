package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * 分享收件箱：别的 App 把一段文字 / 链接丢给天网，先稳稳接住，再由人决定怎么用。
 *
 * 对照 DSH Go 的 links/share_intake + native_inbox：分享进来的东西不能只在
 * 启动那一瞬活着 —— 用户还没想好要不要用它，进程一被回收就没了，等于没接住。
 * 这里落成一个手机私有目录里的 JSON 文件，和 [SessionStore] 一样先写临时文件再改名。
 *
 * 纪律：只存文本本身，不自动发给模型、不自动联网、不自动入库。
 * 收到什么、什么时候用，都由人点。
 */
data class SharedItem(
    val id: String = UUID.randomUUID().toString(),
    val text: String = "",
    val kind: String = "text",
    val from: String = "",
    val at: Long = System.currentTimeMillis(),
)

object ShareInbox {
    private const val FILE = "share_inbox.json"
    private const val MAX = 200

    private fun file(): File = File(App.ctx.filesDir, FILE)

    fun load(): List<SharedItem> {
        val f = file()
        if (!f.isFile) return emptyList()
        return runCatching {
            val arr = JSONArray(f.readText(Charsets.UTF_8))
            val out = ArrayList<SharedItem>(arr.length())
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                out.add(
                    SharedItem(
                        id = o.optString("id").ifBlank { "sh-$i" },
                        text = o.optString("text"),
                        kind = o.optString("kind", "text"),
                        from = o.optString("from"),
                        at = o.optLong("at", 0L),
                    )
                )
            }
            out.sortedByDescending { it.at }
        }.getOrDefault(emptyList())
    }

    private fun save(items: List<SharedItem>) {
        runCatching {
            val arr = JSONArray()
            for (it in items.take(MAX)) {
                arr.put(
                    JSONObject()
                        .put("id", it.id).put("text", it.text)
                        .put("kind", it.kind).put("from", it.from).put("at", it.at),
                )
            }
            val tmp = File(file().parentFile, "$FILE.tmp")
            tmp.writeText(arr.toString(), Charsets.UTF_8)
            if (!tmp.renameTo(file())) {
                file().writeText(arr.toString(), Charsets.UTF_8)
                tmp.delete()
            }
        }
    }

    /** 同一段文字 5 分钟内重复分享只留一条，避免手抖连点刷满列表。 */
    fun add(text: String, kind: String, from: String): Boolean {
        val t = text.trim()
        if (t.isEmpty()) return false
        val now = System.currentTimeMillis()
        val items = load().toMutableList()
        if (items.any { it.text == t && now - it.at < 300_000L }) return false
        items.add(0, SharedItem(text = t, kind = kind, from = from, at = now))
        save(items)
        return true
    }

    fun remove(id: String) = save(load().filter { it.id != id })

    fun clear() = save(emptyList())

    fun copy(ctx: Context, text: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("天网收件箱", text))
    }

    fun shareOut(ctx: Context, text: String) {
        val i = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        runCatching { ctx.startActivity(Intent.createChooser(i, "转发这段").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
    }
}

/** 从系统分享 Intent 里取文本；不认识的形状一律不收，不猜。 */
object ShareIntake {
    fun absorb(intent: Intent?): Boolean {
        val i = intent ?: return false
        val action = i.action ?: return false
        val text = when (action) {
            Intent.ACTION_SEND -> i.getStringExtra(Intent.EXTRA_TEXT)
            Intent.ACTION_PROCESS_TEXT -> i.getCharSequenceExtra(Intent.EXTRA_PROCESS_TEXT)?.toString()
            else -> null
        } ?: return false
        val kind = when {
            text.trim().startsWith("http://", true) || text.trim().startsWith("https://", true) -> "link"
            else -> "text"
        }
        return ShareInbox.add(text, kind, i.getStringExtra(Intent.EXTRA_SUBJECT) ?: "")
    }
}

@Composable
fun InboxScreen() {
    val ctx = LocalContext.current
    var items by remember { mutableStateOf(ShareInbox.load()) }
    var note by remember { mutableStateOf("") }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("分享收件箱", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("别的 App 用「分享 → 天网 Next」送来的文字和链接会留在这里。只存手机本地，不自动发给模型、不联网。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                items = ShareInbox.load()
                note = "已刷新 · ${items.size} 条"
            }, modifier = Modifier.weight(1f)) { Text("刷新") }
            Button(onClick = {
                ShareInbox.clear()
                items = emptyList()
                note = "已清空"
            }, modifier = Modifier.weight(1f)) { Text("清空") }
        }
        if (note.isNotBlank()) {
            Spacer(Modifier.height(5.dp))
            Text(note, fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
        }
        Spacer(Modifier.height(10.dp))
        if (items.isEmpty()) {
            Text("还没有收到东西。在任意 App 里点分享，选「天网 Next」试试。",
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            return@Column
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(7.dp)) {
            items(items, key = { it.id }) { it ->
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .padding(12.dp),
                ) {
                    Text(
                        (if (it.kind == "link") "链接" else "文字") + " · " +
                            SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(it.at)) +
                            if (it.from.isNotBlank()) " · ${it.from.take(40)}" else "",
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.height(5.dp))
                    Text(it.text.take(600), fontSize = 12.sp, lineHeight = 17.sp,
                        color = MaterialTheme.colorScheme.onSurface)
                    Row(modifier = Modifier.fillMaxWidth()) {
                        TextButton(onClick = {
                            ShareInbox.copy(ctx, it.text)
                            note = "已复制，可粘进对话"
                        }) { Text("复制") }
                        TextButton(onClick = { ShareInbox.shareOut(ctx, it.text) }) { Text("转发") }
                        Spacer(Modifier.weight(1f))
                        TextButton(onClick = {
                            ShareInbox.remove(it.id)
                            items = ShareInbox.load()
                            note = "已删除一条"
                        }) { Text("删除") }
                    }
                }
            }
        }
    }
}
