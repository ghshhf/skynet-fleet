package app.skynet.tianwang.next

import android.content.ClipData
import android.content.ClipboardManager
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
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

/**
 * 代码工作台：在手机上写一段简单代码，点运行，立刻看到输出。
 *
 * 对照 Horus / JCode 的「手机上自己写代码」形态，但执行层是自包含的：
 * 默认用 Android 自带 Linux shell，装了 Alpine + proot 就自动升级到完整用户态。
 * 写过的文件落工作区、运行留历史，都能被局域网对端通过 self_code_* 工具复用。
 */
@Composable
fun CodeWorkbenchScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var name by remember { mutableStateOf("check.sh") }
    var lang by remember { mutableStateOf(Lang.SH) }
    var code by remember { mutableStateOf(CodeTemplates.all[0].code) }
    var args by remember { mutableStateOf("") }
    var timeoutText by remember { mutableStateOf("15000") }
    var output by remember { mutableStateOf("还没运行。选个模板，点「运行」就有结果。") }
    var running by remember { mutableStateOf(false) }
    var notice by remember { mutableStateOf("") }

    val files = remember(tick) { CodeEngine.listFiles() }
    val status = remember(tick) { LinuxRuntime.status() }

    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(14.dp),
    ) {
        FeatureHeader("代码工作台", "手机里写代码、跑脚本、看输出。不用电脑、不用 Termux、不用 root。")
        Spacer(Modifier.height(8.dp))

        FeatureCard(highlighted = true) {
            Text("当前引擎：${status.engineLabel}", fontSize = 13.sp,
                color = MaterialTheme.colorScheme.primary)
            Spacer(Modifier.height(4.dp))
            Text(status.note, fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(4.dp))
            Text("工作区 ${status.workspaceBytes / 1024} KB · 运行记录 ${CodeEngine.history().size} 条",
                fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            FeatureActions(
                "运行时设置" to { onOpen("RUNTIME") },
                "工作区文件" to { onOpen("WORKSPACE") },
                "运行历史" to { onOpen("CODEHISTORY") },
            )
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("一键填入示例（都是真能跑的）")
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            CodeTemplates.all.forEach { t ->
                TextButton(onClick = {
                    name = t.name
                    lang = t.lang
                    code = t.code
                    notice = "已填入「${t.label}」"
                }) { Text(t.label, fontSize = 11.sp) }
                Spacer(Modifier.width(4.dp))
            }
        }

        Spacer(Modifier.height(8.dp))
        FeatureSection("语言")
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            Lang.entries.forEach { l ->
                TextButton(onClick = { lang = l }) {
                    Text(if (lang == l) "● ${l.label}" else l.label, fontSize = 11.5.sp,
                        color = if (lang == l) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                }
            }
        }
        if (lang.needsLinux) {
            Text("${lang.label} 需要完整 Linux 用户态（Alpine + proot）；未装时会明确报错，不会假装跑过。",
                fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(8.dp))
        FeatureSection("已保存的文件（点一下载入）")
        if (files.isEmpty()) {
            Text("还没有文件，运行或保存后会出现在这里。", fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else {
            Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
                files.forEach { f ->
                    val n = f["name"].orEmpty()
                    TextButton(onClick = {
                        name = n
                        lang = Lang.of(f["lang"].orEmpty())
                        code = CodeEngine.readFile(n)
                        notice = "已载入 $n"
                    }) { Text(n, fontSize = 11.sp) }
                }
            }
        }

        Spacer(Modifier.height(8.dp))
        FeatureField(value = name, onChange = { name = it }, label = "文件名")
        Spacer(Modifier.height(6.dp))
        FeatureField(value = args, onChange = { args = it }, label = "运行参数（可空）")
        Spacer(Modifier.height(6.dp))
        FeatureField(value = timeoutText, onChange = { timeoutText = it }, label = "超时毫秒")

        Spacer(Modifier.height(8.dp))
        FeatureSection("代码")
        OutlinedTextField(
            value = code,
            onValueChange = { code = it },
            modifier = Modifier.fillMaxWidth().height(250.dp),
            textStyle = TextStyle(fontSize = 11.5.sp, lineHeight = 16.sp),
            label = { Text("在这里写", fontSize = 10.sp) },
        )

        Spacer(Modifier.height(6.dp))
        if (notice.isNotBlank()) {
            Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
        }
        FeatureActions(
            "运行" to {
                if (!running) {
                    val t = timeoutText.toIntOrNull()?.coerceIn(200, 60_000) ?: 15_000
                    running = true
                    output = "执行中…"
                    val n = name; val l = lang; val c = code; val a = args
                    scope.launch(Dispatchers.IO) {
                        val r = CodeEngine.run(n, l, c, a, t)
                        withContext(Dispatchers.Main) {
                            output = "引擎=${r.engine} · ${r.ms}ms · " +
                                (if (r.ok) "成功" else "未成功") + "\n" + r.output
                            running = false
                            tick++
                            notice = "运行完成，已存入工作区与运行历史"
                        }
                    }
                }
            },
            "保存" to {
                val saved = CodeEngine.saveFile(name, lang, code)
                notice = "已保存 $saved"
                tick++
            },
            "删除" to {
                CodeEngine.deleteFile(name)
                notice = "已删除 $name"
                tick++
            },
            "网页预览" to { onOpen("MINIAPP") },
            "复制输出" to {
                val cm = App.ctx.getSystemService(ClipboardManager::class.java)
                cm?.setPrimaryClip(ClipData.newPlainText("天网运行输出", output))
                notice = "输出已复制到剪贴板"
            },
        )

        Spacer(Modifier.height(8.dp))
        FeatureSection("输出")
        FeatureCard {
            Text(output, fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurface)
        }

        Spacer(Modifier.height(10.dp))
        FeatureCard {
            Text("给对端用：电脑 / 另一台手机在局域网里调 self_code_run、self_code_command、" +
                "self_code_files、self_code_task，就能让这台手机自己写代码、跑脚本、跑一组任务。",
                fontSize = 11.sp, lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
            FeatureActions("自主任务" to { onOpen("TASKAGENT") })
        }
        Spacer(Modifier.height(20.dp))
    }
}
