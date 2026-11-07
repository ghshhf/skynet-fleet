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
import androidx.compose.foundation.lazy.rememberLazyListState
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
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.snapshotFlow
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
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.distinctUntilChanged
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * 资源流：手机自己抓、自己存、自己显示。
 *
 * 卡片来自本机 SQLite（首次启动带一份已爬完的成果），抓取与打分也在本机完成。
 * 别的节点只作为可选共享来源，拉不到照常能刷。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ResourcesScreen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    // 与启动预热共用同一个会话，保证预热的那 20 条就是首页真正显示的那 20 条。
    val session = ResourceApi.FEED_SESSION

    var items by remember { mutableStateOf<List<ResourceCard>>(emptyList()) }
    var topics by remember { mutableStateOf<List<String>>(emptyList()) }
    var topic by remember { mutableStateOf("") }
    var next by remember { mutableStateOf<Int?>(null) }
    var busy by remember { mutableStateOf(false) }
    var refreshing by remember { mutableStateOf(false) }
    var autoCrawlDone by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()
    var err by remember { mutableStateOf("") }
    var info by remember { mutableStateOf("") }
    var detail by remember { mutableStateOf<ResourceDetail?>(null) }
    var viewer by remember { mutableStateOf<ResourceCard?>(null) }
    var previews by remember { mutableStateOf<Map<String, LinkPreview>>(emptyMap()) }
    var commentFor by remember { mutableStateOf<ResourceCard?>(null) }
    var commentText by remember { mutableStateOf("") }

    /** 只从本机库读一页；下拉刷新和首屏共用它，不额外发任何网络请求。 */
    suspend fun fetchPage(reset: Boolean) {
        val f = ResourceApi.feed(topic, if (reset) 0 else (next ?: 0), session)
        items = if (reset) f.items else items + f.items
        next = f.next
        topics = (topics + f.items.flatMap { it.tags }).distinct().take(12)
        // 零请求先把规则封面 / 已缓存 OG 铺满这一页：卡片立刻有图有标题，
        // 不等后台预热，也不出现「先一堆文字、再转圈、最后出图」。
        val instant = runCatching { ResourceApi.previewInstant(f.items.map { it.url }) }
            .getOrDefault(emptyMap())
        if (instant.isNotEmpty()) previews = previews + instant
    }

    /** 顶部下拉 = 自己联网抓一轮 + 重载本机库。单个源失败只记一行，不影响其余入库。 */
    fun refresh() {
        if (busy || refreshing) return
        refreshing = true
        err = ""
        info = "正在联网抓取新资源…"
        scope.launch {
            val o = runCatching { ResourceApi.crawl() }.getOrNull()
            runCatching { fetchPage(true) }
                .onFailure { err = it.message ?: "读取本机资源失败" }
            info = if (o == null) "抓取没成功，已显示本机最新内容"
            else {
                val failed = o.optJSONArray("failed")
                val tail = if (failed != null && failed.length() > 0) " · ${failed.optString(0)}" else ""
                "抓取 ${o.optInt("sources")}/${o.optInt("sources_total")} 个源，" +
                    "新增 ${o.optInt("new")} 条，合并 ${o.optInt("merged")} 条$tail"
            }
            if (items.isEmpty()) info = "本机还没有资源，稍后再下拉刷新一次"
            refreshing = false
        }
    }

    fun load(reset: Boolean) {
        if (busy || refreshing) return
        if (!reset && next == null) return
        busy = true
        err = ""
        scope.launch {
            val before = items.size
            runCatching { fetchPage(reset) }
                .onFailure { err = it.message ?: "读取本机资源失败" }
            val freshUrls = if (reset) items.take(ResourcePrefetchLogic.FIRST_PAGE).map { it.url }
            else items.drop(before).map { it.url }
            // 后台把这一页的 OG + 封面图预先下好。首次启动那次已在 App 启动时跑过，
            // 这里命中磁盘缓存几乎零请求；只有下拉换了新内容才补新的。
            if (freshUrls.isNotEmpty()) scope.launch {
                val got = runCatching { ResourceApi.prefetch(freshUrls) }.getOrDefault(emptyMap())
                if (got.isNotEmpty()) previews = previews + got
            }
            if (reset && items.isEmpty()) info = "本机还没有资源，下拉刷新即可自己联网抓取"
            busy = false
            // 只有本机库真的空（例如清过数据）才自动补一次，绝不在每次进页面时抓 26 个源。
            if (reset && items.isEmpty() && !autoCrawlDone && ResourceApi.crawlDue()) {
                autoCrawlDone = true
                refresh()
            }
        }
    }

    /** 可选：跟登记过的对等节点共享一批，没有节点也不报错。 */
    fun doSync() {
        if (busy) return
        busy = true
        err = ""
        scope.launch {
            runCatching { ResourceApi.syncPeers() }
                .onSuccess { o ->
                    info = o.optString("skipped").ifBlank {
                        "共享：新增 ${o.optInt("new")} 条，合并 ${o.optInt("merged")} 条"
                    }
                    busy = false
                    if (o.optInt("new") > 0) load(true)
                }
                .onFailure {
                    err = it.message ?: "共享失败"
                    busy = false
                }
        }
    }

    fun act(c: ResourceCard, action: String, text: String = "") {
        scope.launch {
            runCatching { ResourceApi.act(c.id, action, text) }
                .onSuccess { o ->
                    if (!o.optBoolean("ok", false)) {
                        err = o.optString("err", "操作失败")
                        return@onSuccess
                    }
                    val removed = o.optBoolean("removed", false)
                    items = items.map { x ->
                        if (x.id != c.id) x else when (action) {
                            "like" -> x.copy(liked = !removed)
                            "favorite" -> x.copy(faved = !removed)
                            "comment" -> x.copy(comments = x.comments + 1)
                            else -> x
                        }
                    }
                    info = when {
                        removed && action == "like" -> "已取消赞"
                        removed && action == "favorite" -> "已取消收藏"
                        action == "like" -> "已赞"
                        action == "favorite" -> "已收藏"
                        else -> info
                    }
                }
                .onFailure { err = it.message ?: "操作失败" }
        }
    }

    /** 点卡片就在 App 内把页面加载出来，不跳浏览器。 */
    fun open(c: ResourceCard) {
        if (c.url.startsWith("http", true)) viewer = c
        else err = "这条资源没有可打开的链接"
    }

    fun showDetail(c: ResourceCard) {
        scope.launch {
            runCatching { ResourceApi.detail(c.id) }
                .onSuccess { detail = it }
                .onFailure { err = it.message ?: "详情失败" }
        }
    }

    /** 滑动补漏：首屏 20 条已预先备好，这里只兜住快速滑到很后面的情况。
     *  复用同一套预热器（跳过已缓存、低并发），不会跟抓取器抢带宽。 */
    fun ensurePreviews(from: Int) {
        if (viewer != null || items.isEmpty()) return
        val urls = items.drop(from).take(6).map { it.url }
            .filter { it.startsWith("http", true) }.distinct()
        if (urls.isEmpty()) return
        scope.launch {
            val got = runCatching { ResourceApi.prefetch(urls) }.getOrDefault(emptyMap())
            if (got.isNotEmpty()) previews = previews + got
        }
    }

    LaunchedEffect(topic) { load(true) }

    // 首屏 / 每次滑动停下来，只补可见窗口那几条；文章打开时完全不抢带宽。
    LaunchedEffect(items, viewer) {
        if (viewer != null) return@LaunchedEffect
        ensurePreviews(listState.firstVisibleItemIndex)
        snapshotFlow { listState.firstVisibleItemIndex }
            .distinctUntilChanged()
            .collect { idx -> ensurePreviews((idx - 1).coerceAtLeast(0)) }
    }

    // 提前 8 条就续页（视频平台那套），不等滚到底才拉。用 (末尾位置, 条数) 作键，
    // 每加载一页都会重新判定，不会被「一直 true」的去重卡住。
    LaunchedEffect(listState, next, items.size) {
        snapshotFlow {
            val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
            last to items.size
        }.distinctUntilChanged().collect { (last, size) ->
            if (ResourcePrefetchLogic.due(last, size, next != null)) load(false)
        }
    }

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
            Text("下拉自动刷新", fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            TextButton(onClick = { doSync() }, enabled = !busy && !refreshing) {
                Text("节点共享", fontSize = 12.sp)
            }
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

        PullToRefreshBox(
            isRefreshing = refreshing,
            onRefresh = { refresh() },
            modifier = Modifier.weight(1f),
        ) {
            if (items.isEmpty() && !busy && !refreshing) {
                // 空态也要是可滚动内容，下拉手势才有地方落；否则库空时反而拉不动。
                Column(
                    Modifier.fillMaxSize().verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Text(
                        "本机资源库还没内容。\n在顶部下拉，手机就自己联网抓一轮。",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(24.dp),
                    )
                }
            } else {
                LazyColumn(
                    state = listState,
                    contentPadding = PaddingValues(top = 4.dp, bottom = 24.dp),
                ) {
                    items(items, key = { it.id }) { c ->
                        ResourceRow(c, preview = previews[c.url],
                            onOpen = { open(c) }, onAct = { a -> act(c, a) },
                            onComment = { commentFor = c; commentText = "" })
                    }
                    // 滚到底自动续页，这里只留一个轻量进度点，不用点按钮
                    if (next != null) {
                        item {
                            Box(
                                Modifier.fillMaxWidth().padding(12.dp),
                                contentAlignment = Alignment.Center,
                            ) {
                                if (busy) {
                                    CircularProgressIndicator(
                                        Modifier.size(18.dp), strokeWidth = 2.dp,
                                    )
                                }
                            }
                        }
                    }
                }
            }
            if (busy && items.isEmpty() && !refreshing) {
                CircularProgressIndicator(Modifier.align(Alignment.Center))
            }
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
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = {
                            ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(d.card.url)))
                        }) { Text("浏览器", fontSize = 11.sp) }
                        TextButton(onClick = {
                            detail = null
                            viewer = d.card
                        }) { Text("App 内打开", fontSize = 11.sp) }
                    }
                }
            },
        )
    }

    viewer?.let { c ->
        ResourceViewer(
            card = c,
            onClose = { viewer = null },
            onDetail = { viewer = null; showDetail(c) },
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
    preview: LinkPreview?,
    onOpen: () -> Unit,
    onAct: (String) -> Unit,
    onComment: () -> Unit,
) {
    val dim = MaterialTheme.colorScheme.onSurfaceVariant
    val acc = MaterialTheme.colorScheme.primary
    Column {
        Row(Modifier.fillMaxWidth().padding(horizontal = 14.dp, vertical = 12.dp)) {
            // 头像用域名首字母 + 由域名算出的稳定色，不请求网络图片
            Box(
                Modifier.size(40.dp).clip(CircleShape).background(avatarColor(c.domain)),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    c.domain.removePrefix("www.").take(1).uppercase(),
                    color = Color.White, fontSize = 15.sp, fontWeight = FontWeight.Bold,
                )
            }
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        c.domain.ifBlank { "未知来源" },
                        fontSize = 13.5.sp, fontWeight = FontWeight.SemiBold,
                        maxLines = 1, modifier = Modifier.weight(1f, fill = false),
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(kindLabel(c.kind), fontSize = 10.sp, color = acc)
                    val rel = relTime(c.at)
                    if (rel.isNotBlank()) {
                        Spacer(Modifier.width(6.dp))
                        Text("· $rel", fontSize = 11.sp, color = dim, maxLines = 1)
                    }
                }
                Text(
                    c.title, fontSize = 15.sp, fontWeight = FontWeight.SemiBold, maxLines = 2,
                    modifier = Modifier.clickable(onClick = onOpen).padding(top = 4.dp),
                )
                if (c.summary.isNotBlank()) {
                    Text(c.summary, fontSize = 13.sp, maxLines = 3, color = dim,
                        modifier = Modifier.padding(top = 3.dp))
                }
                if (c.url.startsWith("http", true)) {
                    PreviewBlock(preview, c, onOpen)
                }
                if (c.tags.isNotEmpty()) {
                    Text(c.tags.take(4).joinToString("  #") { it }, fontSize = 11.sp,
                        color = acc, maxLines = 1, modifier = Modifier.padding(top = 4.dp))
                }
                if (c.why.isNotBlank()) {
                    Text(c.why + if (c.score != 0.0) " · ${"%.2f".format(c.score)}" else "",
                        fontSize = 10.5.sp, color = dim, maxLines = 1,
                        modifier = Modifier.padding(top = 4.dp))
                }
                Row(Modifier.fillMaxWidth().padding(top = 6.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    MiniAct(Icons.Filled.ChatBubbleOutline,
                        if (c.comments > 0) c.comments.toString() else "", dim, onComment)
                    Spacer(Modifier.weight(1f))
                    MiniAct(Icons.Filled.Star, if (c.faved) "已收藏" else "",
                        if (c.faved) acc else dim) { onAct("favorite") }
                    Spacer(Modifier.weight(1f))
                    MiniAct(Icons.Filled.ThumbUp, if (c.liked) "已赞" else "",
                        if (c.liked) acc else dim) { onAct("like") }
                    Spacer(Modifier.weight(1f))
                }
            }
        }
        // 平铺：用一条分隔线代替原来的圆角卡片
        Box(Modifier.fillMaxWidth().height(1.dp).background(MaterialTheme.colorScheme.outline))
    }
}

