package app.skynet.tianwang.next

import org.json.JSONObject

/**
 * 本地模型的工具闭环：提示词 → 解析工具意图 → 真执行 → 结果回灌 → 再生成。
 *
 * 与云侧 agent loop 的区别：这边没有服务端编排，也没有原生 function calling。
 * 所以只有一条硬规则：**模型必须按 [LocalToolProtocol] 的 JSON 格式吐工具名；
 * 解析不出来就当普通回答，绝不替它猜。**
 *
 * 最多连续调 [maxSteps] 次工具，防止小模型在一件事上打转把电量烧光。
 */
object LocalAgent {
    suspend fun run(
        profile: ModelProfile,
        history: List<ChatMsg>,
        tools: List<McpTool>,
        allowActions: Boolean,
        maxSteps: Int = 3,
        execute: suspend (String, JSONObject) -> ToolResult,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> {
        val visible = tools.filter { allowActions || !it.sideEffect }
        val convo = ArrayList<ChatMsg>(history.size + 2)
        convo.add(ChatMsg(role = ChatMsg.Role.SYSTEM,
            text = LocalToolProtocol.systemPrompt(visible, allowActions)))
        convo.addAll(history.filter { it.text.isNotBlank() })

        repeat(maxSteps) {
            // 工具 JSON 不流式显示：先攒完再判断，避免用户看见半截 {"tool":...
            var full = ""
            val r = LocalBackend.chat(profile, convo.toList(), onDelta = { full += it })
            val out = r.getOrElse { return Result.failure(it) }
            if (out.isBlank()) {
                return Result.failure(IllegalStateException("本地模型没有产出（可能是上下文/显存不够）"))
            }

            val intent = LocalToolProtocol.parse(out)
            if (intent == null) {
                // 正常回答：一次性交给 UI；本地小模型的文本不值得为了"流式"再切碎。
                onDelta(out)
                return Result.success(out)
            }

            val tool = visible.firstOrNull { it.name == intent.name }
            if (tool == null) {
                convo.add(ChatMsg(role = ChatMsg.Role.ASSISTANT, text = out))
                convo.add(ChatMsg(role = ChatMsg.Role.USER,
                    text = "错误：没有工具 ${intent.name}。只能从这些里选：" +
                        visible.joinToString("、") { it.name }))
                return@repeat
            }

            val started = System.currentTimeMillis()
            val running = ToolCall(name = intent.name, args = intent.args.toString())
            onTool(running)
            val result = runCatching { execute(intent.name, intent.args) }
                .getOrElse { ToolResult(false, err = "${it.javaClass.simpleName}: ${it.message ?: "执行失败"}") }
            val ms = System.currentTimeMillis() - started
            onTool(running.copy(
                state = if (result.ok) ToolCall.State.OK else ToolCall.State.FAIL,
                result = if (result.ok) result.text else result.err,
                ms = ms,
            ))

            convo.add(ChatMsg(role = ChatMsg.Role.ASSISTANT, text = out))
            convo.add(ChatMsg(role = ChatMsg.Role.USER,
                text = "工具 ${intent.name} 执行结果（${if (result.ok) "成功" else "失败"}）：\n" +
                    (if (result.ok) result.text else result.err)))
        }
        return Result.failure(IllegalStateException(
            "连续 $maxSteps 步都在调工具，已停手。最后一步的结果见上面的工具卡片。"))
    }
}
