package app.skynet.tianwang.next

import org.json.JSONObject

/**
 * 会话层的对外面孔。
 *
 * 关键点：这些是**同一份实现的两个入口**——AI 走 MCP `tools/call`，人走终端会话屏，
 * 两边最终都落到 [TtySession]。所以「人点出来的状态」和「AI 看到的状态」不会各说各话。
 *
 * 工具名统一 `self_tty_*`，在 PhoneMcp.executeTool 里整段转发，不走无障碍分派
 * （会话能力跟界面无关，不该被无障碍开关挡住）。
 */
object TtyMcp {

    fun describe(): String {
        val s = TtySession.list()
        val head = "后端：${TtySession.supportedModes()}\n" +
            "上限：${TtySession.MAX_SESSIONS} 个会话 · 每个保留最近 ${TtySession.MAX_KEEP / 1024}KB 输出"
        if (s.isEmpty()) return "$head\n当前没有会话。用 self_tty_open 开一个。"
        val body = s.joinToString("\n") {
            "${it.id} · ${it.mode.label} · ${if (it.running) "运行中" else "已退出(exit=${it.exit ?: "?"})"} · " +
                "累计 ${it.total} 字 / 保留 ${it.kept} · ${it.cwd}"
        }
        return "$head\n$body"
    }

    fun run(name: String, args: JSONObject): ToolResult = try {
        when (name) {
            "self_tty_list" -> ToolResult(true, text = describe())

            "self_tty_open" -> {
                val cwd = args.optString("cwd")
                val pty = args.optBoolean("pty", false)
                val id = TtySession.open(cwd, pty)
                val now = TtySession.list().firstOrNull { it.id == id }
                ToolResult(
                    true,
                    text = "已开会话 $id（${now?.mode?.label ?: "?"}）\n目录：${now?.cwd ?: cwd}\n" +
                        "下一步：self_tty_write 传命令，self_tty_read 取输出。",
                )
            }

            "self_tty_write" -> {
                val id = args.optString("id")
                val text = args.optString("text")
                val newline = args.optBoolean("enter", true)
                // Ctrl-C 走这里发：text 传 \u0003 即可中断前台命令，不必杀会话。
                if (id.isBlank()) ToolResult(false, err = "id 不能为空")
                else if (text.isEmpty()) ToolResult(false, err = "text 不能为空（发 Ctrl-C 用 \\u0003）")
                else if (!TtySession.write(id, text, newline)) ToolResult(false, err = "会话 $id 不存在或已关闭")
                else ToolResult(true, text = "已写入 $id（${text.length} 字${if (newline) " + 回车" else "，不带回车"}）")
            }

            "self_tty_read" -> {
                val id = args.optString("id")
                val since = args.optLong("since", 0L)
                val max = args.optInt("max_chars", 8000)
                if (id.isBlank()) ToolResult(false, err = "id 不能为空")
                else {
                    val r = TtySession.read(id, since, max)
                    if (r == null) {
                        ToolResult(false, err = "会话 $id 不存在")
                    } else {
                        val head = StringBuilder()
                        head.append(if (r.running) "运行中" else "已退出")
                        r.exit?.let { head.append("(exit=$it)") }
                        head.append(" · cursor=${r.cursor} · ${r.cwd}")
                        if (r.droppedBefore > 0) head.append("\n（更早的 ${r.droppedBefore} 字已按上限丢弃）")
                        if (r.truncated) head.append("\n（本次只回最近 $max 字，用 cursor 继续取尾部之后的新输出）")
                        val body = if (r.text.isBlank()) "（暂无新输出）" else r.text.trimEnd()
                        ToolResult(true, text = "$head\n$body")
                    }
                }
            }

            "self_tty_close" -> {
                val id = args.optString("id")
                if (id.isBlank()) {
                    val n = TtySession.closeAll()
                    ToolResult(true, text = "已关闭 $n 个会话")
                } else if (!TtySession.close(id)) {
                    ToolResult(false, err = "会话 $id 不存在")
                } else {
                    ToolResult(true, text = "已关闭 $id（先 TERM，1 秒不退才强杀）")
                }
            }

            // 一次性把「开→写→等→读」串起来，省得远端为一条命令来回四趟。
            "self_tty_run" -> {
                val cmd = args.optString("command")
                val cwd = args.optString("cwd")
                val waitMs = args.optInt("wait_ms", 4000).coerceIn(200, 30_000)
                if (cmd.isBlank()) {
                    ToolResult(false, err = "command 不能为空")
                } else {
                    val id = TtySession.open(cwd, false)
                    val start = TtySession.read(id, 0L, 2000)?.cursor ?: 0L
                    TtySession.write(id, cmd, true)
                    var cur = start
                    var text = ""
                    var running = true
                    val deadline = System.currentTimeMillis() + waitMs
                    // 边等边收：进程还在跑就把已出的输出先带回，不必干等到超时。
                    while (System.currentTimeMillis() < deadline) {
                        Thread.sleep(120)
                        val r = TtySession.read(id, cur, 16_000) ?: break
                        text += r.text
                        cur = r.cursor
                        running = r.running
                        if (!running) break
                    }
                    TtySession.close(id)
                    val note = if (running) "\n（仍在运行，已到等待上限 ${waitMs}ms，已关闭该会话）" else ""
                    ToolResult(true, text = (if (text.isBlank()) "（无输出）" else text.trimEnd()) + note)
                }
            }

            else -> ToolResult(false, err = "未知会话工具：$name")
        }
    } catch (t: Throwable) {
        ToolResult(false, err = "会话异常：${t.javaClass.simpleName}: ${t.message}")
    }
}
