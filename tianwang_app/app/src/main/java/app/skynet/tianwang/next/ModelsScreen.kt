package app.skynet.tianwang.next

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
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
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.Download
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 模型页 —— 这是「三个融合」最该被看见的那一页。
 *
 * 🔴 上游两家的病根就在这一页：
 *   pocketpal 这一页只有 GGUF 文件列表，云端是**不存在**的分类；
 *   Operit 这一页只有 API 端点，本地模型是塞在"工具"里的旁枝。
 *   所以在这里，本地和云端**共用一张列表、同一套参数面板、同一个"设为当前"**，
 *   区别只体现在左上角那个图标和一句副标题上。
 *   用户不需要先决定"我要用哪种范式"，他只需要挑一个模型。
 */
@Composable
fun ModelsScreen(
    models: List<ModelProfile>,
    currentId: String,
    onPick: (ModelProfile) -> Unit,
) {
    var gateways by remember { mutableStateOf<List<Gateway>>(emptyList()) }
    var gwErr by remember { mutableStateOf("") }
    LaunchedEffect(Unit) {
        if (Net.hasConfig()) {
            runCatching { Net.getProviders() }
                .onSuccess { gateways = it }
                .onFailure { gwErr = it.message ?: "拉取失败" }
        }
    }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        item("local-head") {
            SectionHead(
                icon = { Icon(Icons.Filled.Memory, null, Modifier.size(14.dp),
                    tint = StatusColors.ok) },
                title = "本地引擎",
                sub = "llama.cpp / GGUF · 断网也能跑 · 数据不出手机",
            )
        }
        items(models.filter { it.kind == BackendKind.LOCAL }, key = { it.id }) {
            ModelRow(it, it.id == currentId) { onPick(it) }
        }

        item("cloud-head") {
            Spacer(Modifier.height(8.dp))
            SectionHead(
                icon = { Icon(Icons.Filled.Cloud, null, Modifier.size(14.dp),
                    tint = MaterialTheme.colorScheme.primary) },
                title = "云端引擎",
                sub = "OpenAI 兼容端点 · 长上下文 · 需要联网",
            )
        }
        // 🔴 P1 判据：这里要能看见至少 2 个网关的真实状态（活/死、延迟、模型数）。
        // 数据来自云侧 /api/providers，不是写死的 demo。
        item("gateways") {
            if (gwErr.isNotEmpty()) {
                Text(gwErr, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
            } else if (gateways.isEmpty()) {
                Text("（未检测到网关 —— 云侧未启动，或本机还没在「设置」连上）",
                    fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                // 🔴 Compose 铁律：forEach 的 lambda 不是 @Composable 上下文，
                // 不能在里面直接调自由组合函数 GatewayRow。用 for 循环（纯控制流），
                // 循环体落在 Column 的可组合作用域里，调用才合法。
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    for (gw in gateways) {
                        GatewayRow(gw)
                    }
                }
            }
        }
        items(models.filter { it.kind == BackendKind.CLOUD }, key = { it.id }) {
            ModelRow(it, it.id == currentId) { onPick(it) }
        }

        item("tail") {
            Spacer(Modifier.height(10.dp))
            Text(
                "两条腿都要。日常与敏感内容走本地，重活走云端——" +
                    "这不是二选一的取舍，是同一台机器上两种预算。",
                fontSize = 11.5.sp,
                lineHeight = 18.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun SectionHead(icon: @Composable () -> Unit, title: String, sub: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        icon()
        Spacer(Modifier.width(7.dp))
        Text(title, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Spacer(Modifier.width(8.dp))
        Text(sub, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun ModelRow(m: ModelProfile, active: Boolean, onClick: () -> Unit) {
    val local = m.kind == BackendKind.LOCAL
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(
                if (active) 1.5.dp else 1.dp,
                if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outline,
                RoundedCornerShape(12.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 11.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(7.dp).clip(CircleShape).background(
                    when {
                        !local -> MaterialTheme.colorScheme.primary
                        m.down -> StatusColors.ok
                        else -> StatusColors.idle
                    }
                )
            )
            Spacer(Modifier.width(9.dp))
            Text(
                m.label, fontSize = 14.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f),
            )
            if (active) {
                Text("当前", fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.width(8.dp))
            }
            Text(
                if (local) "LOCAL" else "CLOUD",
                fontSize = 9.5.sp,
                fontFamily = FontFamily.Monospace,
                color = if (local) StatusColors.ok else MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .clip(RoundedCornerShape(4.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(horizontal = 5.dp, vertical = 2.dp),
            )
        }

        if (m.note.isNotBlank()) {
            Text(
                m.note, fontSize = 11.5.sp, lineHeight = 17.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 16.dp, top = 5.dp),
            )
        }

        if (local && m.sizeMb > 0) {
            Spacer(Modifier.height(9.dp))
            if (m.down) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "${m.sizeMb} MB · ctx ${m.ctx}",
                        fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.weight(1f))
                    Text("已就绪", fontSize = 10.5.sp, color = StatusColors.ok)
                }
            } else {
                // 下载进度条：上游 pocketpal 这块做得比我们细（分片/断点续传/校验），
                // 这里先把视觉位占住，别让"下载中"只表现为一个转圈
                LinearProgressIndicator(
                    progress = { 0.62f },
                    modifier = Modifier.fillMaxWidth().height(3.dp)
                        .clip(RoundedCornerShape(2.dp)),
                    color = MaterialTheme.colorScheme.primary,
                    trackColor = MaterialTheme.colorScheme.surfaceVariant,
                )
                Spacer(Modifier.height(6.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        "下载中 62% · ${m.sizeMb} MB",
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.weight(1f))
                    Text("暂停", fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
                }
            }
        }

        if (!local && m.target.isNotBlank()) {
            Spacer(Modifier.height(7.dp))
            Text(
                m.target,
                fontSize = 10.sp, fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
            )
        }
    }
}

/** 一个网关的真实状态卡（来自云侧 /api/providers）。活/死用色点 + 文案双重表达。 */
@Composable
private fun GatewayRow(gw: Gateway) {
    val alive = gw.status == "活"
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(1.dp, MaterialTheme.colorScheme.outline, RoundedCornerShape(10.dp))
            .padding(horizontal = 12.dp, vertical = 9.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(7.dp).clip(CircleShape)
            .background(if (alive) StatusColors.ok else StatusColors.fail))
        Spacer(Modifier.width(9.dp))
        Column(Modifier.weight(1f)) {
            Text(
                gw.id.ifBlank { gw.endpoint }, fontSize = 13.sp,
                fontWeight = FontWeight.Medium, color = MaterialTheme.colorScheme.onSurface,
            )
            val sub = buildList {
                if (gw.endpoint.isNotBlank()) add(gw.endpoint)
                if (gw.modelCount > 0) add("${gw.modelCount} 模型")
                if (gw.latencyMs > 0) add("${gw.latencyMs}ms")
            }.joinToString(" · ")
            if (sub.isNotBlank()) {
                Text(
                    sub, fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Text(
            gw.status, fontSize = 11.sp,
            color = if (alive) StatusColors.ok else StatusColors.fail,
        )
    }
}
