package app.skynet.tianwang.next

import android.os.Handler
import android.os.Looper
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import java.io.File

/** 工作区文件：代码、脚本、任务产物都落在这里，对端也能读走。 */
private fun exchangeDir(): File {
    val base = App.ctx.getExternalFilesDir(null) ?: App.ctx.filesDir
    return File(base, "lanfiles").apply { mkdirs() }
}

private fun exportToExchange(name: String, text: String): String {
    val clean = name.trim().replace(Regex("[^A-Za-z0-9_.-]"), "_").ifBlank { "file.txt" }
    File(exchangeDir(), clean).writeText(text, Charsets.UTF_8)
    return clean
}

@Composable
fun WorkspaceScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var newName by remember { mutableStateOf("new.sh") }
    var newCode by remember { mutableStateOf("echo 'hello from phone'\nls -la") }
    var notice by remember { mutableStateOf("") }
    var command by remember { mutableStateOf("") }

    val files = remember(tick) { CodeEngine.listFiles() }
    val status = remember(tick) { LinuxRuntime.status() }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("工作区文件", "手机本地的私有目录：代码、脚本、任务产物都在这里，卸载 App 才清。")
        Spacer(Modifier.height(8.dp))
        FeatureCard(highlighted = true) {
            FeatureKV("路径", LinuxRuntime.workspace().absolutePath)
            FeatureKV("占用", "${status.workspaceBytes / 1024} KB")
            FeatureKV("文件数", "${files.size}")
            Spacer(Modifier.height(4.dp))
            Text("导出到交换目录后，对端用 self_file_read 就能取走；也可以在工作台里直接打开继续改。",
                fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("跑一条临时命令")
        FeatureField(value = command, onChange = { command = it }, label = "例如：ls -la / uname -a / cat manifest.txt")
        FeatureActions(
            "执行" to {
                if (command.isNotBlank()) {
                    val c = command
                    scope.launch(Dispatchers.IO) {
                        val out = CodeEngine.runCommand(c, 15_000).output
                        withContext(Dispatchers.Main) {
                            notice = out
                            tick++
                        }
                    }
                }
            },
            "回工作台" to { onOpen("WORKBENCH") },
        )
        if (notice.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            FeatureCard { Text(notice, fontSize = 10.5.sp, lineHeight = 15.sp) }
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("新建文件")
        FeatureField(value = newName, onChange = { newName = it }, label = "文件名")
        Spacer(Modifier.height(6.dp))
        OutlinedTextField(
            value = newCode,
            onValueChange = { newCode = it },
            modifier = Modifier.fillMaxWidth().height(120.dp),
            textStyle = TextStyle(fontSize = 11.sp),
            label = { Text("内容", fontSize = 10.sp) },
        )
        FeatureActions(
            "保存" to {
                val n = CodeEngine.saveFile(newName, Lang.SH, newCode)
                notice = "已保存 $n"
                tick++
            },
            "导出" to {
                notice = "已导出 ${exportToExchange(newName, newCode)}"
            },
        )

        Spacer(Modifier.height(10.dp))
        FeatureSection("已存文件（${files.size}）")
        if (files.isEmpty()) {
            Text("还没有文件。", fontSize = 11.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            files.forEach { f ->
                val n = f["name"].orEmpty()
                val text = CodeEngine.readFile(n)
                FeatureCard {
                    Text(n, fontSize = 12.5.sp, color = MaterialTheme.colorScheme.onSurface)
                    Text("${f["lang"]} · ${text.length} 字 · ${text.lines().size} 行",
                        fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(4.dp))
                    Text(text.take(140), fontSize = 10.sp, maxLines = 3,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    FeatureActions(
                        "导出" to { notice = "已导出 ${exportToExchange(n, text)}" },
                        "删除" to { CodeEngine.deleteFile(n); tick++; notice = "已删除 $n" },
                    )
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}

@Composable
fun RuntimeSetupScreen() {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var busy by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf("") }
    var confirmReset by remember { mutableStateOf(false) }

    val s = remember(tick) { LinuxRuntime.status() }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("Linux 运行时", "App 自带 Linux 用户态：不装 Termux、不 root、不连电脑。")
        Spacer(Modifier.height(8.dp))

        FeatureCard(highlighted = true) {
            Text("当前引擎：${s.engineLabel}", fontSize = 14.sp,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(5.dp))
            Text(s.note, fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(6.dp))
            FeatureKV("架构", if (s.archSupported) s.arch else "${s.arch}（不支持）")
            FeatureKV("Alpine rootfs", if (s.rootfsReady) "已就绪" else "未安装")
            FeatureKV("PRoot", "${if (s.prootReady) "有 · " else "无 · "}${s.prootSource}")
            FeatureKV("镜像源", s.mirror)
            FeatureKV("可用空间", "${s.spaceFreeBytes / 1024 / 1024} MB")
            val py = s.available["python3"]; val nd = s.available["node"]
            FeatureKV("python3",
                if (s.engine == "alpine-proot") (if (py == true) "已装" else "需 apk add") else "仅完整发行版")
            FeatureKV("node",
                if (s.engine == "alpine-proot") (if (nd == true) "已装" else "需 apk add") else "仅完整发行版")
            FeatureKV("下载包", "${s.archiveBytes / 1024} KB")
            FeatureKV("用户态", "${s.rootfsBytes / 1024} KB")
            FeatureKV("工作区", "${s.workspaceBytes / 1024} KB")
            if (s.sha.isNotBlank()) FeatureKV("SHA-256", s.sha.take(16) + "…")
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("安装 / 升级 Linux 用户态")
        FeatureCard {
            Text("来源：Alpine 官方 CDN，自动按本机架构选镜像（${if (s.archSupported) s.arch else "不支持"}）" +
                "，清华 / 阿里云镜像兜底（约 3.8 MB）。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.height(3.dp))
            Text(s.rootfsUrl, fontSize = 9.5.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(3.dp))
            Text("自适应：装之前先看剩余空间，架构不支持或空间不足会直接拒；" +
                "校验取官方 .sha256 交叉核对，不通过就不解压，离线取不到校验值也拒装。",
                fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(4.dp))
            if (progress.isNotBlank()) {
                Text(progress, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
            }
            FeatureActions(
                "一键安装" to {
                    if (!busy) {
                        busy = true
                        progress = "开始…"
                        val handler = Handler(Looper.getMainLooper())
                        scope.launch(Dispatchers.IO) {
                            val ok = LinuxRuntime.install { line ->
                                handler.post { progress = line }
                            }
                            withContext(Dispatchers.Main) {
                                busy = false
                                tick++
                                progress = if (ok) "安装完成" else "安装未完成，见上方提示"
                            }
                        }
                    }
                },
                "清理下载包" to {
                    LinuxRuntime.clearArchive()
                    tick++
                    progress = "已删除下载包（工作区文件保留）"
                },
            )
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("它到底怎么跑起来的")
        FeatureCard {
            Text("① 默认层：Android 本身就是 Linux 内核，/system/bin/sh + toybox 开箱可用。" +
                "写 shell 脚本、处理文本、整理文件这一层离线就能跑，不需要下载任何东西。",
                fontSize = 11.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.height(5.dp))
            Text("② 增强层：装上 Alpine 用户态、且 APK 里提供 libproot.so 后，PRoot 把命令" +
                "放进完整 Linux 用户态执行，python3 / node / apk 都能用。缺 proot 时状态会直说，" +
                "不会拿 Android shell 冒充发行版。",
                fontSize = 11.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurface)
            Spacer(Modifier.height(5.dp))
            Text("③ 对端层：self_code_* 工具让电脑 / 另一台手机通过局域网直接让这台手机" +
                "写代码、跑脚本、跑任务，手机是完整对等节点，不依附 PC。",
                fontSize = 11.sp, lineHeight = 16.sp, color = MaterialTheme.colorScheme.onSurface)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("危险操作")
        FeatureActions(
            "重置全部运行时" to { confirmReset = true },
        )
        Spacer(Modifier.height(20.dp))
    }

    if (confirmReset) {
        AlertDialog(
            onDismissRequest = { confirmReset = false },
            title = { Text("重置全部运行时？") },
            text = {
                Text("会删除 Linux 用户态、下载包以及工作区里所有代码文件（不可恢复）。" +
                    "只清下载包请用上面的「清理下载包」。")
            },
            confirmButton = {
                TextButton(onClick = {
                    LinuxRuntime.resetAll()
                    CodeEngine.clearHistory()
                    KB.put(CodeEngine.K_FILES, JSONArray())
                    confirmReset = false
                    tick++
                    progress = "运行时已重置"
                }) { Text("确认重置") }
            },
            dismissButton = {
                TextButton(onClick = { confirmReset = false }) { Text("取消") }
            },
        )
    }
}

@Composable
fun CodeHistoryScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var expanded by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf("") }
    val history = remember(tick) { CodeEngine.history() }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp),
    ) {
        FeatureHeader("运行历史", "每次跑代码/命令都留一条，能回看、能重跑、能清空。")
        Spacer(Modifier.height(8.dp))
        FeatureActions(
            "清空" to { CodeEngine.clearHistory(); KB.put(CodeEngine.K_TASK_RUNS, JSONArray()); tick++ },
            "回工作台" to { onOpen("WORKBENCH") },
            "自主任务" to { onOpen("TASKAGENT") },
        )
        if (notice.isNotBlank()) {
            Spacer(Modifier.height(4.dp))
            Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
        }

        Spacer(Modifier.height(8.dp))
        if (history.isEmpty()) {
            Text("还没有运行记录。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            history.take(60).forEach { h ->
                val id = h["id"].orEmpty()
                FeatureCard(onClick = { expanded = if (expanded == id) "" else id }) {
                    Text("${h["name"]} · ${h["lang"]} · ${h["engine"]}",
                        fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurface)
                    Text("${h["ms"]}ms · " + if (h["ok"] == "true") "成功" else "未成功",
                        fontSize = 10.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    if (expanded == id) {
                        Spacer(Modifier.height(5.dp))
                        Text("输出：", fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(h["output"].orEmpty().take(1200), fontSize = 10.sp, lineHeight = 14.sp)
                        Spacer(Modifier.height(4.dp))
                        Text("代码：", fontSize = 10.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                        Text(h["code"].orEmpty().take(800), fontSize = 10.sp, lineHeight = 14.sp)
                        FeatureActions(
                            "重跑" to {
                                val lang = Lang.of(h["lang"].orEmpty())
                                val n = h["name"].orEmpty()
                                val c = h["code"].orEmpty()
                                scope.launch(Dispatchers.IO) {
                                    val r = CodeEngine.run(n, lang, c)
                                    withContext(Dispatchers.Main) {
                                        notice = if (r.ok) "重跑成功（${r.ms}ms）" else r.output.take(160)
                                        tick++
                                    }
                                }
                            },
                        )
                    }
                }
                Spacer(Modifier.height(6.dp))
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}
