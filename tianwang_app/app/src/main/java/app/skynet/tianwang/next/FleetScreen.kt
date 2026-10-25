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
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Memory
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * 编队页。**这一整页是两个上游都没有的**——它们是单机 App，我们是编队里的一个节点。
 *
 * 所以这里不是「设备管理」那种静态列表，要回答三个问题：
 *   我这台是什么机器／我在编队里的位置／谁现在掉了。
 *
 * 🔴 2026-10-25 去假数据：旧版这一页吃的是写死的四条节点（1号机/2号机/旧笔记本），
 * 换台电脑就全错。现在本机档案来自 [Dev] 的实测探测，节点列表来自云侧 /api/nodes；
 * 拿不到就显示「拿不到」和原因 —— 空着也比骗人强。
 */
@Composable
fun FleetScreen(modifier: Modifier = Modifier) {
    var dev by remember { mutableStateOf<DeviceProfile?>(null) }
    var plan by remember { mutableStateOf<InferencePlan?>(null) }
    var snap by remember { mutableStateOf<FleetSnapshot?>(null) }
    var loading by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        // 设备探测会碰 native（枚举 GPU 设备），别放在组合期同步跑
        runCatching {
            val p = Dev.profile(App.ctx)
            dev = p
            val firstLocal = ModelCatalog.local().firstOrNull()
            if (firstLocal != null) plan = Dev.plan(App.ctx, firstLocal.fileMb, Prefs.mode, firstLocal)
        }
        if (Net.hasConfig()) {
            runCatching { Net.getNodes() }.onSuccess { snap = it }
        }
        loading = false
    }

    LazyColumn(
        modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        item("self") {
            val d = dev
            if (d == null) {
                CardLine("正在探测本机…")
            } else {
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(12.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .padding(14.dp),
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(Icons.Filled.PhoneAndroid, null,
                            tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(19.dp))
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text(d.model, fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                                color = MaterialTheme.colorScheme.onSurface)
                            Text(d.soc, fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Box(Modifier.size(7.dp).clip(CircleShape).background(StatusColors.ok))
                    }
                    Spacer(Modifier.height(10.dp))
                    KeyVal("核心", "${d.totalCores} 逻辑 / ${d.perfCores} 性能核" +
                        if (d.bigFreqMhz > 0) "（最高 ${d.bigFreqMhz} MHz）" else "")
                    KeyVal("内存", String.format("%.1f GB / 可用 %d MB", d.ramGb, d.availRamMb))
                    KeyVal("GPU", if (d.hasGpu) "${d.gpuDevices} 个 · ${d.gpuName}" else "无 GPU 后端（走 CPU）")
                    KeyVal("ABI", d.abi)
                }
            }
        }

        item("plan") {
            val p = plan
            if (p != null) {
                Spacer(Modifier.height(4.dp))
                GroupTitle("本机推理参数（按实测推出来的，不是写死的）")
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(MaterialTheme.colorScheme.surface)
                        .padding(12.dp),
                ) {
                    KeyVal("模式", p.mode.label)
                    KeyVal("线程", "${p.threads}")
                    KeyVal("上下文", "${p.ctx} token")
                    KeyVal("GPU 层", if (p.gpuPercent == 0) "0（纯 CPU）" else "${p.gpuPercent}%")
                    Spacer(Modifier.height(6.dp))
                    Text(p.reason, fontSize = 10.5.sp, lineHeight = 16.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }

        item("fleet-head") {
            Spacer(Modifier.height(4.dp))
            GroupTitle("编队")
        }

        item("fleet-summary") {
            val s = snap
            Row(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(14.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text(
                        when {
                            !Net.hasConfig() -> "未连接云侧"
                            s == null -> if (loading) "正在连接…" else "没有拿到编队数据"
                            else -> "${s.onlineCount} / ${s.totalCount} 在线"
                        },
                        fontSize = 17.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                    Text(
                        when {
                            !Net.hasConfig() -> "去「设置」填地址和 Token"
                            s == null -> "数据未到，不显示占位节点"
                            s.cpAvailable -> "控制面可用 · 注册表已连"
                            else -> "控制面不可用${if (s.cpErr.isBlank()) "" else "：" + s.cpErr}"
                        },
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(6.dp).clip(CircleShape).background(
                        if (s != null && s.cpAvailable) StatusColors.ok else StatusColors.idle))
                    Spacer(Modifier.width(6.dp))
                    Text("控制面", fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }

        val s = snap
        if (s != null) {
            val all = buildList {
                s.self?.let { add(it) }
                addAll(s.nodes)
            }
            if (all.isEmpty()) {
                item("empty") {
                    Text("注册表里没有别的节点。这不是错误 —— 是真的没有。",
                        fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            items(all, key = { it.name }) { n -> NodeRow(n, isSelf = n.name == s.self?.name) }
        }
    }
}

@Composable
private fun NodeRow(n: NodeInfo, isSelf: Boolean) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 13.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            if (isSelf) Icons.Filled.PhoneAndroid else Icons.Filled.Computer,
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
        Box(Modifier.size(7.dp).clip(CircleShape)
            .background(if (n.online) StatusColors.ok else StatusColors.idle))
    }
}

/** 设置页。只放「会改变行为」的开关，纯展示信息挪到关于里。 */
@Composable
fun SettingsScreen() {
    var mode by remember { mutableStateOf(Prefs.mode) }
    var noThink by remember { mutableStateOf(Prefs.noThink) }
    var maxTok by remember { mutableStateOf(Prefs.maxTokens.toFloat()) }
    var lang by remember { mutableStateOf(Prefs.replyLang) }
    var dev by remember { mutableStateOf<DeviceProfile?>(null) }
    var ver by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        runCatching { Dev.profile(App.ctx) }.onSuccess { dev = it }
        ver = runCatching {
            App.ctx.packageManager.getPackageInfo(App.ctx.packageName, 0).versionName ?: ""
        }.getOrDefault("")
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(2.dp),
    ) {
        item {
            GroupTitle("回复语言")
            Text(
                "设成默认值，每轮都生效 —— 不用再在提问里手写「用中文回答」。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 13.dp, bottom = 6.dp),
            )
            for (l in ReplyLang.entries) {
                ChoiceRow(l.label, l == ReplyLang.ZH_CN, l == lang) {
                    lang = l; Prefs.replyLang = l
                }
            }
        }
        item {
            Spacer(Modifier.height(12.dp))
            GroupTitle("推理模式")
            val d = dev
            if (d != null && !d.hasGpu) {
                Text(
                    "这台/这一版没有 GPU 后端，选 GPU 或混合也会自动回落 CPU。" +
                        "装上带 Vulkan 后端的 .so 之后这里才会真生效。",
                    fontSize = 10.5.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 13.dp, bottom = 6.dp),
                )
            }
            for (m in InferenceMode.entries) {
                ModeRow(m, m == mode) { mode = m; Prefs.mode = m }
            }
        }
        item {
            Spacer(Modifier.height(12.dp))
            GroupTitle("生成")
            ToggleRow(
                "跳过思考链",
                "Qwen3 这类混合思考模型会先吐一大段内心戏；开了直接给答案。只对支持该开关的模型生效",
                noThink,
            ) { noThink = it; Prefs.noThink = it }

            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(horizontal = 13.dp, vertical = 11.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("单轮最大 token", fontSize = 13.5.sp,
                            color = MaterialTheme.colorScheme.onSurface)
                        Text("手机上生成速度有限，给太大等于让它一直说下去", fontSize = 10.5.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text("${maxTok.toInt()}", fontSize = 13.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.primary)
                }
                Slider(
                    value = maxTok,
                    onValueChange = { maxTok = it },
                    onValueChangeFinished = { Prefs.maxTokens = maxTok.toInt() },
                    valueRange = 64f..2048f,
                    steps = 30,
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
            InfoRow("版本", ver.ifBlank { "读取失败" })
            InfoRow("后端", "编队控制面 + 本地 llama.cpp + 云端")
            InfoRow("抄自", "Operit（Agent 工作台）· pocketpal-ai（本地推理）")
        }
    }
}

@Composable
private fun ModeRow(m: InferenceMode, active: Boolean, onClick: () -> Unit) {
    val accent = MaterialTheme.colorScheme.primary
    Row(
        Modifier.fillMaxWidth()
            .padding(bottom = 6.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(
                if (active) 1.5.dp else 1.dp,
                if (active) accent else MaterialTheme.colorScheme.outline,
                RoundedCornerShape(10.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 13.dp, vertical = 11.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f)) {
            Text(m.label, fontSize = 13.5.sp, color = MaterialTheme.colorScheme.onSurface)
            Text(m.desc, fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 2.dp))
        }
        if (active) {
            Spacer(Modifier.width(8.dp))
            Text("当前", fontSize = 10.5.sp, color = accent)
        }
    }
}

/**
 * 通用单选项。语言这类「选一个」的设置都走它 —— 与 ModeRow 长一样，
 * 但内容不写死，换选项不用再复制一坨布局代码。
 * [isDefault] 标出哪一项是出厂默认（主人要能一眼看出「原来默认是什么」）。
 */
@Composable
private fun ChoiceRow(label: String, isDefault: Boolean, active: Boolean, onClick: () -> Unit) {
    val accent = MaterialTheme.colorScheme.primary
    Row(
        Modifier.fillMaxWidth()
            .padding(bottom = 6.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(
                if (active) 1.5.dp else 1.dp,
                if (active) accent else MaterialTheme.colorScheme.outline,
                RoundedCornerShape(10.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 13.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, fontSize = 13.5.sp, color = MaterialTheme.colorScheme.onSurface,
            modifier = Modifier.weight(1f))
        if (isDefault) {
            Text("默认", fontSize = 9.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier
                    .clip(RoundedCornerShape(4.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant)
                    .padding(horizontal = 5.dp, vertical = 2.dp))
            Spacer(Modifier.width(8.dp))
        }
        if (active) Text("当前", fontSize = 10.5.sp, color = accent)
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
private fun CardLine(t: String) {
    Text(t, fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(8.dp))
}

@Composable
private fun KeyVal(k: String, v: String) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(k, fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(64.dp))
        Text(v, fontSize = 11.5.sp, fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurface, modifier = Modifier.weight(1f))
    }
}

@Composable
private fun ToggleRow(title: String, sub: String, on: Boolean, onChange: (Boolean) -> Unit) {
    Row(
        Modifier.fillMaxWidth()
            .padding(bottom = 2.dp)
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
