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
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
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
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject

/**
 * 「手机本机」页：无障碍通道的开关状态 + 本机能力清单 + 单点执行。
 *
 * 验收对齐文档第一步：**不连电脑也能打开指定 App、读出界面文字、点中控件**。
 * 所以这一页自己就是验收入口 —— 不需要 adb，也不需要云侧。
 */
@Composable
fun PhoneSelfPane() {
    val ctx = LocalContext.current
    var connected by remember { mutableStateOf(PhoneSelf.connected()) }
    var enabled by remember { mutableStateOf(PhoneSelf.enabled(ctx)) }
    var sel by remember { mutableStateOf<SelfCap?>(null) }
    var argText by remember { mutableStateOf("{}") }
    var result by remember { mutableStateOf<ToolResult?>(null) }
    var busy by remember { mutableStateOf(false) }
    var linkStatus by remember { mutableStateOf(PhoneMcpLink.status) }
    var linkConnected by remember { mutableStateOf(PhoneMcpLink.connected) }
    var linkError by remember { mutableStateOf(PhoneMcpLink.lastError) }
    var showLink by remember { mutableStateOf(false) }
    var notifOn by remember { mutableStateOf(NotificationWatch.enabled(ctx)) }
    val scope = rememberCoroutineScope()

    // 用户去系统设置开完再回来时，Compose 不会自己知道 —— 轮询是最省事的衔接。
    LaunchedEffect(Unit) {
        while (true) {
            connected = PhoneSelf.connected()
            enabled = PhoneSelf.enabled(ctx)
            linkStatus = PhoneMcpLink.status
            linkConnected = PhoneMcpLink.connected
            linkError = PhoneMcpLink.lastError
            notifOn = NotificationWatch.enabled(ctx)
            delay(1200)
        }
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Text("手机本机", style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onSurface)
        Text("无障碍通道 · 读界面 / 点控件 / 输入 / 开 App，全程不出本机",
            fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(8.dp))

        val tone = when {
            connected -> MaterialTheme.colorScheme.primary
            enabled -> MaterialTheme.colorScheme.tertiary
            else -> MaterialTheme.colorScheme.error
        }
        Card(
            Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            shape = RoundedCornerShape(10.dp),
        ) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier.size(8.dp)
                            .background(tone, CircleShape)
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(PhoneSelf.statusLine(ctx),
                        fontSize = 13.sp, fontWeight = FontWeight.SemiBold, color = tone)
                }
                Spacer(Modifier.height(6.dp))
                Text(
                    when {
                        connected -> "通道在线。下面每条都真在这台手机上执行。"
                        enabled -> "系统已授权，但服务还没绑上来。退出设置等 1–2 秒；仍不通就重开 App。"
                        else -> "第一步：点下面的按钮去系统设置；第二步：无障碍 → 天网 Next → 打开开关。"
                    },
                    fontSize = 10.5.sp, lineHeight = 16.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (!connected) {
                    Spacer(Modifier.height(8.dp))
                    Button(onClick = {
                        if (!PhoneSelf.openSettings(ctx)) {
                            android.widget.Toast.makeText(
                                ctx, "打不开无障碍设置页，请手动进系统设置", android.widget.Toast.LENGTH_LONG
                            ).show()
                        }
                    }) { Text(if (enabled) "重新打开设置" else "去开启无障碍") }
                }
            }
        }

        Spacer(Modifier.height(10.dp))
        val linkTone = when {
            linkConnected -> MaterialTheme.colorScheme.primary
            linkStatus.contains("失败") || linkStatus.contains("拒") -> MaterialTheme.colorScheme.error
            else -> MaterialTheme.colorScheme.tertiary
        }
        Card(
            Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            shape = RoundedCornerShape(10.dp),
        ) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(Modifier.size(8.dp).background(linkTone, CircleShape))
                    Spacer(Modifier.width(8.dp))
                    Text("入网（MCP over WebSocket）", fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface)
                }
                Spacer(Modifier.height(4.dp))
                Text(linkStatus, fontSize = 12.sp, color = linkTone,
                    fontWeight = FontWeight.SemiBold)
                Text(
                    if (linkConnected) {
                        "控制面已配对。可调 ${PhoneMcp.toolCount()} 个本机工具；" +
                            if (Prefs.mcpAllowActions) "当前允许操作。" else "当前只读。"
                    } else {
                        "手机只主动往外连中继，不开端口、不走 adb。"
                    },
                    fontSize = 10.5.sp, lineHeight = 15.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 2.dp),
                )
                if (linkError.isNotBlank()) {
                    Text("错误：$linkError", fontSize = 10.5.sp, lineHeight = 15.sp,
                        color = MaterialTheme.colorScheme.error)
                }
                Row(verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = { showLink = true }) { Text("入网设置") }
                    if (Prefs.mcpConfigured()) {
                        TextButton(onClick = {
                            if (PhoneMcpLink.connected) PhoneLinkService.stop(ctx)
                            else PhoneLinkService.start(ctx)
                        }) { Text(if (PhoneMcpLink.connected) "断开" else "连接") }
                    } else {
                        Text("未配置", fontSize = 10.5.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }

        Spacer(Modifier.height(10.dp))
        Card(
            Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
            shape = RoundedCornerShape(10.dp),
        ) {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier.size(8.dp).background(
                            if (notifOn) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.tertiary,
                            CircleShape,
                        )
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        if (notifOn) "通知读取：已授权" else "通知读取：未授权",
                        fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                }
                Text(
                    if (notifOn) "最近通知会留在本机内存（最多 40 条），可用 self_notif_list 读；进程重启即清空。"
                    else "很多验证码 / 到账 / 消息不必截图，直接读通知更快。去系统设置授予通知使用权。",
                    fontSize = 10.5.sp, lineHeight = 15.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 2.dp),
                )
                if (!notifOn) {
                    TextButton(onClick = {
                        if (!NotificationWatch.openSettings(ctx)) {
                            android.widget.Toast.makeText(
                                ctx, "打不开通知使用权设置页，请手动进系统设置", android.widget.Toast.LENGTH_LONG
                            ).show()
                        }
                    }) { Text("去授权") }
                }
            }
        }

        Spacer(Modifier.height(10.dp))
        Text("本机能力", fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(start = 2.dp, bottom = 6.dp))

        LazyColumn(
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            items(PhoneSelf.caps) { cap ->
                Card(
                    Modifier.fillMaxWidth().clickable {
                        sel = cap
                        argText = defaultArgs(cap)
                        result = null
                    },
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.surfaceVariant),
                    shape = RoundedCornerShape(10.dp),
                ) {
                    Column(Modifier.fillMaxWidth().padding(12.dp)) {
                        Text(cap.title, fontSize = 13.sp,
                            color = MaterialTheme.colorScheme.onSurface)
                        Text(cap.name, fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(cap.desc, fontSize = 10.5.sp, lineHeight = 15.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 3.dp))
                    }
                }
            }
        }
    }

    if (showLink) {
        var url by remember { mutableStateOf(Prefs.mcpUrl) }
        var room by remember { mutableStateOf(Prefs.mcpRoom) }
        var secret by remember { mutableStateOf(Prefs.mcpSecret) }
        var allow by remember { mutableStateOf(Prefs.mcpAllowActions) }
        var editError by remember { mutableStateOf("") }
        AlertDialog(
            onDismissRequest = { showLink = false },
            title = { Text("手机入网（MCP）") },
            text = {
                Column(Modifier.heightIn(max = 460.dp).verticalScroll(rememberScrollState())) {
                    Text("手机和 PC 各自出站连同一个 /pair 中继，配对后控制面即可调用本机工具。三处配置必须一致。",
                        fontSize = 11.sp, lineHeight = 16.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(url, { url = it },
                        modifier = Modifier.fillMaxWidth(), singleLine = true,
                        label = { Text("中继地址") },
                        placeholder = { Text("wss://relay.example.com/pair") })
                    Spacer(Modifier.height(6.dp))
                    OutlinedTextField(room, { room = it },
                        modifier = Modifier.fillMaxWidth(), singleLine = true,
                        label = { Text("房间") },
                        placeholder = { Text("phone-self-<随机串>") })
                    Spacer(Modifier.height(6.dp))
                    OutlinedTextField(secret, { secret = it },
                        modifier = Modifier.fillMaxWidth(), singleLine = true,
                        label = { Text("密钥（与中继 WSRELAY_SECRET 相同）") },
                        visualTransformation = PasswordVisualTransformation())
                    Spacer(Modifier.height(10.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Column(Modifier.weight(1f)) {
                            Text("允许操作", fontSize = 12.sp, fontWeight = FontWeight.SemiBold,
                                color = MaterialTheme.colorScheme.onSurface)
                            Text("打开后 PC 才能调 tap / text / scroll / key / launch；默认只读。",
                                fontSize = 10.5.sp, lineHeight = 15.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        Switch(allow, { allow = it })
                    }
                    if (editError.isNotBlank()) {
                        Spacer(Modifier.height(8.dp))
                        Text(editError, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    val cfg = McpLinkConfig(url, room, secret)
                    val bad = cfg.validError()
                    if (bad.isNotBlank()) {
                        editError = bad
                    } else {
                        Prefs.mcpUrl = url
                        Prefs.mcpRoom = room
                        Prefs.mcpSecret = secret
                        Prefs.mcpAllowActions = allow
                        editError = ""
                        showLink = false
                        PhoneLinkService.start(ctx)
                    }
                }) { Text("保存并连接") }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = { showLink = false }) { Text("取消") }
                    if (Prefs.mcpConfigured()) {
                        TextButton(onClick = {
                            PhoneLinkService.stop(ctx)
                            showLink = false
                        }) { Text("断开") }
                    }
                }
            },
        )
    }

    val cap = sel
    if (cap != null) {
        AlertDialog(
            onDismissRequest = { if (!busy) sel = null },
            title = { Text(cap.title) },
            text = {
                Column(Modifier.heightIn(max = 420.dp).verticalScroll(rememberScrollState())) {
                    Text(cap.desc, fontSize = 12.sp, lineHeight = 17.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (cap.params.isNotEmpty()) {
                        Spacer(Modifier.height(8.dp))
                        Text(
                            cap.params.joinToString("\n") {
                                "· ${it.name}(${it.type})${if (it.required) " 必填" else " 可选"} — ${it.desc}"
                            },
                            fontSize = 10.5.sp, lineHeight = 16.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        Spacer(Modifier.height(6.dp))
                        Text("参数（JSON）", fontSize = 11.sp)
                        Spacer(Modifier.height(4.dp))
                        OutlinedTextField(
                            argText, { argText = it },
                            modifier = Modifier.fillMaxWidth(), singleLine = false,
                        )
                    }
                    if (busy) {
                        Spacer(Modifier.height(8.dp))
                        Text("执行中…", fontSize = 11.sp, color = MaterialTheme.colorScheme.primary)
                    }
                    result?.let { r ->
                        Spacer(Modifier.height(10.dp))
                        Text(
                            if (r.ok) r.text else "失败：${r.err}",
                            fontSize = 11.5.sp, lineHeight = 17.sp,
                            color = if (r.ok) MaterialTheme.colorScheme.onSurface
                            else MaterialTheme.colorScheme.error,
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(
                    enabled = !busy,
                    onClick = {
                        val args = runCatching { JSONObject(argText).toMap() }.getOrNull() ?: emptyMap()
                        scope.launch {
                            busy = true
                            result = PhoneSelf.run(cap.name, args)
                            busy = false
                        }
                    },
                ) { Text(if (busy) "执行中" else "执行") }
            },
            dismissButton = {
                TextButton(enabled = !busy, onClick = { sel = null }) { Text("关闭") }
            },
        )
    }
}

/** 按能力声明的必填参数拼一份可改的模板，省得用户每次从 `{}` 开始猜字段名。 */
private fun defaultArgs(cap: SelfCap): String {
    if (cap.params.none { it.required }) return "{}"
    val o = JSONObject()
    for (p in cap.params) {
        if (!p.required) continue
        when (p.type) {
            "int", "number" -> o.put(p.name, 0)
            "bool", "boolean" -> o.put(p.name, false)
            else -> o.put(p.name, "")
        }
    }
    return o.toString()
}
