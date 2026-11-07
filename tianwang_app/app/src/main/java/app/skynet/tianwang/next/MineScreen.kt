package app.skynet.tianwang.next

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Bookmark
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.Info
import androidx.compose.material.icons.filled.Label
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.ThumbUp
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import kotlinx.coroutines.launch
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.json.JSONObject

/**
 * 「我」页 —— 国内外大厂共有的那一格（微信 / 支付宝 / X / Instagram 都有）。
 *
 * 存在的理由不是「看上去像大厂」：收藏、赞过的、关注主题、设置这些东西
 * 原本散落在资源流、抽屉、工具箱里，没有「我」就根本没有归处。
 *
 * 数据全部在本机 SQLite；电脑和其他节点只是可选共享来源。
 * 这一页只摆给人看的东西；给 AI 调的底层能力不在这里，也不在工具箱。
 */
@Composable
fun MineScreen(
    onFavorites: () -> Unit,
    onLiked: () -> Unit,
    onFocus: () -> Unit,
    onSettings: () -> Unit,
    onDevice: () -> Unit,
    onAbout: () -> Unit,
) {
    var stats by remember { mutableStateOf<JSONObject?>(null) }
    var err by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        runCatching { ResourceApi.stats() }
            .onSuccess { stats = it; err = "" }
            .onFailure { err = it.message ?: "读取统计失败" }
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(vertical = 12.dp),
    ) {
        item {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Box(
                    Modifier.size(54.dp).clip(CircleShape)
                        .background(MaterialTheme.colorScheme.primaryContainer),
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(Icons.Filled.PhoneAndroid, null,
                        tint = MaterialTheme.colorScheme.onPrimaryContainer,
                        modifier = Modifier.size(26.dp))
                }
                Spacer(Modifier.width(14.dp))
                Column(Modifier.weight(1f)) {
                    Text("本机空间", fontSize = 17.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface)
                    Text(
                        if (stats != null) "资源 ${stats!!.optInt("resources")} 条 · 关注 ${stats!!.optJSONArray("focus")?.length() ?: 0} 个主题"
                        else err.ifBlank { "正在读取…" },
                        fontSize = 11.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }

        item {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                MineQuick("我的收藏", Icons.Filled.Bookmark, Modifier.weight(1f), onFavorites)
                MineQuick("我赞过的", Icons.Filled.ThumbUp, Modifier.weight(1f), onLiked)
                MineQuick("关注主题", Icons.Filled.Label, Modifier.weight(1f), onFocus)
            }
        }

        item { MineGroup("我的内容") }
        item { MineRow(Icons.Filled.Bookmark, "我的收藏", "收藏过的资源卡片", onFavorites) }
        item { MineRow(Icons.Filled.ThumbUp, "点赞与评论", "我点过赞、写过评论的", onLiked) }
        item { MineRow(Icons.Filled.Label, "关注主题", "决定信息流里先看到什么", onFocus) }

        item { MineGroup("系统") }
        item { MineRow(Icons.Filled.Settings, "设置", "回复语言 / 推理模式 / 大模型 API", onSettings) }
        item { MineRow(Icons.Filled.PhoneAndroid, "设备与权限", "授权状态与手机入网", onDevice) }
        item { MineRow(Icons.Filled.Info, "关于与版本", "版本、后端与数据来源", onAbout) }
        item { Spacer(Modifier.height(20.dp)) }
    }
}

@Composable
private fun MineQuick(label: String, icon: ImageVector, modifier: Modifier, onClick: () -> Unit) {
    Column(
        modifier
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onClick)
            .padding(vertical = 14.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(icon, null, tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(21.dp))
        Spacer(Modifier.height(7.dp))
        Text(label, fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurface)
    }
}

@Composable
private fun MineGroup(t: String) {
    Text(t, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 18.dp, top = 18.dp, bottom = 4.dp))
}

@Composable
private fun MineRow(icon: ImageVector, title: String, sub: String, onClick: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, null, tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(19.dp))
        Spacer(Modifier.width(13.dp))
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 13.5.sp, color = MaterialTheme.colorScheme.onSurface)
            Text(sub, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Icon(Icons.Filled.ChevronRight, null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(17.dp))
    }
    HorizontalDivider(color = MaterialTheme.colorScheme.outline,
        modifier = Modifier.padding(horizontal = 16.dp))
}

