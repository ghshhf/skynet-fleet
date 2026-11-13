package app.skynet.tianwang.next

import android.content.Context
import androidx.compose.foundation.background
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
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 双机互联（局域网直连 + 集群名册）本机管理页。
 *
 * 🔴 这一页是「手机是对等节点、不依附任何 PC」最直接的落地：
 * 之前 self.lan.* / self.peer.* 这些能力只能被远端控制面调，机主自己在这台手机上
 * 看不到也管不了——等于把「双机互联」交给别人来开关。现在机主能在本机：
 *   - 看直连服务状态（端口 / 本机可入站地址 / 口令头尾）；
 *   - 一键开 / 关本机直连服务（口令即授权，开的时候把完整口令摊给他看一次）；
 *   - 看集群名册、加一台对端、踢一台、逐台握手刷新电量 / 屏幕 / 能力数。
 *
 * 纪律（和项目一致）：口令只露头尾，不在列表 / 日志里回显完整串；
 * 写操作（探测/加名册）走后台线程，不在组合期阻塞主线程。
 */
@Composable
fun PeersScreen() {
    val ctx = LocalContext.current
    val scope = rememberCoroutineScope()

    var lanRunning by remember { mutableStateOf(LanHub.running) }
    var lanPort by remember { mutableStateOf(LanHub.port) }
    var status by remember { mutableStateOf(LanHub.statusText()) }
    var peers by remember { mutableStateOf(LanHub.peers()) }
    var note by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }

    var showAdd by remember { mutableStateOf(false) }
    var addName by remember { mutableStateOf("") }
    var addHost by remember { mutableStateOf("") }
    var addPort by remember { mutableStateOf(LanProto.DEFAULT_PORT.toString()) }
    var addToken by remember { mutableStateOf("") }
    var addErr by remember { mutableStateOf("") }

    fun refresh() {
        lanRunning = LanHub.running
        lanPort = LanHub.port
        status = LanHub.statusText()
        peers = LanHub.peers()
    }

    LaunchedEffect(Unit) { refresh() }

    fun withBusy(block: suspend () -> Unit) {
        busy = true
        note = ""
        scope.launch(Dispatchers.IO) {
            runCatching { block() }.onFailure {
                withContext(Dispatchers.Main) { note = "出错了：${it.message}" }
            }
            withContext(Dispatchers.Main) { busy = false; refresh() }
        }
    }

    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(14.dp),
        verticalArrangement = Arrangement.spacedBy(9.dp),
    ) {
        item("head") {
            GroupTitle("本机直连服务")
            Text(
                "同一 Wi-Fi 优先走 IPv6 链路本地一跳直达；跨网走全局 IPv6；都不通才回中继。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 4.dp, bottom = 6.dp),
            )
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(14.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        Modifier.size(8.dp).clip(CircleShape)
                            .background(if (lanRunning) StatusColors.ok else StatusColors.idle),
                    )
                    Spacer(Modifier.width(8.dp))
                    Text(
                        if (lanRunning) "运行中 · 端口 $lanPort" else "未启动",
                        fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                    Spacer(Modifier.weight(1f))
                    Button(
                        onClick = {
                            withBusy {
                                if (LanHub.running) {
                                    LanHub.stop(ctx)
                                    withContext(Dispatchers.Main) { note = "已关闭本机直连服务" }
                                } else {
                                    val r = LanHub.start(ctx)
                                    withContext(Dispatchers.Main) {
                                        note = if (r.ok) r.text else "启动失败：${r.err}"
                                    }
                                }
                            }
                        },
                        enabled = !busy,
                    ) {
                        Text(if (lanRunning) "关闭" else "开启")
                    }
                }
            }
        }

        item("status") {
            Column(
                Modifier.fillMaxWidth()
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surface)
                    .padding(12.dp),
            ) {
                Text("状态明细", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(4.dp))
                Text(status, fontSize = 11.sp, lineHeight = 16.sp,
                    fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurface)
            }
        }

        item("roster-head") {
            Spacer(Modifier.height(4.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                GroupTitleInline("集群名册 · ${peers.size} 台")
                Spacer(Modifier.weight(1f))
                IconButton(onClick = { withBusy { LanHub.peerProbe() } }, enabled = !busy) {
                    Icon(Icons.Filled.Refresh, "探测",
                        tint = if (busy) MaterialTheme.colorScheme.onSurfaceVariant
                        else MaterialTheme.colorScheme.primary)
                }
                IconButton(onClick = { showAdd = true }) {
                    Icon(Icons.Filled.Add, "加入对端",
                        tint = MaterialTheme.colorScheme.primary)
                }
            }
            Text(
                "探测 = 逐台握手，刷新电量 / 充电 / 亮屏 / 能力数。",
                fontSize = 10.5.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 4.dp, bottom = 4.dp),
            )
        }

        if (peers.isEmpty()) {
            item("empty") {
                Text(
                    "名册为空。先在另一台手机上「开启直连服务」拿到地址与完整口令，再点右上 + 加入。",
                    fontSize = 11.5.sp, lineHeight = 17.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(start = 8.dp, top = 4.dp),
                )
            }
        } else {
            items(peers, key = { it.name }) { p ->
                PeerRow(p, onRemove = {
                    withBusy { LanHub.peerRemove(mapOf("name" to p.name)) }
                })
            }
        }

        if (note.isNotBlank()) {
            item("note") {
                Spacer(Modifier.height(4.dp))
                Column(
                    Modifier.fillMaxWidth()
                        .clip(RoundedCornerShape(10.dp))
                        .background(MaterialTheme.colorScheme.surfaceVariant)
                        .padding(12.dp),
                ) {
                    Text(note, fontSize = 11.sp, lineHeight = 16.sp,
                        fontFamily = FontFamily.Monospace,
                        color = MaterialTheme.colorScheme.onSurface)
                }
            }
        }
    }

    if (showAdd) {
        AlertDialogPeersAdd(
            name = addName, host = addHost, port = addPort, token = addToken, err = addErr,
            onName = { addName = it }, onHost = { addHost = it },
            onPort = { addPort = it }, onToken = { addToken = it },
            onCancel = { showAdd = false; addErr = "" },
            onConfirm = {
                addErr = ""
                val name = addName.trim()
                val host = addHost.trim()
                val token = addToken.trim()
                if (name.isBlank() || host.isBlank() || token.isBlank()) {
                    addErr = "名字 / 地址 / 口令都要填"
                    return@AlertDialogPeersAdd
                }
                val port = addPort.toIntOrNull() ?: LanProto.DEFAULT_PORT
                val hostArg = if (host.contains(':')) host else "$host:$port"
                scope.launch(Dispatchers.IO) {
                    val r = LanHub.peerAdd(
                        mapOf("name" to name, "host" to hostArg, "token" to token),
                    )
                    withContext(Dispatchers.Main) {
                        if (r.ok) {
                            peers = LanHub.peers()
                            showAdd = false
                            addName = ""; addHost = ""; addToken = ""
                            note = "已加入 $name · 建议点一下探测刷新状态"
                        } else {
                            addErr = r.err ?: "加入失败"
                        }
                    }
                }
            },
        )
    }
}

