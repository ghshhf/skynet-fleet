package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 全局搜索：一次查 会话 / 资源 / 能力工具 三类。
 *
 * DSH Go 有全局 search；天网原来只在左侧抽屉里搜会话标题。这里把资源流和 64 个工具
 * 也拉进同一个框 —— 用户不用在三个页面之间来回切着找。命中即跳转：
 * 会话直接打开、工具跳到能力目录并带词、资源复制链接（资源详情页本就不独立存在）。
 *
 * 纪律：资源搜索不写曝光（recordExposure=false），别让一次搜索把首页排序搅乱。
 */
@Composable
fun GlobalSearchScreen(
    sessions: List<Session>,
    onPickSession: (Session) -> Unit,
    onOpenCaps: (String) -> Unit,
) {
    val ctx = LocalContext.current
    var q by remember { mutableStateOf("") }
    val s = q.trim()

    val sessHits = if (s.isEmpty()) emptyList() else sessions.filter {
        it.title.contains(s, true) || it.msgs.any { m -> m.text.contains(s, true) }
    }.take(20)
    val toolHits = remember(s) {
        if (s.isEmpty()) emptyList() else CapabilityCatalog.search(
            runCatching { PhoneMcp.toolList() }.getOrDefault(emptyList()), s).take(20)
    }
    val resHits = remember(s) {
        if (s.isEmpty()) emptyList() else runCatching {
            ResourceStore.feed(s, 0, "global_search", 30, "", false).items
        }.getOrDefault(emptyList())
    }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("全局搜索", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("一次查 会话 / 资源 / 能力工具。", fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        BasicTextField(
            value = q, onValueChange = { q = it }, singleLine = true,
            textStyle = TextStyle(fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            modifier = Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(9.dp))
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 12.dp, vertical = 10.dp),
            decorationBox = { inner ->
                if (q.isEmpty()) Text("搜关键词…", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                inner()
            },
        )
        Spacer(Modifier.height(10.dp))
        if (s.isEmpty()) {
            Text("输入关键词后，下面会分三段列出命中的会话、资源、工具。", fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            return@Column
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(7.dp)) {
            if (sessHits.isNotEmpty()) {
                item { Group("会话 · ${sessHits.size}") }
                items(sessHits, key = { it.id }) { s2 ->
                    SearchRow(s2.title, "会话 · ${s2.msgs.size} 条消息") { onPickSession(s2) }
                }
            }
            if (resHits.isNotEmpty()) {
                item { Group("资源 · ${resHits.size}") }
                items(resHits, key = { it.id }) { c ->
                    SearchRow(c.title, "${c.domain} · ${c.kind}") {
                        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                        cm?.setPrimaryClip(ClipData.newPlainText("天网资源", c.url))
                        Toast.makeText(ctx, "已复制链接：${c.title.take(20)}", Toast.LENGTH_SHORT).show()
                    }
                }
            }
            if (toolHits.isNotEmpty()) {
                item { Group("能力工具 · ${toolHits.size}") }
                items(toolHits, key = { it.name }) { t ->
                    SearchRow(t.name, t.description.take(60)) { onOpenCaps(t.name) }
                }
            }
            if (sessHits.isEmpty() && resHits.isEmpty() && toolHits.isEmpty()) {
                item { Text("没有命中「$s」。", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
        }
    }
}

@Composable
private fun Group(t: String) {
    Text(t, fontSize = 12.sp, fontWeight = FontWeight.Medium,
        color = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(top = 6.dp, start = 3.dp))
}

@Composable
private fun SearchRow(title: String, sub: String, onClick: () -> Unit) {
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onClick)
            .padding(11.dp),
    ) {
        Text(title, fontSize = 12.sp, fontFamily = FontFamily.Monospace,
            fontWeight = FontWeight.Medium, color = MaterialTheme.colorScheme.onSurface, maxLines = 1)
        Spacer(Modifier.height(3.dp))
        Text(sub, fontSize = 10.5.sp, lineHeight = 15.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 2)
    }
}
