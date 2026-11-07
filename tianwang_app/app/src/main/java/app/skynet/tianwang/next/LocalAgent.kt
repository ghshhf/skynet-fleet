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
        // 每一步的事实账本：失败分类、重复动作、停手时的已完成清单都从这里来。
        val steps = ArrayList<Replan.Step>()

        repeat(maxSteps) {
            // 工具 JSON 不流式显示：先攒完再判断，避免用户看见半截 {"tool":...
            var full = ""
            val r = LocalBackend.chat(profile, convo.toList(), onDelta = { full += it })
            var out = r.getOrElse { return Result.failure(it) }
            if (out.isBlank()) {
                return Result.failure(IllegalStateException("本地模型没有产出（可能是上下文/显存不够）"))
            }

            var intent = LocalToolProtocol.parse(out)
            if (intent == null && visible.isNotEmpty() &&
                LocalToolProtocol.looksLikeActionAttempt(out)) {
                // 🔴 小模型「想调工具但 JSON 没守住」：用 GBNF 把它钉成 JSON 再试一次。
                //    只在这一次重试里加约束 —— 全程强制 JSON 会把正常聊天毁掉。
                //    同 prompt 再跑一遍，KV 前缀全部复用，代价只有重新采样的几十个 token。
                var retry = ""
                val grammar = LocalToolProtocol.toolGrammar(visible)
                val r2 = LocalBackend.chatWithGrammar(profile, convo.toList(), grammar) { retry += it }
                val fixed = r2.getOrNull()
                    ?.takeIf { it.isNotBlank() }
                    ?.let { LocalToolProtocol.parse(it) }
                if (fixed != null) {
                    intent = fixed
                    out = retry
                }
            }
            val picked = intent
            if (picked == null) {
                // 正常回答：一次性交给 UI；本地小模型的文本不值得为了"流式"再切碎。
                onDelta(out)
                return Result.success(out)
            }

            val tool = visible.firstOrNull { it.name == picked.name }
            if (tool == null) {
                val argKey = argFingerprint(picked.args)
                steps.add(Replan.Step(picked.name, argKey, false, "工具不在册"))
                convo.add(ChatMsg(role = ChatMsg.Role.ASSISTANT, text = out))
                convo.add(ChatMsg(role = ChatMsg.Role.USER,
                    text = "错误：没有工具 ${picked.name}。只能从这些里选：" +
                        visible.joinToString("、") { it.name }))
                Replan.stall(steps)?.let { why ->
                    return Result.failure(IllegalStateException("$why。\n${Replan.summary(steps)}"))
                }
                return@repeat
            }

            val argKey = argFingerprint(picked.args)
            // 🔴 写操作防重放：同一次目标里已经成功过的同一动作，不许再执行。
            Replan.blockBefore(steps, picked.name, argKey, tool.sideEffect)?.let { why ->
                return Result.failure(IllegalStateException("$why。\n${Replan.summary(steps)}"))
            }

            val started = System.currentTimeMillis()
            val running = ToolCall(name = picked.name, args = picked.args.toString())
            onTool(running)
            val result = runCatching { execute(picked.name, picked.args) }
                .getOrElse { ToolResult(false, err = "${it.javaClass.simpleName}: ${it.message ?: "执行失败"}") }
            val ms = System.currentTimeMillis() - started
            onTool(running.copy(
                state = if (result.ok) ToolCall.State.OK else ToolCall.State.FAIL,
                result = if (result.ok) result.text else result.err,
                ms = ms,
            ))
            steps.add(Replan.Step(
                name = picked.name,
                argKey = argKey,
                ok = result.ok,
                err = result.err,
                text = result.text,
                sideEffect = tool.sideEffect,
            ))

            convo.add(ChatMsg(role = ChatMsg.Role.ASSISTANT, text = out))
            convo.add(ChatMsg(role = ChatMsg.Role.USER,
                text = if (result.ok) {
                    "工具 ${picked.name} 执行结果（成功）：\n${result.text}"
                } else {
                    // 失败不只回错误原文：附一条确定的换路建议，避免小模型原样重试。
                    val kind = Replan.classify(result.err)
                    "工具 ${picked.name} 执行结果（失败）：\n${result.err}\n\n" +
                        "下一步建议（${kind.label}）：${Replan.hint(kind, picked.name)}"
                }))

            // 硬止损：权限没开 / 锁屏 / 同一动作重复失败 / 累计失败，
            // 都不该把剩下的步数烧在同一个死胡同里。
            Replan.stall(steps)?.let { why ->
                return Result.failure(IllegalStateException("$why。\n${Replan.summary(steps)}"))
            }
        }
        return Result.failure(IllegalStateException(
            "连续 $maxSteps 步都在调工具，已停手。\n${Replan.summary(steps)}"))
    }

    /** 参数判重键：按键名排序，避免模型换一个字段顺序就被当成「新动作」。 */
    private fun argFingerprint(args: JSONObject): String {
        val keys = args.keys().asSequence().toList().sorted()
        return keys.joinToString("&") { "$it=${args.opt(it)}" }
    }
}
