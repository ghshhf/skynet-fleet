package app.skynet.tianwang.next

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
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 工具链自检：把「这台手机现在到底能跑什么」一次说清，不说有其实没有的东西。 */
private val TOOL_LIST = listOf(
    "sh" to "Shell（默认层）",
    "ls" to "列目录",
    "cat" to "读文件",
    "grep" to "文本过滤",
    "awk" to "文本处理",
    "sed" to "流式编辑",
    "tar" to "解 rootfs / 打包",
    "wc" to "统计",
    "find" to "查找文件",
    "curl" to "联网取文件",
    "python3" to "Python（需 Linux 用户态）",
    "node" to "Node.js（需 Linux 用户态）",
    "git" to "Git（需 Linux 用户态）",
    "apk" to "Alpine 包管理（需 Linux 用户态）",
)

@Composable
fun ToolchainScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var report by remember { mutableStateOf("还没自检。点「跑一次工具自检」看真实可用清单。") }
    var pkg by remember { mutableStateOf("python3") }
    var busy by remember { mutableStateOf(false) }

    val s = remember { LinuxRuntime.status() }

    fun probe() {
        if (busy) return
        busy = true
        report = "自检中…"
        scope.launch(Dispatchers.IO) {
            val cmds = TOOL_LIST.joinToString(";") { (c, _) -> "command -v $c || echo \"MISSING $c\"" }
            val r = CodeEngine.runCommand(cmds, 20_000)
            val lines = r.output.lines()
            val text = buildString {
                append("引擎：${r.engine} · ${r.ms}ms\n")
                TOOL_LIST.forEach { (c, label) ->
                    val miss = lines.any { it.trim() == "MISSING $c" }
                    append(if (miss) "缺  " else "有  ").append(c.padEnd(9)).append(label).append('\n')
                }
                append("\n--- 原始输出 ---\n").append(r.output)
            }
            withContext(Dispatchers.Main) {
                report = text
                busy = false
            }
        }
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp)) {
        FeatureHeader("工具链自检", "这台手机现在到底能跑什么，一条条查出来，不拿「应该有」当「有」。")
        Spacer(Modifier.height(8.dp))
        FeatureCard(highlighted = true) {
            FeatureKV("引擎", s.engineLabel)
            FeatureKV("Alpine 用户态", if (s.rootfsReady) "已装" else "未装")
            FeatureKV("PRoot", if (s.prootReady) "有" else "无")
            Spacer(Modifier.height(4.dp))
            Text(s.note, fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(8.dp))
        FeatureActions(
            "跑一次工具自检" to { probe() },
            "生成诊断报告" to {
                scope.launch(Dispatchers.IO) {
                    val r = CodeEngine.runCommand("uname -a; id; pwd; ls -la; ${
                        TOOL_LIST.joinToString(";") { (c, _) -> "command -v $c || echo \"MISSING $c\"" }
                    }", 20_000)
                    CodeEngine.saveFile("diagnostics.txt", Lang.SH, r.output)
                    withContext(Dispatchers.Main) { report = "已写出 diagnostics.txt\n" + r.output }
                }
            },
            "Linux 运行时" to { onOpen("RUNTIME") },
            "会话终端" to { onOpen("SETERMINAL") },
        )

        Spacer(Modifier.height(8.dp))
        FeatureSection("自检结果")
        FeatureCard { Text(report, fontSize = 10.5.sp, lineHeight = 15.sp) }

        Spacer(Modifier.height(10.dp))
        FeatureSection("Alpine 包安装（仅完整 Linux 用户态可用）")
        FeatureField(value = pkg, onChange = { pkg = it }, label = "包名，例如 python3 / nodejs / git")
        FeatureActions(
            "apk add" to {
                if (!s.rootfsReady || !s.prootReady) {
                    report = "当前没有完整 Linux 用户态（需 rootfs + proot），apk 不可用。\n" +
                        "默认 Android shell 层无需装包，直接写 shell 脚本即可。"
                } else {
                    val p = pkg.trim().replace(Regex("[^A-Za-z0-9._+-]"), "")
                    scope.launch(Dispatchers.IO) {
                        val r = CodeEngine.runCommand("apk add --no-cache $p", 60_000)
                        withContext(Dispatchers.Main) { report = r.output }
                    }
                }
            },
        )
        Spacer(Modifier.height(6.dp))
        Text("说明：Android 自带 shell 层不需要装包；只有装了 Alpine + proot 之后，" +
            "python3 / node / git / apk 这些才从 rootfs 里来。缺什么状态里直说。",
            fontSize = 10.5.sp, lineHeight = 15.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(20.dp))
    }
}

