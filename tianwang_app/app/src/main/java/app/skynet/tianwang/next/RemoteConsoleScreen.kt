package app.skynet.tianwang.next

import android.graphics.BitmapFactory
import android.util.Base64
import androidx.compose.foundation.Image
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
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Devices
import androidx.compose.material.icons.filled.Photo
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Send
import androidx.compose.material.icons.filled.Sync
import androidx.compose.material.icons.filled.TouchApp
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import androidx.compose.runtime.rememberCoroutineScope
import org.json.JSONObject

/**
 * 远程控制台：手机作为对等节点，在**本机**就能通过局域网直连控制别的对端
 * （另一台手机，或跑了 [lan_mcp_server] 的电脑）。
 *
 * 这一页把已有的传输层（`LanClient` / `LanServer` / `LanHub` 名册）摊开成人和人能用的界面：
 *   · 看本机直连状态 + 一键开关直连服务（让别的对端能反向控这台手机）；
 *   · 名册 + mDNS 发现的邻近设备，一键加入 / 删除；
 *   · 进一台对端 → 握手取元数据与工具表 → 按工具名带 JSON 参数调用；
 *   · 手机对端额外给「截图 / 按坐标点 / 滑动 / 按键」快捷控制，直接看到屏、直接点。
 *
 * 不引入新协议、不重写传输：对端要么是另一台手机的 [LanServer]，要么是电脑侧
 * 的 [lan_mcp_server]，两者讲同一套「hello + tools/call」协议，本页只是它的控制器。
 */
