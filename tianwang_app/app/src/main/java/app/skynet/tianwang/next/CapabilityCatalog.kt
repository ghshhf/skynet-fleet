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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * 全部能力目录：把这台手机**真的挂着的全部东西**摊开成一份名单。
 *
 * 之前这块只看 [PhoneMcp.toolList]（50 多个 `self_*` 工具），于是同一台机器上
 * 存在三份互不相认的名单：
 *   1. `PhoneMcp.toolList()` —— AI 能调的工具
 *   2. `Overlay` 枚举 —— 119 个给人点的界面屏
 *   3. `Chat.Capability` —— 云侧 `/api/caps` 下发的工具卡
 * 谁也不知道全貌，加一个功能要改三处。
 *
 * 这一版把 1 和 2 合成一份 [CapEntry] 目录：
 *  - 工具行：可展开填参数、**直接执行**（走 [PhoneMcp.executeTool]，跟 AI 同一条路）；
 *  - 界面行：点一下跳到原来那个屏，功能一个没删。
 *
 * 3 那份是云侧下发的显示用数据，格式不同也不执行，暂不并进来 —— 并了反而更乱。
 *
 * 纪律：目录不另造名单，工具一律现取 `toolList()`；标了 sideEffect 的必须显出来，
 * 免得「只读」变成文案而不是事实。
 */
object CapabilityCatalog {
    data class Domain(val name: String, val tools: List<McpTool>)

    /** 界面入口统一归到这个域，排在工具后面，一眼能分清「能跑」和「只能进」。 */
    const val UI_DOMAIN = "界面入口"

    /** 顺序即页面顺序。未归到任何前缀的进「其它」。 */
    private val RULES = listOf(
        Triple("self_tty_", "会话层", "长驻 shell / 流式输出 / 可中断"),
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

    /**
     * 统一目录的一行。
     *
     * [toolName] 非空 = 可以直接执行；[overlay] 非空 = 点了跳界面。两者至少有一个。
     */
    data class CapEntry(
        val key: String,
        val title: String,
        val desc: String,
        val domain: String,
        val sideEffect: Boolean = false,
        val toolName: String? = null,
        val overlay: String? = null,
        val schema: JSONObject? = null,
        /** 谁在用。工具一律归 AI —— 人不必手点；界面入口按 [Overlay.use] 如实标。 */
        val use: OverlayUse = OverlayUse.AI,
    ) {
        val runnable: Boolean get() = toolName != null
    }

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

    /**
     * 合并两个来源：本机工具（可执行）+ 界面入口（点了跳屏）。
     *
     * [ui] 每项是 (Overlay 枚举名, 标题, 一句话说明)。
     */
    fun entries(
        tools: List<McpTool>,
        ui: List<Triple<String, String, String>>,
    ): List<CapEntry> {
        val out = ArrayList<CapEntry>(tools.size + ui.size)
        for (t in tools) {
            val hit = RULES.firstOrNull { (p, _, _) -> t.name.startsWith(p) }
            out.add(
                CapEntry(
                    key = t.name,
                    title = t.name,
                    desc = t.description,
                    domain = hit?.second ?: "其它",
                    sideEffect = t.sideEffect,
                    toolName = t.name,
                    schema = t.inputSchema,
                )
            )
        }
        for ((name, title, sub) in ui) {
            out.add(
                CapEntry(
                    key = "ui.$name",
                    title = title,
                    desc = sub,
                    domain = UI_DOMAIN,
                    overlay = name,
                    use = Overlay.values().firstOrNull { it.name == name }?.use ?: OverlayUse.BOTH,
                )
            )
        }
        return out
    }

    /** 按「谁在用」筛。传 null 表示全部。 */
    fun filterUse(list: List<CapEntry>, use: OverlayUse?): List<CapEntry> =
        if (use == null) list else list.filter { it.use == use }

    /** 人用 / AI 用各有多少，用于说明文字，别让人以为 119 个都得自己点。 */
    fun useCounts(list: List<CapEntry>): Map<OverlayUse, Int> =
        list.groupingBy { it.use }.eachCount()

    fun searchEntries(list: List<CapEntry>, q: String): List<CapEntry> {
        val s = q.trim().lowercase()
        if (s.isEmpty()) return list
        return list.filter { e ->
            e.key.lowercase().contains(s) ||
                e.title.lowercase().contains(s) ||
                e.desc.lowercase().contains(s) ||
                e.domain.lowercase().contains(s) ||
                e.overlay?.lowercase()?.contains(s) == true
        }
    }

    /** 分组并按域内顺序展开；「界面入口」固定排最后。 */
    fun groupEntries(list: List<CapEntry>): List<Pair<String, List<CapEntry>>> {
        val grouped = list.groupBy { it.domain }
        val normal = grouped.filterKeys { it != UI_DOMAIN }.toList()
        val ui = grouped[UI_DOMAIN]?.let { listOf(UI_DOMAIN to it) } ?: emptyList()
        return normal + ui
    }

    /** 把 inputSchema 里的参数名与说明读出来给人看。 */
    fun paramsOf(schema: JSONObject?): List<Pair<String, String>> {
        val p = schema?.optJSONObject("properties") ?: return emptyList()
        val out = ArrayList<Pair<String, String>>()
        val keys = p.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            val o = p.optJSONObject(k) ?: JSONObject()
            out.add(k to (o.optString("description").ifBlank { o.optString("type", "任意") }))
        }
        return out
    }

