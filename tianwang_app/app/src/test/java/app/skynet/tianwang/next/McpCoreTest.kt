package app.skynet.tianwang.next

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class McpCoreTest {
    private val calls = mutableListOf<Pair<String, JSONObject>>()

    private fun schema(): JSONObject = JSONObject()
        .put("type", "object")
        .put("properties", JSONObject())

    private fun core(allowActions: Boolean = false): McpCore = McpCore(
        tools = {
            listOf(
                McpTool("self_status", "看状态", schema()),
                McpTool("self_ui_tap", "点控件", schema(), sideEffect = true),
            )
        },
        actionsAllowed = { allowActions },
        call = { name, args ->
            calls += name to args
            if (name == "self_status") ToolResult(true, text = "ok") else ToolResult(true, text = "tapped")
        },
    )

    private fun req(id: Int, method: String, params: JSONObject? = null): JSONObject =
        JSONObject().put("jsonrpc", "2.0").put("id", id).put("method", method)
            .apply { params?.let { put("params", it) } }

    @Test
    fun initializeAdvertisesTools() {
        val out = JSONObject(core().handleLine(req(1, "initialize").toString())!!)
        assertEquals("2.0", out.getString("jsonrpc"))
        assertEquals(1, out.getInt("id"))
        val result = out.getJSONObject("result")
        assertEquals("phone-self", result.getJSONObject("serverInfo").getString("name"))
        assertFalse(result.getJSONObject("capabilities").getJSONObject("tools").getBoolean("listChanged"))
    }

    @Test
    fun toolsListMarksSideEffects() {
        val out = JSONObject(core().handleLine(req(2, "tools/list").toString())!!)
        val tools = out.getJSONObject("result").getJSONArray("tools")
        assertEquals(2, tools.length())
        assertEquals("self_status", tools.getJSONObject(0).getString("name"))
        assertTrue(tools.getJSONObject(0).getJSONObject("annotations").getBoolean("readOnlyHint"))
        assertFalse(tools.getJSONObject(1).getJSONObject("annotations").getBoolean("readOnlyHint"))
    }

    @Test
    fun readOnlyToolRuns() {
        val out = JSONObject(core().handleLine(
            req(3, "tools/call", JSONObject().put("name", "self_status")).toString()
        )!!)
        val result = out.getJSONObject("result")
        assertFalse(result.getBoolean("isError"))
        assertEquals("ok", result.getJSONArray("content").getJSONObject(0).getString("text"))
    }

    @Test
    fun sideEffectBlockedWhenReadOnly() {
        val out = JSONObject(core(allowActions = false).handleLine(
            req(4, "tools/call", JSONObject().put("name", "self_ui_tap")
                .put("arguments", JSONObject().put("query", "搜索"))).toString()
        )!!)
        val result = out.getJSONObject("result")
        assertTrue(result.getBoolean("isError"))
        assertTrue(result.getJSONArray("content").getJSONObject(0).getString("text").contains("只读"))
        assertTrue(calls.isEmpty())
    }

    @Test
    fun sideEffectAllowedPassesArguments() {
        val out = JSONObject(core(allowActions = true).handleLine(
            req(5, "tools/call", JSONObject().put("name", "self_ui_tap")
                .put("arguments", JSONObject().put("query", "搜索"))).toString()
        )!!)
        assertFalse(out.getJSONObject("result").getBoolean("isError"))
        assertEquals("self_ui_tap", calls.single().first)
        assertEquals("搜索", calls.single().second.getString("query"))
    }

    @Test
    fun imageResultCarriesImageAndText() {
        val core = McpCore(
            tools = { listOf(McpTool("self_ui_screenshot", "截屏", schema())) },
            actionsAllowed = { false },
            call = { _, _ -> ToolResult(true, text = "/sdcard/shot.png", imageBase64 = "aGVsbG8=", mimeType = "image/jpeg") },
        )
        val out = JSONObject(core.handleLine(
            req(11, "tools/call", JSONObject().put("name", "self_ui_screenshot")).toString()
        )!!)
        val content = out.getJSONObject("result").getJSONArray("content")
        assertEquals(2, content.length())
        assertEquals("image", content.getJSONObject(0).getString("type"))
        assertEquals("aGVsbG8=", content.getJSONObject(0).getString("data"))
        assertEquals("image/jpeg", content.getJSONObject(0).getString("mimeType"))
        assertEquals("/sdcard/shot.png", content.getJSONObject(1).getString("text"))
    }

    @Test
    fun unknownToolIsToolErrorNotProtocolError() {
        val out = JSONObject(core().handleLine(
            req(6, "tools/call", JSONObject().put("name", "nope")).toString()
        )!!)
        assertTrue(out.getJSONObject("result").getBoolean("isError"))
    }

    @Test
    fun missingToolNameIsInvalidParams() {
        val out = JSONObject(core().handleLine(req(7, "tools/call").toString())!!)
        assertEquals(-32602, out.getJSONObject("error").getInt("code"))
    }

    @Test
    fun unknownMethodAndBadJsonAreExplicit() {
        val unknown = JSONObject(core().handleLine(req(8, "nope").toString())!!)
        assertEquals(-32601, unknown.getJSONObject("error").getInt("code"))

        val bad = JSONObject(core().handleLine("{bad")!!)
        assertEquals(-32700, bad.getJSONObject("error").getInt("code"))
    }

    @Test
    fun notificationNeverExecutesOrReplies() {
        val note = JSONObject().put("jsonrpc", "2.0").put("method", "tools/call")
            .put("params", JSONObject().put("name", "self_ui_tap")
                .put("arguments", JSONObject().put("query", "搜索")))
        assertNull(core(allowActions = true).handleLine(note.toString()))
        assertTrue(calls.isEmpty())
    }

    @Test
    fun toolsListSeesToolsAddedAfterConstruction() {
        val extra = mutableListOf<McpTool>()
        val core = McpCore(
            tools = { listOf(McpTool("self_status", "看状态", schema())) + extra },
            actionsAllowed = { false },
            call = { _, _ -> ToolResult(true, text = "ok") },
        )
        fun listed(): Int = JSONObject(core.handleLine(req(20, "tools/list").toString())!!)
            .getJSONObject("result").getJSONArray("tools").length()

        assertEquals(1, listed())
        extra += McpTool("mcp__pc__search", "外部搜索", schema(), sideEffect = true)
        assertEquals(2, listed())
    }

    @Test
    fun batchReturnsArray() {
        val batch = JSONArray()
            .put(req(9, "ping"))
            .put(req(10, "tools/list"))
        val out = JSONArray(core().handleLine(batch.toString())!!)
        assertEquals(2, out.length())
    }
}
