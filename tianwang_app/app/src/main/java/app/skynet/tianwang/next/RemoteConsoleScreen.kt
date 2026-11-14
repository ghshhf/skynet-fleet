package app.skynet.tianwang.next

import android.graphics.BitmapFactory
import android.util.Base64
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
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
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
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

    // ---- 手机遥控：看屏 → 直接在图上点/划/打字 ----
    var live by remember { mutableStateOf(false) }
    var liveImg by remember { mutableStateOf<ImageBitmap?>(null) }
    var liveErr by remember { mutableStateOf("") }
    var actErr by remember { mutableStateOf("") }
    var liveNote by remember { mutableStateOf("") }
    var liveBusy by remember { mutableStateOf(false) }
    var scrW by remember { mutableStateOf(0) }
    var scrH by remember { mutableStateOf(0) }
    var typed by remember { mutableStateOf("") }

    fun callPeer(p: PeerBook.Peer, tool: String, args: JSONObject, timeoutMs: Int = 30_000): LanClient.Reply =
        LanClient.call(LanProto.Target(p.host, p.port), p.token, LanProto.toolRequest(tool, args), timeoutMs)

    /** 拉一帧对端屏幕；不知道对端分辨率时先问一次 self_status。 */
    fun pullScreen(p: PeerBook.Peer) {
        if (liveBusy) return
        liveBusy = true
        val knownW = scrW
        val knownH = scrH
        scope.launch(Dispatchers.IO) {
            var w = knownW
            var h = knownH
            if (w <= 0 || h <= 0) {
                val st = callPeer(p, "self_status", JSONObject(), 10_000)
                if (!st.ok) {
                    withContext(Dispatchers.Main) {
                        liveBusy = false
                        liveErr = "读对端状态失败：${st.err}"
                    }
                    return@launch
                }
                val size = RemoteScreen.parseScreenSize(st.text)
                if (size != null) {
                    w = size.first
                    h = size.second
                }
            }
            val shot = callPeer(p, "self_ui_screenshot", JSONObject().put("max", 1080), 20_000)
            val (txt, img) = if (shot.ok) parseReply(shot.text) else (null to null)
            withContext(Dispatchers.Main) {
                liveBusy = false
                if (img != null) {
                    liveImg = img
                    liveErr = ""
                    liveNote = txt.orEmpty().lineSequence().firstOrNull().orEmpty()
                    if (w > 0 && h > 0) { scrW = w; scrH = h }
                } else {
                    liveErr = if (!shot.ok) shot.err.ifBlank { "截图失败" }
                    else "对端回包拿不到图片：${txt.orEmpty().take(120)}"
                }
            }
        }
    }

    /** 做一次遥控动作，稍等对端渲染后再拉一帧，让人马上看到结果。 */
    fun remoteAct(p: PeerBook.Peer, tool: String, args: JSONObject) {
        scope.launch(Dispatchers.IO) {
            val r = callPeer(p, tool, args, 20_000)
            withContext(Dispatchers.Main) {
                actErr = if (r.ok) "" else "${tool} 失败：${r.err}"
                if (r.ok) {
                    delay(260)
                    pullScreen(p)
                }
            }
        }
    }

    fun remoteTap(p: PeerBook.Peer, px: Float, py: Float, viewW: Float, viewH: Float, img: ImageBitmap) {
        val pt = RemoteScreen.mapToScreen(px, py, viewW, viewH, img.width, img.height, scrW, scrH)
        if (pt == null) {
            liveErr = "还没拿到对端屏幕分辨率（点「刷新」重试），这次点击没发出去"
            return
        }
        remoteAct(p, "self_ui_tap_at", JSONObject().put("x", pt.first).put("y", pt.second))
    }

    fun remoteSwipe(
        p: PeerBook.Peer, sx: Float, sy: Float, ex: Float, ey: Float,
        viewW: Float, viewH: Float, img: ImageBitmap,
    ) {
        val a = RemoteScreen.mapToScreen(sx, sy, viewW, viewH, img.width, img.height, scrW, scrH)
        val b = RemoteScreen.mapToScreen(ex, ey, viewW, viewH, img.width, img.height, scrW, scrH)
        if (a == null || b == null) {
            liveErr = "还没拿到对端屏幕分辨率（点「刷新」重试），这次滑动没发出去"
            return
        }
        remoteAct(
            p, "self_ui_swipe",
            JSONObject().put("x1", a.first).put("y1", a.second)
                .put("x2", b.first).put("y2", b.second).put("duration", 320),
        )
    }

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

    // ---- 全屏遥控：整页只放对端画面，点哪点哪 ----
    val peer = selected
    if (live && peer != null) {
        Column(
            Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().padding(10.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = { live = false }) {
                    Icon(Icons.Filled.ArrowBack, "退出遥控")
                }
                Column(Modifier.weight(1f)) {
                    Text("遥控 ${peer.name}", fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface)
                    Text(
                        when {
                            actErr.isNotBlank() -> actErr
                            liveBusy -> "拉屏中…"
                            liveErr.isNotBlank() -> liveErr
                            liveNote.isNotBlank() -> liveNote
                            else -> "点图即点屏 · 拖动即滑动"
                        },
                        fontSize = 10.5.sp, maxLines = 2,
                        color = if (actErr.isNotBlank() || liveErr.isNotBlank())
                            MaterialTheme.colorScheme.error
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                TextButton(onClick = { pullScreen(peer) }, enabled = !liveBusy) { Text("刷新") }
            }
            Spacer(Modifier.height(6.dp))
            Box(
                Modifier.fillMaxWidth().weight(1f)
                    .clip(RoundedCornerShape(10.dp))
                    .background(MaterialTheme.colorScheme.surfaceVariant),
                contentAlignment = Alignment.Center,
            ) {
                val img = liveImg
                if (img == null) {
                    Text(if (liveBusy) "正在取对端画面…" else "还没有画面，点右上「刷新」",
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                } else {
                    Image(
                        img, "对端屏幕",
                        contentScale = ContentScale.Fit,
                        modifier = Modifier
                            .fillMaxSize()
                            .pointerInput(peer.name, img, scrW, scrH) {
                                detectTapGestures { off ->
                                    remoteTap(peer, off.x, off.y,
                                        size.width.toFloat(), size.height.toFloat(), img)
                                }
                            }
                            .pointerInput(peer.name, img, scrW, scrH) {
                                var sx = 0f; var sy = 0f
                                var lx = 0f; var ly = 0f
                                var moved = false
                                detectDragGestures(
                                    onDragStart = { o ->
                                        sx = o.x; sy = o.y; lx = o.x; ly = o.y; moved = false
                                    },
                                    onDrag = { ch, d ->
                                        ch.consume(); lx += d.x; ly += d.y; moved = true
                                    },
                                    onDragEnd = {
                                        if (moved) {
                                            remoteSwipe(peer, sx, sy, lx, ly,
                                                size.width.toFloat(), size.height.toFloat(), img)
                                        }
                                    },
                                )
                            },
                    )
                }
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                RemoteKey("返回") { remoteAct(peer, "self_ui_key", JSONObject().put("key", "back")) }
                RemoteKey("桌面") { remoteAct(peer, "self_ui_key", JSONObject().put("key", "home")) }
                RemoteKey("任务") { remoteAct(peer, "self_ui_key", JSONObject().put("key", "recents")) }
                RemoteKey("通知") { remoteAct(peer, "self_ui_key", JSONObject().put("key", "notifications")) }
            }
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    typed, { typed = it }, singleLine = true,
                    modifier = Modifier.weight(1f), textStyle = TextStyle(fontSize = 12.sp),
                    placeholder = { Text("打进对端当前输入框", fontSize = 11.sp) },
                )
                Spacer(Modifier.width(6.dp))
                TextButton(
                    onClick = {
                        val t = typed.trim()
                        if (t.isNotEmpty()) {
                            remoteAct(peer, "self_ui_text", JSONObject().put("text", t))
                            typed = ""
                        }
                    },
                    enabled = !liveBusy,
                ) {
                    Icon(Icons.Filled.Send, null, Modifier.size(16.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("发送")
                }
            }
        }
        return
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
                // 手机对端：进全屏遥控 —— 看对端画面，直接在图上点/划/打字
                if (isPhone) {
                    Spacer(Modifier.height(6.dp))
                    Row {
                        QuickControlButton("遥控这台手机", Icons.Filled.TouchApp) {
                            liveImg = null
                            liveErr = ""
                            actErr = ""
                            liveNote = ""
                            live = true
                            pullScreen(p)
                        }
                        Spacer(Modifier.width(6.dp))
                        QuickControlButton("截一张", Icons.Filled.Photo) {
                            doCall(p, "self_ui_screenshot", "{}")
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    Text("遥控页里：点图 = 点对端屏幕，拖动 = 滑动，底部可打字。",
                        fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(6.dp))
                } else {
                    // 电脑端：由对端 pcctl.* 提供
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.sysinfo", "{}") }) { Text("系统信息") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.windows", "{}") }) { Text("窗口列表") }
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.mouse", """{"action":"move","x":0,"y":0}""") }) { Text("鼠标移动") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.mouse", """{"action":"click"}""") }) { Text("鼠标点击") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.mouse", """{"action":"drag","from":[0,0],"to":[100,100]}""") }) { Text("鼠标拖拽") }
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.input", """{"action":"key","keys":"ctrl+c"}""") }) { Text("键盘复制") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.input", """{"action":"key","keys":"ctrl+v"}""") }) { Text("键盘粘贴") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.input", """{"action":"key","keys":"enter"}""") }) { Text("键盘回车") }
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.proc.list", "{}") }) { Text("进程列表") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.proc.kill", """{"pid":0}""") }) { Text("结束进程") }
                    }
                    Spacer(Modifier.height(6.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        TextButton(onClick = { doCall(p, "pcctl.clipboard.read", "{}") }) { Text("读剪贴板") }
                        Spacer(Modifier.width(6.dp))
                        TextButton(onClick = { doCall(p, "pcctl.clipboard.write", """{"text":""}""") }) { Text("写剪贴板") }
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

@Composable
private fun RemoteKey(label: String, onClick: () -> Unit) {
    Surface(
        tonalElevation = 1.dp,
        shape = RoundedCornerShape(10.dp),
        modifier = Modifier.clickable { onClick() },
    ) {
        Text(label, fontSize = 12.sp, color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 8.dp))
    }
}

/**
 * 遥控屏的纯计算部分：屏幕尺寸解析 + 图上坐标换算。
 *
 * 放在一起是为了能在 JVM 单测里直接验：图片按 Fit 缩放后四周会有留白，
 * 点在留白上不算，落在图上才换算成对端的物理像素。
 */
internal object RemoteScreen {
    /** 从 self_status 的回包文字里取「屏幕：1080×2400」。 */
    fun parseScreenSize(statusText: String): Pair<Int, Int>? {
        val m = Regex("屏幕\\s*[:：]\\s*(\\d+)\\s*[×x]\\s*(\\d+)").find(statusText) ?: return null
        val w = m.groupValues[1].toIntOrNull() ?: return null
        val h = m.groupValues[2].toIntOrNull() ?: return null
        return if (w > 0 && h > 0) w to h else null
    }

    /**
     * 把控件上的点击点（px,py）换算成对端屏幕像素。
     * 图片按 Fit 显示：缩放到恰好塞进 viewW×viewH 并居中，留白处返回 null。
     */
    fun mapToScreen(
        px: Float, py: Float,
        viewW: Float, viewH: Float,
        imgW: Int, imgH: Int,
        screenW: Int, screenH: Int,
    ): Pair<Int, Int>? {
        if (viewW <= 0f || viewH <= 0f) return null
        if (imgW <= 0 || imgH <= 0 || screenW <= 0 || screenH <= 0) return null
        val scale = minOf(viewW / imgW, viewH / imgH)
        val drawnW = imgW * scale
        val drawnH = imgH * scale
        val dx = (viewW - drawnW) / 2f
        val dy = (viewH - drawnH) / 2f
        if (px < dx || py < dy || px > dx + drawnW || py > dy + drawnH) return null
        val u = ((px - dx) / drawnW).coerceIn(0f, 1f)
        val v = ((py - dy) / drawnH).coerceIn(0f, 1f)
        val x = (u * screenW).toInt().coerceIn(0, screenW - 1)
        val y = (v * screenH).toInt().coerceIn(0, screenH - 1)
        return x to y
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