@Composable
private fun MiniAct(icon: ImageVector, label: String, tint: Color, onClick: () -> Unit) {
    Row(Modifier.clickable(onClick = onClick).padding(4.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(icon, label, tint = tint, modifier = Modifier.size(16.dp))
        if (label.isNotBlank()) {
            Spacer(Modifier.width(4.dp))
            Text(label, fontSize = 10.5.sp, color = tint, maxLines = 1)
        }
    }
}

/**
 * 推特式链接卡片：能带图就带图，带不出图也是「站点 + 标题 + 摘要」的一块内容，
 * 不是一行裸链接。点整块直接在 App 内加载页面。
 */
@Composable
private fun PreviewBlock(p: LinkPreview?, c: ResourceCard, onClick: () -> Unit) {
    val shape = RoundedCornerShape(12.dp)
    val dim = MaterialTheme.colorScheme.onSurfaceVariant
    val site = p?.site?.takeIf { it.isNotBlank() } ?: c.domain
    val head = p?.title?.takeIf { it.isNotBlank() } ?: c.title
    val desc = p?.description?.takeIf { it.isNotBlank() } ?: c.summary
    Column(
        Modifier.fillMaxWidth().padding(top = 8.dp)
            .clip(shape)
            .border(1.dp, MaterialTheme.colorScheme.outline.copy(alpha = 0.45f), shape)
            .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.35f))
            .clickable(onClick = onClick),
    ) {
        if (p?.hasImage == true) {
            RemoteImage(url = p.image, label = site,
                modifier = Modifier.fillMaxWidth().height(168.dp))
        } else {
            Box(
                Modifier.fillMaxWidth().height(50.dp)
                    .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.6f)),
                contentAlignment = Alignment.CenterStart,
            ) {
                Row(
                    Modifier.padding(horizontal = 12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(site.removePrefix("www.").take(1).uppercase(), fontSize = 20.sp,
                        fontWeight = FontWeight.Bold, color = MaterialTheme.colorScheme.primary)
                    Spacer(Modifier.width(10.dp))
                    Text(site, fontSize = 11.sp, color = dim, maxLines = 1)
                }
            }
        }
        Column(Modifier.padding(horizontal = 10.dp, vertical = 8.dp)) {
            Text(site, fontSize = 10.5.sp, color = dim, maxLines = 1)
            Text(head, fontSize = 13.sp, fontWeight = FontWeight.SemiBold, maxLines = 2,
                modifier = Modifier.padding(top = 2.dp))
            if (desc.isNotBlank() && desc != c.summary) {
                Text(desc, fontSize = 11.5.sp, color = dim, maxLines = 2,
                    modifier = Modifier.padding(top = 3.dp))
            }
        }
    }
}

