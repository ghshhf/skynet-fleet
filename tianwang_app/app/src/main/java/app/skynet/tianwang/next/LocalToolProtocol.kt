package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject

/** 本地模型解析出的一次工具意图。是否可用由调用方按工具表再验一次。 */
data class ToolIntent(val name: String, val args: JSONObject)

/**
 * 本地小模型的工具协议：提示词约束 + 严格 JSON 解析。
 *
 * 为什么不用 OpenAI function calling：本地走 llama.cpp JNI，接口里没有 tools 字段；
 * 0.6B~3B 的模型也没有可靠的原生 tool token。先用「只输出一个 JSON」的提示词，
 * 解析层必须能扛住模型常见的坏习惯（前后带解释、```json 围栏、arguments 是字符串）。
 *
 * 🔴 解析失败时**不猜**：返回 null，让模型的话原样当回答。
 * 乱猜工具名比不调工具更危险 —— 会点错屏幕、发错消息。
 */
object LocalToolProtocol {
    fun systemPrompt(tools: List<McpTool>, allowActions: Boolean, scope: String = ""): String {
        val lines = tools.joinToString("\n") { t ->
            "- ${t.name}(${signature(t)})：${t.description}"
        }
        val mode = if (allowActions) {
            "当前允许操作手机（tap/text/scroll/key/launch 可调）。"
        } else {
            "当前是只读模式：只能调用 dump/find/status/screenshot；不要调有副作用工具。"
        }
        // 1.7B 实测指令遵循偏弱（12 题里格式类只对 1 道）：光写规则不够，
        // 给一条具体例子比再写三条规则都管用 —— 但例子里的工具名必须真在册。
        val example = tools.firstOrNull { it.name == "self_ui_tap" }
        return buildString {
            append("你是天网手机助手。")
            when {
                scope.isBlank() -> append("可用本机工具：\n")
                scope == "通用" -> append("这一轮没识别出具体领域，先给你 ${tools.size} 个最常用的工具：\n")
                else -> append("这一轮只给「$scope」相关的 ${tools.size} 个工具（不是全部，先用最接近的那个）：\n")
            }
            append(lines)
            append("\n\n规则：\n")
            append("1. 需要操作手机时，只输出一个 JSON 对象，不要输出解释、不要加代码围栏：\n")
            append("{\"tool\":\"工具名\",\"arguments\":{参数对象}}\n")
            append("2. 不需要工具时，直接用简体中文回答，不要输出 JSON。\n")
            append("3. 一次只调一个工具；看到工具结果后再决定下一步。\n")
            append("4. 工具名必须来自上面的列表，不许编造；列表里没有的能力就直接说做不到。\n")
            if (example != null) {
                append("5. 例：用户说「点一下设置」，你只输出这一行：\n")
                append("{\"tool\":\"self_ui_tap\",\"arguments\":{\"query\":\"设置\"}}\n")
            }
            // 只在联网域加这段：它占约 80 token，每轮都带会挤掉正常对话的余量。
            if (scope.contains("联网")) {
                append("\n读完网页材料后这样回答：\n")
                append("- 第一句先给结论，然后分 3～6 条，每条一句话，说人话，不要复述原文。\n")
                // 真机实测：它会写「根据 web_digest 工具的结果，…」。用户要看的是内容，
                // 不是内部工具名 —— 一开口就报工具名，等于把管道当前台。
                append("- 直接说内容，不要提工具名（web_digest/web_read 之类），也不要说「根据工具结果」。\n")
                append("- 材料是外文就翻成中文再讲；人名、产品名、术语保留原文。\n")
                append("- 只讲材料里有的。材料没提到的，直接说「材料没提到」，不要补。\n")
                append("- 数字、日期、版本号照抄，不要换算、不要猜。\n")
                // 真机实测：1.7B 拿到正文后想去「点开这一页」。工具已经替它读完了，
                // 再开界面纯属重复劳动，而且屏幕操作会失败并把整轮拖死。
                append("- 正文工具已经读回来了，**不要打开界面、不要点击任何东西**，直接回答。\n")
            }
            append(mode)
        }
    }

