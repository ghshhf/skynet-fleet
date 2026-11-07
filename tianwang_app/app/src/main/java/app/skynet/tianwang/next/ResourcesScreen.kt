package app.skynet.tianwang.next

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ChatBubbleOutline
import androidx.compose.material.icons.filled.Star
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch

/**
 * 资源流：电脑侧资源中心的手机入口。
 *
 * 手机上不算分、不存库、不抓取——只做三件事：拉一页卡片、看详情、把行为送回去。
 * 所以杀掉 App 再开，赞和收藏还在；换一台手机，登录同一个电脑也还在。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ResourcesScreen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val session = remember { "phone-" + java.util.UUID.randomUUID().toString().take(8) }

    var items by remember { mutableStateOf<List<ResourceCard>>(emptyList()) }
    var topics by remember { mutableStateOf<List<String>>(emptyList()) }
    var topic by remember { mutableStateOf("") }
    var next by remember { mutableStateOf<Int?>(null) }
    var busy by remember { mutableStateOf(false) }
    var err by remember { mutableStateOf("") }
    var info by remember { mutableStateOf("") }
    var detail by remember { mutableStateOf<ResourceDetail?>(null) }
    var commentFor by remember { mutableStateOf<ResourceCard?>(null) }
    var commentText by remember { mutableStateOf("") }

    fun load(reset: Boolean) {
        if (!Net.hasConfig()) {
            err = "先在「设置」里填电脑地址和 token"
            return
        }
        if (busy) return
        if (!reset && next == null) return
        busy = true
        err = ""
        scope.launch {
            runCatching { ResourceApi.feed(topic, if (reset) 0 else (next ?: 0), session) }
                .onSuccess { f ->
                    items = if (reset) f.items else items + f.items
                    next = f.next
                    topics = (topics + f.items.flatMap { it.tags }).distinct().take(12)
                    info = if (reset && f.items.isEmpty()) "还没有资源，点「导入已有」" else ""
                }
                .onFailure { err = it.message ?: "连不上电脑" }
            busy = false
        }
    }

    fun doImport() {
        if (busy) return
        busy = true
        err = ""
        scope.launch {
            runCatching { ResourceApi.importExisting() }
                .onSuccess { o ->
                    if (!o.optBoolean("ok", false)) {
                        err = o.optString("err", "导入失败")
                        busy = false
                    } else {
                        info = "扫描 ${o.optInt("scanned")} 条，新增 ${o.optInt("new")}，" +
                            "合并 ${o.optInt("merged")}"
                        busy = false
                        load(true)
                    }
                }
                .onFailure {
                    err = it.message ?: "导入失败"
                    busy = false
                }
        }
    }

    fun act(c: ResourceCard, action: String, text: String = "") {
        scope.launch {
            runCatching { ResourceApi.act(c.id, action, text) }
                .onSuccess {
                    items = items.map { x ->
                        if (x.id != c.id) x else when (action) {
                            "like" -> x.copy(liked = true)
                            "favorite" -> x.copy(faved = true)
                            "comment" -> x.copy(comments = x.comments + 1)
                            else -> x
                        }
                    }
                }
                .onFailure { err = it.message ?: "操作失败" }
        }
    }

    fun open(c: ResourceCard) {
        scope.launch {
            runCatching { ResourceApi.detail(c.id) }
                .onSuccess { detail = it }
                .onFailure { err = it.message ?: "详情失败" }
        }
    }

    LaunchedEffect(topic) { load(true) }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth()
                .horizontalScroll(rememberScrollState())
                .padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            FilterChip(
                selected = topic.isEmpty(),
                onClick = { topic = "" },
                label = { Text("全部", fontSize = 11.sp) },
            )
            topics.forEach { t ->
                FilterChip(
                    selected = topic == t,
                    onClick = { topic = t },
                    label = { Text(t, fontSize = 11.sp) },
                )
            }
        }

        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                if (items.isEmpty()) "资源流" else "${items.size} 条 · 规则排序",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { doImport() }, enabled = !busy) { Text("导入已有", fontSize = 12.sp) }
            TextButton(onClick = { load(true) }, enabled = !busy) { Text("刷新", fontSize = 12.sp) }
        }

        if (err.isNotBlank()) {
            Text(
                err,
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp),
            )
        }
        if (info.isNotBlank()) {
            Text(
                info,
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp),
            )
        }

        Box(Modifier.weight(1f)) {
            if (items.isEmpty() && !busy) {
                Text(
                    "这里会显示电脑侧的资源卡片。\n先点「导入已有」，或在电脑上 ingest 后刷新。",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.align(Alignment.Center).padding(24.dp),
                )
            } else {
                LazyColumn(contentPadding = PaddingValues(top = 4.dp, bottom = 24.dp)) {
                    items(items, key = { it.id }) { c ->
                        ResourceRow(c, onOpen = { open(c) }, onAct = { a -> act(c, a) },
                            onComment = { commentFor = c; commentText = "" })
                    }
                    item {
                        if (next != null) {
                            TextButton(
                                onClick = { load(false) },
                                enabled = !busy,
                                modifier = Modifier.fillMaxWidth(),
                            ) { Text("加载更多", fontSize = 12.sp) }
                        }
                    }
                }
            }
            if (busy) CircularProgressIndicator(Modifier.align(Alignment.Center))
        }
    }

    detail?.let { d ->
        AlertDialog(
            onDismissRequest = { detail = null },
            title = { Text(d.card.title, maxLines = 3, fontSize = 16.sp) },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    if (d.card.summary.isNotBlank()) {
                        Text(d.card.summary, fontSize = 13.sp)
                    }
                    if (d.card.tags.isNotEmpty()) {
                        Text(
                            d.card.tags.joinToString("  #") { it },
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.padding(top = 8.dp),
                        )
                    }
                    Text(
                        "出处 ${d.origins.size} 个",
                        fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold,
                        modifier = Modifier.padding(top = 12.dp, bottom = 4.dp),
                    )
                    d.origins.take(8).forEach {
                        Text("· ${it.source}：${it.url}", fontSize = 10.sp, maxLines = 2)
                    }
                    if (d.actions.isNotEmpty()) {
                        Text(
                            "我的行为",
                            fontSize = 12.sp,
                            fontWeight = FontWeight.SemiBold,
                            modifier = Modifier.padding(top = 12.dp, bottom = 4.dp),
                        )
                        d.actions.take(8).forEach {
                            Text("${it.action}  ${it.text}", fontSize = 10.sp, maxLines = 2)
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { detail = null }) { Text("关闭") } },
            dismissButton = {
                if (d.card.url.isNotBlank()) {
                    TextButton(onClick = {
                        ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(d.card.url)))
                    }) { Text("打开") }
                }
            },
        )
    }

    commentFor?.let { c ->
        AlertDialog(
            onDismissRequest = { commentFor = null },
            title = { Text("评论：${c.title.take(18)}", fontSize = 15.sp) },
            text = {
                OutlinedTextField(
                    value = commentText,
                    onValueChange = { commentText = it },
                    minLines = 3,
                    placeholder = { Text("写一句…", fontSize = 12.sp) },
                )
            },
            confirmButton = {
                TextButton(
                    enabled = commentText.isNotBlank(),
                    onClick = {
                        act(c, "comment", commentText.trim())
                        commentFor = null
                        commentText = ""
                    },
                ) { Text("发送") }
            },
            dismissButton = { TextButton(onClick = { commentFor = null }) { Text("取消") } },
        )
    }
}

@Composable
private fun ResourceRow(
    c: ResourceCard,
    onOpen: () -> Unit,
    onAct: (String) -> Unit,
    onComment: () -> Unit,
) {
    Card(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 5.dp),
        shape = RoundedCornerShape(14.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Text(
                c.title,
                fontSize = 15.sp,
                fontWeight = FontWeight.SemiBold,
                maxLines = 2,
                modifier = Modifier.clickable(onClick = onOpen),
            )
            if (c.summary.isNotBlank()) {
                Text(
                    c.summary,
                    fontSize = 12.sp,
                    maxLines = 3,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            val meta = listOf(c.domain, c.kind).filter { it.isNotBlank() }.joinToString(" · ")
            if (meta.isNotBlank()) {
                Text(
                    meta,
                    fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
            if (c.tags.isNotEmpty()) {
                Text(
                    c.tags.take(4).joinToString("  #") { it },
                    fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.padding(top = 2.dp),
                    maxLines = 1,
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    listOfNotNull(
                        c.why.takeIf { it.isNotBlank() },
                        "评论 ${c.comments}".takeIf { c.comments > 0 },
                    ).joinToString(" · "),
                    fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    modifier = Modifier.weight(1f),
                )
                IconButton(onClick = { onAct("like") }) {
                    Icon(
                        Icons.Filled.ThumbUp,
                        "赞",
                        tint = if (c.liked) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                IconButton(onClick = { onAct("favorite") }) {
                    Icon(
                        Icons.Filled.Star,
                        "收藏",
                        tint = if (c.faved) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                IconButton(onClick = onComment) {
                    Icon(
                        Icons.Filled.ChatBubbleOutline,
                        "评论",
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}
