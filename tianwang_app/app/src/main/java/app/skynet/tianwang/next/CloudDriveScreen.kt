package app.skynet.tianwang.next

import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
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
import kotlinx.coroutines.launch

/**
 * 工具箱里的「网盘聚合」：添加几个网盘连接（OpenList 服务 / WebDAV），
 * 统一浏览、跨盘搜索。手机端做前端；聚合逻辑在服务端（OpenList）或各 WebDAV。
 *
 * 搜索在 OpenList 上全功能（/api/fs/search 跨所有挂载盘）；WebDAV 只支持浏览。
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

    fun loadDir(p: String) {
        if (conns.isEmpty()) return
        val c = conns[sel]
        busy = true; err = ""
        scope.launch {
            runCatching { CloudDriveApi.list(c, p) }
                .onSuccess { items = it; path = p; mode = "browse" }
                .onFailure { err = it.message ?: "列出失败" }
            busy = false
        }
    }

    fun doSearch(k: String) {
        if (conns.isEmpty() || k.isBlank()) return
        val c = conns[sel]
        busy = true; err = ""
        scope.launch {
            runCatching { CloudDriveApi.search(c, k) }
                .onSuccess { items = it; mode = "search" }
                .onFailure { err = it.message ?: "搜索失败" }
            busy = false
        }
    }

    fun addConn(c: CloudConn) {
        val list = conns + c
        CloudDrivePrefs.saveAll(list)
        conns = list
        sel = list.lastIndex
        showAdd = false
        scope.launch {
            runCatching { CloudDriveApi.probe(c) }
                .onSuccess { loadDir("/") }
                .onFailure { err = it.message ?: "连接失败" }
        }
    }

    fun removeAt(i: Int) {
        val list = conns.toMutableList().also { it.removeAt(i) }
        CloudDrivePrefs.saveAll(list); conns = list
        if (sel >= list.size) sel = list.lastIndex.coerceAtLeast(0)
        items = emptyList(); path = "/"
    }

    LaunchedEffect(Unit) { if (conns.isNotEmpty()) loadDir("/") }

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
            IconButton(onClick = { showAdd = true }) { Icon(Icons.Filled.Add, "添加网盘") }
        }

        if (conns.isEmpty()) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(
                    "还没有网盘连接。点右上 ＋ 添加 OpenList 服务或 WebDAV。",
                    fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
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
                placeholder = { Text("搜索（OpenList 跨盘，WebDAV 不支持）") },
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
            if (busy) {
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
            LazyColumn(Modifier.fillMaxWidth()) {
                items(items) { f ->
                    Row(
                        Modifier.fillMaxWidth()
                            .clickable { if (f.isDir) loadDir(f.path) }
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
                                if (f.isDir) "目录" else humanSize(f.size), fontSize = 11.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        if (f.isDir) {
                            IconButton(onClick = { removeAt(sel) }) {
                                Icon(Icons.Filled.Delete, "删除该连接", tint = MaterialTheme.colorScheme.onSurfaceVariant)
                            }
                        }
                    }
                }
            }
        }
    }

    if (showAdd) {
        AddConnDialog(onDismiss = { showAdd = false }, onAdd = { addConn(it) })
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AddConnDialog(onDismiss: () -> Unit, onAdd: (CloudConn) -> Unit) {
    var type by remember { mutableStateOf("openlist") }
    var name by remember { mutableStateOf("") }
    var host by remember { mutableStateOf("") }
    var user by remember { mutableStateOf("") }
    var pass by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("添加网盘连接") },
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
                OutlinedTextField(
                    name, { name = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("名称，如 我家 NAS") }, singleLine = true,
                )
                OutlinedTextField(
                    host, { host = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("地址，如 http://192.168.1.10:5244") }, singleLine = true,
                )
                OutlinedTextField(
                    user, { user = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("账号 / 用户名") }, singleLine = true,
                )
                OutlinedTextField(
                    pass, { pass = it }, modifier = Modifier.fillMaxWidth(),
                    placeholder = { Text("密码（OpenList 登录密码 / WebDAV 密码）") },
                    singleLine = true, visualTransformation = PasswordVisualTransformation(),
                )
                Text(
                    "OpenList：填主机+账号密码，自动登录取 token。WebDAV：填完整 DAV 地址+账号密码。",
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
