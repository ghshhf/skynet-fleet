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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 编队页。**这一整页是两个上游都没有的**——它们是单机 App，我们是编队里的一个节点。
 *
 * 所以这里不是"设备管理"那种静态列表，要回答的是三个问题：
 *   我这台在编队里的位置（谁在管我）／我能借到谁的算力／哪些机器现在掉了。
 */
@Composable
fun FleetScreen(nodes: List<NodeInfo>) {
    val online = nodes.count { it.online }
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        item("summary") {
            Row(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text("$online / ${nodes.size} 在线", fontSize = 17.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface)
                    Text("本机是节点，不是孤岛", fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(6.dp).clip(CircleShape)
                        .background(if (online > 0) StatusColors.ok else StatusColors.fail))
                    Spacer(Modifier.width(6.dp))
                    Text("控制面", fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }

        items(nodes, key = { it.name }) { n ->
            Row(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .clickable { }
                    .padding(horizontal = 13.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(
                    if (n.name.contains("手机")) Icons.Filled.PhoneAndroid
                    else Icons.Filled.Computer,
                    null,
                    tint = if (n.online) MaterialTheme.colorScheme.primary else StatusColors.idle,
                    modifier = Modifier.size(19.dp),
                )
                Spacer(Modifier.width(11.dp))
                Column(Modifier.weight(1f)) {
                    Text(n.name, fontSize = 13.5.sp, fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onSurface)
                    Text(
                        n.host + (if (n.caps > 0) "  ·  ${n.caps} 项能力" else ""),
                        fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    if (n.note.isNotBlank()) {
                        Text(n.note, fontSize = 10.5.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 2.dp))
                    }
                }
                Box(
                    Modifier.size(7.dp).clip(CircleShape)
                        .background(if (n.online) StatusColors.ok else StatusColors.idle)
                )
            }
        }
    }
}

/** 设置页。只放"会改变行为"的开关，纯展示信息挪到关于里。 */
@Composable
fun SettingsScreen() {
    var allowExec by remember { mutableStateOf(true) }
    var askDanger by remember { mutableStateOf(true) }
    var preferLocal by remember { mutableStateOf(true) }
    var cloudFallback by remember { mutableStateOf(false) }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        item {
            GroupTitle("执行")
            ToggleRow("允许本机执行 shell", "关掉之后只剩对话，任何动手能力都下线",
                allowExec) { allowExec = it }
            ToggleRow("危险动作先问我", "删除、覆盖、装包这类不可逆操作要过一次确认",
                askDanger) { askDanger = it }
        }
        item {
            Spacer(Modifier.height(12.dp))
            GroupTitle("模型路由")
            // 这条是两个上游都没有的：它们只有一种后端，没有"路由"这个概念
            ToggleRow("优先本地", "能本地解决就不外发，隐私优先、也省额度",
                preferLocal) { preferLocal = it }
            ToggleRow("本地失败时自动转云端", "本地 OOM / 超时就换云端重跑同一轮",
                cloudFallback) { cloudFallback = it }
            if (cloudFallback) {
                Text(
                    "⚠ 开启后，本地失败的那段上下文会发到云端。涉密内容别开这个。",
                    fontSize = 11.sp, lineHeight = 17.sp,
                    color = StatusColors.running,
                    modifier = Modifier.padding(start = 13.dp, top = 4.dp),
                )
            }
        }
        item {
            Spacer(Modifier.height(12.dp))
            AgentConfigScreen()
        }
        item {
            Spacer(Modifier.height(12.dp))
            GroupTitle("关于")
            InfoRow("版本", "0.1-proto（Compose 原型）")
            InfoRow("后端", "天网编队 · 控制面 + 本地推理 + 云端")
            InfoRow("抄自", "Operit（Agent 工作台）· pocketpal-ai（本地推理）")
        }
    }
}

@Composable
private fun GroupTitle(t: String) {
    Text(
        t, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 4.dp, top = 4.dp, bottom = 6.dp),
    )
}

@Composable
private fun ToggleRow(title: String, sub: String, on: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable { onChange(!on) }
            .padding(horizontal = 13.dp, vertical = 11.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, fontSize = 13.5.sp, color = MaterialTheme.colorScheme.onSurface)
            if (sub.isNotBlank()) {
                Text(sub, fontSize = 10.5.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 2.dp))
            }
        }
        Spacer(Modifier.width(10.dp))
        Switch(checked = on, onCheckedChange = onChange)
    }
}

@Composable
private fun InfoRow(k: String, v: String) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 13.dp, vertical = 11.dp),
    ) {
        Text(k, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.width(64.dp))
        Text(v, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(1f))
    }
}
