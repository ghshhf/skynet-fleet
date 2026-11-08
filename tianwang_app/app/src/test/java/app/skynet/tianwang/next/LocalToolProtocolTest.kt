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

    @Test
    fun detectsToolNameWithoutJson() {
        val tools = listOf(tool("self_ui_tap", sideEffect = true))
        // 1.7B 最常见的形态：说得出工具名，但没按 JSON 格式吐
        assertTrue(
            LocalToolProtocol.looksLikeActionAttempt("我用 self_ui_tap 点一下那个按钮", tools)
        )
        // 长解释里顺带提到工具名，不该被硬掰成 JSON（否则会误执行动作）
        val long = "我不能调用 self_ui_tap 这个工具，因为当前是只读模式，" +
            "你需要先在手机的入网设置里打开允许操作，然后我才能继续帮你点击目标控件。".repeat(3)
        assertTrue(
            "长解释不该触发 GBNF 重试",
            !LocalToolProtocol.looksLikeActionAttempt(long, tools)
        )
        // 不给工具表时保持旧行为，不影响纯聊天
        assertTrue(!LocalToolProtocol.looksLikeActionAttempt("我用 self_ui_tap 点一下"))
    }

    @Test
    fun repairsMissingLinksFromUserText() {
        val goal = "看看这两篇 https://a.example/x 和 https://b.example/y"
        // 模型把 urls 写空了：从用户原话补回来
        val fixed = LocalToolProtocol.repairLinkArgs("web_digest", JSONObject(), goal)
        assertEquals(2, WebLogic.parseUrls(fixed.getString("urls"), 5).size)

        // 模型写对了就不动它的值
        val ok = JSONObject().put("urls", "https://c.example/z")
        assertEquals(
            "https://c.example/z",
            LocalToolProtocol.repairLinkArgs("web_digest", ok, goal).getString("urls"),
        )

        // 用户压根没给链接：保持原样，协议层不编造
        val none = LocalToolProtocol.repairLinkArgs("web_digest", JSONObject(), "讲个笑话")
        assertEquals("", none.optString("urls"))
    }

    @Test
    fun webScopeCarriesDigestRulesOnlyThere() {
        val tools = listOf(tool("web_digest"))
        val web = LocalToolProtocol.systemPrompt(tools, false, "联网查资料")
        assertTrue("联网域要带「先给结论」的讲解规范", web.contains("第一句先给结论"))
        assertTrue("联网域要明确禁止补材料里没有的内容", web.contains("材料没提到"))

        // 其他域不该为这 80 个 token 买单
        val ui = LocalToolProtocol.systemPrompt(tools, false, "界面操作")
        assertTrue("非联网域不该带网页规范", !ui.contains("材料没提到"))
    }
}
