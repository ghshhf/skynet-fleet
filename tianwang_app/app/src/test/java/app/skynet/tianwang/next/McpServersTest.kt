package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 外部 MCP 联邦的纯逻辑测试：配置编解码、命名约束、冲突后缀。
 * 不碰 Android、不联网。
 */
class McpServersTest {

    @Test
    fun encodeDecodeRoundTrip() {
        val list = listOf(
            McpServers.Server("pc", "http://192.168.1.10:8765/mcp", "t0ken", true),
            McpServers.Server("远程", "https://mcp.example.com/x", "", false),
        )
        val back = McpServers.decode(McpServers.encode(list))
        assertEquals(2, back.size)
        assertEquals(list[0], back[0])
        assertEquals("远程", back[1].name)
        assertFalse(back[1].enabled)
    }

    @Test
    fun decodeSkipsBadEntriesInsteadOfCrashing() {
        val back = McpServers.decode(
            """[
              {"name":"ok","url":"http://a.b/mcp"},
              {"name":"","url":"http://a.b/mcp"},
              {"name":"bad","url":"ftp://a.b/mcp"},
              {"name":"nohost","url":"http:///mcp"}
            ]"""
        )
        assertEquals(1, back.size)
        assertEquals("ok", back.single().name)
    }

    @Test
    fun decodeToleratesEmptyOrBrokenJson() {
        assertEquals(0, McpServers.decode("").size)
        assertEquals(0, McpServers.decode("{not json").size)
    }

    @Test
    fun slugKeepsAsciiAndFoldsTheRest() {
        assertEquals("pc-1", McpServers.slug("PC-1"))
        assertEquals("my_server", McpServers.slug("My Server"))
        assertEquals("x", McpServers.slug("服务"))
        assertEquals("a_b_c", McpServers.slug("a.b:c"))
    }

    @Test
    fun exposedNameIsPrefixedAndBounded() {
        val n = McpServers.exposedName("pc", "search.web")
        assertEquals("mcp__pc__search_web", n)
        val long = McpServers.exposedName("s".repeat(40), "t".repeat(80))
        assertTrue("工具名不能超过 64 字符，实际 ${long.length}", long.length <= 64)
        assertTrue(long.startsWith("mcp__"))
    }

    @Test
    fun uniqueNameAppendsSuffixOnCollision() {
        val base = "mcp__pc__search"
        assertEquals(base, McpServers.uniqueName(base, emptySet()))
        assertEquals("mcp__pc__search__2", McpServers.uniqueName(base, setOf(base)))
        assertEquals(
            "mcp__pc__search__3",
            McpServers.uniqueName(base, setOf(base, "mcp__pc__search__2")),
        )
    }

    @Test
    fun validUrlRequiresHttpAndHost() {
        assertTrue(McpServers.validUrl("http://127.0.0.1:8765/mcp"))
        assertTrue(McpServers.validUrl("https://example.com/mcp"))
        assertFalse(McpServers.validUrl("ftp://example.com/mcp"))
        assertFalse(McpServers.validUrl("http:///mcp"))
        assertFalse(McpServers.validUrl("example.com/mcp"))
    }

    @Test
    fun externalToolShapeIsMarkedSideEffect() {
        // 联邦工具必须走「允许操作」闸门，不能伪装成只读。
        val tool = McpTool("mcp__pc__search", "外部", JSONObject(), sideEffect = true)
        assertTrue(tool.sideEffect)
        assertTrue(tool.describe().getJSONObject("annotations").getBoolean("readOnlyHint").not())
    }
}