    /** 把 inputSchema 里的参数名与说明读出来给人看，不执行任何工具。 */
    fun params(t: McpTool): List<Pair<String, String>> = paramsOf(t.inputSchema)

    /**
     * 执行一个工具。
     *
     * 走 [PhoneMcp.executeTool] —— 跟 MCP 入网、局域网直连、本地模型工具协议
     * 完全同一个入口。界面不是一个「另一套实现」，只是另一个调用方。
     */
    suspend fun run(toolName: String, args: JSONObject): ToolResult =
        PhoneMcp.executeTool(toolName, args)

    fun copyName(ctx: Context, name: String) {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        cm.setPrimaryClip(ClipData.newPlainText("工具名", name))
    }
}

@Composable
fun CapabilityCatalogScreen(
    initialQuery: String = "",
    onOpen: ((String) -> Unit)? = null,
) {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()
    val tools = remember { runCatching { PhoneMcp.toolList() }.getOrDefault(emptyList()) }
    val uiEntries = remember {
        Overlay.values().map { Triple(it.name, it.title, it.sub) }
    }
    val allEntries = remember(tools, uiEntries) {
        CapabilityCatalog.entries(tools, uiEntries)
    }

    var q by remember { mutableStateOf(initialQuery) }
    var useFilter by remember { mutableStateOf<OverlayUse?>(null) }
    var opened by remember { mutableStateOf("") }
    var result by remember { mutableStateOf<Pair<String, ToolResult>?>(null) }
    var busy by remember { mutableStateOf("") }

    val searched = CapabilityCatalog.searchEntries(allEntries, q)
    val matched = CapabilityCatalog.filterUse(searched, useFilter)
    val groups = CapabilityCatalog.groupEntries(matched)
    val toolCount = allEntries.count { it.runnable }
    val counts = CapabilityCatalog.useCounts(allEntries)
    val humanCount = counts[OverlayUse.HUMAN] ?: 0
    val aiCount = counts[OverlayUse.AI] ?: 0

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        Text("全部能力", fontSize = 18.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.onSurface)
        Text(
            "$toolCount 个工具能直接跑（都归 AI 用，人不必手点）；界面入口里人常用的只有 $humanCount 个，" +
                "另有 $aiCount 个是 AI 用的诊断/采集屏。名单现取，不另写一份。",
            fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
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
                if (q.isEmpty()) Text("搜名字或说明，例如 截图 / lan / 文件",
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                inner()
            },
        )
        Spacer(Modifier.height(8.dp))
        UseFilterRow(active = useFilter, counts = counts) { useFilter = it }
        Spacer(Modifier.height(8.dp))
        Text("命中 ${matched.size} 个 · ${groups.size} 个域", fontSize = 11.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(6.dp))

        LazyColumn(verticalArrangement = Arrangement.spacedBy(7.dp)) {
            groups.forEach { (domain, list) ->
                item(key = "h-$domain") {
                    Text("$domain · ${list.size}", fontSize = 12.sp,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.primary,
                        modifier = Modifier.padding(top = 6.dp, start = 3.dp))
                }
                items(list, key = { it.key }) { e ->
                    CapEntryRow(
                        e = e,
                        open = opened == e.key,
                        busy = busy == e.key,
                        onToggle = { opened = if (opened == e.key) "" else e.key },
                        onCopy = { CapabilityCatalog.copyName(ctx, e.key) },
                        onOpenUi = { e.overlay?.let { ov -> onOpen?.invoke(ov) } },
                        onRun = { args ->
                            val name = e.toolName
                            if (name != null) {
                                busy = e.key
                                scope.launch {
                                    val r = withContext(Dispatchers.IO) {
                                        runCatching { CapabilityCatalog.run(name, args) }
                                            .getOrElse {
                                                ToolResult(
                                                    ok = false,
                                                    err = "调用失败：${it.message ?: it.javaClass.simpleName}",
                                                )
                                            }
                                    }
                                    busy = ""
                                    result = e.title to r
                                }
                            }
                        },
                    )
                }
            }
        }
    }

    result?.let { (title, r) ->
        AlertDialog(
            onDismissRequest = { result = null },
            title = {
                Text(
                    if (r.ok) title else "失败：$title",
                    fontSize = 14.sp,
                    color = if (r.ok) MaterialTheme.colorScheme.onSurface
                    else MaterialTheme.colorScheme.error,
                )
            },
            text = {
                Text(
                    (if (r.ok) r.text else r.err).ifBlank { "(没有返回内容)" },
                    fontSize = 11.5.sp,
                    lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            },
            confirmButton = {
                TextButton(onClick = { result = null }) { Text("知道了", fontSize = 12.sp) }
            },
        )
    }
}