/** 收藏 / 赞过：同一套卡片，只在本机库里按行为筛。 */
@Composable
fun MineListScreen(kind: String) {
    val scope = rememberCoroutineScope()
    val session = remember { "mine-" + java.util.UUID.randomUUID().toString().take(8) }
    var items by remember { mutableStateOf<List<ResourceCard>>(emptyList()) }
    var busy by remember { mutableStateOf(true) }
    var err by remember { mutableStateOf("") }
    var viewer by remember { mutableStateOf<ResourceCard?>(null) }

    LaunchedEffect(kind) {
        busy = true
        runCatching { ResourceApi.mine(kind, 0, session) }
            .onSuccess { items = it.items; err = if (it.items.isEmpty()) "这里还没有内容" else "" }
            .onFailure { err = it.message ?: "读取失败" }
        busy = false
    }

    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 24.dp)) {
        item {
            Text(
                if (kind == "favorite") "我的收藏" else "我赞过的",
                fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp),
            )
        }
        if (busy) item { Text("加载中…", fontSize = 12.sp,
            modifier = Modifier.padding(16.dp),
            color = MaterialTheme.colorScheme.onSurfaceVariant) }
        if (err.isNotBlank() && !busy) item {
            Text(err, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(16.dp))
        }
        items(items, key = { it.id }) { c ->
            Column(
                Modifier.fillMaxWidth().clickable { viewer = c }
                    .padding(horizontal = 16.dp, vertical = 12.dp),
            ) {
                Text(c.domain.ifBlank { "未知来源" }, fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(4.dp))
                Text(c.title, fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurface, maxLines = 2)
                if (c.summary.isNotBlank()) {
                    Spacer(Modifier.height(4.dp))
                    Text(c.summary, fontSize = 11.5.sp, maxLines = 2,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "点开在 App 内看 · ${c.url.removePrefix("https://").take(40)}",
                        fontSize = 10.sp, maxLines = 1,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.width(8.dp))
                    TextButton(onClick = {
                        scope.launch {
                            runCatching { ResourceApi.act(c.id, kind) }
                                .onSuccess { o ->
                                    if (o.optBoolean("removed", false)) {
                                        items = items.filter { it.id != c.id }
                                    }
                                }
                                .onFailure { err = it.message ?: "取消失败" }
                        }
                    }) {
                        Text(if (kind == "favorite") "取消收藏" else "取消赞", fontSize = 11.sp)
                    }
                }
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(horizontal = 16.dp))
        }
    }
    viewer?.let { c -> ResourceViewer(card = c, onClose = { viewer = null }) }
}

/** 关注主题：直接读写本机 focus 表。 */
@Composable
fun MineFocusScreen() {
    val scope = rememberCoroutineScope()
    var tags by remember { mutableStateOf<List<String>>(emptyList()) }
    var busy by remember { mutableStateOf(true) }
    var err by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        busy = true
        runCatching { ResourceApi.focus() }
            .onSuccess { tags = it; err = "" }
            .onFailure { err = it.message ?: "读取失败" }
        busy = false
    }

    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 24.dp)) {
        item {
            Text("点一下取消关注。关注状态记在本机，可通过对等节点共享同步。",
                fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 12.dp))
        }
        if (busy) item { Text("加载中…", fontSize = 12.sp,
            modifier = Modifier.padding(16.dp),
            color = MaterialTheme.colorScheme.onSurfaceVariant) }
        if (!busy && err.isBlank() && tags.isEmpty()) item {
            Text("还没有关注主题。在资源流里点标签即可关注。", fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(16.dp))
        }
        if (err.isNotBlank()) item {
            Text(err, fontSize = 12.sp, color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(16.dp))
        }
        items(tags, key = { it }) { tag ->
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("#$tag", fontSize = 13.5.sp, modifier = Modifier.weight(1f),
                    color = MaterialTheme.colorScheme.onSurface)
                TextButton(onClick = {
                    // 复用 act(focus) 的开关语义：再点一次即取消
                    scope.launch {
                        runCatching { ResourceApi.toggleFocus(tag) }
                            .onSuccess { tags = it; err = "" }
                            .onFailure { err = it.message ?: "操作失败" }
                    }
                }) { Text("取消关注", fontSize = 11.sp) }
            }
            HorizontalDivider(color = MaterialTheme.colorScheme.outline,
                modifier = Modifier.padding(horizontal = 16.dp))
        }
    }
}

/** 关于与版本：纯展示，不摆「给 AI 用的」东西。 */
@Composable
fun MineAboutScreen() {
    val ctx = LocalContext.current
    val ver = remember {
        runCatching {
            ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: ""
        }.getOrDefault("")
    }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 14.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        item { AboutLine("版本", ver.ifBlank { "读取失败" }) }
        item { AboutLine("后端", "编队控制面 + 本地 llama.cpp + 云端") }
        item { AboutLine("资源", "本机 SQLite + 自主联网抓取，可与对等节点共享") }
        item { AboutLine("抄自", "Operit（Agent 工作台）· pocketpal-ai（本地推理）") }
    }
}

@Composable
private fun AboutLine(k: String, v: String) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 13.dp, vertical = 11.dp),
    ) {
        Text(k, fontSize = 12.5.sp, modifier = Modifier.width(64.dp),
            color = MaterialTheme.colorScheme.onSurface)
        Text(v, fontSize = 12.5.sp, modifier = Modifier.weight(1f),
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