@Composable
private fun PeerRow(p: PeerBook.Peer, onRemove: () -> Unit) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .padding(horizontal = 13.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.PhoneAndroid, null,
            tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(19.dp))
        Spacer(Modifier.width(11.dp))
        Column(Modifier.weight(1f)) {
            Text(p.name, fontSize = 13.5.sp, fontWeight = FontWeight.Medium,
                color = MaterialTheme.colorScheme.onSurface)
            Text(p.endpoint(), fontSize = 10.5.sp, fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            val sub = buildString {
                if (p.battery >= 0) append("电量 ${p.battery}%")
                if (p.charging) append(" · 充电中")
                append(" · 屏幕${if (p.screenOn) "亮" else "灭/未知"}")
                append(" · ${if (p.caps > 0) "${p.caps} 项能力" else "未探测"}")
            }
            Text(sub, fontSize = 10.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 2.dp))
            if (p.lastError.isNotBlank()) {
                Text("上次错误：${p.lastError}", fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 2.dp))
            }
        }
        TextButton(onClick = onRemove) {
            Text("移出", color = MaterialTheme.colorScheme.error, fontSize = 12.sp)
        }
    }
}

@Composable
private fun AlertDialogPeersAdd(
    name: String, host: String, port: String, token: String, err: String,
    onName: (String) -> Unit, onHost: (String) -> Unit, onPort: (String) -> Unit,
    onToken: (String) -> Unit, onCancel: () -> Unit, onConfirm: () -> Unit,
) {
    androidx.compose.material3.AlertDialog(
        onDismissRequest = onCancel,
        title = { Text("加入对端手机") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                DialogField("短名（如 b-phone）", name, onName)
                DialogField("地址（fe80::1%wlan0 或 192.168.1.7）", host, onHost)
                DialogField("端口（默认 ${LanProto.DEFAULT_PORT}）", port, onPort)
                DialogField("对端完整口令（在它「开启直连」后给的那串）", token, onToken)
                if (err.isNotBlank()) {
                    Text(err, fontSize = 11.sp, color = MaterialTheme.colorScheme.error)
                }
                Text(
                    "口令即授权：拿到它就能调本机全部工具（含写操作）。只在这两台手机之间用，别发群里。",
                    fontSize = 10.sp, lineHeight = 15.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(onClick = onConfirm) { Text("加入") }
        },
        dismissButton = {
            TextButton(onClick = onCancel) { Text("取消") }
        },
    )
}

@Composable
private fun DialogField(label: String, value: String, onValueChange: (String) -> Unit) {
    Column(Modifier.fillMaxWidth()) {
        Text(label, fontSize = 10.5.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(3.dp))
        BasicTextField(
            value = value,
            onValueChange = onValueChange,
            singleLine = true,
            modifier = Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(8.dp))
                .background(MaterialTheme.colorScheme.surfaceVariant)
                .padding(10.dp),
            textStyle = androidx.compose.ui.text.TextStyle(
                fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface),
            cursorBrush = androidx.compose.ui.graphics.SolidColor(
                MaterialTheme.colorScheme.primary),
        )
    }
}

@Composable
private fun GroupTitle(t: String) {
    Text(t, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(start = 4.dp, top = 4.dp, bottom = 6.dp))
}

@Composable
private fun GroupTitleInline(t: String) {
    Text(t, fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
}
