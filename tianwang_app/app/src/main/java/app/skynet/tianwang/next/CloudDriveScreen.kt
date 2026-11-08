package app.skynet.tianwang.next

import android.app.DownloadManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.InsertDriveFile
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.launch

/**
 * 工具箱「网盘聚合」——**所有设备都是对等节点**。
 *
 * - 任意节点（手机 / PC / NAS / 其它手机）都可以自己跑一份 OpenList 或 WebDAV，
 *   这里不预设主节点、不预设地址，节点可自动发现或手动填，凭据只落本机；
 * - 各网盘账号的登录（扫码 / 短信验证码 / 密码）在**手机内嵌 WebView 里**打开
 *   所选节点的管理页完成，不要求先去另一台设备登录；
 * - 本机同时把列目录 / 搜索 / 取直链注册成 MCP 工具，别的节点可以反过来调这台手机。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CloudDriveScreen(onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    var conns by remember { mutableStateOf(CloudDrivePrefs.load()) }
    var sel by remember { mutableStateOf(0) }
    var path by remember { mutableStateOf("/") }
    var items by remember { mutableStateOf<List<FileItem>>(emptyList()) }
    var mode by remember { mutableStateOf("browse") }
    var kw by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var err by remember { mutableStateOf("") }
    var showAdd by remember { mutableStateOf(false) }
    var confirmDel by remember { mutableStateOf(false) }
    var fileAction by remember { mutableStateOf<FileItem?>(null) }
    var fileLink by remember { mutableStateOf("") }
    var resolving by remember { mutableStateOf(false) }
    var manageHost by remember { mutableStateOf("") }
    var discovered by remember { mutableStateOf<List<CloudDriveNodes.Hint>>(emptyList()) }
    var discovering by remember { mutableStateOf(false) }

    fun loadDir(p: String) {
        val c = conns.getOrNull(sel) ?: return
        busy = true; err = ""
        scope.launch {
            runCatching { CloudDriveApi.list(c, p) }
                .onSuccess { items = it; path = p; mode = "browse" }
                .onFailure { err = it.message ?: "列出失败" }
            busy = false
        }
    }

    fun doSearch(k: String) {
        val c = conns.getOrNull(sel) ?: return
        if (k.isBlank()) return
        busy = true; err = ""
        scope.launch {
            runCatching { CloudDriveApi.search(c, k) }
                .onSuccess { items = it; mode = "search" }
                .onFailure { err = it.message ?: "搜索失败" }
            busy = false
        }
    }

    fun findNodes() {
        discovering = true; err = ""
        scope.launch {
            runCatching { CloudDriveNodes.discover() }
                .onSuccess { discovered = it }
                .onFailure { err = "发现节点失败：${it.message}" }
            discovering = false
        }
    }

    fun addConn(c: CloudConn) {
        val list = conns + c
        CloudDrivePrefs.saveAll(list)
        conns = list
        sel = list.lastIndex
        showAdd = false
        busy = true; err = ""
        scope.launch {
            runCatching { CloudDriveApi.probe(c) }
                .onSuccess { loadDir("/") }
                .onFailure { err = it.message ?: "连接失败" }
            busy = false
        }
    }

    fun removeCurrent() {
        val list = conns.toMutableList().also { if (it.isNotEmpty()) it.removeAt(sel) }
        CloudDrivePrefs.saveAll(list); conns = list
        if (sel >= list.size) sel = list.lastIndex.coerceAtLeast(0)
        items = emptyList(); path = "/"; confirmDel = false; err = ""
        if (list.isNotEmpty()) loadDir("/")
    }

    fun openFile(f: FileItem) {
        val c = conns.getOrNull(sel) ?: return
        fileAction = f; fileLink = ""; resolving = true; err = ""
        scope.launch {
            runCatching { CloudDriveApi.fileUrl(c, f.path) }
                .onSuccess { fileLink = it }
                .onFailure { err = "取直链失败：${it.message}" }
            resolving = false
        }
    }

    fun download() {
        val f = fileAction ?: return
        if (fileLink.isBlank()) return
        runCatching {
            val dm = App.ctx.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            val req = DownloadManager.Request(Uri.parse(fileLink))
                .setTitle(f.name)
                .setDescription("天网 · 网盘聚合")
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, f.name)
            dm.enqueue(req)
            Toast.makeText(App.ctx, "已开始下载：${f.name}", Toast.LENGTH_SHORT).show()
            fileAction = null
        }.onFailure { err = "下载失败：${it.message}" }
    }

    fun copyLink() {
        val cm = App.ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("网盘直链", fileLink))
        Toast.makeText(App.ctx, "直链已复制", Toast.LENGTH_SHORT).show()
    }

    LaunchedEffect(Unit) {
        if (conns.isNotEmpty()) loadDir("/")
        runCatching { discovered = CloudDriveNodes.discover() }
    }

    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            IconButton(onClick = onBack) { Icon(Icons.Filled.ArrowBack, "返回") }
            Text(
                "网盘聚合", fontSize = 16.sp, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.weight(1f),
            )
            if (conns.isNotEmpty()) {
                IconButton(onClick = { confirmDel = true }) {
                    Icon(Icons.Filled.Delete, "删除当前节点")
                }
            }
            IconButton(onClick = { showAdd = true }) { Icon(Icons.Filled.Add, "添加节点") }
        }

        // 节点操作条：发现 / 管理，两个动作对任意节点都一样，没有「主设备」。
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = { findNodes() }, enabled = !discovering) {
                Text(if (discovering) "发现中…" else "发现节点", fontSize = 11.5.sp)
            }
            TextButton(
                enabled = conns.getOrNull(sel)?.type != "webdav" && conns.isNotEmpty(),
                onClick = { manageHost = conns[sel].host },
            ) { Text("在本机登录 / 管理网盘", fontSize = 11.5.sp) }
        }
        if (discovered.isNotEmpty()) {
            Row(
                Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("可达节点：", fontSize = 10.5.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                discovered.forEach { h ->
                    FilterChip(
                        selected = false,
                        onClick = { showAdd = true; },
                        label = { Text("${h.name} ${h.host}", fontSize = 10.sp) },
                    )
                    Spacer(Modifier.width(4.dp))
                }
            }
        }

        if (conns.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        "还没有节点连接，点右上 ＋ 添加，或点「发现节点」自动找。",
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        "所有设备都是对等节点：任意一台都可以跑自己的聚合服务，\n" +
                            "任意一台都能调用其它节点；节点地址留空不影响本机其它能力。",
                        fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp, start = 24.dp, end = 24.dp),
                    )
                }
            }
        } else {
            Row(
                Modifier.horizontalScroll(rememberScrollState()).padding(horizontal = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                conns.forEachIndexed { i, c ->
                    FilterChip(
                        selected = sel == i,
                        onClick = { sel = i; loadDir("/") },
                        label = { Text(c.name.ifBlank { c.host }) },
                    )
                    Spacer(Modifier.width(6.dp))
                }
            }
            OutlinedTextField(
                kw, { kw = it },
                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
                placeholder = { Text("搜索文件名（节点没索引时自动遍历目录）") },
                trailingIcon = {
                    IconButton(onClick = { doSearch(kw) }) { Icon(Icons.Filled.Search, "搜") }
                },
                singleLine = true,
            )
            if (mode == "browse" && path != "/") {
                Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp)) {
                    Text(
                        path, fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = { loadDir(parentOf(path)) }) { Text("上级") }
                }
            }
            if (mode == "search") {
                TextButton(onClick = { loadDir("/") }) { Text("← 返回浏览") }
            }
            if (busy || resolving || discovering) {
                Box(Modifier.fillMaxWidth().padding(8.dp), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp)
                }
            }
            if (err.isNotBlank()) {
                Text(
                    err, fontSize = 11.sp, color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 4.dp),
                )
            }
            if (!busy && items.isEmpty() && err.isBlank()) {
                Text(
                    "这里还没有内容。点「在本机登录 / 管理网盘」即可在这台节点上添加阿里/百度/夸克等网盘。",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
                )
            }
            LazyColumn(Modifier.fillMaxWidth().weight(1f)) {
                items(items) { f ->
                    Row(
                        Modifier.fillMaxWidth()
                            .clickable { if (f.isDir) loadDir(f.path) else openFile(f) }
                            .padding(horizontal = 14.dp, vertical = 11.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Icon(
                            if (f.isDir) Icons.Filled.Folder else Icons.Filled.InsertDriveFile, null,
                            Modifier.size(20.dp),
                            tint = MaterialTheme.colorScheme.primary,
                        )
                        Spacer(Modifier.width(10.dp))
                        Column(Modifier.weight(1f)) {
                            Text(f.name, fontSize = 13.5.sp, maxLines = 1)
                            Text(
                                if (f.isDir) "目录 · 点开下钻" else "${humanSize(f.size)} · 点开下载",
                                fontSize = 11.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
            }
        }
    }

    if (showAdd) {
        AddConnDialog(
            hints = discovered,
            onDismiss = { showAdd = false },
            onAdd = { addConn(it) },
            onDiscover = { findNodes() },
        )
    }
    if (confirmDel) {
        AlertDialog(
            onDismissRequest = { confirmDel = false },
            title = { Text("删除这个节点连接？") },
            text = { Text("只删除本机保存的连接信息，不会动节点或网盘里的任何文件。") },
            confirmButton = { TextButton(onClick = { removeCurrent() }) { Text("删除") } },
            dismissButton = { TextButton(onClick = { confirmDel = false }) { Text("取消") } },
        )
    }
    fileAction?.let { f ->
        AlertDialog(
            onDismissRequest = { fileAction = null },
            title = { Text(f.name, fontSize = 15.sp) },
            text = {
                Column {
                    Text("大小：${humanSize(f.size)}", fontSize = 12.sp)
                    Text("路径：${f.path}", fontSize = 11.sp)
                    if (resolving) Text("正在取下载直链…", fontSize = 11.sp)
                    if (fileLink.isNotBlank()) {
                        Text(
                            "直链已就绪，下载会存到手机的「下载」目录。",
                            fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.padding(top = 6.dp),
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(onClick = { download() }, enabled = fileLink.isNotBlank()) {
                    Text("下载到手机")
                }
            },
            dismissButton = {
                Row {
                    TextButton(onClick = { copyLink() }, enabled = fileLink.isNotBlank()) {
                        Text("复制链接")
                    }
                    TextButton(onClick = { fileAction = null }) { Text("关闭") }
                }
            },
        )
    }
    if (manageHost.isNotBlank()) {
        NodeManageDialog(host = manageHost, onDismiss = { manageHost = "" })
    }
}

/**
 * 节点管理页：内嵌 WebView，登录（扫码 / 短信验证码 / 密码）与添加网盘都在**手机本机**完成。
 * 显式白底 + 加载进度 + 失败重试，避免首帧未到时露出深色 window 被误认成黑屏。
 */