/** 项目脚手架：一次生成一组真文件，工作区/工作台/对端都能看见与取走。 */
data class ScaffoldFile(val name: String, val lang: Lang, val code: String)
data class Scaffold(
    val key: String,
    val label: String,
    val sub: String,
    val files: List<ScaffoldFile>,
    val entry: String,
)

object Scaffolds {
    val all = listOf(
        Scaffold("site", "静态站点", "HTML + CSS + JS + 说明，可在小应用工坊预览", listOf(
            ScaffoldFile("site_index.html", Lang.HTML,
                "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>" +
                    "<link rel='stylesheet' href='site_style.css'><title>手机站点</title></head><body>" +
                    "<h1>手机里搭的站点</h1><p id='o'>还没点击</p>" +
                    "<button onclick=\"o.textContent='页面已运行'\">点我</button>" +
                    "<script src='site_app.js'></script></body></html>"),
            ScaffoldFile("site_style.css", Lang.SH,
                "body{font-family:sans-serif;padding:24px;background:#fafafa;color:#222}" +
                    "button{font-size:16px;padding:8px 16px}"),
            ScaffoldFile("site_app.js", Lang.NODE,
                "console.log('站点脚本已加载'); document.title = '手机站点（已运行）';"),
            ScaffoldFile("site_README.md", Lang.SH,
                "# 静态站点\n由天网项目脚手架在手机本地生成。\n把 site_index.html 复制进小应用工坊即可预览。"),
        ), "sh site_entry.sh"),
        Scaffold("pytool", "Python 小工具", "main.py + 示例数据 + 说明（需 Linux）", listOf(
            ScaffoldFile("py_main.py", Lang.PY,
                "import os\nprint('工作目录:', os.getcwd())\n" +
                    "try:\n    print('数据:', open('py_data.txt').read().strip())\n" +
                    "except Exception as e:\n    print('读数据失败:', e)\n"),
            ScaffoldFile("py_data.txt", Lang.SH, "手机本地生成的一行示例数据"),
            ScaffoldFile("py_README.md", Lang.SH,
                "# Python 小工具\n需要完整 Linux 用户态（Alpine + proot）才能跑 python3。"),
        ), "python3 py_main.py"),
        Scaffold("nodetool", "Node 脚本", "index.js + 说明（需 Linux）", listOf(
            ScaffoldFile("node_index.js", Lang.NODE,
                "console.log('Node:', process.version);\n" +
                    "console.log('平台:', process.platform, process.arch);\n" +
                    "console.log('1..5 平方:', [1,2,3,4,5].map(function(n){return n*n}));\n"),
            ScaffoldFile("node_README.md", Lang.SH,
                "# Node 脚本\n需要完整 Linux 用户态（Alpine + proot）才能跑 node。"),
        ), "node node_index.js"),
        Scaffold("shtools", "Shell 工具包", "run.sh + lib.sh + 说明，默认层直接能跑", listOf(
            ScaffoldFile("sh_lib.sh", Lang.SH,
                "#!/system/bin/sh\nsay() { echo \"[工具包] $1\"; }\n"),
            ScaffoldFile("sh_run.sh", Lang.SH,
                "#!/system/bin/sh\n. ./sh_lib.sh\n" +
                    "say '开始'\nsay \"当前目录: $(pwd)\"\nls -la | head -8\nsay '完成'\n"),
            ScaffoldFile("sh_README.md", Lang.SH,
                "# Shell 工具包\n默认 Android shell 层直接能跑：sh sh_run.sh"),
        ), "sh 'sh_run.sh'"),
    )

