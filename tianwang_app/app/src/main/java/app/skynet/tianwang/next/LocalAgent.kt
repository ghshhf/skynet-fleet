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
    /**
     * 回灌给模型的单条工具结果上限（字符）。
     *
     * 不设上限会直接毁掉多步闭环：`web_read` 一页正文动辄上万字，
     * 三步就能把 1.7B 那点 ctx 撑爆，表现成「后面几步突然胡说」而不是报错。
     *
     * 2000 而不是 1600：`web_digest` 的材料总预算是 1500 字，再算上每篇的
     * 标题/来源行，1600 会正好把最后一篇切掉半条 —— 那比不读还误导。
     * 2000 汉字 ≈ 1400 token，8K 上下文里仍留得下回答与历史。
     */
    private const val TOOL_TEXT_LIMIT = 2000

    /** 超限就截断，但要**明确告诉模型被截了**，否则它会以为那就是全部。 */
    private fun clipToolText(s: String): String =
        if (s.length <= TOOL_TEXT_LIMIT) s
        else s.take(TOOL_TEXT_LIMIT) + "\n…（已截断，原始结果共 ${s.length} 字）"

    suspend fun run(
        profile: ModelProfile,
        history: List<ChatMsg>,
        tools: List<McpTool>,
        allowActions: Boolean,
        /**
         * 连续调工具的上限。
         *
         * 4 而不是 3：联网任务的最小闭环是「搜 → 读一条 → 回答」，
         * 3 步刚好卡满，中间失败一次就没机会改道了。界面操作有
         * [Replan] 的防重放与死胡同检测兜着，多一步不会变成打转。
         */
        maxSteps: Int = 4,
        execute: suspend (String, JSONObject) -> ToolResult,
        onDelta: (String) -> Unit,
        onTool: (ToolCall) -> Unit,
    ): Result<String> {
        val readable = tools.filter { allowActions || !it.sideEffect }
        // 🔴 工具面先裁再喂：60 个全量塞 prompt，58 个工具实测 56.2 秒（超时线 25 秒），
        //    而且 6 个近邻工具起小模型就开始选错。裁到 ≤8 个再决策。
        //    信号只取最新一条用户消息 —— 多轮里第一句会把域带偏。
        val goal = history.lastOrNull { it.role == ChatMsg.Role.USER }?.text.orEmpty()
        val picked = ToolScope.pick(goal, readable)
        val visible = picked.tools
        // 域判断错了就会「模型明明能做却说做不到」，所以把判定结果打进日志，真机可查。
        android.util.Log.i("TianwangToolScope",
            "goal=${goal.take(40)} domain=${picked.domain} " +
                "tools=${visible.size}/${readable.size} " +
                "names=${visible.joinToString(",") { it.name }}")
        val convo = ArrayList<ChatMsg>(history.size + 2)
        convo.add(ChatMsg(role = ChatMsg.Role.SYSTEM,
            text = LocalToolProtocol.systemPrompt(visible, allowActions, picked.domain)))
        convo.addAll(history.filter { it.text.isNotBlank() })
        // 每一步的事实账本：失败分类、重复动作、停手时的已完成清单都从这里来。
        val steps = ArrayList<Replan.Step>()
        // 连续「想重复读同一个东西」的次数：两次就说明它在原地打转，直接逼它回答。
        var blockedRepeat = 0

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
                LocalToolProtocol.looksLikeActionAttempt(out, visible)) {
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
                    return wrapUp(profile, convo, onDelta, steps, why)
                }
                return@repeat
            }

            // 链接是「原样照抄」类参数，1.7B 最容易抄漏一段路径或干脆留空。
            // 补的只从用户这句话里捞，**不改模型已经给对的值**。
            // 必须在判重之前补：否则「留空」和「补好」会被当成两个不同动作，
            // 同一篇网页会被读两遍、烧掉一半步数。
            val callArgs = LocalToolProtocol.repairLinkArgs(picked.name, picked.args, goal)
            val argKey = argFingerprint(callArgs)
            // 🔴 写操作防重放：同一次目标里已经成功过的同一动作，不许再执行。
            //    内容型只读（web_read 同一个 URL 等）同样拦，但**不终止任务** ——
            //    把「别再调了」回灌给模型，逼它用已有结果回答。
            Replan.blockBefore(steps, picked.name, argKey, tool.sideEffect)?.let { why ->
                if (tool.sideEffect) {
                    return Result.failure(IllegalStateException("$why。\n${Replan.summary(steps)}"))
                }
                convo.add(ChatMsg(role = ChatMsg.Role.ASSISTANT, text = out))
                convo.add(ChatMsg(role = ChatMsg.Role.USER, text = why))
                blockedRepeat++
                if (blockedRepeat >= 2) return finalAnswer(profile, convo, onDelta)
                return@repeat
            }

            val started = System.currentTimeMillis()
            val running = ToolCall(name = picked.name, args = callArgs.toString())
            onTool(running)
            val result = runCatching { execute(picked.name, callArgs) }
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
                    "工具 ${picked.name} 执行结果（成功）：\n${clipToolText(result.text)}"
                } else {
                    // 失败不只回错误原文：附一条确定的换路建议，避免小模型原样重试。
                    val kind = Replan.classify(result.err)
                    "工具 ${picked.name} 执行结果（失败）：\n${result.err}\n\n" +
                        "下一步建议（${kind.label}）：${Replan.hint(kind, picked.name)}"
                }))

            // 硬止损：权限没开 / 锁屏 / 同一动作重复失败 / 累计失败，
            // 都不该把剩下的步数烧在同一个死胡同里。
            Replan.stall(steps)?.let { why ->
                return wrapUp(profile, convo, onDelta, steps, why)
            }
        }
        // 步数用尽但已经拿到了材料：再给一次「只回答」的机会。
        // 直接甩一句「连续 4 步都在调工具」对用户毫无价值 —— 他要的是结论。
        if (steps.any { it.ok }) return finalAnswer(profile, convo, onDelta)
        return Result.failure(IllegalStateException(
            "连续 $maxSteps 步都在调工具，已停手。\n${Replan.summary(steps)}"))
    }

    /**
     * 碰到死胡同时的分流：**已经拿到材料就先作答，一步没成才报失败**。
     *
     * 为什么必须分这一刀 —— 真机实测：模型先 `web_read` + `web_digest` 拿到了正文，
     * 接着想去点页面标题，无障碍没开，`stall` 判 PERMISSION 立刻停手。
     * 停手是对的（原样重试确实没用），但把整轮判成失败、一句正文都不给，
     * 对用户是净损失：他要的是那几段内容，不是「无障碍没开」。
     *
     * 所以：有成功步骤 → 收敛成回答；一步没成 → 如实失败，把失败账本带回去。
     */
    private suspend fun wrapUp(
        profile: ModelProfile,
        convo: List<ChatMsg>,
        onDelta: (String) -> Unit,
        steps: List<Replan.Step>,
        why: String,
    ): Result<String> {
        if (steps.any { it.ok }) return finalAnswer(profile, convo, onDelta)
        return Result.failure(IllegalStateException("$why。\n${Replan.summary(steps)}"))
    }

    /**
     * 收敛轮：把工具表从上下文里「摘掉」，只让模型用已有材料回答。
     *
     * 为什么单独一轮而不是在循环里加规则：1.7B 看到工具列表就忍不住再调一次，
     * 提示词劝不住。这一轮明确说「停止调用工具、只输出自然语言」，
     * 并且不再给它任何「下一步做什么」的空间。
     */
    private suspend fun finalAnswer(
        profile: ModelProfile,
        convo: List<ChatMsg>,
        onDelta: (String) -> Unit,
    ): Result<String> {
        val closing = ArrayList<ChatMsg>(convo.size + 2)
        closing.addAll(convo)
        closing.add(ChatMsg(role = ChatMsg.Role.ASSISTANT, text = "（先停一下）"))
        closing.add(ChatMsg(role = ChatMsg.Role.USER,
            text = "停止调用工具，不要再输出 JSON。用上面已经拿到的内容回答我的问题：" +
                "简体中文，第一句直接给结论，再分 3～6 条说清楚。材料里没有的不要编。"))
        // onDelta 直接透传：界面照常流式显示这一轮。
        return LocalBackend.chat(profile, closing, onDelta = onDelta, onTool = {})
            .fold(
                onSuccess = { text ->
                    if (text.isBlank()) {
                        Result.failure(IllegalStateException("材料拿到了，但模型没能收敛成回答（空输出）"))
                    } else {
                        Result.success(text)
                    }
                },
                onFailure = { Result.failure(it) },
            )
    }

    /** 参数判重键：按键名排序，避免模型换一个字段顺序就被当成「新动作」。 */
    private fun argFingerprint(args: JSONObject): String {
        val keys = args.keys().asSequence().toList().sorted()
        return keys.joinToString("&") { "$it=${args.opt(it)}" }
    }
}
