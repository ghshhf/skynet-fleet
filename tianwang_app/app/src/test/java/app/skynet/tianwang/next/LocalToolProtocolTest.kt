package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalToolProtocolTest {
    private fun tool(name: String, sideEffect: Boolean = false): McpTool = McpTool(
        name = name,
        description = "测试工具",
        inputSchema = JSONObject()
            .put("type", "object")
            .put("properties", JSONObject().put("query", JSONObject().put("type", "string")))
            .put("required", org.json.JSONArray().put("query")),
        sideEffect = sideEffect,
    )

    @Test
    fun parsesPlainToolJson() {
        val p = LocalToolProtocol.parse("""{"tool":"self_ui_tap","arguments":{"query":"搜索"}}""")!!
        assertEquals("self_ui_tap", p.name)
        assertEquals("搜索", p.args.getString("query"))
    }

    @Test
    fun parsesFencedJsonWithProseAroundIt() {
        val text = """
            我准备点搜索按钮。
            ```json
            {"tool":"self_ui_tap","arguments":{"query":"搜索"}}
            ```
            点完再读界面。
        """.trimIndent()
        val p = LocalToolProtocol.parse(text)!!
        assertEquals("self_ui_tap", p.name)
    }

    @Test
    fun keepsNestedBracesAndEscapedQuotes() {
        val p = LocalToolProtocol.parse(
            """{"tool":"self_ui_text","arguments":{"text":"他说：\"{别动}\"","query":"输入框"}}"""
        )!!
        assertEquals("他说：\"{别动}\"", p.args.getString("text"))
        assertEquals("输入框", p.args.getString("query"))
    }

    @Test
    fun acceptsNameAndArgsVariant() {
        val p = LocalToolProtocol.parse("""{"name":"self_status","args":{}}""")!!
        assertEquals("self_status", p.name)
        assertTrue(p.args.length() == 0)
    }

    @Test
    fun acceptsOpenAiFunctionShape() {
        val p = LocalToolProtocol.parse(
            """{"function":{"name":"self_app_launch","arguments":{"pkg":"com.android.settings"}}}"""
        )!!
        assertEquals("self_app_launch", p.name)
        assertEquals("com.android.settings", p.args.getString("pkg"))
    }

    @Test
    fun acceptsStringifiedArguments() {
        val p = LocalToolProtocol.parse(
            """{"tool":"self_ui_tap","arguments":"{\"query\":\"返回\"}"}"""
        )!!
        assertEquals("返回", p.args.getString("query"))
    }

    @Test
    fun ignoresNonToolJsonAndPlainAnswer() {
        assertNull(LocalToolProtocol.parse("""{"foo":"bar"}"""))
        assertNull(LocalToolProtocol.parse("直接用中文回答，不动手机。"))
        assertNull(LocalToolProtocol.parse("""{"tool":""}"""))
    }

    @Test
    fun picksFirstActualToolObjectAfterOtherJson() {
        val text = """{"note":"思考"}\n{"tool":"self_ui_dump","arguments":{"limit":30}}"""
        val p = LocalToolProtocol.parse(text)!!
        assertEquals("self_ui_dump", p.name)
        assertEquals(30, p.args.getInt("limit"))
    }

    @Test
    fun unknownToolIsStillParsedSoAgentCanCorrectIt() {
        val p = LocalToolProtocol.parse("""{"tool":"made_up_tool","arguments":{}}""")!!
        assertEquals("made_up_tool", p.name)
    }

    @Test
    fun promptListsToolsAndMode() {
        val prompt = LocalToolProtocol.systemPrompt(
            listOf(tool("self_status"), tool("self_ui_tap", sideEffect = true)),
            allowActions = false,
        )
        assertTrue(prompt.contains("self_status"))
        assertTrue(prompt.contains("self_ui_tap"))
        assertTrue(prompt.contains("只读"))
        assertTrue(prompt.contains("query:string"))
    }

    @Test
    fun grammarEnumeratesEveryToolName() {
        val g = LocalToolProtocol.toolGrammar(
            listOf(tool("self_status"), tool("self_ui_tap", sideEffect = true))
        )
        // 工具名必须是文法的合法分支，编造的名字在采样层就生成不出来。
        // 注意 GBNF 里每个名字本身要带引号，所以文本是 "\"self_status\""。
        assertTrue(g.contains("root ::="))
        assertTrue(g.contains("\\\"self_status\\\""))
        assertTrue(g.contains("\\\"self_ui_tap\\\""))
        assertTrue(g.contains(" | "))
        // JSON 基础规则齐全（否则 grammar 解析会在设备上失败）
        for (rule in listOf("value ::=", "object ::=", "array ::=", "string ::=", "number ::=", "ws ::=")) {
            assertTrue("缺规则 $rule", g.contains(rule))
        }
    }

    @Test
    fun grammarIsEmptyWithoutTools() {
        assertEquals("", LocalToolProtocol.toolGrammar(emptyList()))
    }

    @Test
    fun detectsHalfWrittenToolCall() {
        assertTrue(LocalToolProtocol.looksLikeActionAttempt("{\"tool\":\"self_ui_tap\",\"arguments\":{\"query\":"))
        assertTrue(LocalToolProtocol.looksLikeActionAttempt("{\"tool\": \"self_ui_tap\""))
        // 正常中文回答不能被误判，否则会莫名被强制成 JSON
        assertTrue(!LocalToolProtocol.looksLikeActionAttempt("好的，我帮你看一下屏幕。"))
        assertTrue(!LocalToolProtocol.looksLikeActionAttempt("这个 JSON 示例是 {\"a\":1}"))
    }
}