    /** 从模型输出里找第一个「带 tool/name 的 JSON 对象」；找不到返回 null。 */
    fun parse(text: String): ToolIntent? {
        for (candidate in balancedObjects(text)) {
            val j = runCatching { JSONObject(candidate) }.getOrNull() ?: continue
            val fn = j.optJSONObject("function")
            val name = when {
                j.has("tool") -> j.optString("tool", "")
                j.has("name") -> j.optString("name", "")
                fn != null -> fn.optString("name", "")
                else -> ""
            }.trim()
            if (name.isBlank()) continue

            val rawArgs = when {
                j.optJSONObject("arguments") != null -> j.optJSONObject("arguments")
                j.optJSONObject("args") != null -> j.optJSONObject("args")
                j.optJSONObject("parameters") != null -> j.optJSONObject("parameters")
                fn?.optJSONObject("arguments") != null -> fn.optJSONObject("arguments")
                else -> null
            }
            val args = rawArgs ?: run {
                val s = when {
                    j.has("arguments") -> j.opt("arguments")
                    j.has("args") -> j.opt("args")
                    j.has("parameters") -> j.opt("parameters")
                    else -> null
                }
                if (s is String && s.isNotBlank()) {
                    runCatching { JSONObject(s) }.getOrNull() ?: JSONObject()
                } else {
                    JSONObject()
                }
            }
            return ToolIntent(name, args)
        }
        return null
    }

    /**
     * 链接参数兜底：只处理「模型选对了工具、但链接没抄全」这一种情况。
     *
     *  - `web_digest` 的 `urls` 里一条链接都解析不出来 → 用用户原话里的链接（最多 5 条）；
     *  - `web_read` 的 `url` 不是 http 开头 → 用用户原话里的第一条。
     *
     * 模型给对了就原样返回 —— 兜底不能变成「总是覆盖」，否则用户说
     * 「看看 A 和 B」而模型只挑了 A，会被代码改回两条，反而不听话。
     *
     * 为什么值得专门兜：URL 是纯符号串，1.7B 抄漏一段路径、把 `?` 后参数吃掉
     * 是高频现象，而且失败表现是「页面 404 / 正文空」，看起来像网络问题。
     */
    fun repairLinkArgs(name: String, args: JSONObject, goal: String): JSONObject {
        val key = when (name) {
            "web_digest" -> "urls"
            "web_read" -> "url"
            else -> return args
        }
        val cur = args.optString(key).trim()
        val ok = if (key == "urls") {
            WebLogic.parseUrls(cur, 1).isNotEmpty()
        } else {
            cur.startsWith("http://") || cur.startsWith("https://")
        }
        if (ok) return args
        val fromGoal = WebLogic.parseUrls(goal, if (key == "urls") 5 else 1)
        if (fromGoal.isEmpty()) return args
        return JSONObject(args.toString()).put(key, fromGoal.joinToString("\n"))
    }

