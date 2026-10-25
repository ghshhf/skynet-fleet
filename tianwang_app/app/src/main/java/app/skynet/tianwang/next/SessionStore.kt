package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/**
 * 会话历史的落盘层。
 *
 * 🔴 为什么必须有（2026-10-26 主人提的第三条）：
 *   原来 `sessions` 只是内存里的一个 mutableStateListOf，
 *   而 `Demo.sessions = emptyList()` —— 于是「会话」抽屉永远是空的，
 *   切到别的 tab 再回来、或者被系统杀掉进程，聊天记录就全没了。
 *   一个连自己说过什么都记不住的助手，用户不会当它存在。
 *
 * 设计取舍：
 * - **一个 JSON 文件**，不用数据库：几十条会话、几万条消息，SQLite 的收益
 *   抵不过多一层 schema 迁移的维护成本；真到十万条再换。
 * - **只存要显示的东西**：`streaming` / `failed` 这类运行时状态一律不存，
 *   否则下次打开会看到一条永远卡在「生成中」的死消息。
 * - **写失败不抛**：保存是一次「尽量做」的动作，磁盘满了不该让整个对话崩掉。
 */
object SessionStore {
    private const val FILE = "sessions.json"
    /** 最多留这么多条会话，超了丢最旧的 —— 手机存储不是无底洞。 */
    private const val MAX_SESSIONS = 200

    private fun file(): File = File(App.ctx.filesDir, FILE)

    fun load(): List<Session> {
        val f = file()
        if (!f.isFile) return emptyList()
        return runCatching {
            val arr = JSONArray(f.readText(Charsets.UTF_8))
            val out = ArrayList<Session>(arr.length())
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                out.add(
                    Session(
                        id = o.optString("id").ifBlank { "s-$i" },
                        title = o.optString("title", "新会话"),
                        modelId = o.optString("modelId", ""),
                        at = o.optLong("at", 0L),
                        msgs = readMsgs(o.optJSONArray("msgs")),
                    )
                )
            }
            out.sortedByDescending { it.at }
        }.getOrDefault(emptyList())
    }

    fun save(sessions: List<Session>) {
        runCatching {
            val arr = JSONArray()
            for (s in sessions.take(MAX_SESSIONS)) {
                arr.put(
                    JSONObject()
                        .put("id", s.id)
                        .put("title", s.title)
                        .put("modelId", s.modelId)
                        .put("at", s.at)
                        .put("msgs", writeMsgs(s.msgs))
                )
            }
            // 先写临时文件再改名：写到一半被杀进程不会留下半个 JSON，
            // 否则下次启动读到坏文件 = 历史全丢（这比没保存更伤人）。
            val tmp = File(file().parentFile, "$FILE.tmp")
            tmp.writeText(arr.toString(), Charsets.UTF_8)
            if (!tmp.renameTo(file())) {
                file().writeText(arr.toString(), Charsets.UTF_8)
                tmp.delete()
            }
        }
    }

    private fun writeMsgs(msgs: List<ChatMsg>): JSONArray {
        val a = JSONArray()
        for (m in msgs) {
            // 空消息、还在流式的草稿、失败占位 —— 都不进历史
            if (m.text.isBlank() && m.tools.isEmpty()) continue
            if (m.streaming) continue
            val o = JSONObject()
                .put("role", m.role.name)
                .put("text", m.text)
                .put("at", m.at)
            if (m.tools.isNotEmpty()) {
                val ta = JSONArray()
                for (t in m.tools) {
                    ta.put(
                        JSONObject()
                            .put("name", t.name)
                            .put("args", t.args)
                            .put("result", t.result.take(2000))
                            .put("state", t.state.name)
                            .put("ms", t.ms)
                    )
                }
                o.put("tools", ta)
            }
            a.put(o)
        }
        return a
    }

    private fun readMsgs(a: JSONArray?): List<ChatMsg> {
        if (a == null) return emptyList()
        val out = ArrayList<ChatMsg>(a.length())
        for (i in 0 until a.length()) {
            val o = a.optJSONObject(i) ?: continue
            val role = runCatching { ChatMsg.Role.valueOf(o.optString("role", "USER")) }
                .getOrDefault(ChatMsg.Role.USER)
            val tools = ArrayList<ToolCall>()
            o.optJSONArray("tools")?.let { ta ->
                for (k in 0 until ta.length()) {
                    val t = ta.optJSONObject(k) ?: continue
                    tools.add(
                        ToolCall(
                            name = t.optString("name"),
                            args = t.optString("args"),
                            result = t.optString("result"),
                            state = runCatching { ToolCall.State.valueOf(t.optString("state", "OK")) }
                                .getOrDefault(ToolCall.State.OK),
                            ms = t.optLong("ms", 0),
                        )
                    )
                }
            }
            out.add(
                ChatMsg(
                    id = o.optString("id").ifBlank { "m-$i" },
                    role = role,
                    text = o.optString("text"),
                    at = o.optLong("at", 0L),
                    tools = tools,
                    streaming = false,
                )
            )
        }
        return out
    }
}