    fun of(key: String): Scaffold = all.firstOrNull { it.key == key } ?: all.first()
}

@Composable
fun ScaffoldScreen(onOpen: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    var picked by remember { mutableStateOf("shtools") }
    var tick by remember { mutableStateOf(0) }
    var log by remember { mutableStateOf("选一个模板，点「一键生成」。") }
    var busy by remember { mutableStateOf(false) }

    val tpl = remember(picked) { Scaffolds.of(picked) }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(14.dp)) {
        FeatureHeader("项目脚手架", "一次生成一组真文件，工作区、代码编辑器、对端都能看到和取走。")
        Spacer(Modifier.height(8.dp))
        FeatureCard(highlighted = true) {
            Text("默认引擎：${LinuxRuntime.status().engineLabel}", fontSize = 12.5.sp,
                color = MaterialTheme.colorScheme.primary)
            Text("Shell 工具包开箱即跑；Python / Node 模板要完整 Linux 用户态才执行得起来。",
                fontSize = 10.5.sp, lineHeight = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Spacer(Modifier.height(10.dp))
        FeatureSection("模板")
        Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())) {
            Scaffolds.all.forEach { s ->
                TextButton(onClick = { picked = s.key }) {
                    Text(if (picked == s.key) "● ${s.label}" else s.label, fontSize = 11.sp,
                        color = if (picked == s.key) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface)
                }
                Spacer(Modifier.width(4.dp))
            }
        }
        Text(tpl.sub, fontSize = 10.5.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(6.dp))
        tpl.files.forEach { f ->
            FeatureCard {
                Text("${f.name}  [${f.lang.key}]", fontSize = 11.5.sp,
                    color = MaterialTheme.colorScheme.onSurface)
                Text("${f.code.length} 字 · ${f.code.lines().size} 行", fontSize = 10.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Spacer(Modifier.height(5.dp))
        }

        Spacer(Modifier.height(6.dp))
        FeatureActions(
            "一键生成" to {
                if (!busy) {
                    busy = true
                    scope.launch(Dispatchers.IO) {
                        val sb = StringBuilder("模板：${tpl.label}\n")
                        tpl.files.forEach { f ->
                            CodeEngine.saveFile(f.name, f.lang, f.code)
                            sb.append("已写出 ${f.name}（${f.code.length} 字）\n")
                        }
                        // 生成入口脚本，工作台里点运行就能把整个项目跑一遍。
                        val entryName = "${tpl.key}_entry.sh"
                        CodeEngine.saveFile(entryName, Lang.SH,
                            "#!/system/bin/sh\ncd '${LinuxRuntime.workspace().absolutePath}'\n${tpl.entry}\n")
                        sb.append("入口：$entryName\n\n命令：${tpl.entry}\n")
                        withContext(Dispatchers.Main) {
                            log = sb.toString()
                            busy = false
                            tick++
                        }
                    }
                }
            },
            "跑一遍入口" to {
                scope.launch(Dispatchers.IO) {
                    val r = CodeEngine.runCommand(tpl.entry, 20_000)
                    withContext(Dispatchers.Main) {
                        log = "引擎=${r.engine} · ${r.ms}ms\n" + r.output
                    }
                }
            },
            "看工作区" to { onOpen("WORKSPACE") },
            "去编辑器" to { onOpen("CODEEDITOR") },
            "小应用预览" to { onOpen("MINIAPP") },
        )

        Spacer(Modifier.height(8.dp))
        FeatureSection("本次结果")
        FeatureCard { Text(log, fontSize = 10.5.sp, lineHeight = 15.sp) }
        Spacer(Modifier.height(8.dp))
        Text("已登记文件 ${CodeEngine.listFiles().size} 个（tick=$tick）；" +
            "对端可用 self_code_files / self_code_read 把整套项目读走。",
            fontSize = 10.5.sp, lineHeight = 15.sp,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(20.dp))
    }
}