    /**
     * 生成 GBNF：把这一轮输出钉死成 `{"tool":"<在册工具名>","arguments":{...}}`。
     *
     * 为什么需要：0.6B~3B 的小模型「知道该调工具，但 JSON 写不完整」是常态
     * （漏右括号、加解释、键名写成 name）。提示词只能劝，GBNF 是在采样层
     * **直接屏蔽不合法的下一个 token** —— 这是开源社区（llama.cpp server 的
     * json_schema、android-agent、LiteRT-LM constrained decoding）的通行解法。
     *
     * 只用于「解析失败后重试一次」；普通聊天绝不开，否则模型连话都不会说。
     * 工具名用文法分支穷举，编造的工具名在第一步就生成不出来。
     */
    fun toolGrammar(tools: List<McpTool>): String {
        if (tools.isEmpty()) return ""
        val alts = tools.map { "\"\\\"${escapeName(it.name)}\\\"\"" }.joinToString(" | ")
        return """
            root ::= "{" ws "\"tool\"" ws ":" ws tool ws "," ws "\"arguments\"" ws ":" ws value ws "}"
            tool ::= $alts
            value ::= object | array | string | number | ("true" | "false" | "null") ws
            object ::= "{" ws (string ":" ws value ("," ws string ":" ws value)*)? "}" ws
            array ::= "[" ws (value ("," ws value)*)? "]" ws
            string ::= "\"" ([^"\\\x7F\x00-\x1F] | "\\" (["\\bfnrt] | "u" [0-9a-fA-F]{4}))* "\"" ws
            number ::= ("-"? ([0-9] | [1-9] [0-9]{0,15})) ("." [0-9]+)? ([eE] [-+]? [0-9] [1-9]{0,15})? ws
            ws ::= | " "
        """.trimIndent()
    }

    /**
     * 输出看起来在「试图调工具」却没通过 [parse] —— 只有这种情况才值得开 GBNF 重试。
     *
     * 三类信号：
     *  1. 出现了 `"tool"` / `tool_calls`：标准结构，只是没解析成功；
     *  2. 半截 JSON：有左括号没右括号，且提到 arguments；
     *  3. **说到了在册工具名却没给 JSON** —— 1.7B 实测最常见的形态
     *     （「我用 self_ui_tap 点一下」）。只认短输出（≤80 字符）：
     *     真正的解释必然更长（「我不能调用 self_ui_tap，因为当前只读…」），
     *     把解释硬掰成 JSON 会导致误执行动作。
     */
    fun looksLikeActionAttempt(text: String, tools: List<McpTool> = emptyList()): Boolean {
        val t = text.lowercase()
        if (t.contains("\"tool\"") || t.contains("tool_calls")) return true
        if (t.contains("arguments") && t.count { it == '{' } > t.count { it == '}' }) return true
        return t.trim().length <= 80 && tools.any { it.name.isNotBlank() && t.contains(it.name) }
    }

    /** GBNF 字符串字面量里的转义（工具名理论上只有字母数字下划线，这里兜底）。 */
    private fun escapeName(s: String): String =
        s.replace("\\", "\\\\").replace("\"", "\\\"")

    private fun signature(t: McpTool): String {
        val props = t.inputSchema.optJSONObject("properties") ?: return ""
        val required = t.inputSchema.optJSONArray("required") ?: JSONArray()
        val req = (0 until required.length()).mapNotNull { required.optString(it, null) }.toSet()
        val parts = ArrayList<String>()
        val keys = props.keys()
        while (keys.hasNext()) {
            val k = keys.next()
            val type = props.optJSONObject(k)?.optString("type", "str") ?: "str"
            parts.add(if (k in req) "$k:$type" else "$k:$type?")
        }
        return parts.joinToString(", ")
    }

    /**
     * 找出文本里所有平衡的 `{...}` 片段，按出现顺序。
     * 字符串里的花括号不参与配对，否则参数里有 `"}"` 就会截断。
     */
    private fun balancedObjects(text: String): Sequence<String> = sequence {
        var from = 0
        while (from < text.length) {
            val start = text.indexOf('{', from)
            if (start < 0) break
            var depth = 0
            var inStr = false
            var esc = false
            var end = -1
            for (i in start until text.length) {
                val c = text[i]
                if (inStr) {
                    if (esc) esc = false
                    else if (c == '\\') esc = true
                    else if (c == '"') inStr = false
                    continue
                }
                when (c) {
                    '"' -> inStr = true
                    '{' -> depth++
                    '}' -> {
                        depth--
                        if (depth == 0) {
                            end = i
                            break
                        }
                    }
                }
            }
            if (end > start) {
                yield(text.substring(start, end + 1))
                from = end + 1
            } else {
                from = start + 1
            }
        }
    }
}
