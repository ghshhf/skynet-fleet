package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** 手机里能写的几种代码。HTML 走小应用工坊预览，其余交给运行时执行。 */
enum class Lang(
    val key: String,
    val label: String,
    val ext: String,
    val needsLinux: Boolean,
) {
    SH("sh", "Shell 脚本", "sh", false),
    PY("py", "Python", "py", true),
    NODE("js", "Node.js", "js", true),
    HTML("html", "网页小应用", "html", false),
    ;

    companion object {
        fun of(key: String): Lang = entries.firstOrNull { it.key == key } ?: SH
    }
}

/** 一次运行的回执。 */
data class RunOutcome(
    val engine: String,
    val output: String,
    val ok: Boolean,
    val ms: Long,
)

/** 一段可一键填入的示例，都是真能跑的，不是空壳模板。 */
data class CodeTemplate(val label: String, val lang: Lang, val name: String, val code: String)

object CodeTemplates {
    val all = listOf(
        CodeTemplate(
            "环境体检", Lang.SH, "check.sh",
            "echo '== 内核 / 系统 =='\nuname -a\n" +
                "echo '== 我是谁 =='\nid\n" +
                "echo '== 可用工具 =='\nfor c in sh ls cat grep awk sed tar wc curl python3 node; do command -v \"\$c\" || echo \"缺 \$c\"; done\n" +
                "echo '== 工作区 =='\npwd\nls -la",
        ),
        CodeTemplate(
            "整理文件清单", Lang.SH, "manifest.sh",
            "ls -la > manifest.txt\n" +
                "echo '== 已写入 manifest.txt =='\ncat manifest.txt\n" +
                "echo '== 行数 =='\nwc -l manifest.txt",
        ),
        CodeTemplate(
            "批量生成文件", Lang.SH, "batch.sh",
            "for i in 1 2 3 4 5; do\n" +
                "  echo \"第 \$i 条：由手机本地脚本生成，时间 $(date)\" > \"note_\$i.txt\"\n" +
                "done\n" +
                "echo '== 生成结果 =='\nls -l note_*.txt\n" +
                "echo '== 内容 =='\ncat note_1.txt",
        ),
        CodeTemplate(
            "生成一个网页小应用", Lang.SH, "make_page.sh",
            "cat > hello.html <<'EOF'\n" +
                "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>\n" +
                "<title>手机制作的小应用</title></head><body style='font-family:sans-serif;padding:24px'>\n" +
                "<h2>你好，这是手机自己生成的应用</h2>\n" +
                "<button onclick=\"document.getElementById('o').textContent='点击成功，页面在手机上跑起来了'\">点我</button>\n" +
                "<p id='o'></p></body></html>\n" +
                "EOF\n" +
                "echo '== 已写出 hello.html =='\nls -l hello.html\nhead -3 hello.html",
        ),
        CodeTemplate(
            "Python 示例（需 Linux）", Lang.PY, "hello.py",
            "import platform, os\n" +
                "print('Python 版本:', platform.python_version())\n" +
                "print('工作目录:', os.getcwd())\n" +
                "print('平方表:', [i*i for i in range(1, 6)])",
        ),
        CodeTemplate(
            "Node 示例（需 Linux）", Lang.NODE, "hello.js",
            "console.log('Node 版本:', process.version)\n" +
                "console.log('平台:', process.platform, process.arch)\n" +
                "console.log('斐波那契:', [1,1,2,3,5,8,13])",
        ),
        CodeTemplate(
            "小应用：计数器", Lang.HTML, "counter.html",
            "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>\n" +
                "<title>计数器</title><style>body{font-family:sans-serif;padding:24px;background:#fafafa;color:#222}\n" +
                "button{font-size:18px;padding:8px 16px;margin:4px}</style></head><body>\n" +
                "<h2>计数器</h2><div id='n' style='font-size:40px'>0</div>\n" +
                "<button onclick=\"n.textContent=+n.textContent+1\">+1</button>\n" +
                "<button onclick=\"n.textContent=0\">归零</button>\n" +
                "</body></html>",
        ),
    )
}