/** 「谁在用」筛选：全部 / 人用 / 人AI / AI 用。默认全部，但把数量摊开，人一眼知道哪些不必自己点。 */
@Composable
private fun UseFilterRow(
    active: OverlayUse?,
    counts: Map<OverlayUse, Int>,
    onPick: (OverlayUse?) -> Unit,
) {
    val all = counts.values.sum()
    val items: List<Pair<OverlayUse?, String>> = listOf(
        null to "全部 $all",
        OverlayUse.HUMAN to "人用 ${counts[OverlayUse.HUMAN] ?: 0}",
        OverlayUse.BOTH to "人/AI ${counts[OverlayUse.BOTH] ?: 0}",
        OverlayUse.AI to "AI 用 ${counts[OverlayUse.AI] ?: 0}",
    )
    Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.fillMaxWidth()) {
        for ((use, label) in items) {
            val on = active == use
            Text(
                label,
                fontSize = 10.5.sp,
                color = if (on) MaterialTheme.colorScheme.surface
                else MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .clip(RoundedCornerShape(20.dp))
                    .background(
                        if (on) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.surfaceVariant
                    )
                    .clickable { onPick(use) }
                    .padding(horizontal = 9.dp, vertical = 5.dp),
            )
        }
    }
}

@Composable
private fun CapEntryRow(
    e: CapabilityCatalog.CapEntry,
    open: Boolean,
    busy: Boolean,
    onToggle: () -> Unit,
    onCopy: () -> Unit,
    onOpenUi: () -> Unit,
    onRun: (JSONObject) -> Unit,
) {
    val ps = remember(e.key) { CapabilityCatalog.paramsOf(e.schema) }
    val inputs = remember(e.key) { mutableStateMapOf<String, String>() }

    Column(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onToggle)
            .padding(11.dp),
    ) {
        Row {
            Text(e.title, fontSize = 12.sp, fontFamily = FontFamily.Monospace,
                fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.weight(1f))
            if (e.sideEffect) {
                Text("会改状态", fontSize = 10.sp, color = MaterialTheme.colorScheme.error)
            } else if (!e.runnable) {
                Text(e.use.label, fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        Spacer(Modifier.height(3.dp))
        Text(e.desc, fontSize = 11.sp, lineHeight = 16.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)

        if (!open) return@Column

        Spacer(Modifier.height(7.dp))

        // 界面入口：不执行，只跳转。
        if (!e.runnable) {
            Text("这是一个界面屏，功能照旧，点下面进去。", fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Row(Modifier.fillMaxWidth()) {
                TextButton(onClick = onOpenUi, enabled = true) {
                    Text(if (e.overlay != null) "打开界面" else "无入口")
                }
                TextButton(onClick = onCopy) { Text("复制名字") }
            }
            return@Column
        }

        if (ps.isEmpty()) {
            Text("无参数", fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            for ((k, d) in ps) {
                Text("· $k — $d", fontSize = 10.5.sp, lineHeight = 15.sp,
                    fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(3.dp))
                BasicTextField(
                    value = inputs[k].orEmpty(),
                    onValueChange = { inputs[k] = it },
                    singleLine = true,
                    textStyle = TextStyle(fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurface),
                    cursorBrush = SolidColor(MaterialTheme.colorScheme.primary),
                    modifier = Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(7.dp))
                        .background(MaterialTheme.colorScheme.surfaceVariant)
                        .padding(horizontal = 9.dp, vertical = 7.dp),
                    decorationBox = { inner ->
                        if (inputs[k].orEmpty().isEmpty()) {
                            Text("填 $k", fontSize = 11.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        inner()
                    },
                )
                Spacer(Modifier.height(5.dp))
            }
        }

        Row(modifier = Modifier.fillMaxWidth()) {
            TextButton(
                onClick = {
                    val args = JSONObject()
                    for ((k, _) in ps) {
                        val v = inputs[k].orEmpty().trim()
                        if (v.isNotEmpty()) args.put(k, v)
                    }
                    onRun(args)
                },
                enabled = !busy,
            ) {
                Text(if (busy) "执行中…" else "执行", fontSize = 12.sp)
            }
            TextButton(onClick = onCopy) { Text("复制名字", fontSize = 12.sp) }
        }
    }
}