@Composable
private fun NodeManageDialog(host: String, onDismiss: () -> Unit) {
    var progress by remember { mutableStateOf(0) }
    var failed by remember { mutableStateOf(false) }
    var web by remember { mutableStateOf<WebView?>(null) }
    val url = CloudDriveNodes.manageUrl(host)
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("节点管理 · 登录网盘", fontSize = 15.sp) },
        text = {
            Column(Modifier.fillMaxWidth()) {
                Text(
                    "在这台节点上添加阿里/百度/夸克/115 等网盘，扫码或短信验证码都在本机完成。",
                    fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(
                    host, fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(vertical = 3.dp),
                )
                if (progress < 100 && !failed) {
                    LinearProgressIndicator(Modifier.fillMaxWidth().height(2.dp))
                    Text("正在打开节点管理页…", fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (failed) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("这个页面没加载出来。", fontSize = 11.sp,
                            color = MaterialTheme.colorScheme.error,
                            modifier = Modifier.weight(1f))
                        TextButton(onClick = {
                            failed = false; progress = 0
                            runCatching { web?.reload() }
                        }) { Text("重试", fontSize = 11.sp) }
                    }
                }
                AndroidView(
                    modifier = Modifier.fillMaxWidth().height(400.dp).padding(top = 4.dp),
                    factory = { c ->
                        WebView(c).apply {
                            settings.apply {
                                javaScriptEnabled = true
                                domStorageEnabled = true
                                loadsImagesAutomatically = true
                                useWideViewPort = true
                                loadWithOverviewMode = true
                                setSupportZoom(true)
                                builtInZoomControls = true
                                displayZoomControls = false
                                allowFileAccess = false
                                mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
                            }
                            setBackgroundColor(android.graphics.Color.WHITE)
                            webChromeClient = object : WebChromeClient() {
                                override fun onProgressChanged(view: WebView, p: Int) { progress = p }
                            }
                            webViewClient = object : WebViewClient() {
                                override fun shouldOverrideUrlLoading(
                                    view: WebView, req: WebResourceRequest,
                                ): Boolean {
                                    val s = req.url.scheme?.lowercase()
                                    return s != "http" && s != "https"
                                }

                                override fun onReceivedError(
                                    view: WebView, req: WebResourceRequest, e: WebResourceError,
                                ) {
                                    if (req.isForMainFrame) failed = true
                                }

                                override fun onPageFinished(view: WebView, u: String?) {
                                    if (progress >= 100) failed = false
                                }
                            }
                            loadUrl(url)
                        }.also { web = it }
                    },
                    onRelease = { w ->
                        runCatching { w.stopLoading(); w.destroy() }
                        web = null
                    },
                )
            }
        },
        confirmButton = { TextButton(onClick = {
            runCatching { web?.reload() }
        }) { Text("刷新") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("关闭") } },
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddConnDialog(
    hints: List<CloudDriveNodes.Hint>,
    onDismiss: () -> Unit,
    onAdd: (CloudConn) -> Unit,
    onDiscover: () -> Unit,
) {
    var type by remember { mutableStateOf("openlist") }
    var name by remember { mutableStateOf("") }
    var host by remember { mutableStateOf("") }
    var user by remember { mutableStateOf("") }
    var pass by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("添加节点") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    FilterChip(
                        selected = type == "openlist",
                        onClick = { type = "openlist" },
                        label = { Text("OpenList") },
                    )
                    FilterChip(
                        selected = type == "webdav",
                        onClick = { type = "webdav" },
                        label = { Text("WebDAV") },
                    )
                }
                if (hints.isNotEmpty()) {
                    Text("发现到的对等节点（点一下填地址）：", fontSize = 10.5.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 6.dp))
                    Column {
                        hints.forEach { h ->
                            TextButton(onClick = {
                                host = h.host
                                if (name.isBlank()) name = h.name
                            }) {
                                Text("${h.name}  ${h.host}", fontSize = 10.5.sp)
                            }
                        }
                    }
                }
                TextButton(onClick = { onDiscover() }) { Text("重新发现节点", fontSize = 10.5.sp) }
                OutlinedTextField(
                    name, { name = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("名称，如 卧室节点 / 随身节点") }, singleLine = true,
                )
                OutlinedTextField(
                    host, { host = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("任意节点地址，如 http://192.168.1.5:5244") },
                    singleLine = true,
                )
                OutlinedTextField(
                    user, { user = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("账号 / 用户名") }, singleLine = true,
                )
                OutlinedTextField(
                    pass, { pass = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("密码（只存本机）") },
                    singleLine = true, visualTransformation = PasswordVisualTransformation(),
                )
                Text(
                    "所有设备都是对等节点，没有主设备：任意一台都能跑聚合服务，\n" +
                        "也能被其它设备调用。地址留空不影响本机其它能力。\n" +
                        "网盘账号在点「在本机登录 / 管理网盘」后用手机内嵌页面登录。",
                    fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(
                enabled = host.isNotBlank(),
                onClick = { onAdd(CloudConn(type, name.ifBlank { host }, host, user, pass)) },
            ) { Text("添加并测试") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

private fun humanSize(b: Long): String = when {
    b < 1024 -> "$b B"
    b < 1024 * 1024 -> "${b / 1024} KB"
    b < 1024 * 1024 * 1024 -> "${b / 1024 / 1024} MB"
    else -> "${b / 1024 / 1024 / 1024} GB"
}

private fun parentOf(p: String): String {
    val t = p.trimEnd('/')
    val i = t.lastIndexOf('/')
    return if (i <= 0) "/" else t.substring(0, i)
}
