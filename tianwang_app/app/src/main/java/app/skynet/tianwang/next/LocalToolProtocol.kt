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
    fun systemPrompt(tools: List<McpTool>, allowActions: Boolean): String {
        val lines = tools.joinToString("\n") { t ->
            "- ${t.name}(${signature(t)})：${t.description}"
        }
        val mode = if (allowActions) {
            "当前允许操作手机（tap/text/scroll/key/launch 可调）。"
        } else {
            "当前是只读模式：只能调用 dump/find/status/screenshot；不要调有副作用工具。"
        }
        return buildString {
            append("你是天网手机助手。可用本机工具：\n")
            append(lines)
            append("\n\n规则：\n")
            append("1. 需要操作手机时，只输出一个 JSON 对象，不要输出解释、不要加代码围栏：\n")
            append("{\"tool\":\"工具名\",\"arguments\":{参数对象}}\n")
            append("2. 不需要工具时，直接用简体中文回答，不要输出 JSON。\n")
            append("3. 一次只调一个工具；看到工具结果后再决定下一步。\n")
            append("4. 工具名必须来自上面的列表，不许编造。\n")
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
