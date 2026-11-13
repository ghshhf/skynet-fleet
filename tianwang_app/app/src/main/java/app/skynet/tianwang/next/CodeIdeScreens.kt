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
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 编辑器配色：浅底深字，关键字/字符串/注释分开，不靠黑色背景撑对比。 */
private data class CodePalette(
    val plain: Color,
    val keyword: Color,
    val string: Color,
    val comment: Color,
    val number: Color,
    val lineNo: Color,
)

@Composable
private fun palette(): CodePalette = CodePalette(
    plain = MaterialTheme.colorScheme.onSurface,
    keyword = Color(0xFF6B3FA0),
    string = Color(0xFF0B6E6E),
    comment = Color(0xFF7A7A7A),
    number = Color(0xFFB45309),
    lineNo = MaterialTheme.colorScheme.onSurfaceVariant,
)

private val KEYWORDS = setOf(
    "for", "in", "do", "done", "if", "then", "else", "elif", "fi", "while", "case", "esac",
    "echo", "cd", "ls", "cat", "grep", "awk", "sed", "tar", "wc", "rm", "mkdir", "cp", "mv",
    "printf", "find", "head", "exit", "return", "function", "var", "let", "const", "def",
    "class", "import", "from", "as", "print", "console", "sh", "python3", "node", "git", "apk",
)

/** 把一行代码着成 AnnotatedString；按字符扫描，够用且不引第三方高亮库。 */
private fun highlight(line: String, p: CodePalette): AnnotatedString = buildAnnotatedString {
    var i = 0
    while (i < line.length) {
        val c = line[i]
        when {
            c == '#' || (c == '/' && i + 1 < line.length && line[i + 1] == '/') -> {
                withStyle(SpanStyle(color = p.comment)) { append(line.substring(i)) }
                return@buildAnnotatedString
            }
            c == '"' || c == '\'' -> {
                val quote = c
                var j = i + 1
                while (j < line.length && line[j] != quote) j++
                if (j < line.length) j++
                withStyle(SpanStyle(color = p.string)) { append(line.substring(i, j)) }
                i = j
            }
            c.isDigit() -> {
                var j = i
                while (j < line.length && (line[j].isDigit() || line[j] == '.')) j++
                withStyle(SpanStyle(color = p.number)) { append(line.substring(i, j)) }
                i = j
            }
            c.isLetter() || c == '_' -> {
                var j = i
                while (j < line.length && (line[j].isLetterOrDigit() || line[j] == '_')) j++
                val word = line.substring(i, j)
                withStyle(SpanStyle(color = if (word in KEYWORDS) p.keyword else p.plain)) {
                    append(word)
                }
                i = j
            }
            else -> {
                append(c)
                i++
            }
        }
    }
}

/** 带行号与着色的代码视图；只读展示，编辑仍在下面的输入框里。 */
@Composable
private fun CodeView(code: String, p: CodePalette, maxLines: Int = 80) {
    Column(Modifier.fillMaxWidth()) {
        code.lines().take(maxLines).forEachIndexed { idx, line ->
            Row {
                Text((idx + 1).toString().padStart(3, ' '), fontSize = 10.sp,
                    fontFamily = FontFamily.Monospace, color = p.lineNo,
                    modifier = Modifier.width(30.dp))
                Text(highlight(line, p), fontSize = 10.5.sp,
                    fontFamily = FontFamily.Monospace,
                    modifier = Modifier.weight(1f))
            }
        }
        if (code.lines().size > maxLines) {
            Text("…还有 ${code.lines().size - maxLines} 行未显示", fontSize = 10.sp, color = p.comment)
        }
    }
}

/**
 * 代码编辑器：多文件标签、行号着色预览、查找与替换全部、保存、运行。
 * 对照 JCode / Forge 的编辑器形态，但不引第三方编辑器库，保持零新依赖。
 */
