package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
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
import androidx.compose.material3.TextButton
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
import org.json.JSONObject

/**
 * 全部能力目录：把这台手机真的挂着的工具按域摊开给人看，能搜、能看参数、能复制名字。
 *
 * 对照 DSH Go 的 ui/plugins + ui/features：能力不是只有模型能用，人也得看得到
 * 「这台机器到底会什么、哪些会改状态」。天网原来工具箱只有常用动作，
 * 64 个 `self_*` 工具全藏在 MCP 的 tools/list 里，装了什么全靠读代码。
 *
 * 纪律：目录只读 [PhoneMcp.toolList]，不另造名单；标了 sideEffect 的必须显出来，
 * 免得「只读」这两个字变成文案而不是事实。
 */
object CapabilityCatalog {
    data class Domain(val name: String, val tools: List<McpTool>)

    /** 顺序即页面顺序。未归到任何前缀的进「其它」。 */
    private val RULES = listOf(
        Triple("self_ui_", "界面操作", "读屏 / 点击 / 输入 / 截图"),
        Triple("self_app_", "应用与系统", "启动 / 列表 / 打开链接"),
        Triple("self_notif_", "通知", "读最近通知"),
        Triple("self_clipboard_", "剪贴板", "读写系统剪贴板"),
        Triple("self_battery_", "电量与电源", "电量 / 充电 / 省电"),
        Triple("self_audio_", "音量", "媒体音量读写"),
        Triple("self_net_", "网络状态", "网络类型 / 计费"),
        Triple("self_screen_", "屏幕", "亮灭 / 锁屏"),
        Triple("self_device_", "设备信息", "型号 / 版本 / ABI"),
        Triple("self_diag", "自检", "全通道自检"),
        Triple("self_audit", "审计", "最近动作"),
        Triple("self_perm_", "权限矩阵", "每条能力走哪条通道"),
        Triple("self_shizuku_", "Shizuku", "shell / 授权"),
        Triple("self_shell_", "Shell", "uid 2000 命令"),
        Triple("self_owner_", "设备所有者", "静默授权 / 系统限制 / 重启"),
        Triple("self_lan_", "局域网直连", "手机之间互调 / 测速"),
        Triple("self_peer_", "集群协作", "名册 / 探测 / 派单 / 并行算力"),
        Triple("self_mcp_", "外部 MCP", "接入别处的工具"),
        Triple("self_dns_", "域名解析", "DNS 查询"),
        Triple("self_ddns_", "动态域名", "IPv6 换址自动上报"),
        Triple("self_model_", "本地模型", "本机推理"),
        Triple("self_api_", "外部算力", "电脑 / API 车道"),
        Triple("self_agent_", "目标委派", "整单交给 agent"),
        Triple("self_status", "状态", "通道状态"),
    )

    fun domains(tools: List<McpTool>): List<Domain> {
        val grouped = LinkedHashMap<String, MutableList<McpTool>>()
        val order = ArrayList<String>()
        for ((prefix, name, _) in RULES) {
            grouped[name] = ArrayList()
            order.add(name)
        }
        for (t in tools) {
            val hit = RULES.firstOrNull { (prefix, _, _) -> t.name.startsWith(prefix) }
            val key = hit?.second ?: "其它"
            if (!grouped.containsKey(key)) {
                grouped[key] = ArrayList()
                order.add(key)
            }
            grouped[key]!!.add(t)
        }
        return order.mapNotNull { k ->
            val v = grouped[k].orEmpty()
            if (v.isEmpty()) null else Domain(k, v.sortedBy { it.name })
        }
    }

    fun search(tools: List<McpTool>, q: String): List<McpTool> {
        val s = q.trim().lowercase()
        if (s.isEmpty()) return tools
        return tools.filter {
            it.name.contains(s, true) || it.description.contains(s, true)
        }
    }

    /** 把 inputSchema 里的参数名与说明读出来给人看，不执行任何工具。 */
    fun params(t: McpTool): List<Pair<String, String>> {
        val p = t.inputSchema.optJSONObject("properties") ?: return emptyList()
        val out = ArrayList<Pair<String, String>>()
        val keys = p.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            val o = p.optJSONObject(k) ?: JSONObject()
            out.add(k to (o.optString("description").ifBlank { o.optString("type", "任意") }))
        }
        return out
    }

    fun copyName(ctx: Context, name: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("工具名", name))
    }
}

@Composable
fun CapabilityCatalogScreen(initialQuery: String = "") {
    val ctx = LocalContext.current
    val all = remember { runCatching { PhoneMcp.toolList() }.getOrDefault(emptyList()) }
    var q by remember { mutableStateOf(initialQuery) }
    var opened by remember { mutableStateOf("") }
    val matched = CapabilityCatalog.search(all, q)
    val groups = CapabilityCatalog.domains(matched)
    val sideCount = all.count { it.sideEffect }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("全部能力", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text("本机挂了 ${all.size} 个工具，其中 $sideCount 个会改状态（默认被闸门挡着）。名单直接读 tools/list，不另写一份。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        BasicTextField(
            value = q,
            onValueChange = { q = it },
            singleLine = true,
            textStyle = TextStyle(fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
            modifier = Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(9.dp))
                .background(MaterialTheme.colorScheme.surface)
                .padding(horizontal = 12.dp, vertical = 10.dp),
            decorationBox = { inner ->
                if (q.isEmpty()) Text("搜工具名或说明，例如 截图 / lan / mcp",
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                inner()
            },
        )
        Spacer(Modifier.height(10.dp))
        Text("命中 ${matched.size} 个 · ${groups.size} 个域", fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(6.dp))
        LazyColumn(verticalArrangement = Arrangement.spacedBy(7.dp)) {
            groups.forEach { g ->
                item(key = "h-${g.name}") {
                    Text("${g.name} · ${g.tools.size}", fontSize = 12.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(top = 6.dp, start = 3.dp))
                }
                items(g.tools, key = { it.name }) { t ->
                    ToolRow(t, opened == t.name, onToggle = {
                        opened = if (opened == t.name) "" else t.name
                    }, onCopy = { CapabilityCatalog.copyName(ctx, t.name) })
                }
            }
        }
    }
}

@Composable
private fun ToolRow(
    t: McpTool,
    open: Boolean,
    onToggle: () -> Unit,
    onCopy: () -> Unit,
) {
    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onToggle)
            .padding(11.dp),
    ) {
        Row {
            Text(t.name, fontSize = 12.sp, fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.weight(1f))
            if (t.sideEffect) {
                Text("会改状态", fontSize = 10.sp, color = MaterialTheme.colorScheme.error)
            }
        }
        Spacer(Modifier.height(3.dp))
        Text(t.description, fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (open) {
            Spacer(Modifier.height(7.dp))
            val ps = CapabilityCatalog.params(t)
            if (ps.isEmpty()) {
                Text("无参数", fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else {
                for ((k, d) in ps) {
                    Text("· $k — $d", fontSize = 10.5.sp, lineHeight = 15.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Row(modifier = Modifier.fillMaxWidth()) {
                TextButton(onClick = onCopy) { Text("复制工具名") }
            }
        }
    }
}
