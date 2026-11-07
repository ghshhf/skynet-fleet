package app.skynet.tianwang.next

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 资源流 JSON 契约测试。
 *
 * 手机不抓取、不推荐，但「电脑返回什么字段」必须和卡片一一对上；
 * 这里用一段真实形状的响应把字段名钉住，防止电脑侧改字段后手机静默空白。
 */
class ResourceApiTest {

    @Test
    fun `解析卡片与分页游标`() {
        val o = JSONObject(
            """
            {"ok":true,"total":2,"next_cursor":20,"items":[
              {"id":"r_1","title":"示例资源","summary":"一句话","url":"https://a.example/x",
               "domain":"a.example","kind":"site","tags":["论文","工具"],"score":0.81,
               "why":"兴趣 +0.28 · 没刷过","published_at":"2026-11-07T00:00:00Z",
               "liked":true,"faved":false,"comments":2},
              {"id":"r_2","title":"","domain":"b.example","tags":[]}
            ]}
            """.trimIndent()
        )
        val f = ResourceApi.parseFeed(o)
        assertEquals(2, f.items.size)
        assertEquals(20, f.next)
        val a = f.items[0]
        assertEquals("r_1", a.id)
        assertEquals("示例资源", a.title)
        assertEquals(listOf("论文", "工具"), a.tags)
        assertEquals(0.81, a.score, 0.0001)
        assertTrue(a.liked)
        assertFalse(a.faved)
        assertEquals(2, a.comments)
        // 标题空时退回域名，卡片不会出现空白标题
        assertEquals("b.example", f.items[1].title)
    }

    @Test
    fun `没有下一页时 next 为空`() {
        val f = ResourceApi.parseFeed(JSONObject("""{"ok":true,"items":[],"next_cursor":null}"""))
        assertTrue(f.items.isEmpty())
        assertNull(f.next)
    }
}