private fun kindLabel(k: String): String = when (k) {
    "article" -> "文章"
    "video" -> "视频"
    "paper" -> "论文"
    "repo" -> "开源项目"
    "model" -> "模型"
    "tool" -> "工具"
    "image" -> "图片"
    "story" -> "帖子"
    "topic" -> "主题"
    "work" -> "作品"
    "series" -> "系列"
    "author" -> "作者"
    "music" -> "音乐"
    "domain" -> "站点"
    "owner" -> "号主"
    "page" -> "页面"
    "space" -> "空间"
    "dataset" -> "数据集"
    else -> k.ifBlank { "资源" }
}

private fun avatarColor(s: String): Color {
    var h = 0
    for (ch in s) h = (h * 31 + ch.code) % 360
    return Color.hsv(h.toFloat(), 0.48f, 0.60f)
}

private fun relTime(s: String): String {
    if (s.isBlank()) return ""
    val v = s.replace('T', ' ').replace("Z", "").trim()
    val d = runCatching {
        SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.getDefault()).parse(v.take(19))
    }.getOrNull() ?: return s.take(10)
    val sec = ((System.currentTimeMillis() - d.time) / 1000).coerceAtLeast(0)
    return when {
        sec < 60 -> "刚刚"
        sec < 3600 -> "${sec / 60} 分钟前"
        sec < 86400 -> "${sec / 3600} 小时前"
        sec < 604800 -> "${sec / 86400} 天前"
        else -> s.take(10)
    }
}
