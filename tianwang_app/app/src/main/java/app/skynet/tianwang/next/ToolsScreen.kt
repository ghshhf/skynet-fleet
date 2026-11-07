package app.skynet.tianwang.next

import androidx.compose.foundation.background
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 工具箱入口：电脑侧 / 手机本机两条能力面。
 *
 * 分开但放在同一页，是因为对用户来说都是「点一下真跑」；
 * 区别只在执行端 —— 电脑侧要云连接，手机本机要无障碍通道。
 */
@Composable
fun ToolsScreen() {
    var side by remember { mutableStateOf(0) }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            SideChip("电脑侧", side == 0) { side = 0 }
            SideChip("手机本机", side == 1) { side = 1 }
        }
        HorizontalDivider(color = MaterialTheme.colorScheme.outline)
        if (side == 0) RemoteToolsPane() else PhoneSelfPane()
    }
}

@Composable
private fun SideChip(label: String, selected: Boolean, onClick: () -> Unit) {
    Box(
        Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(
                if (selected) MaterialTheme.colorScheme.primary.copy(alpha = 0.16f)
                else MaterialTheme.colorScheme.surfaceVariant
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 7.dp)
    ) {
        Text(
            label,
            fontSize = 12.sp,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
            color = if (selected) MaterialTheme.colorScheme.primary
            else MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** 电脑侧真实能力按类别列出来，点一下就在电脑上真跑。 */
@Composable
private fun RemoteToolsPane() {
    var groups by remember { mutableStateOf<Map<String, List<Capability>>>(emptyMap()) }
    var loading by remember { mutableStateOf(true) }
    var err by remember { mutableStateOf("") }
    var sel by remember { mutableStateOf<Capability?>(null) }
    var argText by remember { mutableStateOf("{}") }
    var result by remember { mutableStateOf<ToolResult?>(null) }
    val scope = rememberCoroutineScope()

    // 首次进入即拉取（即使已配置也拉；未配置给出明确提示）。
    if (loading && groups.isEmpty() && err.isEmpty()) {
        androidx.compose.runtime.LaunchedEffect(Unit) {
            if (!Net.hasConfig()) {
                err = "未连接云侧：去「设置」填地址和 Token"
                loading = false
                return@LaunchedEffect
            }
            runCatching { Net.getCaps() }
                .onSuccess { groups = it; loading = false }
                .onFailure { err = it.message ?: "拉取失败"; loading = false }
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text("工具箱", style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Text("电脑侧真实能力（按类别）—— 点一下就在电脑上真跑，不是演示",
            fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(8.dp))
        if (loading) Text("加载中…", fontSize = 12.sp)
        if (err.isNotEmpty()) Text(err, color = MaterialTheme.colorScheme.error, fontSize = 12.sp)

        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            groups.forEach { (domain, caps) ->
                item {
                    Spacer(Modifier.height(4.dp))
                    Text(domain.uppercase(), fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.primary)
                }
                items(caps) { cap ->
                    Card(
                        Modifier.fillMaxWidth().clickable {
                            sel = cap; argText = "{}"; result = null
                        },
                        colors = CardDefaults.cardColors(
                            containerColor = MaterialTheme.colorScheme.surfaceVariant),
                        shape = RoundedCornerShape(10.dp),
                    ) {
                        Row(Modifier.fillMaxWidth().padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically) {
                            Column(Modifier.weight(1f)) {
                                Text(cap.title.ifEmpty { cap.name },
                                    fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface)
                                Text(cap.name, fontSize = 10.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                                if (cap.destructive)
                                    Text("⚠ 不可逆操作", fontSize = 10.sp,
                                        color = MaterialTheme.colorScheme.error)
                            }
                            Spacer(Modifier.width(8.dp))
                        }
                    }
                }
            }
        }
    }

    val cap = sel
    if (cap != null) {
        AlertDialog(
            onDismissRequest = { sel = null },
            title = { Text(cap.title.ifEmpty { cap.name }) },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    Text(cap.note.ifEmpty { cap.name }, fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (cap.params.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text("参数（JSON，例：{\"x\":1}）", fontSize = 11.sp)
                        Spacer(Modifier.height(4.dp))
                        OutlinedTextField(argText, { argText = it },
                            modifier = Modifier.fillMaxWidth(), singleLine = false)
                    }
                    result?.let { r ->
                        Spacer(Modifier.height(10.dp))
                        Text(
                            if (r.ok) r.text else "失败：${r.err}",
                            fontSize = 12.sp, maxLines = 12,
                            color = if (r.ok) MaterialTheme.colorScheme.onSurface
                            else MaterialTheme.colorScheme.error)
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val args = runCatching { JSONObject(argText).toMap() }.getOrNull() ?: emptyMap()
                    scope.launch {
                        runCatching { Net.execTool(cap.name, args) }.onSuccess { result = it }
                    }
                }) { Text("执行") }
            },
            dismissButton = { TextButton({ sel = null }) { Text("关闭") } },
        )
    }
}

fun JSONObject.toMap(): Map<String, Any> {
    val m = mutableMapOf<String, Any>()
    val it = keys()
    while (it.hasNext()) { val k = it.next(); m[k] = opt(k) }
    return m
}