@Composable
fun CodeEditorScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var tick by remember { mutableStateOf(0) }
    var name by remember { mutableStateOf("check.sh") }
    var lang by remember { mutableStateOf(Lang.SH) }
    var code by remember { mutableStateOf(CodeTemplates.all[0].code) }
    var query by remember { mutableStateOf("") }
    var replace by remember { mutableStateOf("") }
    var notice by remember { mutableStateOf("") }
    var output by remember { mutableStateOf("") }

    val files = remember(tick) { CodeEngine.listFiles() }
    val p = palette()

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp)) {
        FeatureHeader("代码编辑器", "多文件、行号着色、查找替换、直接运行。手机本地，零依赖。")
        Spacer(Modifier.height(8.dp))

        FeatureSection("文件标签")
        if (files.isEmpty()) {
            Text("暂无文件，运行一次代码就会保存。", fontSize = 11.sp,
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
                    }) {
                        Text(if (n == name) "● $n" else n, fontSize = 11.sp,
                            color = if (n == name) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.onSurface)
                    }
                    Spacer(Modifier.width(4.dp))
                }
            }
        }

        Spacer(Modifier.height(8.dp))
        FeatureField(value = name, onChange = { name = it }, label = "当前文件名")
        Spacer(Modifier.height(6.dp))
        Row(Modifier.fillMaxWidth()) {
            Lang.entries.forEach { l ->
                TextButton(onClick = { lang = l }) {
                    Text(if (l == lang) "● ${l.label}" else l.label, fontSize = 10.5.sp,
                        color = if (l == lang) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                }
            }
        }

        Spacer(Modifier.height(8.dp))
        FeatureSection("查找 / 替换")
        FeatureField(value = query, onChange = { query = it }, label = "查找")
        Spacer(Modifier.height(5.dp))
        FeatureField(value = replace, onChange = { replace = it }, label = "替换为")
        FeatureActions(
            "统计匹配" to {
                notice = if (query.isBlank()) "先填查找内容" else
                    "匹配 ${code.split(query).size - 1} 处 · ${code.lines().count { it.contains(query) }} 行"
            },
            "替换全部" to {
                if (query.isNotBlank()) {
                    val before = code.split(query).size - 1
                    code = code.replace(query, replace)
                    notice = "已替换 $before 处"
                }
            },
            "跳到工作台" to { onOpen("WORKBENCH") },
        )

        Spacer(Modifier.height(8.dp))
        FeatureSection("带行号的着色视图（只读）")
        FeatureCard {
            Column(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
                CodeView(code, p)
            }
        }

        Spacer(Modifier.height(8.dp))
        FeatureSection("编辑")
        OutlinedTextField(
            value = code,
            onValueChange = { code = it },
            modifier = Modifier.fillMaxWidth().height(220.dp),
            textStyle = TextStyle(fontSize = 11.sp, fontFamily = FontFamily.Monospace),
        )
        Spacer(Modifier.height(6.dp))
        if (notice.isNotBlank()) {
            Text(notice, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.primary)
        }
        FeatureActions(
            "运行" to {
                val n = name; val l = lang; val c = code
                scope.launch(Dispatchers.IO) {
                    val r = CodeEngine.run(n, l, c)
                    withContext(Dispatchers.Main) {
                        output = "引擎=${r.engine} · ${r.ms}ms\n${r.output}"
                        tick++
                        notice = "运行完成"
                    }
                }
            },
            "保存" to { CodeEngine.saveFile(name, lang, code); tick++; notice = "已保存 $name" },
            "复制" to {
                val cm = App.ctx.getSystemService(ClipboardManager::class.java)
                cm?.setPrimaryClip(ClipData.newPlainText("天网代码", code))
                notice = "已复制代码"
            },
            "会话终端" to { onOpen("SETERMINAL") },
        )
        Spacer(Modifier.height(8.dp))
        if (output.isNotBlank()) {
            FeatureCard { Text(output, fontSize = 10.5.sp, lineHeight = 15.sp) }
        }
        Spacer(Modifier.height(20.dp))
    }
}

/** 会话式终端：命令一条接一条跑在同一工作区，带历史回溯与快捷命令条。 */
@Composable
fun SessionTerminalScreen() {
    val scope = rememberCoroutineScope()
    var cmd by remember { mutableStateOf("") }
    var output by remember { mutableStateOf("$ 就绪。当前引擎：" + LinuxRuntime.status().engineLabel + "\n") }
    var busy by remember { mutableStateOf(false) }
    val history = remember { mutableStateListOf<String>() }
    var cursor by remember { mutableStateOf(-1) }

    fun submit(text: String) {
        if (text.isBlank() || busy) return
        busy = true
        history.add(text)
        cursor = history.size
        cmd = ""
        output += "\n$ $text\n"
        scope.launch(Dispatchers.IO) {
            val r = CodeEngine.runCommand(text, 20_000)
            withContext(Dispatchers.Main) {
                output += r.output + "\n[${r.engine} · ${r.ms}ms]\n"
                busy = false
            }
        }
    }

    Column(Modifier.fillMaxSize().padding(14.dp)) {
        FeatureHeader("会话终端", "一条接一条地跑，命令历史可回溯，快捷命令一点即填。")
        Spacer(Modifier.height(8.dp))
        FeatureCard(highlighted = true) {
            Text(LinuxRuntime.status().note, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.height(8.dp))
        FeatureSection("快捷命令")
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            listOf("uname -a", "id", "pwd", "ls -la", "date", "echo \$PATH", "cat manifest.txt")
                .forEach { c ->
                    TextButton(onClick = { cmd = c }) { Text(c, fontSize = 10.5.sp) }
                    Spacer(Modifier.width(3.dp))
                }
        }
        Spacer(Modifier.height(6.dp))
        FeatureField(value = cmd, onChange = { cmd = it }, label = "输入命令")
        FeatureActions(
            "执行" to { submit(cmd) },
            "上一条" to {
                if (history.isNotEmpty()) {
                    cursor = (cursor - 1).coerceAtLeast(0)
                    cmd = history[cursor]
                }
            },
            "下一条" to {
                if (history.isNotEmpty()) {
                    cursor = (cursor + 1).coerceAtMost(history.size - 1)
                    cmd = history[cursor]
                }
            },
            "清屏" to { output = "$ 已清屏\n" },
            "复制输出" to {
                val cm = App.ctx.getSystemService(ClipboardManager::class.java)
                cm?.setPrimaryClip(ClipData.newPlainText("天网终端", output))
            },
        )
        Spacer(Modifier.height(8.dp))
        FeatureCard {
            Column(Modifier.fillMaxWidth().height(380.dp).verticalScroll(rememberScrollState())) {
                Text(output, fontSize = 10.5.sp, lineHeight = 15.sp,
                    fontFamily = FontFamily.Monospace,
                    color = MaterialTheme.colorScheme.onSurface)
            }
        }
        Spacer(Modifier.height(20.dp))
    }
}