@Composable
fun RemoteConsoleScreen(onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var running by remember { mutableStateOf(LanHub.running) }
    var port by remember { mutableStateOf(LanHub.port) }
    var token by remember { mutableStateOf(Prefs.lanToken) }

    var peers by remember { mutableStateOf(LanHub.peers()) }
    var discovered by remember { mutableStateOf(LanHub.discovered()) }

    var selected by remember { mutableStateOf<PeerBook.Peer?>(null) }
    var meta by remember { mutableStateOf<JSONObject?>(null) }
    var metaErr by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var resultText by remember { mutableStateOf("选一台对端，握手后就能调它的工具。") }
    var resultImg by remember { mutableStateOf<ImageBitmap?>(null) }

    var showAdd by remember { mutableStateOf(false) }
    var addName by remember { mutableStateOf("") }
    var addHost by remember { mutableStateOf("") }
    var addPort by remember { mutableStateOf("18765") }
    var addToken by remember { mutableStateOf("") }

    // 控制表单
    var argStr by remember { mutableStateOf("{}") }

    fun reload() {
        peers = LanHub.peers()
        discovered = LanHub.discovered()
    }

    fun toggleServer() {
        if (running) {
            LanHub.stop(ctx = App.ctx, keepService = false)
            running = false
            port = 0
        } else {
            val r = LanHub.start(App.ctx, rotate = false)
            running = LanHub.running
            port = LanHub.port
            token = Prefs.lanToken
            if (!r.ok) resultText = "开直连失败：${r.err}"
        }
    }

    fun probePeer(p: PeerBook.Peer) {
        busy = true
        metaErr = ""
        scope.launch(Dispatchers.IO) {
            val r = LanClient.probe(LanProto.Target(p.host, p.port), p.token)
            withContext(Dispatchers.Main) {
                busy = false
                if (r.ok) {
                    meta = r.hello
                    metaErr = ""
                } else {
                    meta = null
                    metaErr = r.err
                }
            }
        }
    }

    fun doCall(p: PeerBook.Peer, tool: String, rawArgs: String) {
        val argObj = runCatching { JSONObject(rawArgs.ifBlank { "{}" }) }.getOrNull()
        if (argObj == null) {
            resultText = "参数不是合法 JSON：${rawArgs.take(80)}"
            return
        }
        busy = true
        resultImg = null
        resultText = "调用 $tool 中…"
        scope.launch(Dispatchers.IO) {
            val r = LanClient.call(
                LanProto.Target(p.host, p.port), p.token,
                LanProto.toolRequest(tool, argObj), 30_000,
            )
            val (txt, img) = if (r.ok) parseReply(r.text) else (null to null)
            val via = if (r.via.isNotBlank()) " · 经 ${r.via}" else ""
            withContext(Dispatchers.Main) {
                busy = false
                resultText = if (r.ok) "${txt ?: ""}\n—— $tool · ${r.ms}ms$via"
                else "✗ $tool\n${r.err}"
                resultImg = img
            }
        }
    }

    fun addPeer() {
        val name = addName.trim()
        val host = addHost.trim()
        val tok = addToken.trim()
        if (name.isEmpty() || host.isEmpty() || tok.isEmpty()) {
            resultText = "名字 / 地址 / 口令都要填"
            return
        }
        val portNum = addPort.toIntOrNull()?.coerceIn(1, 65535) ?: 18765
        val r = LanHub.peerAdd(mapOf(
            "name" to name, "host" to host, "port" to portNum, "token" to tok,
        ))
        if (r.ok) {
            showAdd = false
            addName = ""; addHost = ""; addPort = "18765"; addToken = ""
            reload()
        } else {
            resultText = "加入失败：${r.err}"
        }
    }

    fun removePeer(name: String) {
        LanHub.peerRemove(mapOf("name" to name))
        reload()
        if (selected?.name == name) { selected = null; meta = null }
    }

    Column(
        Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
    ) {
        // ---- 本机直连（让别的对端能反向控这台手机） ----
        Surface(
            tonalElevation = 1.dp,
            shape = RoundedCornerShape(14.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(14.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Filled.Devices, null,
                        tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(20.dp))
                    Spacer(Modifier.width(8.dp))
                    Text("本机直连服务", fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface)
                    Spacer(Modifier.weight(1f))
                    val online = running
                    Box(
                        Modifier.size(9.dp)
                            .background(
                                if (online) MaterialTheme.colorScheme.primary
                                else MaterialTheme.colorScheme.outline,
                                RoundedCornerShape(50),
                            ),
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(if (online) "运行中" else "未启动",
                        fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(8.dp))
                if (running) {
                    val addrs = LanProto.localAddresses()
                    Text("端口 $port · 对端在它的「远程控制台 / 双机互联」里填：",
                        fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    addrs.take(4).forEach {
                        Text("  ${LanProto.bracket(it.host)}:$port  · ${it.family.label}",
                            fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurface)
                    }
                    Text("口令只在本机与对端之间用；不要发到群里或进截图。换口令：关掉再开。",
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 4.dp))
                } else {
                    Text("开了之后，别的设备才能用口令反向控制这台手机（截图 / 点屏 / 跑工具）。",
                        fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Spacer(Modifier.height(8.dp))
                Row {
                    androidx.compose.material3.Button(onClick = { toggleServer() }) {
                        Icon(if (running) Icons.Filled.Close else Icons.Filled.PlayArrow, null)
                        Spacer(Modifier.width(4.dp))
                        Text(if (running) "停止" else "开启直连")
                    }
                    Spacer(Modifier.weight(1f))
                    if (running) {
                        TextButton(onClick = {
                            val t = Prefs.lanToken
                            if (t.isNotBlank()) {
                                val cm = App.ctx.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                                    as? android.content.ClipboardManager
                                cm?.setPrimaryClip(android.content.ClipData.newPlainText("lan-token", t))
                                resultText = "口令已复制到剪贴板（勿外泄）。"
                            }
                        }) { Text("复制口令") }
                    }
                }
            }
        }

        Spacer(Modifier.height(14.dp))

        // ---- 对端列表：名册 + 发现 ----
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("对端设备", fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.weight(1f))
            TextButton(onClick = { reload() }) {
                Icon(Icons.Filled.Sync, null, Modifier.size(16.dp))
                Spacer(Modifier.width(4.dp))
                Text("刷新")
            }
            TextButton(onClick = { showAdd = true }) {
                Icon(Icons.Filled.Add, null, Modifier.size(16.dp))
                Spacer(Modifier.width(4.dp))
                Text("加入")
            }
        }

        // 名册
        if (peers.isEmpty()) {
            Text("名册为空。点「加入」填对端的 host:port:token；" +
                "或在电脑上跑 lan_mcp_server.py 后把它的地址加进来。",
                fontSize = 11.5.sp, lineHeight = 17.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 4.dp, top = 6.dp))
        }
        peers.forEach { p ->
            PeerRow(p = p, onlineHint = "",
                onClick = {
                    selected = p
                    meta = null
                    metaErr = ""
                    probePeer(p)
                },
                onRemove = { removePeer(p.name) },
                selected = selected?.name == p.name)
        }

        // 发现但未入册
        val unknown = discovered.filter { d -> peers.none { it.name.equals(d.name, true) } }
        if (unknown.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text("局域网发现（未加入）", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 4.dp, top = 6.dp, bottom = 4.dp))
            unknown.take(10).forEach { d ->
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(vertical = 6.dp)
                        .clickable {
                            addName = d.name
                            addHost = d.host
                            addPort = d.port.toString()
                            showAdd = true
                        },
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(Icons.Filled.Devices, null, Modifier.size(18.dp),
                        MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.width(8.dp))
                    Column(Modifier.weight(1f)) {
                        Text(d.name, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface)
                        Text("${LanProto.bracket(d.host)}:${d.port}",
                            fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text("点此加入", fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.primary)
                }
            }
        }

        // ---- 选中对端详情 ----
        selected?.let { p ->
            Spacer(Modifier.height(16.dp))
            HorizontalDivider(color = MaterialTheme.colorScheme.outline)
            Spacer(Modifier.height(12.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("控制 ${p.name}", fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurface)
                Spacer(Modifier.weight(1f))
                TextButton(onClick = { probePeer(p) }) {
                    Icon(Icons.Filled.Sync, null, Modifier.size(16.dp))
                    Spacer(Modifier.width(4.dp)); Text("重握手")
                }
            }
            meta?.let { m ->
                val platform = m.optString("platform").ifBlank { "未知机型" }
                val battery = m.optInt("battery", -1)
                val caps = m.optInt("caps", 0)
                val toolsArr = m.optJSONArray("tools")
                val toolCount = toolsArr?.length() ?: 0
                Text("$platform · 能力 $caps" + if (battery >= 0) " · 电量 $battery%" else "",
                    fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(8.dp))
                Text("工具表（$toolCount）", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurface)
                val isPhone = (0 until toolCount).any { toolsArr?.optString(it) == "self_ui_screenshot" }
                // 手机对端：快捷控制
                if (isPhone) {
                    Spacer(Modifier.height(6.dp))
                    Row {
                        QuickControlButton("截图", Icons.Filled.Photo) { doCall(p, "self_ui_screenshot", "{}") }
                        Spacer(Modifier.width(6.dp))
                        QuickControlButton("回桌面", Icons.Filled.TouchApp) { doCall(p, "self_ui_key", JSONObject().put("key","home").toString()) }
                        Spacer(Modifier.width(6.dp))
                        QuickControlButton("返回", Icons.Filled.ArrowBack) { doCall(p, "self_ui_key", JSONObject().put("key","back").toString()) }
                    }
Spacer(Modifier.height(6.dp))
                // 电脑端信息/窗口（由对端即 PC 侧 pcctl.* 返回）
                if (isPhone) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.sysinfo", "{}") }) { "系统信息" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.windows", "{}") }) { "窗口列表" }
                    }
                    Spacer(Modifier.height(6.dp))
                }
                Text("按坐标点（x,y）：", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    var xy by remember { mutableStateOf("500,800") }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(xy, { xy = it }, singleLine = true,
                            modifier = Modifier.weight(1f), textStyle = TextStyle(fontSize = 12.sp))
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = {
                            val parts = xy.split(",").map { it.trim().toIntOrNull() ?: 0 }
                            if (parts.size >= 2) doCall(p, "self_ui_tap_at",
                                JSONObject().put("x", parts[0]).put("y", parts[1]).toString())
                        }) {
                            Icon(Icons.Filled.TouchApp, null, Modifier.size(16.dp))
                            Spacer(Modifier.width(4.dp)); Text("点")
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    Text("滑动（x1,y1 -> x2,y2）：", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    var sw by remember { mutableStateOf("500,800,500,300") }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        OutlinedTextField(sw, { sw = it }, singleLine = true,
                            modifier = Modifier.weight(1f), textStyle = TextStyle(fontSize = 12.sp))
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = {
                            val a = sw.split(",").map { it.trim().toIntOrNull() ?: 0 }
                            if (a.size >= 4) doCall(p, "self_ui_swipe",
                                JSONObject().put("x1", a[0]).put("y1", a[1])
                                    .put("x2", a[2]).put("y2", a[3]).toString())
                        }) {
                            Icon(Icons.Filled.Send, null, Modifier.size(16.dp))
                            Spacer(Modifier.width(4.dp)); Text("滑")
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                }
                // 鼠标键盘远程控制（由对端即 PC 侧 pcctl.* 返回）
                if (isPhone) {
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.mouse", """{"action":"move","x":0,"y":0}""") }) { "鼠标移动" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.mouse", """{"action":"click"}""") }) { "鼠标点击" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.mouse", """{"action":"drag","from":[0,0],"to":[100,100]}""") }) { "鼠标拖拽" }
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.input", """{"action":"key","keys":"ctrl+c"}""") }) { "键盘复制" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.input", """{"action":"key","keys":"ctrl+v"}""") }) { "键盘粘贴" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.input", """{"action":"key","keys":"enter"}""") }) { "键盘回车" }
                    }
                    Spacer(Modifier.height(6.dp))
                }
                // 进程管理（由对端即 PC 侧 pcctl.proc.* 返回）
                if (isPhone) {
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.proc.list", "{}") }) { "进程列表" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.proc.kill", """{"pid":0}""") }) { "结束进程" }
                    }
                    Spacer(Modifier.height(6.dp))
                }
                // 剪贴板同步（由对端即 PC 侧 pcctl.clipboard.* 返回）
                if (isPhone) {
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.clipboard.read", "{}") }) { "读剪贴板" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.clipboard.write", """{"text":""}""") }) { "写剪贴板" }
                    }
                    Spacer(Modifier.height(6.dp))
                }
                // 探测对端状态（由 LanHub.peerProbe 返回，刷新电量/工具表/在线标记）
                if (isPhone) {
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { probePeer(p) }) { "探测对端" }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { reload() }) { "刷新名册" }
                    }
                    Spacer(Modifier.height(6.dp))
                }
                // 工具表：点击即用，参数用下面的文本框
                Spacer(Modifier.height(6.dp))
                Text("调用任意工具（JSON 参数）：", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                OutlinedTextField(argStr, { argStr = it },
                    modifier = Modifier.fillMaxWidth(), textStyle = TextStyle(fontSize = 12.sp),
                    singleLine = false, minLines = 2,
                    label = { Text("参数 JSON", fontSize = 11.sp) })
                Spacer(Modifier.height(6.dp))
                val tools = (0 until toolCount).mapNotNull { toolsArr?.optString(it) }.filter { it.isNotBlank() }
                val chipRows = tools.chunked(3)
                chipRows.forEach { rowTools ->
                    Row(Modifier.fillMaxWidth()) {
                        rowTools.forEach { t ->
                            Surface(
                                tonalElevation = 1.dp,
                                shape = RoundedCornerShape(8.dp),
                                modifier = Modifier
                                    .padding(end = 6.dp, bottom = 6.dp)
                                    .clickable {
                                        // 把工具名预填到参数框提示里，直接调用
                                        doCall(p, t, argStr)
                                    },
                            ) {
                                Text(t, fontSize = 10.5.sp,
                                    color = MaterialTheme.colorScheme.primary,
                                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 5.dp))
                            }
                        }
                    }
                }
            } ?: run {
                if (metaErr.isNotBlank()) {
                    Text("握手失败：$metaErr", fontSize = 11.5.sp,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(top = 6.dp))
                } else {
                    Text("握手中…", fontSize = 11.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }

        // ---- 结果 ----
        Spacer(Modifier.height(16.dp))
        Surface(
            tonalElevation = 0.dp,
            color = MaterialTheme.colorScheme.surfaceVariant,
            shape = RoundedCornerShape(12.dp),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(12.dp)) {
                Text(if (busy) "执行中…" else "结果", fontSize = 11.sp, fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(6.dp))
                resultImg?.let { bmp ->
                    Image(bmp, "对端截图",
                        modifier = Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp)))
                    Spacer(Modifier.height(8.dp))
                }
                Text(resultText, fontSize = 12.sp, lineHeight = 18.sp,
                    color = MaterialTheme.colorScheme.onSurface,
                    modifier = Modifier.fillMaxWidth())
            }
        }
        Spacer(Modifier.height(20.dp))
    }

    if (showAdd) {
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { showAdd = false },
            title = { Text("加入对端设备") },
            text = {
                Column {
                    OutlinedTextField(addName, { addName = it }, singleLine = true,
                        modifier = Modifier.fillMaxWidth(), textStyle = TextStyle(fontSize = 13.sp),
                        label = { Text("短名（如 b-phone / 家里电脑）") })
                    Spacer(Modifier.height(6.dp))
                    OutlinedTextField(addHost, { addHost = it }, singleLine = true,
                        modifier = Modifier.fillMaxWidth(), textStyle = TextStyle(fontSize = 13.sp),
                        label = { Text("地址：192.168.1.7 / [IPv6]:端口 / fe80::1%wlan0") })
                    Spacer(Modifier.height(6.dp))
                    OutlinedTextField(addPort, { addPort = it }, singleLine = true,
                        modifier = Modifier.fillMaxWidth(), textStyle = TextStyle(fontSize = 13.sp),
                        label = { Text("端口（默认 18765）") })
                    Spacer(Modifier.height(6.dp))
                    OutlinedTextField(addToken, { addToken = it }, singleLine = true,
                        modifier = Modifier.fillMaxWidth(), textStyle = TextStyle(fontSize = 13.sp),
                        label = { Text("口令（对端 self.lan.start 回执里的）") })
                }
            },
            confirmButton = {
                TextButton(onClick = { addPeer() }) { Text("加入") }
            },
            dismissButton = {
                TextButton(onClick = { showAdd = false }) { Text("取消") }
            },
        )
    }
}

@Composable
private fun PeerRow(
    p: PeerBook.Peer,
    onlineHint: String,
    onClick: () -> Unit,
    onRemove: () -> Unit,
    selected: Boolean,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp)
            .then(if (selected) Modifier.border(1.dp, MaterialTheme.colorScheme.primary, RoundedCornerShape(10.dp))
                  else Modifier)
            .clip(RoundedCornerShape(10.dp))
            .clickable { onClick() }
            .padding(horizontal = 8.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(Icons.Filled.Devices, null, Modifier.size(18.dp),
            MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(8.dp))
        Column(Modifier.weight(1f)) {
            Text(p.name, fontSize = 13.sp, color = MaterialTheme.colorScheme.onSurface)
            val meta = buildString {
                append(LanProto.bracket(p.host)).append(':').append(p.port)
                if (p.platform.isNotBlank()) append(" · ").append(p.platform)
                if (p.battery >= 0) append(" · ").append(p.battery).append('%')
                if (p.lastError.isNotBlank()) append(" · ").append(p.lastError)
            }
            Text(meta, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2)
        }
        IconButton(onClick = onRemove) {
            Icon(Icons.Filled.Delete, null, Modifier.size(18.dp), MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun QuickControlButton(label: String, icon: androidx.compose.ui.graphics.vector.ImageVector, onClick: () -> Unit) {
    Surface(
        tonalElevation = 1.dp,
        shape = RoundedCornerShape(10.dp),
        modifier = Modifier.clickable { onClick() },
    ) {
        Row(Modifier.padding(horizontal = 10.dp, vertical = 7.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Icon(icon, null, Modifier.size(16.dp), MaterialTheme.colorScheme.primary)
            Spacer(Modifier.width(4.dp))
            Text(label, fontSize = 11.5.sp, color = MaterialTheme.colorScheme.primary)
        }
    }
}

/** 把 MCP 回包压成一句人话 + （若有）第一张图片，和手机侧 summarizeMcpReply 同口径。 */
private fun parseReply(raw: String): Pair<String?, ImageBitmap?> {
    val j = runCatching { JSONObject(raw) }.getOrNull() ?: return (raw to null)
    if (j.has("error")) {
        return (j.optJSONObject("error")?.optString("message", "对端错误") to null)
    }
    val result = j.optJSONObject("result") ?: return (raw to null)
    val content = result.optJSONArray("content") ?: return ("(空回包)" to null)
    val sb = StringBuilder()
    var img: ImageBitmap? = null
    for (i in 0 until content.length()) {
        val item = content.optJSONObject(i) ?: continue
        when (item.optString("type")) {
            "text" -> sb.append(item.optString("text"))
            "image" -> {
                if (img == null) {
                    val data = item.optString("data")
                    if (data.isNotBlank()) {
                        val bytes = runCatching { Base64.decode(data, Base64.DEFAULT) }.getOrNull()
                        if (bytes != null) {
                            val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                            if (bmp != null) img = bmp.asImageBitmap()
                        }
                    }
                }
                sb.append("\n[图片已显示]")
            }
        }
    }
    val txt = if (sb.isEmpty()) raw else sb.toString()
    return (txt to img)
}