/** 自主任务：一串按顺序执行的动作，每步留轨迹，主人能看清「它自己做了什么」。 */
data class TaskStep(val label: String, val command: String)
data class TaskTemplate(val key: String, val label: String, val sub: String, val steps: List<TaskStep>)

object TaskTemplates {
    val all = listOf(
        TaskTemplate("probe", "体检运行环境", "看内核、身份、可用命令、工作区", listOf(
            TaskStep("读内核与系统", "uname -a"),
            TaskStep("读当前身份", "id"),
            TaskStep("列可用命令", "for c in sh ls cat grep awk sed tar wc python3 node; do command -v \"\$c\" || echo \"缺 \$c\"; done"),
            TaskStep("列工作区文件", "ls -la"),
        )),
        TaskTemplate("manifest", "整理工作区清单", "生成 manifest.txt 并回读核对", listOf(
            TaskStep("生成清单", "ls -la > manifest.txt"),
            TaskStep("核对内容", "cat manifest.txt"),
            TaskStep("统计行数", "wc -l manifest.txt"),
        )),
        TaskTemplate("scaffold", "搭一个小项目骨架", "建目录、写说明、写脚本、跑一遍", listOf(
            TaskStep("建目录", "mkdir -p demo/src demo/out"),
            TaskStep("写 README", "printf '%s\\n' '# 手机本地项目' '由天网代码工作台生成' > demo/README.md"),
            TaskStep("写脚本", "printf '%s\\n' '#!/system/bin/sh' 'echo \"hello from $(pwd)\"' > demo/src/main.sh"),
            TaskStep("跑一遍", "sh demo/src/main.sh"),
            TaskStep("列出骨架", "find demo -type f 2>/dev/null || ls -R demo"),
        )),
        TaskTemplate("page", "生成并自检一个网页应用", "写一个 HTML 页面，检查标签与大小", listOf(
            TaskStep("写页面", "cat > app.html <<'EOF'\n<!doctype html><html><body style='font-family:sans-serif;padding:24px'>\n<h2>手机生成的应用</h2><p id='t'>等待点击</p>\n<button onclick=\"t.textContent='已在手机上运行'\">点我</button>\n</body></html>\nEOF"),
            TaskStep("检查文件", "ls -l app.html"),
            TaskStep("检查结构", "grep -c '<html' app.html; grep -c '<button' app.html"),
        )),
        TaskTemplate("cleanup", "清理临时产物", "只删本工作台生成的临时文件", listOf(
            TaskStep("删除示例文本", "rm -f note_*.txt manifest.txt"),
            TaskStep("删除残留脚本", "rm -f .run-*.sh"),
            TaskStep("确认剩余", "ls -la"),
        )),
    )

    fun of(key: String): TaskTemplate = all.firstOrNull { it.key == key } ?: all.first()
}

/**
 * 代码执行引擎：写文件 → 组装命令 → 交给 [LinuxRuntime] → 落历史。
 *
 * 命令一律用「工作区里的相对文件名」，这样降级到 Android shell 和进 PRoot
 * （-w /workspace）两种引擎下都能找到同一个文件，不用为两套引擎写两套路径。
 */
object CodeEngine {
    const val K_FILES = "code.files"
    const val K_RUNS = "code.runs"
    const val K_TASK_RUNS = "code.task.runs"

    private fun safe(name: String): String {
        val clean = name.trim().replace(Regex("[^A-Za-z0-9_.-]"), "_")
        return clean.ifBlank { "untitled" }.take(64)
    }

    private fun fileOf(name: String): File = File(LinuxRuntime.workspace(), safe(name))

    fun listFiles(): List<Map<String, String>> = kbList(K_FILES).filter {
        fileOf(it["name"].orEmpty()).isFile
    }

    fun readFile(name: String): String = runCatching {
        fileOf(name).readText(Charsets.UTF_8)
    }.getOrDefault("")

    fun langOf(name: String): Lang {
        val key = KB.list(K_FILES).firstOrNull { it.optString("name") == name }
            ?.optString("lang").orEmpty()
        return Lang.of(key)
    }

