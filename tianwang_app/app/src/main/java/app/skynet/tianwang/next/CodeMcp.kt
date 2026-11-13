package app.skynet.tianwang.next

import org.json.JSONObject

/**
 * 把手机本地的代码执行能力开放给对等节点。
 *
 * 手机与电脑 / 另一台手机是平等节点：对方通过 LAN 的 tools/call 调这些工具，
 * 就能让这台手机自己写文件、跑脚本、执行一段简单代码、跑一组任务，
 * 不必先装 Termux、不必连电脑、也不必开无障碍。
 *
 * 工具名统一 self_code_* 前缀，在 PhoneMcp.executeTool 里整段转发，
 * 不走 PhoneSelf 的界面分派（这些能力跟 UI 无关）。
 */
object CodeMcp {

    fun describe(): String {
        val s = LinuxRuntime.status()
        return "引擎=${s.engineLabel} · rootfs=${if (s.rootfsReady) "已装" else "未装"} · " +
            "proot=${if (s.prootReady) "有" else "无"} · 工作区 ${s.workspaceBytes / 1024} KB\n${s.note}"
    }

    fun run(name: String, args: JSONObject): ToolResult = try {
        when (name) {
            "self_code_runtime" -> ToolResult(true, text = describe())

            "self_code_files" -> {
                val files = CodeEngine.listFiles()
                if (files.isEmpty()) ToolResult(true, text = "工作区还没有文件")
                else ToolResult(true, text = files.joinToString("\n") {
                    "${it["name"]}  [${it["lang"]}]  ${CodeEngine.readFile(it["name"].orEmpty()).length} 字"
                })
            }

            "self_code_read" -> {
                val n = args.optString("name")
                if (n.isBlank()) ToolResult(false, err = "name 不能为空")
                else ToolResult(true, text = CodeEngine.readFile(n))
            }

            "self_code_write" -> {
                val n = args.optString("name")
                val code = args.optString("code")
                val lang = Lang.of(args.optString("lang", "sh"))
                if (n.isBlank() || code.isEmpty()) ToolResult(false, err = "name / code 不能为空")
                else ToolResult(true, text = "已写入 ${CodeEngine.saveFile(n, lang, code)}（${code.length} 字）")
            }

            "self_code_run" -> {
                val lang = Lang.of(args.optString("lang", "sh"))
                val code = args.optString("code")
                val n = args.optString("name").ifBlank { "remote.${lang.ext}" }
                val extra = args.optString("args")
                val timeout = args.optInt("timeout_ms", 15_000)
                if (code.isEmpty()) ToolResult(false, err = "code 不能为空")
                else {
                    val r = CodeEngine.run(n, lang, code, extra, timeout)
                    ToolResult(r.ok, text = "引擎=${r.engine} · ${r.ms}ms\n${r.output}")
                }
            }

            "self_code_command" -> {
                val cmd = args.optString("command")
                val timeout = args.optInt("timeout_ms", 15_000)
                if (cmd.isBlank()) ToolResult(false, err = "command 不能为空")
                else {
                    val r = CodeEngine.runCommand(cmd, timeout)
                    ToolResult(r.ok, text = "引擎=${r.engine} · ${r.ms}ms\n${r.output}")
                }
            }

            "self_code_task" -> {
                val key = args.optString("key", "probe")
                val log = CodeEngine.runTask(key) { _, _, _ -> }
                ToolResult(true, text = log.take(6000))
            }

            "self_code_history" -> {
                val h = CodeEngine.history().take(20)
                if (h.isEmpty()) ToolResult(true, text = "还没有运行记录")
                else ToolResult(true, text = h.joinToString("\n") {
                    "${it["name"]} · ${it["engine"]} · ${it["ms"]}ms · " +
                        if (it["ok"] == "true") "成功" else "未成功"
                })
            }

            else -> ToolResult(false, err = "未知代码工具：$name")
        }
    } catch (t: Throwable) {
        ToolResult(false, err = "执行异常：${t.javaClass.simpleName}: ${t.message}")
    }
}
