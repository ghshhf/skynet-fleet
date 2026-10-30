package app.skynet.tianwang.next

import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 云侧 JSON → 界面模型的解析。
 *
 * 这两段原来全靠「界面上显示得对不对」来验，字段名写错一个字母就是一片空白，
 * 而空白和「服务端没数据」在界面上长得一模一样。这里把形状钉住。
 */
class JsonParseTest {

    @Test
    fun `网关快照 —— 活死与协议头剥离`() {
        val arr = JSONArray(
            """
            [
              {"id":"freellmapi","alive":true,"base":"http://127.0.0.1:3001/v1",
               "latency_ms":12,"models":["a","b"],"note":"网关"},
              {"id":"ovh","alive":false,"base":"https://oai.example.com"}
            ]
            """.trimIndent()
        )
        val gs = arr.toGateways()

        assertEquals(2, gs.size)
        assertEquals("freellmapi", gs[0].id)
        assertEquals("活", gs[0].status)
        // 界面上只显示 host:port/path，协议头是噪音
        assertEquals("127.0.0.1:3001/v1", gs[0].endpoint)
        assertEquals(12, gs[0].latencyMs)
        assertEquals(2, gs[0].modelCount)

        assertEquals("死", gs[1].status)
        assertEquals("oai.example.com", gs[1].endpoint)
        // 没有 models 字段时不能当 1 个，必须是 0
        assertEquals(0, gs[1].modelCount)
    }

    @Test
    fun `能力列表 —— 参数与破坏性标记`() {
        val arr = JSONArray(
            """
            [
              {"name":"image.gen","title":"安卓出图","domain":"creative",
               "side_effect":true,"destructive":false,"approval":"auto",
               "note":"手机算力",
               "params":[
                 {"name":"prompt","type":"str","desc":"提示词","required":true},
                 {"name":"steps","required":false}
               ]},
              {"name":"probe.echo","title":"回声","domain":"probe"}
            ]
            """.trimIndent()
        )
        val caps = arr.toCapabilities()

        assertEquals(2, caps.size)
        val gen = caps[0]
        assertEquals("image.gen", gen.name)
        assertEquals("creative", gen.domain)
        assertTrue(gen.sideEffect)
        assertFalse(gen.destructive)
        assertEquals(2, gen.params.size)
        assertEquals("prompt", gen.params[0].name)
        assertTrue(gen.params[0].required)
        assertEquals("提示词", gen.params[0].desc)
        // 没写 type 的默认 str，没写 required 的默认 false
        assertEquals("str", gen.params[1].type)
        assertFalse(gen.params[1].required)

        // 一个字段都不给的极端情况：不崩，给默认值
        assertEquals("probe.echo", caps[1].name)
        assertFalse(caps[1].sideEffect)
        assertEquals("auto", caps[1].approval)
        assertTrue(caps[1].params.isEmpty())
    }

    @Test
    fun `空数组与数组里的非对象元素`() {
        assertEquals(0, JSONArray("[]").toGateways().size)
        assertEquals(0, JSONArray("[]").toCapabilities().size)
        // 服务端理论上不会给 null，但半截 JSON 会，这时该跳过而不是抛
        assertEquals(1, JSONArray("""[null,{"id":"only"}]""").toGateways().size)
    }
}