    fun saveFile(name: String, lang: Lang, code: String): String {
        val n = safe(name)
        runCatching { fileOf(n).writeText(code, Charsets.UTF_8) }
        val arr = KB.array(K_FILES)
        var found = false
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("name") == n) {
                o.put("lang", lang.key)
                o.put("at", System.currentTimeMillis())
                arr.put(i, o)
                found = true
                break
            }
        }
        if (!found) {
            arr.put(JSONObject().put("id", n).put("name", n)
                .put("lang", lang.key).put("at", System.currentTimeMillis()))
        }
        KB.put(K_FILES, arr)
        return n
    }

    fun deleteFile(name: String): Boolean {
        val arr = KB.array(K_FILES)
        val out = JSONArray()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            if (o.optString("name") != name) out.put(o)
        }
        KB.put(K_FILES, out)
        val f = fileOf(name)
        return !f.exists() || f.delete()
    }

    /** 运行一段代码；HTML 不由本引擎执行，交给小应用工坊预览。 */
    fun run(name: String, lang: Lang, code: String, args: String = "", timeoutMs: Int = 15_000): RunOutcome {
        if (lang == Lang.HTML) {
            return RunOutcome("preview", "网页小应用请在「小应用工坊」里预览；这里不执行 HTML。", false, 0L)
        }
        if (lang.needsLinux && LinuxRuntime.prootBinary() == null) {
            return RunOutcome(
                "android-sh",
                "这条需要完整 Linux 用户态（${lang.label}）。当前没有 proot 二进制，" +
                    "手机默认跑 Android 自带 shell；请先装 Alpine 用户提供 proot，或改用 Shell 脚本。",
                false, 0L,
            )
        }
        val n = saveFile(name, lang, code)
        val cmd = when (lang) {
            Lang.SH -> "sh '$n' $args"
            Lang.PY -> "python3 '$n' $args"
            Lang.NODE -> "node '$n' $args"
            Lang.HTML -> ""
        }
        val r = wrap(LinuxRuntime.exec(cmd, LinuxRuntime.workspace(), timeoutMs))
        record(n, lang.key, r, code)
        return r
    }

    /** 跑一条原始命令（终端/任务/MCP 都走这里）。 */
    fun runCommand(command: String, timeoutMs: Int = 15_000): RunOutcome {
        val r = wrap(LinuxRuntime.exec(command, LinuxRuntime.workspace(), timeoutMs))
        record("(命令)", "sh", r, command)
        return r
    }

    private fun wrap(x: LinuxRuntime.ExecResult): RunOutcome =
        RunOutcome(x.engine, x.output, x.ok, x.ms)

    private fun record(name: String, lang: String, r: RunOutcome, code: String) {
        runCatching {
            KB.add(K_RUNS, JSONObject()
                .put("name", name)
                .put("lang", lang)
                .put("engine", r.engine)
                .put("ok", r.ok)
                .put("ms", r.ms)
                .put("output", r.output.take(2000))
                .put("code", code.take(4000)))
        }
    }

    fun history(): List<Map<String, String>> = kbList(K_RUNS).asReversed()

    fun clearHistory() = KB.put(K_RUNS, JSONArray())

    /** 自主任务：顺序执行每一步，逐步留轨迹并整体落一条记录。 */
    fun runTask(key: String, report: (Int, String, String) -> Unit): String {
        val t = TaskTemplates.of(key)
        val lines = StringBuilder("任务：${t.label}\n")
        t.steps.forEachIndexed { idx, step ->
            val r = LinuxRuntime.exec(step.command, LinuxRuntime.workspace(), 20_000)
            val head = "${idx + 1}/${t.steps.size} ${step.label} · ${r.engine} · ${r.ms}ms · " +
                if (r.ok) "成功" else "未成功"
            lines.append("\n--- $head ---\n").append(r.output.take(1500)).append('\n')
            report(idx, step.label, r.output)
        }
        runCatching {
            KB.add(K_TASK_RUNS, JSONObject()
                .put("task", t.key)
                .put("label", t.label)
                .put("steps", t.steps.size)
                .put("log", lines.toString().take(8000)))
        }
        return lines.toString()
    }

    fun taskHistory(): List<Map<String, String>> = kbList(K_TASK_RUNS).asReversed()
}
